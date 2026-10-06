"""Run with: python -m unittest discover -s tests -v (uses only an in-memory DB)."""
import os
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from datetime import date, datetime, timedelta
from pathlib import Path

os.environ["WEB_APP_DATABASE_URI"] = "sqlite://"
os.environ["FLASK_SECRET_KEY"] = "isolated-functional-check-key"

from app import (
    app, db, Exercise, MenuItem, MuscleRecoveryLog, TrainingLog, User,
    WorkoutDay, WorkoutSchedule, WorkoutSession, recovery_estimates,
)


class AppFlowTests(unittest.TestCase):
    def setUp(self):
        app.config["TESTING"] = True
        self.context = app.app_context()
        self.context.push()
        self.assertEqual(str(db.engine.url), "sqlite://")
        db.drop_all()
        db.create_all()
        self.client = app.test_client()

    def tearDown(self):
        db.session.remove()
        db.drop_all()
        self.context.pop()

    def post(self, path, client=None, follow=True, **data):
        client = client or self.client
        with client.session_transaction() as session:
            token = session.get("_csrf_token")
        if token is None:
            client.get("/", follow_redirects=True)
            with client.session_transaction() as session:
                token = session["_csrf_token"]
        return client.post(path, data={"_csrf_token": token, **data}, follow_redirects=follow)

    def register(self, username="tester", client=None):
        response = self.post(
            "/register", client=client, username=username,
            password="testpass123", password_confirmation="testpass123",
        )
        self.assertEqual(response.status_code, 200)
        return User.query.filter_by(username=username).one().id

    def menu(self):
        user_id = self.register()
        exercise = Exercise.query.filter_by(user_id=user_id, name="ベンチプレス").one()
        day = WorkoutDay.query.filter_by(user_id=user_id, split_code="push").one()
        self.post(f"/menu/{day.id}/items", exercise_id=exercise.id, sets=3, rep_min=8, rep_max=12)
        return user_id, exercise.id, day.id

    def start(self, day_id, schedule_id=None):
        data = {"workout_day_id": day_id}
        if schedule_id is not None:
            data["schedule_id"] = schedule_id
        response = self.post("/workouts/start", **data)
        self.assertEqual(response.status_code, 200)
        return WorkoutSession.query.order_by(WorkoutSession.id.desc()).first().id

    def record_set(self, session_id, exercise_id, set_number, reps=12):
        return self.post(
            f"/workouts/{session_id}/sets", exercise_id=exercise_id,
            set_number=set_number, lift_type="ベンチプレス", weight=80,
            reps=reps, target_reps=12,
        )

    def test_registration_password_and_login(self):
        self.assertEqual(self.client.get("/").status_code, 302)
        invalid = self.post("/register", username="tester", password="1234567", password_confirmation="1234567")
        self.assertIn("8文字以上", invalid.get_data(as_text=True))
        self.assertEqual(User.query.count(), 0)
        user_id = self.register()
        self.assertNotEqual(db.session.get(User, user_id).password_hash, "testpass123")
        self.post("/logout")
        failed = self.post("/login", username="tester", password="wrongpass")
        self.assertIn("違います", failed.get_data(as_text=True))
        self.assertEqual(self.post("/login", username="tester", password="testpass123").status_code, 200)
        with self.client.session_transaction() as session:
            self.assertEqual(session["user_id"], user_id)

    def test_csrf_rejects_missing_token(self):
        self.register()
        self.assertEqual(self.client.post("/", data={"weight": 80}).status_code, 400)
        self.assertEqual(self.client.post("/", data={"_csrf_token": "invalid"}).status_code, 400)
        self.assertEqual(TrainingLog.query.count(), 0)

    def test_log_save_edit_delete_and_invalid_input(self):
        self.register()
        for weight in ("", "-1", "nan", "1001", "abc"):
            self.post("/", lift_type="ベンチプレス", weight=weight, reps=10, target_reps=12)
        self.assertEqual(TrainingLog.query.count(), 0)
        self.assertEqual(self.post("/", lift_type="ベンチプレス", weight=80, reps=10, target_reps=12).status_code, 200)
        log = TrainingLog.query.one()
        self.assertEqual(log.one_rm, 100.0)
        edited = self.post(f"/edit/{log.id}", lift_type="ベンチプレス", weight=85, reps=8, target_reps=10, date="2026-10-01T12:00")
        self.assertEqual(edited.status_code, 200)
        self.assertEqual(db.session.get(TrainingLog, log.id).weight, 85)
        self.assertEqual(self.client.get("/edit/999999").status_code, 404)
        self.post(f"/delete/{log.id}")
        self.assertEqual(TrainingLog.query.count(), 0)

    def test_exercise_and_menu_validation(self):
        user_id = self.register()
        self.post("/exercises", name="ラットプルダウン", muscle_group="背中")
        self.post("/exercises", name="ラットプルダウン", muscle_group="背中")
        self.assertEqual(Exercise.query.filter_by(name="ラットプルダウン").count(), 1)
        exercise = Exercise.query.filter_by(name="ラットプルダウン").one()
        day = WorkoutDay.query.filter_by(user_id=user_id, split_code="pull").one()
        self.post(f"/menu/{day.id}/items", exercise_id=exercise.id, sets=0, rep_min=12, rep_max=8)
        self.assertEqual(MenuItem.query.count(), 0)
        self.post(f"/menu/{day.id}/items", exercise_id=exercise.id, sets=3, rep_min=8, rep_max=12)
        item = MenuItem.query.one()
        self.post(f"/menu/items/{item.id}/delete")
        self.assertEqual(MenuItem.query.count(), 0)

    def test_calendar_schedule_cancel_and_reopen(self):
        _, _, day_id = self.menu()
        self.post("/calendar/events", workout_day_id=day_id, scheduled_date=date.today().isoformat())
        event = WorkoutSchedule.query.one()
        cancelled = self.post(f"/calendar/events/{event.id}/status", status="cancelled")
        self.assertIn("予定に戻す", cancelled.get_data(as_text=True))
        self.assertEqual(self.post("/workouts/start", workout_day_id=day_id, schedule_id=event.id).status_code, 400)
        reopened = self.post(f"/calendar/events/{event.id}/status", status="planned")
        self.assertIn("トレーニング開始", reopened.get_data(as_text=True))
        self.post(f"/calendar/events/{event.id}/delete")
        self.assertEqual(WorkoutSchedule.query.count(), 0)

    def test_workout_record_resume_finish_and_calendar_history(self):
        _, exercise_id, day_id = self.menu()
        self.post("/calendar/events", workout_day_id=day_id, scheduled_date=date.today().isoformat())
        event_id = WorkoutSchedule.query.one().id
        session_id = self.start(day_id, event_id)
        self.start(day_id, event_id)
        self.assertEqual(WorkoutSession.query.count(), 1)
        for set_number in (1, 2, 3):
            self.assertEqual(self.record_set(session_id, exercise_id, set_number).status_code, 200)
        self.record_set(session_id, exercise_id, 1)
        self.assertEqual(TrainingLog.query.count(), 3)
        self.post(f"/workouts/{session_id}/finish")
        self.assertEqual(db.session.get(WorkoutSchedule, event_id).status, "completed")
        self.assertEqual(self.record_set(session_id, exercise_id, 1).status_code, 400)
        calendar = self.client.get("/calendar").get_data(as_text=True)
        self.assertIn("実績: ベンチプレス", calendar)
        self.assertIn(f'/workouts/{session_id}', calendar)
        self.post(f"/calendar/events/{event_id}/delete")
        self.assertIsNone(db.session.get(WorkoutSession, session_id).schedule_id)
        self.assertEqual(TrainingLog.query.count(), 3)
        self.assertEqual(self.client.get(f"/workouts/{session_id}").status_code, 200)

    def test_legacy_log_is_used_for_current_target(self):
        _, _, day_id = self.menu()
        self.post("/", lift_type="ベンチプレス", weight=80, reps=10, target_reps=12)
        session_id = self.start(day_id)
        page = self.client.get(f"/workouts/{session_id}").get_data(as_text=True)
        self.assertIn("今回の目安：80.0kg × 11回", page)

    def test_progression_holds_weight_until_all_sets_reach_top(self):
        _, exercise_id, day_id = self.menu()
        first = self.start(day_id)
        self.record_set(first, exercise_id, 1, reps=10)
        self.post(f"/workouts/{first}/finish")
        second = self.start(day_id)
        page = self.client.get(f"/workouts/{second}").get_data(as_text=True)
        self.assertIn("今回の目安：80.0kg × 11回", page)

    def test_progression_increases_weight_after_all_sets_reach_top(self):
        _, exercise_id, day_id = self.menu()
        first = self.start(day_id)
        for set_number in (1, 2, 3):
            self.record_set(first, exercise_id, set_number)
        self.post(f"/workouts/{first}/finish")
        second = self.start(day_id)
        page = self.client.get(f"/workouts/{second}").get_data(as_text=True)
        self.assertIn("今回の目安：82.0kg × 8回", page)

    def test_recovery_weighted_average_and_calendar_candidate(self):
        user_id, _, day_id = self.menu()
        today = date.today()
        self.post("/recovery", muscle_group="胸", trained_date=(today-timedelta(days=10)).isoformat(), recovered_date=(today-timedelta(days=6)).isoformat())
        self.post("/recovery", muscle_group="胸", trained_date=(today-timedelta(days=3)).isoformat(), recovered_date=(today-timedelta(days=1)).isoformat())
        self.assertEqual(recovery_estimates(user_id)["胸"], {"days": 2.8, "samples": 2})
        self.post("/", lift_type="ベンチプレス", weight=80, reps=10, target_reps=12)
        page = self.client.get("/calendar").get_data(as_text=True)
        self.assertIn((today + timedelta(days=3)).isoformat(), page)
        self.assertEqual(self.client.get("/calendar?month=invalid").status_code, 200)
        self.assertEqual(self.client.get("/calendar?month=2024-02").status_code, 200)
        self.assertEqual(self.client.get("/calendar?month=2026-12").status_code, 200)
        latest_recovery_id = MuscleRecoveryLog.query.order_by(MuscleRecoveryLog.id.desc()).first().id
        self.post(f"/recovery/{latest_recovery_id}/delete")
        self.assertEqual(recovery_estimates(user_id)["胸"], {"days": 4.0, "samples": 1})

    def test_recovery_rejects_future_or_reversed_dates(self):
        self.register()
        today = date.today()
        self.post("/recovery", muscle_group="胸", trained_date=today.isoformat(), recovered_date=(today-timedelta(days=1)).isoformat())
        self.post("/recovery", muscle_group="胸", trained_date=today.isoformat(), recovered_date=(today+timedelta(days=1)).isoformat())
        self.assertEqual(MuscleRecoveryLog.query.count(), 0)

    def test_users_cannot_access_each_others_data(self):
        user_id, exercise_id, day_id = self.menu()
        session_id = self.start(day_id)
        self.record_set(session_id, exercise_id, 1)
        log_id = TrainingLog.query.one().id
        self.post("/calendar/events", workout_day_id=day_id, scheduled_date=date.today().isoformat())
        event_id = WorkoutSchedule.query.one().id
        self.post("/recovery", muscle_group="胸", trained_date=date.today().isoformat(), recovered_date=date.today().isoformat())
        recovery_id = MuscleRecoveryLog.query.one().id
        item_id = MenuItem.query.one().id
        second_client = app.test_client()
        self.register("second", second_client)
        self.assertEqual(second_client.get(f"/workouts/{session_id}").status_code, 404)
        self.assertEqual(second_client.get(f"/edit/{log_id}").status_code, 404)
        self.assertEqual(self.post(f"/workouts/{session_id}/finish", client=second_client).status_code, 404)
        self.assertEqual(self.post("/workouts/start", client=second_client, workout_day_id=day_id).status_code, 404)
        self.assertEqual(self.post(f"/menu/{day_id}/items", client=second_client, exercise_id=exercise_id).status_code, 404)
        self.assertEqual(self.post(f"/menu/items/{item_id}/delete", client=second_client).status_code, 404)
        self.assertEqual(self.post(f"/calendar/events/{event_id}/delete", client=second_client).status_code, 404)
        self.assertEqual(self.post(f"/calendar/events/{event_id}/status", client=second_client, status="completed").status_code, 404)
        self.assertEqual(self.post(f"/recovery/{recovery_id}/delete", client=second_client).status_code, 404)
        self.post(f"/delete/{log_id}", client=second_client)
        self.assertEqual(TrainingLog.query.filter_by(user_id=user_id).count(), 1)
        self.assertNotIn("実績: ベンチプレス", second_client.get("/calendar").get_data(as_text=True))

    def test_legacy_db_migrates_and_first_user_inherits_log(self):
        with tempfile.TemporaryDirectory() as temporary:
            legacy_path = Path(temporary) / "legacy.db"
            with sqlite3.connect(legacy_path) as connection:
                connection.execute("CREATE TABLE training_log (id INTEGER PRIMARY KEY, date DATETIME NOT NULL, lift_type VARCHAR(50) NOT NULL, weight FLOAT NOT NULL, reps INTEGER NOT NULL, one_rm FLOAT NOT NULL, target_reps INTEGER NOT NULL, target_weight FLOAT NOT NULL)")
                connection.execute("INSERT INTO training_log VALUES (1, '2026-10-01 12:00:00', 'ベンチプレス', 80, 10, 100, 12, 76.9)")
            connection.close()
            environment = dict(os.environ, WEB_APP_DATABASE_URI="sqlite:///" + legacy_path.as_posix())
            code = """
from app import app, db, TrainingLog, User
app.config['TESTING'] = True
c = app.test_client()
c.get('/register')
with c.session_transaction() as s: token = s['_csrf_token']
r = c.post('/register', data={'_csrf_token': token, 'username': 'legacy', 'password': 'testpass123', 'password_confirmation': 'testpass123'}, follow_redirects=True)
assert r.status_code == 200
with app.app_context():
    log = TrainingLog.query.one()
    assert log.user_id == User.query.one().id
    assert log.weight == 80 and log.session_id is None and log.set_number == 1
"""
            result = subprocess.run([sys.executable, "-c", code], env=environment, capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == "__main__":
    unittest.main()
