import math
import os
import calendar as month_calendar
from functools import wraps
from hmac import compare_digest
from secrets import token_hex, token_urlsafe
from flask import Flask, abort, flash, jsonify, render_template, request, redirect, session, url_for
from flask_sqlalchemy import SQLAlchemy # データベース用の道具
from datetime import date, datetime, time, timedelta # 日付・時刻を扱うための道具
from sqlalchemy import UniqueConstraint, inspect, or_, text
from werkzeug.security import check_password_hash, generate_password_hash

app = Flask(__name__)


def get_secret_key():
    configured_key = os.environ.get("FLASK_SECRET_KEY")
    if configured_key:
        return configured_key

    os.makedirs(app.instance_path, exist_ok=True)
    key_path = os.path.join(app.instance_path, "secret_key")
    try:
        with open(key_path, "r", encoding="utf-8") as key_file:
            return key_file.read().strip()
    except FileNotFoundError:
        generated_key = token_hex(32)
        try:
            with open(key_path, "x", encoding="utf-8") as key_file:
                key_file.write(generated_key)
            return generated_key
        except FileExistsError:
            with open(key_path, "r", encoding="utf-8") as key_file:
                return key_file.read().strip()


app.config["SECRET_KEY"] = get_secret_key()
app.config["SESSION_COOKIE_HTTPONLY"] = True
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"

# --- 1. データベースの基本設定 ---
# SQLiteを使い、training.dbというファイルにデータを保存する設定
app.config['SQLALCHEMY_DATABASE_URI'] = os.environ.get('WEB_APP_DATABASE_URI', 'sqlite:///training.db')
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
db = SQLAlchemy(app)

PPL_SPLITS = (("push", "Push"), ("pull", "Pull"), ("legs", "Legs"))
MUSCLE_GROUPS = ("胸", "背中", "肩", "腕", "脚", "臀部", "体幹", "全身", "その他")
DEFAULT_EXERCISES = (("ベンチプレス", "胸"), ("スクワット", "脚"), ("その他", "全身"))


class User(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(40), unique=True, nullable=False)
    password_hash = db.Column(db.String(255), nullable=False)


class Exercise(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False, index=True)
    name = db.Column(db.String(80), nullable=False)
    muscle_group = db.Column(db.String(30), nullable=False, default="その他")
    __table_args__ = (UniqueConstraint("user_id", "name", name="uq_exercise_user_name"),)


class WorkoutDay(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False, index=True)
    split_code = db.Column(db.String(10), nullable=False)
    name = db.Column(db.String(30), nullable=False)
    items = db.relationship(
        "MenuItem", back_populates="workout_day", cascade="all, delete-orphan",
        order_by="MenuItem.position",
    )
    __table_args__ = (UniqueConstraint("user_id", "split_code", name="uq_workout_day_user_split"),)


class MenuItem(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    workout_day_id = db.Column(db.Integer, db.ForeignKey("workout_day.id"), nullable=False, index=True)
    exercise_id = db.Column(db.Integer, db.ForeignKey("exercise.id"), nullable=False)
    position = db.Column(db.Integer, nullable=False, default=0)
    sets = db.Column(db.Integer, nullable=False, default=3)
    rep_min = db.Column(db.Integer, nullable=False, default=8)
    rep_max = db.Column(db.Integer, nullable=False, default=12)
    workout_day = db.relationship("WorkoutDay", back_populates="items")
    exercise = db.relationship("Exercise")


class WorkoutSchedule(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False, index=True)
    workout_day_id = db.Column(db.Integer, db.ForeignKey("workout_day.id"), nullable=False)
    scheduled_date = db.Column(db.Date, nullable=False, index=True)
    status = db.Column(db.String(20), nullable=False, default="planned")
    workout_day = db.relationship("WorkoutDay")
    session = db.relationship("WorkoutSession", back_populates="schedule", uselist=False)


class WorkoutSession(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False, index=True)
    workout_day_id = db.Column(db.Integer, db.ForeignKey("workout_day.id"), nullable=False)
    schedule_id = db.Column(db.Integer, db.ForeignKey("workout_schedule.id"), nullable=True, unique=True)
    started_at = db.Column(db.DateTime, nullable=False, default=datetime.now)
    completed_at = db.Column(db.DateTime, nullable=True)
    workout_day = db.relationship("WorkoutDay")
    schedule = db.relationship("WorkoutSchedule", back_populates="session")
    items = db.relationship("WorkoutSessionItem", order_by="WorkoutSessionItem.position", cascade="all, delete-orphan")


class WorkoutSessionItem(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    session_id = db.Column(db.Integer, db.ForeignKey("workout_session.id"), nullable=False, index=True)
    exercise_id = db.Column(db.Integer, db.ForeignKey("exercise.id"), nullable=False)
    muscle_group = db.Column(db.String(30), nullable=False)
    position = db.Column(db.Integer, nullable=False)
    sets = db.Column(db.Integer, nullable=False)
    rep_min = db.Column(db.Integer, nullable=False)
    rep_max = db.Column(db.Integer, nullable=False)
    exercise = db.relationship("Exercise")


class TrainingBatch(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False, index=True)
    sets = db.Column(db.Integer, nullable=False)
    rep_min = db.Column(db.Integer, nullable=False)
    rep_max = db.Column(db.Integer, nullable=False)


class RecoveryCheck(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False, index=True)
    muscle_group = db.Column(db.String(30), nullable=False)
    trained_date = db.Column(db.Date, nullable=False)
    last_sore_date = db.Column(db.Date, nullable=True)
    recovery_log_id = db.Column(db.Integer, db.ForeignKey("muscle_recovery_log.id"), nullable=True)
    __table_args__ = (UniqueConstraint("user_id", "muscle_group", "trained_date", name="uq_recovery_check_day"),)


class MuscleRecoveryLog(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False, index=True)
    muscle_group = db.Column(db.String(30), nullable=False)
    trained_date = db.Column(db.Date, nullable=False)
    recovered_date = db.Column(db.Date, nullable=False)


def recovery_estimates(user_id):
    records = MuscleRecoveryLog.query.filter_by(user_id=user_id).order_by(
        MuscleRecoveryLog.recovered_date.desc(), MuscleRecoveryLog.id.desc()
    ).all()
    grouped = {}
    for record in records:
        grouped.setdefault(record.muscle_group, []).append(
            (record.recovered_date - record.trained_date).days
        )

    estimates = {}
    for group, durations in grouped.items():
        weights = [0.7 ** index for index in range(len(durations))]
        average = sum(days * weight for days, weight in zip(durations, weights)) / sum(weights)
        estimates[group] = {"days": round(average, 1), "samples": len(durations)}
    return estimates


def validate_training_form(form, allowed_exercises):
    """フォーム値を検証し、保存に使える値かエラーメッセージを返す。"""
    lift_type = form.get("lift_type", "")
    if lift_type not in allowed_exercises:
        return None, "種目を選択してください。"

    try:
        weight = float(form.get("weight", ""))
    except (TypeError, ValueError):
        return None, "重量は数値で入力してください。"
    if not math.isfinite(weight) or weight <= 0 or weight > 1000:
        return None, "重量は0より大きく、1000kg以下で入力してください。"

    try:
        reps = int(form.get("reps", ""))
    except (TypeError, ValueError):
        return None, "回数は整数で入力してください。"
    if not 1 <= reps <= 100:
        return None, "回数は1〜100の範囲で入力してください。"

    try:
        target_reps = int(form.get("target_reps", ""))
    except (TypeError, ValueError):
        return None, "目標回数は整数で入力してください。"
    if not 1 <= target_reps <= 100:
        return None, "目標回数は1〜100の範囲で入力してください。"

    return {
        "lift_type": lift_type,
        "weight": weight,
        "reps": reps,
        "target_reps": target_reps,
    }, None


def ensure_user_setup(user):
    """PPLの枠と標準種目を用意し、既存記録にある種目名も取り込む。"""
    existing_exercises = {exercise.name: exercise for exercise in Exercise.query.filter_by(user_id=user.id).all()}
    for name, muscle_group in DEFAULT_EXERCISES:
        if name not in existing_exercises:
            exercise = Exercise(user_id=user.id, name=name, muscle_group=muscle_group)
            db.session.add(exercise)
            existing_exercises[name] = exercise

    for log in TrainingLog.query.filter_by(user_id=user.id).all():
        if log.lift_type not in existing_exercises:
            exercise = Exercise(user_id=user.id, name=log.lift_type, muscle_group="その他")
            db.session.add(exercise)
            existing_exercises[log.lift_type] = exercise

    existing_splits = {day.split_code for day in WorkoutDay.query.filter_by(user_id=user.id).all()}
    for split_code, name in PPL_SPLITS:
        if split_code not in existing_splits:
            db.session.add(WorkoutDay(user_id=user.id, split_code=split_code, name=name))

    db.session.commit()
    return Exercise.query.filter_by(user_id=user.id).order_by(Exercise.name).all()

# --- 2. テーブル（表）の設計図：Javaのクラスと同じ感覚 ---
class TrainingLog(db.Model):
    id = db.Column(db.Integer, primary_key=True) # 自動で振られる番号
    date = db.Column(db.DateTime, nullable=False, default=datetime.now) # 記録した日時
    lift_type = db.Column(db.String(50), nullable=False) # 種目
    weight = db.Column(db.Float, nullable=False) # 扱った重量
    reps = db.Column(db.Integer, nullable=False) # 回数
    one_rm = db.Column(db.Float, nullable=False) # 計算された1RM
    target_reps = db.Column(db.Integer, nullable=False) # 目標回数
    target_weight = db.Column(db.Float, nullable=False) # 提案された重量
    # 既存記録を移行する間はNULLを許可し、初回登録者へ引き継ぐ
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=True, index=True)
    session_id = db.Column(db.Integer, db.ForeignKey("workout_session.id"), nullable=True, index=True)
    set_number = db.Column(db.Integer, nullable=False, default=1)
    batch_id = db.Column(db.Integer, db.ForeignKey("training_batch.id"), nullable=True, index=True)

# 初回起動時にデータベースファイル（training.db）を自動で作成する
with app.app_context():
    db.create_all()
    # create_all()は既存テーブルに列を追加しないため、既存SQLite DBを安全に拡張する。
    training_columns = {column["name"] for column in inspect(db.engine).get_columns(TrainingLog.__tablename__)}
    if "user_id" not in training_columns:
        with db.engine.begin() as connection:
            connection.execute(text("ALTER TABLE training_log ADD COLUMN user_id INTEGER REFERENCES user(id)"))
    if "session_id" not in training_columns:
        with db.engine.begin() as connection:
            connection.execute(text("ALTER TABLE training_log ADD COLUMN session_id INTEGER REFERENCES workout_session(id)"))
    if "set_number" not in training_columns:
        with db.engine.begin() as connection:
            connection.execute(text("ALTER TABLE training_log ADD COLUMN set_number INTEGER NOT NULL DEFAULT 1"))
    if "batch_id" not in training_columns:
        with db.engine.begin() as connection:
            connection.execute(text("ALTER TABLE training_log ADD COLUMN batch_id INTEGER REFERENCES training_batch(id)"))
    with db.engine.begin() as connection:
        connection.execute(text("CREATE INDEX IF NOT EXISTS ix_training_log_user_id ON training_log (user_id)"))
        connection.execute(text("CREATE INDEX IF NOT EXISTS ix_training_log_session_id ON training_log (session_id)"))


def get_current_user():
    user_id = session.get("user_id")
    return db.session.get(User, user_id) if user_id is not None else None


def login_required(view):
    @wraps(view)
    def wrapped_view(*args, **kwargs):
        if get_current_user() is None:
            session.clear()
            return redirect(url_for("login"))
        return view(*args, **kwargs)
    return wrapped_view


@app.before_request
def protect_post_requests():
    if request.method == "POST":
        expected = session.get("_csrf_token", "")
        submitted = request.form.get("_csrf_token", "")
        if not expected or not submitted or not compare_digest(expected, submitted):
            abort(400, description="フォームの有効期限が切れました。ページを再読み込みしてください。")


@app.context_processor
def provide_template_helpers():
    def csrf_token():
        token = session.get("_csrf_token")
        if token is None:
            token = token_urlsafe(32)
            session["_csrf_token"] = token
        return token
    return {"csrf_token": csrf_token}

# --- 計算関数 ---
def calculate_1rm(weight, reps, lift_type):
    if lift_type == "ベンチプレス":
        return weight * (1 + reps / 40.0)
    elif lift_type == "スクワット":
        return weight * (1 + reps / 33.3)
    else:
        return weight * (1 + reps / 40.0)

def progression_targets(logs, sets, rep_min, rep_max):
    """全セットの達成状況で増量し、それ以外は各セットの回数を伸ばす。"""
    previous = {log.set_number: log for log in logs}
    complete = all(number in previous for number in range(1, sets + 1))
    weights = [previous[number].weight for number in range(1, sets + 1)] if complete else []
    increase = complete and all(previous[number].reps >= rep_max for number in range(1, sets + 1))
    # 異なる重量のセットは一律に増量せず、それぞれの重量を維持する。
    increase = increase and max(weights) - min(weights) < 0.001
    targets = []
    for number in range(1, sets + 1):
        log = previous.get(number)
        weight = log.weight if log else (logs[0].weight if logs else None)
        reps = rep_min
        if increase:
            weight = min(1000, round(weight + max(1.0, round(weight * 0.025 * 2) / 2), 1))
        elif log:
            reps = min(rep_max, max(rep_min, log.reps + 1))
        targets.append({"set_number": number, "weight": weight, "reps": reps})
    reason = "全セットで上限回数を達成したので、重量を少し増やして下限回数から始めます。" if increase else "重量を維持し、セットごとに回数を1回ずつ伸ばします。上限に届いたセットはその回数を維持します。"
    if not logs:
        reason = "初回は無理のない重量を選び、設定した回数の下限から始めます。"
    return {"targets": targets, "reason": reason, "increased": increase}


def previous_exercise_logs(user_id, lift_type, before=None, exclude_session=None):
    query = TrainingLog.query.outerjoin(WorkoutSession, TrainingLog.session_id == WorkoutSession.id).filter(
        TrainingLog.user_id == user_id, TrainingLog.lift_type == lift_type,
        TrainingLog.weight > 0, TrainingLog.weight <= 1000,
        TrainingLog.reps.between(1, 100),
        or_(TrainingLog.session_id.is_(None), WorkoutSession.completed_at.isnot(None)),
    )
    if before is not None:
        query = query.filter(TrainingLog.date < before)
    if exclude_session is not None:
        query = query.filter(or_(TrainingLog.session_id.is_(None), TrainingLog.session_id != exclude_session))
    latest = query.order_by(TrainingLog.date.desc(), TrainingLog.id.desc()).first()
    if latest is None:
        return []
    group = TrainingLog.query.filter_by(user_id=user_id, lift_type=lift_type)
    if latest.session_id is not None:
        group = group.filter_by(session_id=latest.session_id)
    elif latest.batch_id is not None:
        group = group.filter_by(batch_id=latest.batch_id)
    else:
        # 古い単発記録を同じ日の全セットと見なさない。
        return [latest]
    return group.filter(TrainingLog.weight > 0, TrainingLog.reps.between(1, 100)).order_by(TrainingLog.set_number, TrainingLog.id).all()


def exercise_recipe(user_id, exercise_id):
    item = MenuItem.query.join(WorkoutDay).filter(
        WorkoutDay.user_id == user_id, MenuItem.exercise_id == exercise_id,
    ).order_by(WorkoutDay.id, MenuItem.position).first()
    if item:
        return {"sets": item.sets, "rep_min": item.rep_min, "rep_max": item.rep_max}
    exercise = db.session.get(Exercise, exercise_id)
    latest = TrainingLog.query.filter_by(user_id=user_id, lift_type=exercise.name).filter(
        TrainingLog.batch_id.isnot(None)
    ).order_by(TrainingLog.date.desc(), TrainingLog.id.desc()).first()
    if latest:
        batch = db.session.get(TrainingBatch, latest.batch_id)
        return {"sets": batch.sets, "rep_min": batch.rep_min, "rep_max": batch.rep_max}
    return {"sets": 1, "rep_min": 8, "rep_max": 12}


def ensure_session_items(workout_session):
    if workout_session.items:
        return workout_session.items
    # 以前のセッションは既存のメニュー・保存済み記録から補完する。
    recipes = {item.exercise_id: item for item in workout_session.workout_day.items}
    for log in TrainingLog.query.filter_by(user_id=workout_session.user_id, session_id=workout_session.id).all():
        exercise = Exercise.query.filter_by(user_id=workout_session.user_id, name=log.lift_type).first()
        if exercise and exercise.id not in recipes:
            recipes[exercise.id] = None
    for position, (exercise_id, item) in enumerate(recipes.items()):
        exercise = db.session.get(Exercise, exercise_id)
        saved = TrainingLog.query.filter_by(session_id=workout_session.id, lift_type=exercise.name).all()
        workout_session.items.append(WorkoutSessionItem(
            exercise_id=exercise_id, muscle_group=exercise.muscle_group, position=position,
            sets=item.sets if item else max((log.set_number for log in saved), default=1),
            rep_min=item.rep_min if item else 8, rep_max=item.rep_max if item else 12,
        ))
    db.session.flush()
    return workout_session.items


def refresh_log_targets(log):
    if log.session_id:
        workout_session = db.session.get(WorkoutSession, log.session_id)
        item = next((item for item in ensure_session_items(workout_session) if item.exercise.name == log.lift_type), None)
        recipe = {"sets": item.sets, "rep_min": item.rep_min, "rep_max": item.rep_max} if item else {"sets": 1, "rep_min": 8, "rep_max": 12}
        logs = TrainingLog.query.filter_by(user_id=log.user_id, session_id=log.session_id, lift_type=log.lift_type).order_by(TrainingLog.set_number).all()
    elif log.batch_id:
        batch = db.session.get(TrainingBatch, log.batch_id)
        recipe = {"sets": batch.sets, "rep_min": batch.rep_min, "rep_max": batch.rep_max}
        logs = TrainingLog.query.filter_by(user_id=log.user_id, batch_id=log.batch_id, lift_type=log.lift_type).order_by(TrainingLog.set_number).all()
    else:
        exercise = Exercise.query.filter_by(user_id=log.user_id, name=log.lift_type).first()
        recipe = exercise_recipe(log.user_id, exercise.id)
        logs = [log]
    guidance = progression_targets(logs, **recipe)
    for saved in logs:
        target = guidance["targets"][min(saved.set_number, recipe["sets"]) - 1]
        saved.target_weight = target["weight"] or saved.weight
        saved.target_reps = target["reps"]
    return guidance


def add_recovery_check(user_id, muscle_group, trained_date):
    check = RecoveryCheck.query.filter_by(user_id=user_id, muscle_group=muscle_group, trained_date=trained_date).first()
    if check is None:
        recovered = MuscleRecoveryLog.query.filter_by(user_id=user_id, muscle_group=muscle_group, trained_date=trained_date).first()
        check = RecoveryCheck(user_id=user_id, muscle_group=muscle_group, trained_date=trained_date,
                              recovery_log_id=recovered.id if recovered else None)
        db.session.add(check)
    return check


def sync_recent_recovery_checks(user_id):
    # 旧記録も直近2週間分だけ候補にする。遠い過去を未回復扱いにしない。
    rows = db.session.query(Exercise.muscle_group, db.func.max(TrainingLog.date)).join(
        TrainingLog, TrainingLog.lift_type == Exercise.name
    ).outerjoin(WorkoutSession, TrainingLog.session_id == WorkoutSession.id).filter(
        Exercise.user_id == user_id, TrainingLog.user_id == user_id,
        TrainingLog.date >= datetime.combine(date.today() - timedelta(days=14), time.min),
        TrainingLog.date <= datetime.now(),
        or_(TrainingLog.session_id.is_(None), WorkoutSession.completed_at.isnot(None)),
    ).group_by(Exercise.muscle_group).all()
    for group, trained_at in rows:
        add_recovery_check(user_id, group, trained_at.date())
    db.session.commit()


def build_chart_data(logs):
    return [
        {"date": log.date.strftime("%m/%d"), "lift": log.lift_type, "rm": log.one_rm}
        for log in reversed(logs)
    ]


@app.route("/register", methods=["GET", "POST"])
def register():
    if get_current_user() is not None:
        return redirect(url_for("home"))

    if request.method == "POST":
        username = request.form.get("username", "").strip().lower()
        password = request.form.get("password", "")
        password_confirmation = request.form.get("password_confirmation", "")

        if not 3 <= len(username) <= 40:
            flash("ユーザー名は3〜40文字で入力してください。", "error")
        elif User.query.filter_by(username=username).first() is not None:
            flash("このユーザー名はすでに使われています。", "error")
        elif len(password) < 8:
            flash("パスワードは8文字以上にしてください。", "error")
        elif password != password_confirmation:
            flash("確認用パスワードが一致しません。", "error")
        else:
            is_first_user = User.query.count() == 0
            user = User(username=username, password_hash=generate_password_hash(password))
            db.session.add(user)
            db.session.flush()
            if is_first_user:
                # 現在の7件を、ユーザー指定どおり最初のアカウントへ引き継ぐ。
                TrainingLog.query.filter_by(user_id=None).update(
                    {TrainingLog.user_id: user.id}, synchronize_session=False
                )
            ensure_user_setup(user)
            session.clear()
            session["user_id"] = user.id
            flash("アカウントを作成しました。", "success")
            return redirect(url_for("home"))

    return render_template("auth.html", mode="register")


@app.route("/login", methods=["GET", "POST"])
def login():
    if get_current_user() is not None:
        return redirect(url_for("home"))

    if request.method == "POST":
        username = request.form.get("username", "").strip().lower()
        password = request.form.get("password", "")
        user = User.query.filter_by(username=username).first()
        if user is None or not check_password_hash(user.password_hash, password):
            flash("ユーザー名またはパスワードが違います。", "error")
        else:
            session.clear()
            session["user_id"] = user.id
            return redirect(url_for("home"))

    return render_template("auth.html", mode="login")


@app.route("/logout", methods=["POST"])
@login_required
def logout():
    session.clear()
    return redirect(url_for("login"))

@app.route("/", methods=["GET", "POST"])
@login_required
def home():
    user = get_current_user()
    exercises = ensure_user_setup(user)
    allowed_exercises = [exercise.name for exercise in exercises]
    error = None
    set_rows = []
    if request.method == "POST":
        lift = request.form.get("lift_type", "")
        exercise = next((exercise for exercise in exercises if exercise.name == lift), None)
        recipe = exercise_recipe(user.id, exercise.id) if exercise else {"sets": 1, "rep_min": 8, "rep_max": 12}
        weights = request.form.getlist("weights") or [request.form.get("weight", "")]
        reps_values = request.form.getlist("reps_values") or [request.form.get("reps", "")]
        set_rows = [{"weight": weight, "reps": reps} for weight, reps in zip(weights, reps_values)]
        try:
            rep_max = int(request.form.get("target_reps", recipe["rep_max"]))
            rep_min = int(request.form.get("rep_min", min(recipe["rep_min"], rep_max)))
            expected_sets = int(request.form.get("expected_sets", max(recipe["sets"], len(weights))))
            if not 1 <= rep_min <= rep_max <= 100 or not len(weights) <= expected_sets <= 20:
                raise ValueError
        except (TypeError, ValueError):
            error = "回数の範囲は1〜100、セット数は1〜20で入力してください。"
        if len(weights) != len(reps_values) or not 1 <= len(weights) <= 20:
            error = "各セットの重量と回数を入力してください（最大20セット）。"
        validated = []
        if not error:
            for row in set_rows:
                values, error = validate_training_form({"lift_type": lift, **row, "target_reps": str(rep_max)}, allowed_exercises)
                if error:
                    break
                validated.append(values)
        if not error:
            batch = TrainingBatch(user_id=user.id, sets=expected_sets, rep_min=rep_min, rep_max=rep_max)
            db.session.add(batch)
            db.session.flush()
            recorded_at = datetime.now()
            new_logs = []
            for number, values in enumerate(validated, 1):
                log = TrainingLog(user_id=user.id, batch_id=batch.id, set_number=number, date=recorded_at,
                    lift_type=lift, weight=values["weight"], reps=values["reps"],
                    one_rm=round(calculate_1rm(values["weight"], values["reps"], lift), 1),
                    target_reps=rep_max, target_weight=values["weight"])
                db.session.add(log)
                new_logs.append(log)
            db.session.flush()
            refresh_log_targets(new_logs[0])
            add_recovery_check(user.id, exercise.muscle_group, recorded_at.date())
            db.session.commit()
            flash(f"{lift}の{len(new_logs)}セットを保存しました。", "success")
            return redirect(url_for("home", saved_batch=batch.id))

    guidance_by_exercise = {}
    for exercise in exercises:
        recipe = exercise_recipe(user.id, exercise.id)
        previous = previous_exercise_logs(user.id, exercise.name)
        guidance_by_exercise[exercise.name] = {
            "exercise_id": exercise.id,
            **recipe, **progression_targets(previous, **recipe),
            "previous": [{"set_number": log.set_number, "weight": log.weight, "reps": log.reps} for log in previous],
            "previous_date": previous[0].date.strftime("%Y-%m-%d") if previous else None,
        }
    all_logs = TrainingLog.query.filter_by(user_id=user.id).order_by(TrainingLog.date.desc(), TrainingLog.id.desc()).all()
    ongoing_sessions = WorkoutSession.query.filter_by(user_id=user.id, completed_at=None).order_by(WorkoutSession.started_at.desc()).all()
    next_guidance = None
    saved_batch_id = request.args.get("saved_batch", type=int)
    if saved_batch_id:
        batch = TrainingBatch.query.filter_by(id=saved_batch_id, user_id=user.id).first()
        saved = TrainingLog.query.filter_by(batch_id=saved_batch_id, user_id=user.id).order_by(TrainingLog.set_number).all()
        if batch and saved:
            next_guidance = {"lift_type": saved[0].lift_type, **progression_targets(saved, batch.sets, batch.rep_min, batch.rep_max)}
    return render_template(
        "index.html", logs=all_logs, error=error, form_data=request.form if error else {},
        set_rows=set_rows if error else [], exercises=exercises, guidance_by_exercise=guidance_by_exercise,
        next_guidance=next_guidance, ongoing_sessions=ongoing_sessions, chart_data=build_chart_data(all_logs),
    ), 400 if error else 200


@app.route("/guidance/<int:exercise_id>")
@login_required
def exercise_guidance(exercise_id):
    user = get_current_user()
    exercise = Exercise.query.filter_by(id=exercise_id, user_id=user.id).first_or_404()
    recipe = exercise_recipe(user.id, exercise_id)
    try:
        recipe = {key: int(request.args.get(key, value)) for key, value in recipe.items()}
        if not 1 <= recipe["sets"] <= 20 or not 1 <= recipe["rep_min"] <= recipe["rep_max"] <= 100:
            raise ValueError
    except (TypeError, ValueError):
        return jsonify({"error": "回数の下限・上限とセット数を確認してください。"}), 400
    previous = previous_exercise_logs(user.id, exercise.name)
    return jsonify({**recipe, **progression_targets(previous, **recipe),
        "previous": [{"set_number": log.set_number, "weight": log.weight, "reps": log.reps} for log in previous],
        "previous_date": previous[0].date.strftime("%Y-%m-%d") if previous else None})

# --- 5. データベースから削除 ---
@app.route("/delete/<int:log_id>", methods=["POST"])
@login_required
def delete_log(log_id):
    # 送られてきたIDと一致する記録をデータベースから探し出す
    log_to_delete = TrainingLog.query.filter_by(id=log_id, user_id=get_current_user().id).first()
    
    if log_to_delete:
        group_session_id, group_batch_id = log_to_delete.session_id, log_to_delete.batch_id
        lift_type = log_to_delete.lift_type
        db.session.delete(log_to_delete) # 削除の予約
        db.session.flush()
        if group_session_id or group_batch_id:
            remaining = TrainingLog.query.filter_by(user_id=get_current_user().id, session_id=group_session_id,
                batch_id=group_batch_id, lift_type=lift_type).first()
            if remaining:
                refresh_log_targets(remaining)
        db.session.commit()              # 確定して保存
        
    # 削除が終わったら、home関数に自動で戻る
    return redirect(url_for('home'))

# --- 6. 編集機能 ---
@app.route("/edit/<int:log_id>", methods=["GET", "POST"])
@login_required
def edit_log(log_id):
    user = get_current_user()
    exercises = ensure_user_setup(user)
    log_to_edit = TrainingLog.query.filter_by(id=log_id, user_id=user.id).first()
    if log_to_edit is None:
        return render_template(
            "message.html", title="記録が見つかりません", message="指定された記録は存在しないか、削除されています。"
        ), 404
    recipe = exercise_recipe(user.id, next(exercise.id for exercise in exercises if exercise.name == log_to_edit.lift_type))
    if log_to_edit.batch_id:
        batch = db.session.get(TrainingBatch, log_to_edit.batch_id)
        recipe = {"rep_min": batch.rep_min, "rep_max": batch.rep_max}
    elif log_to_edit.session_id:
        workout = db.session.get(WorkoutSession, log_to_edit.session_id)
        item = next((item for item in ensure_session_items(workout) if item.exercise.name == log_to_edit.lift_type), None)
        if item:
            recipe = {"rep_min": item.rep_min, "rep_max": item.rep_max}
    edit_upper = recipe["rep_max"]
    
    if request.method == "POST":
        # === 保存ボタンが押された時（上書き処理） ===
        
        edit_values = request.form.to_dict()
        if log_to_edit.session_id:
            edit_values["target_reps"] = str(edit_upper)
        values, error = validate_training_form(edit_values, [exercise.name for exercise in exercises])
        if error:
            return render_template(
                "edit.html", log=log_to_edit, error=error, form_data=request.form,
                exercises=exercises, edit_upper=edit_upper,
            ), 400

        new_date_str = request.form.get("date", "")
        try:
            new_date = datetime.strptime(new_date_str, '%Y-%m-%dT%H:%M')
        except (TypeError, ValueError):
            return render_template(
                "edit.html", log=log_to_edit, error="日時を正しく入力してください。",
                form_data=request.form, exercises=exercises, edit_upper=edit_upper,
            ), 400

        new_lift = values["lift_type"]
        new_weight = values["weight"]
        new_reps = values["reps"]
        new_target = values["target_reps"]

        new_max_weight = calculate_1rm(new_weight, new_reps, new_lift)
        new_max_weight = round(new_max_weight, 1)
        if (log_to_edit.session_id or log_to_edit.batch_id) and new_lift != log_to_edit.lift_type:
            return render_template("edit.html", log=log_to_edit, exercises=exercises,
                error="セット記録の種目は変更できません。重量と回数を修正してください。", form_data=request.form, edit_upper=edit_upper), 400
        
        log_to_edit.lift_type = new_lift
        log_to_edit.weight = new_weight
        log_to_edit.reps = new_reps
        log_to_edit.one_rm = new_max_weight
        log_to_edit.target_reps = new_target
        log_to_edit.date = new_date
        log_to_edit.user_id = get_current_user().id
        if log_to_edit.batch_id:
            batch = db.session.get(TrainingBatch, log_to_edit.batch_id)
            batch.rep_max = new_target
            batch.rep_min = min(batch.rep_min, new_target)
        db.session.flush()
        refresh_log_targets(log_to_edit)

        # データベースの変更を確定して保存する
        db.session.commit()     # 確定して保存

        return redirect(url_for('home'))

    else:
        # === 編集ボタンが押された直後 ===
        # まだ作っていませんが、編集専用の画面に log_to_edit を渡して開く
        return render_template(
            "edit.html", log=log_to_edit, error=None, form_data={}, exercises=exercises, edit_upper=edit_upper
        )


@app.route("/calendar")
@login_required
def calendar_page():
    user = get_current_user()
    ensure_user_setup(user)
    sync_recent_recovery_checks(user.id)
    today = date.today()
    month_value = request.args.get("month", today.strftime("%Y-%m"))
    try:
        month_start = datetime.strptime(month_value, "%Y-%m").date().replace(day=1)
    except ValueError:
        month_start = today.replace(day=1)

    month_days = month_calendar.monthrange(month_start.year, month_start.month)[1]
    month_end = month_start.replace(day=month_days)
    next_month_start = (month_end + timedelta(days=1)).replace(day=1)
    schedules = WorkoutSchedule.query.filter(
        WorkoutSchedule.user_id == user.id,
        WorkoutSchedule.scheduled_date >= month_start,
        WorkoutSchedule.scheduled_date <= month_end,
    ).order_by(WorkoutSchedule.scheduled_date, WorkoutSchedule.id).all()
    logs = TrainingLog.query.filter(
        TrainingLog.user_id == user.id,
        TrainingLog.date >= datetime.combine(month_start, time.min),
        TrainingLog.date < datetime.combine(next_month_start, time.min),
    ).order_by(TrainingLog.date).all()

    schedules_by_day = {}
    for schedule in schedules:
        schedules_by_day.setdefault(schedule.scheduled_date.isoformat(), []).append(schedule)
    logs_by_day = {}
    for log in logs:
        logs_by_day.setdefault(log.date.date().isoformat(), []).append(log)

    weeks = [
        [date(month_start.year, month_start.month, day) if day else None for day in week]
        for week in month_calendar.Calendar(firstweekday=6).monthdayscalendar(
            month_start.year, month_start.month
        )
    ]
    previous_month = (month_start - timedelta(days=1)).strftime("%Y-%m")
    following_month = next_month_start.strftime("%Y-%m")
    workout_days = WorkoutDay.query.filter_by(user_id=user.id).order_by(WorkoutDay.id).all()
    estimates = recovery_estimates(user.id)
    muscle_last_dates = {
        group: last_date.date()
        for group, last_date in db.session.query(
            Exercise.muscle_group, db.func.max(TrainingLog.date)
        ).join(TrainingLog, TrainingLog.lift_type == Exercise.name).filter(
            Exercise.user_id == user.id, TrainingLog.user_id == user.id,
        ).group_by(Exercise.muscle_group).all()
        if last_date is not None
    }
    workout_suggestions = []
    for workout_day in workout_days:
        if not workout_day.items:
            continue
        groups = sorted({item.exercise.muscle_group for item in workout_day.items})
        candidate_dates = [today]
        for group in groups:
            last_date = muscle_last_dates.get(group)
            if last_date is not None:
                recovery_days = estimates.get(group, {}).get("days", 2.0)
                recovered = MuscleRecoveryLog.query.filter_by(user_id=user.id, muscle_group=group, trained_date=last_date).order_by(MuscleRecoveryLog.recovered_date.desc()).first()
                candidate_dates.append(recovered.recovered_date if recovered else last_date + timedelta(days=max(1, math.ceil(recovery_days))))
            still_sore = RecoveryCheck.query.filter_by(user_id=user.id, muscle_group=group, recovery_log_id=None).filter(
                RecoveryCheck.last_sore_date.isnot(None)
            ).order_by(RecoveryCheck.last_sore_date.desc()).first()
            if still_sore:
                candidate_dates.append(still_sore.last_sore_date + timedelta(days=1))
        workout_suggestions.append({
            "workout_day": workout_day,
            "muscle_groups": groups,
            "date": max(candidate_dates),
        })
    selected_schedule_date = today
    try:
        selected_schedule_date = date.fromisoformat(request.args.get("scheduled_date", ""))
    except ValueError:
        pass
    try:
        selected_workout_day = int(request.args.get("workout_day", ""))
    except ValueError:
        selected_workout_day = None
    if not any(day.id == selected_workout_day for day in workout_days):
        selected_workout_day = None
    return render_template(
        "calendar.html", weeks=weeks, month_start=month_start,
        month_label=f"{month_start.year}年{month_start.month}月",
        previous_month=previous_month, following_month=following_month,
        current_date=today, today_date=today, selected_schedule_date=selected_schedule_date,
        selected_workout_day=selected_workout_day, workout_suggestions=workout_suggestions,
        recovery_estimates=estimates, schedules_by_day=schedules_by_day,
        logs_by_day=logs_by_day,
        workout_days=workout_days,
    )


@app.route("/calendar/events", methods=["POST"])
@login_required
def add_workout_schedule():
    user = get_current_user()
    try:
        scheduled_date = date.fromisoformat(request.form.get("scheduled_date", ""))
        workout_day_id = int(request.form.get("workout_day_id", ""))
    except (TypeError, ValueError):
        flash("日付とメニューを選択してください。", "error")
        return redirect(url_for("calendar_page"))

    workout_day = WorkoutDay.query.filter_by(id=workout_day_id, user_id=user.id).first()
    if workout_day is None:
        flash("登録済みのPPLメニューを選択してください。", "error")
    elif not workout_day.items:
        flash("先にメニューへ種目を追加してください。", "error")
    else:
        schedule = WorkoutSchedule(
            user_id=user.id, workout_day_id=workout_day.id, scheduled_date=scheduled_date
        )
        db.session.add(schedule)
        db.session.commit()
        flash(f"{scheduled_date.month}月{scheduled_date.day}日に{workout_day.name}を予定しました。", "success")
    return redirect(url_for("calendar_page", month=scheduled_date.strftime("%Y-%m")))


@app.route("/calendar/events/<int:event_id>/status", methods=["POST"])
@login_required
def update_workout_schedule_status(event_id):
    user = get_current_user()
    schedule = WorkoutSchedule.query.filter_by(id=event_id, user_id=user.id).first_or_404()
    status = request.form.get("status")
    if status not in ("planned", "in_progress", "completed", "cancelled"):
        abort(400)
    schedule.status = status
    db.session.commit()
    flash("予定の状態を更新しました。", "success")
    return redirect(url_for("calendar_page", month=schedule.scheduled_date.strftime("%Y-%m")))


@app.route("/calendar/events/<int:event_id>/delete", methods=["POST"])
@login_required
def delete_workout_schedule(event_id):
    user = get_current_user()
    schedule = WorkoutSchedule.query.filter_by(id=event_id, user_id=user.id).first_or_404()
    month_value = schedule.scheduled_date.strftime("%Y-%m")
    db.session.delete(schedule)
    db.session.commit()
    flash("予定を削除しました。", "success")
    return redirect(url_for("calendar_page", month=month_value))


@app.route("/workouts/start", methods=["POST"])
@login_required
def start_workout():
    user = get_current_user()
    try:
        workout_day_id = int(request.form.get("workout_day_id", ""))
        schedule_id = int(request.form["schedule_id"]) if request.form.get("schedule_id") else None
    except (TypeError, ValueError):
        abort(400)

    workout_day = WorkoutDay.query.filter_by(id=workout_day_id, user_id=user.id).first_or_404()
    schedule = None
    if schedule_id is not None:
        schedule = WorkoutSchedule.query.filter_by(id=schedule_id, user_id=user.id).first_or_404()
        if schedule.workout_day_id != workout_day.id or schedule.status == "cancelled":
            abort(400)
        if schedule.session is not None:
            return redirect(url_for("workout_session_page", session_id=schedule.session.id))

    ongoing = WorkoutSession.query.filter_by(user_id=user.id, workout_day_id=workout_day.id, completed_at=None).first()
    if ongoing:
        flash("実施中のトレーニングを再開します。", "success")
        return redirect(url_for("workout_session_page", session_id=ongoing.id))
    if not workout_day.items:
        flash("このメニューには種目がありません。先に種目を登録してください。", "error")
        return redirect(url_for("menu_page"))

    workout_session = WorkoutSession(
        user_id=user.id, workout_day_id=workout_day.id, schedule_id=schedule_id
    )
    db.session.add(workout_session)
    db.session.flush()
    ensure_session_items(workout_session)
    if schedule is not None:
        schedule.status = "in_progress"
    db.session.commit()
    return redirect(url_for("workout_session_page", session_id=workout_session.id))


@app.route("/workouts/<int:session_id>")
@login_required
def workout_session_page(session_id):
    user = get_current_user()
    workout_session = WorkoutSession.query.filter_by(id=session_id, user_id=user.id).first_or_404()
    exercises = ensure_user_setup(user)
    session_logs = TrainingLog.query.filter_by(
        user_id=user.id, session_id=workout_session.id
    ).order_by(TrainingLog.id).all()
    workout_items = []
    for item in ensure_session_items(workout_session):
        previous_sets = previous_exercise_logs(user.id, item.exercise.name, before=workout_session.started_at, exclude_session=session_id)
        guidance = progression_targets(previous_sets, item.sets, item.rep_min, item.rep_max)
        completed_sets = {
            log.set_number: log for log in session_logs if log.lift_type == item.exercise.name
        }
        workout_items.append({
            "item": item,
            "previous_log": previous_sets[0] if previous_sets else None,
            "previous_sets": previous_sets,
            "targets": guidance["targets"],
            "next_reps": guidance["targets"][0]["reps"],
            "target_weight": guidance["targets"][0]["weight"],
            "target_reason": guidance["reason"],
            "completed_sets": completed_sets,
        })

    db.session.commit()
    return render_template(
        "workout.html", workout_session=workout_session, workout_items=workout_items,
        exercises=exercises, session_logs=session_logs,
    )


@app.route("/workouts/<int:session_id>/sets", methods=["POST"])
@login_required
def add_workout_set(session_id):
    user = get_current_user()
    workout_session = WorkoutSession.query.filter_by(id=session_id, user_id=user.id).first_or_404()
    if workout_session.completed_at is not None:
        abort(400, description="終了したトレーニングには記録を追加できません。")

    try:
        exercise_id = int(request.form.get("exercise_id", ""))
        set_number = int(request.form.get("set_number", ""))
    except (TypeError, ValueError):
        flash("種目とセット番号を確認してください。", "error")
        return redirect(url_for("workout_session_page", session_id=session_id))

    item = next((item for item in ensure_session_items(workout_session) if item.exercise_id == exercise_id), None)
    if item is None or not 1 <= set_number <= item.sets:
        abort(400)
    if TrainingLog.query.filter_by(
        user_id=user.id, session_id=workout_session.id,
        lift_type=item.exercise.name, set_number=set_number,
    ).first() is not None:
        flash("このセットはすでに記録済みです。", "error")
        return redirect(url_for("workout_session_page", session_id=session_id))

    values, error = validate_training_form({**request.form.to_dict(), "target_reps": str(item.rep_max)}, [item.exercise.name])
    if error:
        flash(error, "error")
        return redirect(url_for("workout_session_page", session_id=session_id))

    one_rm = round(calculate_1rm(values["weight"], values["reps"], item.exercise.name), 1)
    new_log = TrainingLog(
        user_id=user.id, session_id=workout_session.id, set_number=set_number,
        lift_type=item.exercise.name, weight=values["weight"], reps=values["reps"],
        one_rm=one_rm, target_reps=values["target_reps"], target_weight=values["weight"],
    )
    db.session.add(new_log)
    db.session.flush()
    refresh_log_targets(new_log)
    db.session.commit()
    flash(f"{item.exercise.name}の{set_number}セット目を記録しました。", "success")
    return redirect(url_for("workout_session_page", session_id=session_id))


@app.route("/workouts/<int:session_id>/finish", methods=["POST"])
@login_required
def finish_workout(session_id):
    user = get_current_user()
    workout_session = WorkoutSession.query.filter_by(id=session_id, user_id=user.id).first_or_404()
    if workout_session.completed_at is None:
        saved_logs = TrainingLog.query.filter_by(user_id=user.id, session_id=session_id).all()
        groups = {item.exercise.name: item.muscle_group for item in ensure_session_items(workout_session)}
        for log in saved_logs:
            if log.lift_type in groups:
                add_recovery_check(user.id, groups[log.lift_type], log.date.date())
        workout_session.completed_at = datetime.now()
        if workout_session.schedule is not None:
            workout_session.schedule.status = "completed"
        db.session.commit()
        flash("トレーニングを完了しました。", "success")
    return redirect(url_for("workout_session_page", session_id=session_id))


@app.route("/menu")
@login_required
def menu_page():
    user = get_current_user()
    exercises = ensure_user_setup(user)
    plans_by_code = {
        day.split_code: day for day in WorkoutDay.query.filter_by(user_id=user.id).all()
    }
    plans = [plans_by_code[code] for code, _ in PPL_SPLITS if code in plans_by_code]
    return render_template(
        "menu.html", plans=plans, exercises=exercises, muscle_groups=MUSCLE_GROUPS
    )


@app.route("/exercises", methods=["POST"])
@login_required
def add_exercise():
    name = request.form.get("name", "").strip()
    muscle_group = request.form.get("muscle_group", "")
    user = get_current_user()

    if not 1 <= len(name) <= 80:
        flash("種目名は1〜80文字で入力してください。", "error")
    elif muscle_group not in MUSCLE_GROUPS:
        flash("部位を選択してください。", "error")
    elif Exercise.query.filter_by(user_id=user.id, name=name).first() is not None:
        flash("この種目はすでに登録されています。", "error")
    else:
        db.session.add(Exercise(user_id=user.id, name=name, muscle_group=muscle_group))
        db.session.commit()
        flash(f"「{name}」を種目に追加しました。", "success")
    return redirect(url_for("menu_page"))


@app.route("/menu/<int:day_id>/items", methods=["POST"])
@login_required
def add_menu_item(day_id):
    user = get_current_user()
    workout_day = WorkoutDay.query.filter_by(id=day_id, user_id=user.id).first_or_404()
    try:
        exercise_id = int(request.form.get("exercise_id", ""))
        sets = int(request.form.get("sets", "3"))
        rep_min = int(request.form.get("rep_min", "8"))
        rep_max = int(request.form.get("rep_max", "12"))
    except (TypeError, ValueError):
        flash("種目、セット数、回数を正しく入力してください。", "error")
        return redirect(url_for("menu_page"))

    exercise = Exercise.query.filter_by(id=exercise_id, user_id=user.id).first()
    if exercise is None:
        flash("種目を選択してください。", "error")
    elif not 1 <= sets <= 20:
        flash("セット数は1〜20で入力してください。", "error")
    elif not (1 <= rep_min <= rep_max <= 100):
        flash("回数範囲は1〜100で、下限が上限以下になるよう入力してください。", "error")
    elif MenuItem.query.filter_by(workout_day_id=workout_day.id, exercise_id=exercise.id).first():
        flash("この種目はすでにメニューにあります。", "error")
    else:
        position = max((item.position for item in workout_day.items), default=-1) + 1
        db.session.add(MenuItem(
            workout_day_id=workout_day.id, exercise_id=exercise.id,
            position=position, sets=sets, rep_min=rep_min, rep_max=rep_max,
        ))
        db.session.commit()
        flash(f"「{exercise.name}」を{workout_day.name}メニューに追加しました。", "success")
    return redirect(url_for("menu_page"))


@app.route("/menu/items/<int:item_id>/delete", methods=["POST"])
@login_required
def delete_menu_item(item_id):
    user = get_current_user()
    item = MenuItem.query.join(WorkoutDay).filter(
        MenuItem.id == item_id, WorkoutDay.user_id == user.id
    ).first_or_404()
    db.session.delete(item)
    db.session.commit()
    flash("メニューから種目を削除しました。", "success")
    return redirect(url_for("menu_page"))


@app.route("/settings")
@login_required
def settings_page():
    return render_template("message.html", title="設定", message="設定機能は準備中です。")


@app.route("/recovery", methods=["GET", "POST"])
@login_required
def recovery_page():
    user = get_current_user()
    if request.method == "POST":
        muscle_group = request.form.get("muscle_group", "")
        try:
            trained_date = date.fromisoformat(request.form.get("trained_date", ""))
            recovered_date = date.fromisoformat(request.form.get("recovered_date", ""))
        except (TypeError, ValueError):
            flash("トレーニング日と回復日を入力してください。", "error")
            return redirect(url_for("recovery_page"))

        if muscle_group not in MUSCLE_GROUPS:
            flash("部位を選択してください。", "error")
        elif trained_date > date.today() or recovered_date > date.today():
            flash("未来の日付は登録できません。", "error")
        elif recovered_date < trained_date:
            flash("回復日はトレーニング日以降の日付にしてください。", "error")
        else:
            recovery_log = MuscleRecoveryLog.query.filter_by(user_id=user.id, muscle_group=muscle_group, trained_date=trained_date).first()
            if recovery_log is None:
                recovery_log = MuscleRecoveryLog(user_id=user.id, muscle_group=muscle_group,
                    trained_date=trained_date, recovered_date=recovered_date)
                db.session.add(recovery_log)
            else:
                recovery_log.recovered_date = recovered_date
            db.session.flush()
            check = add_recovery_check(user.id, muscle_group, trained_date)
            check.recovery_log_id = recovery_log.id
            db.session.commit()
            flash("回復記録を保存しました。", "success")
        return redirect(url_for("recovery_page"))

    sync_recent_recovery_checks(user.id)
    recovery_logs = MuscleRecoveryLog.query.filter_by(user_id=user.id).order_by(
        MuscleRecoveryLog.recovered_date.desc(), MuscleRecoveryLog.id.desc()
    ).all()
    return render_template(
        "recovery.html", recovery_logs=recovery_logs,
        pending_checks=RecoveryCheck.query.filter_by(user_id=user.id, recovery_log_id=None).order_by(RecoveryCheck.trained_date.desc(), RecoveryCheck.id).all(),
        recovery_estimates=recovery_estimates(user.id),
        muscle_groups=MUSCLE_GROUPS, today=date.today(),
    )


@app.route("/recovery/checks/<int:check_id>", methods=["POST"])
@login_required
def update_recovery_check(check_id):
    check = RecoveryCheck.query.filter_by(id=check_id, user_id=get_current_user().id).first_or_404()
    status = request.form.get("status")
    if status not in ("sore", "recovered") or check.trained_date > date.today():
        abort(400)
    if check.recovery_log_id is not None:
        flash("この部位の回復日はすでに記録済みです。", "success")
        return redirect(url_for("recovery_page"))
    if status == "sore":
        check.last_sore_date = date.today()
        flash(f"{check.muscle_group}はまだ筋肉痛があると記録しました。", "success")
    else:
        recovery_log = MuscleRecoveryLog.query.filter_by(user_id=check.user_id,
            muscle_group=check.muscle_group, trained_date=check.trained_date).first()
        if recovery_log is None:
            recovery_log = MuscleRecoveryLog(user_id=check.user_id, muscle_group=check.muscle_group,
                trained_date=check.trained_date, recovered_date=date.today())
            db.session.add(recovery_log)
            db.session.flush()
        check.recovery_log_id = recovery_log.id
        flash(f"{check.muscle_group}の回復日を今日で記録しました。", "success")
    db.session.commit()
    return redirect(url_for("recovery_page"))


@app.route("/recovery/<int:log_id>/delete", methods=["POST"])
@login_required
def delete_recovery_log(log_id):
    recovery_log = MuscleRecoveryLog.query.filter_by(
        id=log_id, user_id=get_current_user().id
    ).first_or_404()
    RecoveryCheck.query.filter_by(user_id=recovery_log.user_id, recovery_log_id=log_id).update({RecoveryCheck.recovery_log_id: None})
    db.session.delete(recovery_log)
    db.session.commit()
    flash("回復記録を削除しました。", "success")
    return redirect(url_for("recovery_page"))

if __name__ == "__main__":
    # ローカル開発で必要なときだけ FLASK_DEBUG=1 を設定する。
    app.run(debug=os.environ.get("FLASK_DEBUG") == "1")
