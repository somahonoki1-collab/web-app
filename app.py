import math
import os
import calendar as month_calendar
from functools import wraps
from hmac import compare_digest
from secrets import token_hex, token_urlsafe
from flask import Flask, abort, flash, render_template, request, redirect, session, url_for
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

def calculate_target_weight(max_weight, target_reps, lift_type):
    if lift_type == "ベンチプレス":
        return max_weight / (1 + target_reps / 40.0)
    elif lift_type == "スクワット":
        return max_weight / (1 + target_reps / 33.3)
    else:
        return max_weight / (1 + target_reps / 40.0)


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
    max_weight = None
    next_weight = None

    if request.method == "POST":
        values, error = validate_training_form(request.form, allowed_exercises)
        if error:
            all_logs = TrainingLog.query.filter_by(user_id=get_current_user().id).order_by(TrainingLog.date.desc()).all()
            return render_template(
                "index.html", max_w=None, target_w=None, logs=all_logs,
                error=error, form_data=request.form, exercises=exercises,
                chart_data=build_chart_data(all_logs),
            )

        lift = values["lift_type"]
        last_weight = values["weight"]
        last_reps = values["reps"]
        target = values["target_reps"]
        
        max_weight = calculate_1rm(last_weight, last_reps, lift)
        next_weight = calculate_target_weight(max_weight, target, lift)
        max_weight = round(max_weight, 1)
        next_weight = round(next_weight, 1)

        # --- 3. データベースに保存 ---
        # 1回分のトレーニング結果をインスタンス化
        new_log = TrainingLog(
            lift_type=lift,
            weight=last_weight,
            reps=last_reps,
            one_rm=max_weight,
            target_reps=target,
            target_weight=next_weight,
            user_id=get_current_user().id,
        )
        db.session.add(new_log) # DBに追加
        db.session.commit()     # 確定して保存

    # --- 4. データベースから読み込み ---
    # 保存されている記録を、日付の新しい順（desc）に全て（all）取得する
    all_logs = TrainingLog.query.filter_by(user_id=get_current_user().id).order_by(TrainingLog.date.desc()).all()

    # 取得した all_logs をHTMLに渡す
    return render_template(
        "index.html", max_w=max_weight, target_w=next_weight, logs=all_logs,
        error=None, form_data={}, exercises=exercises, chart_data=build_chart_data(all_logs),
    )

# --- 5. データベースから削除 ---
@app.route("/delete/<int:log_id>", methods=["POST"])
@login_required
def delete_log(log_id):
    # 送られてきたIDと一致する記録をデータベースから探し出す
    log_to_delete = TrainingLog.query.filter_by(id=log_id, user_id=get_current_user().id).first()
    
    if log_to_delete:
        db.session.delete(log_to_delete) # 削除の予約
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
    
    if request.method == "POST":
        # === 保存ボタンが押された時（上書き処理） ===
        
        values, error = validate_training_form(request.form, [exercise.name for exercise in exercises])
        if error:
            return render_template(
                "edit.html", log=log_to_edit, error=error, form_data=request.form,
                exercises=exercises,
            ), 400

        new_date_str = request.form.get("date", "")
        try:
            new_date = datetime.strptime(new_date_str, '%Y-%m-%dT%H:%M')
        except (TypeError, ValueError):
            return render_template(
                "edit.html", log=log_to_edit, error="日時を正しく入力してください。",
                form_data=request.form, exercises=exercises,
            ), 400

        new_lift = values["lift_type"]
        new_weight = values["weight"]
        new_reps = values["reps"]
        new_target = values["target_reps"]

        new_max_weight = calculate_1rm(new_weight, new_reps, new_lift)
        new_next_weight = calculate_target_weight(new_max_weight, new_target, new_lift)
        new_max_weight = round(new_max_weight, 1)
        new_next_weight = round(new_next_weight, 1)
        
        log_to_edit.lift_type = new_lift
        log_to_edit.weight = new_weight
        log_to_edit.reps = new_reps
        log_to_edit.one_rm = new_max_weight
        log_to_edit.target_reps = new_target
        log_to_edit.target_weight = new_next_weight
        log_to_edit.date = new_date
        log_to_edit.user_id = get_current_user().id

        # データベースの変更を確定して保存する
        db.session.commit()     # 確定して保存

        return redirect(url_for('home'))

    else:
        # === 編集ボタンが押された直後 ===
        # まだ作っていませんが、編集専用の画面に log_to_edit を渡して開く
        return render_template(
            "edit.html", log=log_to_edit, error=None, form_data={}, exercises=exercises
        )


@app.route("/calendar")
@login_required
def calendar_page():
    user = get_current_user()
    ensure_user_setup(user)
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
                candidate_dates.append(last_date + timedelta(days=max(1, math.ceil(recovery_days))))
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
    if not workout_day.items:
        flash("このメニューには種目がありません。先に種目を登録してください。", "error")
        return redirect(url_for("menu_page"))
    schedule = None
    if schedule_id is not None:
        schedule = WorkoutSchedule.query.filter_by(id=schedule_id, user_id=user.id).first_or_404()
        if schedule.workout_day_id != workout_day.id or schedule.status == "cancelled":
            abort(400)
        if schedule.session is not None:
            return redirect(url_for("workout_session_page", session_id=schedule.session.id))

    workout_session = WorkoutSession(
        user_id=user.id, workout_day_id=workout_day.id, schedule_id=schedule_id
    )
    db.session.add(workout_session)
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
    for item in workout_session.workout_day.items:
        history_logs = TrainingLog.query.filter_by(
            user_id=user.id, lift_type=item.exercise.name
        ).filter(or_(
            TrainingLog.session_id.is_(None),
            TrainingLog.session_id != workout_session.id,
        )).order_by(TrainingLog.date.desc(), TrainingLog.id.desc()).all()
        previous_log = history_logs[0] if history_logs else None
        previous_sets = []
        if previous_log is not None:
            if previous_log.session_id is not None:
                previous_sets = [log for log in history_logs if log.session_id == previous_log.session_id]
            else:
                previous_sets = [
                    log for log in history_logs
                    if log.session_id is None and log.date.date() == previous_log.date.date()
                ]
        completed_sets = {
            log.set_number: log for log in session_logs if log.lift_type == item.exercise.name
        }
        next_reps = item.rep_min
        target_weight = previous_log.weight if previous_log is not None else None
        target_reason = "前回と同じ重量で、各セットの回数を少しずつ伸ばします。"
        if previous_log is not None:
            all_sets_at_top = (
                len(previous_sets) >= item.sets
                and all(log.reps >= item.rep_max for log in previous_sets[:item.sets])
            )
            if all_sets_at_top:
                increment = max(1.0, round(previous_log.weight * 0.025 * 2) / 2)
                target_weight = round(previous_log.weight + increment, 1)
                target_reason = "前回すべてのセットで上限回数に届いたため、重量を小幅に上げる目安です。"
                next_reps = item.rep_min
            else:
                best_reps = max((log.reps for log in previous_sets), default=previous_log.reps)
                next_reps = min(item.rep_max, max(item.rep_min, best_reps + 1))
        workout_items.append({
            "item": item,
            "previous_log": previous_log,
            "next_reps": next_reps,
            "target_weight": target_weight,
            "target_reason": target_reason,
            "completed_sets": completed_sets,
        })

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

    item = MenuItem.query.join(WorkoutDay).filter(
        MenuItem.exercise_id == exercise_id,
        MenuItem.workout_day_id == workout_session.workout_day_id,
        WorkoutDay.user_id == user.id,
    ).first()
    if item is None or not 1 <= set_number <= item.sets:
        abort(400)
    if TrainingLog.query.filter_by(
        user_id=user.id, session_id=workout_session.id,
        lift_type=item.exercise.name, set_number=set_number,
    ).first() is not None:
        flash("このセットはすでに記録済みです。", "error")
        return redirect(url_for("workout_session_page", session_id=session_id))

    values, error = validate_training_form(request.form, [item.exercise.name])
    if error:
        flash(error, "error")
        return redirect(url_for("workout_session_page", session_id=session_id))

    one_rm = round(calculate_1rm(values["weight"], values["reps"], item.exercise.name), 1)
    target_weight = round(
        calculate_target_weight(one_rm, values["target_reps"], item.exercise.name), 1
    )
    db.session.add(TrainingLog(
        user_id=user.id, session_id=workout_session.id, set_number=set_number,
        lift_type=item.exercise.name, weight=values["weight"], reps=values["reps"],
        one_rm=one_rm, target_reps=values["target_reps"], target_weight=target_weight,
    ))
    db.session.commit()
    flash(f"{item.exercise.name}の{set_number}セット目を記録しました。", "success")
    return redirect(url_for("workout_session_page", session_id=session_id))


@app.route("/workouts/<int:session_id>/finish", methods=["POST"])
@login_required
def finish_workout(session_id):
    user = get_current_user()
    workout_session = WorkoutSession.query.filter_by(id=session_id, user_id=user.id).first_or_404()
    if workout_session.completed_at is None:
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
            db.session.add(MuscleRecoveryLog(
                user_id=user.id, muscle_group=muscle_group,
                trained_date=trained_date, recovered_date=recovered_date,
            ))
            db.session.commit()
            flash("回復記録を保存しました。", "success")
        return redirect(url_for("recovery_page"))

    recovery_logs = MuscleRecoveryLog.query.filter_by(user_id=user.id).order_by(
        MuscleRecoveryLog.recovered_date.desc(), MuscleRecoveryLog.id.desc()
    ).all()
    return render_template(
        "recovery.html", recovery_logs=recovery_logs,
        recovery_estimates=recovery_estimates(user.id),
        muscle_groups=MUSCLE_GROUPS, today=date.today(),
    )


@app.route("/recovery/<int:log_id>/delete", methods=["POST"])
@login_required
def delete_recovery_log(log_id):
    recovery_log = MuscleRecoveryLog.query.filter_by(
        id=log_id, user_id=get_current_user().id
    ).first_or_404()
    db.session.delete(recovery_log)
    db.session.commit()
    flash("回復記録を削除しました。", "success")
    return redirect(url_for("recovery_page"))

if __name__ == "__main__":
    # ローカル開発で必要なときだけ FLASK_DEBUG=1 を設定する。
    app.run(debug=os.environ.get("FLASK_DEBUG") == "1")
