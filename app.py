import math
import json
import os
import calendar as month_calendar
from functools import wraps
from hmac import compare_digest
from hmac import new as hmac_new
from hashlib import sha256
from secrets import token_hex, token_urlsafe
from flask import Flask, abort, flash, jsonify, render_template, request, redirect, session, url_for
from flask_sqlalchemy import SQLAlchemy # データベース用の道具
from datetime import date, datetime, time, timedelta # 日付・時刻を扱うための道具
from sqlalchemy import UniqueConstraint, case, inspect, or_, text
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.exc import IntegrityError
from werkzeug.security import check_password_hash, generate_password_hash
from growth import register_growth
from runtime_config import configure_environment
from time_utils import japan_now, japan_today

IS_PRODUCTION = configure_environment()
instance_options = {"instance_path": os.environ["WEB_APP_INSTANCE_PATH"]} if os.environ.get("WEB_APP_INSTANCE_PATH") else {}
app = Flask(__name__, **instance_options)


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
app.config["SESSION_COOKIE_SECURE"] = IS_PRODUCTION
app.config["PERMANENT_SESSION_LIFETIME"] = timedelta(days=7)
app.config["SESSION_REFRESH_EACH_REQUEST"] = False
app.config["MAX_CONTENT_LENGTH"] = 128 * 1024
app.config["MAX_FORM_MEMORY_SIZE"] = 128 * 1024
app.config["MAX_FORM_PARTS"] = 300
if IS_PRODUCTION:
    app.config["TRUSTED_HOSTS"] = [os.environ["WEB_APP_HOST"]]

# --- 1. データベースの基本設定 ---
# SQLiteを使い、training.dbというファイルにデータを保存する設定
app.config['SQLALCHEMY_DATABASE_URI'] = os.environ.get('WEB_APP_DATABASE_URI', 'sqlite:///training.db')
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
db = SQLAlchemy(app)

PPL_SPLITS = (("push", "Push"), ("pull", "Pull"), ("legs", "Legs"))
MUSCLE_GROUPS = ("胸", "背中", "肩", "腕", "脚", "臀部", "体幹", "全身", "その他")
DEFAULT_EXERCISES = (("ベンチプレス", "胸"), ("スクワット", "脚"), ("その他", "全身"))
RECORDING_MODES = {"weight": "重量＋回数", "bodyweight": "自重＋加重・補助", "reps": "回数のみ", "negative": "ネガティブ（重量＋回数）"}


class User(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(40), unique=True, nullable=False)
    password_hash = db.Column(db.String(255), nullable=False)
    auth_version = db.Column(db.Integer, nullable=False, default=1)


class AuthThrottle(db.Model):
    key = db.Column(db.String(64), primary_key=True)
    window_started_at = db.Column(db.DateTime, nullable=False)
    attempts = db.Column(db.Integer, nullable=False)


class Exercise(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False, index=True)
    name = db.Column(db.String(80), nullable=False)
    muscle_group = db.Column(db.String(30), nullable=False, default="その他")
    is_archived = db.Column(db.Boolean, nullable=False, default=False)
    recording_mode = db.Column(db.String(20), nullable=False, default="weight")
    __table_args__ = (UniqueConstraint("user_id", "name", name="uq_exercise_user_name"),)


class WorkoutDay(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False, index=True)
    split_code = db.Column(db.String(10), nullable=False)
    name = db.Column(db.String(30), nullable=False)
    is_archived = db.Column(db.Boolean, nullable=False, default=False)
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
    alternatives = db.relationship("MenuItemAlternative", cascade="all, delete-orphan",
                                   order_by="MenuItemAlternative.id")


class MenuItemAlternative(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    menu_item_id = db.Column(db.Integer, db.ForeignKey("menu_item.id"), nullable=False, index=True)
    exercise_id = db.Column(db.Integer, db.ForeignKey("exercise.id"), nullable=False)
    exercise = db.relationship("Exercise")
    __table_args__ = (UniqueConstraint("menu_item_id", "exercise_id", name="uq_menu_alternative"),)


class WorkoutSchedule(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False, index=True)
    workout_day_id = db.Column(db.Integer, db.ForeignKey("workout_day.id"), nullable=False)
    scheduled_date = db.Column(db.Date, nullable=False, index=True)
    status = db.Column(db.String(20), nullable=False, default="planned")
    created_at = db.Column(db.DateTime, nullable=True, default=japan_now)
    workout_day = db.relationship("WorkoutDay")
    session = db.relationship("WorkoutSession", back_populates="schedule", uselist=False)


class WorkoutSession(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False, index=True)
    workout_day_id = db.Column(db.Integer, db.ForeignKey("workout_day.id"), nullable=False)
    schedule_id = db.Column(db.Integer, db.ForeignKey("workout_schedule.id"), nullable=True, unique=True)
    started_at = db.Column(db.DateTime, nullable=False, default=japan_now)
    completed_at = db.Column(db.DateTime, nullable=True)
    paused_at = db.Column(db.DateTime, nullable=True)
    menu_name = db.Column(db.String(30), nullable=True)
    workout_day = db.relationship("WorkoutDay")
    schedule = db.relationship("WorkoutSchedule", back_populates="session")
    items = db.relationship("WorkoutSessionItem", order_by="WorkoutSessionItem.position", cascade="all, delete-orphan")

    @property
    def display_name(self):
        return self.menu_name or self.workout_day.name


class WorkoutSessionItem(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    session_id = db.Column(db.Integer, db.ForeignKey("workout_session.id"), nullable=False, index=True)
    exercise_id = db.Column(db.Integer, db.ForeignKey("exercise.id"), nullable=False)
    muscle_group = db.Column(db.String(30), nullable=False)
    position = db.Column(db.Integer, nullable=False)
    sets = db.Column(db.Integer, nullable=False)
    rep_min = db.Column(db.Integer, nullable=False)
    rep_max = db.Column(db.Integer, nullable=False)
    is_skipped = db.Column(db.Boolean, nullable=False, default=False)
    exercise = db.relationship("Exercise")
    options = db.relationship("WorkoutSessionOption", cascade="all, delete-orphan",
                              order_by="WorkoutSessionOption.id")


class WorkoutSessionOption(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    session_item_id = db.Column(db.Integer, db.ForeignKey("workout_session_item.id"), nullable=False, index=True)
    exercise_id = db.Column(db.Integer, db.ForeignKey("exercise.id"), nullable=False)
    muscle_group = db.Column(db.String(30), nullable=False)
    exercise = db.relationship("Exercise")
    __table_args__ = (UniqueConstraint("session_item_id", "exercise_id", name="uq_session_option"),)


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
    dismissed = db.Column(db.Boolean, nullable=False, default=False)
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


def validate_training_form(form, allowed_exercises, mode="weight"):
    """フォーム値を検証し、保存に使える値かエラーメッセージを返す。"""
    lift_type = form.get("lift_type", "")
    if lift_type not in allowed_exercises:
        return None, "種目を選択してください。"

    if not isinstance(mode, str) or mode not in RECORDING_MODES:
        return None, "記録方式を選択してください。"
    if form.get("recording_mode", mode) != mode:
        return None, "記録方式が変更されています。ページを再読み込みしてから記録してください。"
    components = {"body_weight": None, "added_weight": None, "assistance_weight": None}
    try:
        if mode == "reps":
            weight = 0.0
        elif mode == "bodyweight":
            components = {key: float(form.get(key, "0") or "0") for key in components}
            body, added, assistance = components.values()
            if not all(math.isfinite(value) and 0 <= value <= 1000 for value in components.values()):
                raise ValueError
            if body <= 0 or (added > 0 and assistance > 0) or assistance >= body:
                return None, "体重を入力し、加重と補助はどちらか一方にしてください。補助重量は体重未満にしてください。"
            weight = round(body + added - assistance, 3)
        else:
            weight = float(form.get("weight", ""))
        if not math.isfinite(weight) or (mode != "reps" and not 0 < weight <= 1000):
            raise ValueError
    except (TypeError, ValueError):
        return None, "重量・体重は数値で入力してください。計算後の重量は0より大きく1000kg以下にしてください。"

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
        "recording_mode": mode,
        **components,
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
    return Exercise.query.filter_by(user_id=user.id, is_archived=False).order_by(Exercise.name).all()

# --- 2. テーブル（表）の設計図：Javaのクラスと同じ感覚 ---
class TrainingLog(db.Model):
    id = db.Column(db.Integer, primary_key=True) # 自動で振られる番号
    date = db.Column(db.DateTime, nullable=False, default=japan_now) # 記録した日時
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
    recording_mode = db.Column(db.String(20), nullable=False, default="weight")
    body_weight = db.Column(db.Float, nullable=True)
    added_weight = db.Column(db.Float, nullable=True)
    assistance_weight = db.Column(db.Float, nullable=True)

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
    for model, name, declaration in (
        (User, "auth_version", "INTEGER NOT NULL DEFAULT 1"),
        (Exercise, "is_archived", "BOOLEAN NOT NULL DEFAULT 0"),
        (Exercise, "recording_mode", "VARCHAR(20) NOT NULL DEFAULT 'weight'"),
        (TrainingLog, "recording_mode", "VARCHAR(20) NOT NULL DEFAULT 'weight'"),
        (TrainingLog, "body_weight", "FLOAT"),
        (TrainingLog, "added_weight", "FLOAT"),
        (TrainingLog, "assistance_weight", "FLOAT"),
        (WorkoutDay, "is_archived", "BOOLEAN NOT NULL DEFAULT 0"),
        (WorkoutSession, "paused_at", "DATETIME"),
        (WorkoutSession, "menu_name", "VARCHAR(30)"),
        (WorkoutSessionItem, "is_skipped", "BOOLEAN NOT NULL DEFAULT 0"),
        (WorkoutSchedule, "created_at", "DATETIME"),
        (RecoveryCheck, "dismissed", "BOOLEAN NOT NULL DEFAULT 0"),
    ):
        columns = {column["name"] for column in inspect(db.engine).get_columns(model.__tablename__)}
        if name not in columns:
            with db.engine.begin() as connection:
                connection.execute(text(f"ALTER TABLE {model.__tablename__} ADD COLUMN {name} {declaration}"))
    with db.engine.begin() as connection:
        connection.execute(text("CREATE INDEX IF NOT EXISTS ix_training_log_user_id ON training_log (user_id)"))
        connection.execute(text("CREATE INDEX IF NOT EXISTS ix_training_log_session_id ON training_log (session_id)"))
    if IS_PRODUCTION and User.query.first() is None:
        raise RuntimeError("最初の管理用アカウントをローカルで作成し、その記録データを配置してから公開してください。")


def get_current_user():
    user_id = session.get("user_id")
    user = db.session.get(User, user_id) if user_id is not None else None
    if user is None or session.get("auth_version", 1) != user.auth_version:
        return None
    return user


def auth_rate_limit(mode, username=""):
    """Persist fixed windows in SQLite; never trust client-supplied forwarding headers."""
    now = japan_now()
    address = request.remote_addr or "unknown"
    rules = [(f"register:{address}", 10, 3600)] if mode == "register" else [
        (f"login:{address}", 30, 900),
        (f"login-user:{address}:{username[:40]}", 10, 900),
    ]
    AuthThrottle.query.filter(AuthThrottle.window_started_at < now - timedelta(days=2)).delete(
        synchronize_session=False)
    for identity, limit, seconds in rules:
        key = hmac_new(app.secret_key.encode("utf-8"), identity.encode("utf-8"), sha256).hexdigest()
        expired = AuthThrottle.window_started_at <= now - timedelta(seconds=seconds)
        statement = sqlite_insert(AuthThrottle).values(key=key, window_started_at=now, attempts=1)
        statement = statement.on_conflict_do_update(index_elements=[AuthThrottle.key], set_={
            "window_started_at": case((expired, now), else_=AuthThrottle.window_started_at),
            "attempts": case((expired, 1), else_=AuthThrottle.attempts + 1),
        })
        db.session.execute(statement)
        saved = db.session.execute(db.select(AuthThrottle).where(AuthThrottle.key == key)).scalar_one()
        if saved.attempts > limit:
            retry_after = max(1, math.ceil(seconds - (now - saved.window_started_at).total_seconds()))
            db.session.commit()
            flash("試行回数が多いため、しばらく待ってから再度お試しください。", "error")
            return render_template("auth.html", mode=mode), 429, {"Retry-After": str(retry_after)}
    db.session.commit()
    return None


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
        if not expected or not submitted or not compare_digest(expected.encode("utf-8"), submitted.encode("utf-8")):
            abort(400, description="フォームの有効期限が切れました。ページを再読み込みしてください。")


@app.after_request
def add_response_headers(response):
    response.headers["X-Robots-Tag"] = "noindex, nofollow, noarchive"
    response.headers["Cache-Control"] = "no-store"
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "same-origin"
    if IS_PRODUCTION:
        response.headers["Strict-Transport-Security"] = "max-age=31536000"
    return response


@app.get("/robots.txt")
def robots():
    # Crawlers must be able to read the noindex response header.
    return app.response_class("User-agent: *\nDisallow:\n", mimetype="text/plain")


@app.context_processor
def provide_template_helpers():
    def csrf_token():
        token = session.get("_csrf_token")
        if token is None:
            token = token_urlsafe(32)
            session["_csrf_token"] = token
        return token
    return {"csrf_token": csrf_token, "recording_modes": RECORDING_MODES,
            "record_text": record_text, "history_key": history_key, "record_metric": record_metric}

# --- 計算関数 ---
def calculate_1rm(weight, reps, lift_type):
    divisor = 33.3 if lift_type == "スクワット" else 40.0
    return weight * (1 + reps / divisor)


def apply_record_values(log, values):
    for key in ("weight", "reps", "recording_mode", "body_weight", "added_weight", "assistance_weight"):
        setattr(log, key, values[key])
    log.one_rm = (round(calculate_1rm(log.weight, log.reps, log.lift_type), 1)
                  if log.recording_mode in ("weight", "bodyweight") else 0.0)


def record_text(log):
    if log.recording_mode == "reps":
        return f"{log.reps}回"
    if log.recording_mode == "bodyweight":
        return f"体重{log.body_weight:g}kg ＋ 加重{log.added_weight:g}kg − 補助{log.assistance_weight:g}kg × {log.reps}回"
    prefix = "ネガティブ · " if log.recording_mode == "negative" else ""
    return f"{prefix}{log.weight:g}kg × {log.reps}回"


def history_key(log):
    return log.lift_type if log.recording_mode == "weight" else f"{log.lift_type}（{RECORDING_MODES[log.recording_mode]}）"


def record_metric(log):
    if log.recording_mode == "reps":
        return log.reps, "最高回数（回）"
    if log.recording_mode == "negative":
        return log.weight, "ネガティブ最高重量（kg）"
    return log.one_rm, "推定1RM（kg）" if log.recording_mode == "weight" else "自重・推定1RM目安（kg）"

def sync_growth_records(user_id):
    """Keep active competition snapshots in the workout transaction."""
    growth = app.extensions.get("growth")
    if growth is not None:
        growth["sync_user"](user_id)

def progression_targets(logs, sets, rep_min, rep_max, mode="weight"):
    """全セットの達成状況で増量し、それ以外は各セットの回数を伸ばす。"""
    previous = {log.set_number: log for log in logs}
    complete = all(number in previous for number in range(1, sets + 1))
    weights = [previous[number].weight for number in range(1, sets + 1)] if complete else []
    increase = complete and all(previous[number].reps >= rep_max for number in range(1, sets + 1))
    # 異なる重量のセットは一律に増量せず、それぞれの重量を維持する。
    increase = increase and max(weights) - min(weights) < 0.001 and mode == "weight"
    targets = []
    for number in range(1, sets + 1):
        log = previous.get(number)
        weight = log.weight if log else (logs[0].weight if logs else None)
        reps = rep_min
        if increase:
            weight = min(1000, round(weight + max(1.0, round(weight * 0.025 * 2) / 2), 1))
        elif log:
            reps = min(rep_max, max(rep_min, log.reps + 1))
        basis = log or (logs[0] if logs else None)
        targets.append({"set_number": number, "weight": weight, "reps": reps,
                        **{key: getattr(basis, key) if basis else None
                           for key in ("body_weight", "added_weight", "assistance_weight")}})
    reason = "全セットで上限回数を達成したので、重量を少し増やして下限回数から始めます。" if increase else "重量を維持し、セットごとに回数を1回ずつ伸ばします。上限に届いたセットはその回数を維持します。"
    if not logs:
        reason = "初回は無理のない重量を選び、設定した回数の下限から始めます。"
    if mode != "weight":
        reason = ("同じ条件で回数を伸ばします。上限達成後の難度・加重・補助の変更は自分で調整してください。"
                  if logs else "設定した回数の下限を目安に、実施した回数を記録してください。")
    return {"targets": targets, "reason": reason, "increased": increase}


def previous_exercise_logs(user_id, lift_type, before=None, exclude_session=None, mode=None):
    if mode is None:
        exercise = Exercise.query.filter_by(user_id=user_id, name=lift_type).first()
        mode = exercise.recording_mode if exercise else "weight"
    query = TrainingLog.query.outerjoin(WorkoutSession, TrainingLog.session_id == WorkoutSession.id).filter(
        TrainingLog.user_id == user_id, TrainingLog.lift_type == lift_type,
        TrainingLog.weight >= 0, TrainingLog.weight <= 1000,
        TrainingLog.reps.between(1, 100),
        or_(TrainingLog.session_id.is_(None), WorkoutSession.completed_at.isnot(None)),
    )
    if mode != "all":
        query = query.filter(TrainingLog.recording_mode == mode)
    if before is not None:
        query = query.filter(TrainingLog.date < before)
    if exclude_session is not None:
        query = query.filter(or_(TrainingLog.session_id.is_(None), TrainingLog.session_id != exclude_session))
    latest = query.order_by(TrainingLog.date.desc(), TrainingLog.id.desc()).first()
    if latest is None:
        return []
    group = TrainingLog.query.filter_by(user_id=user_id, lift_type=lift_type)
    if mode != "all":
        group = group.filter_by(recording_mode=mode)
    if latest.session_id is not None:
        group = group.filter_by(session_id=latest.session_id)
    elif latest.batch_id is not None:
        group = group.filter_by(batch_id=latest.batch_id)
    else:
        # 古い単発記録を同じ日の全セットと見なさない。
        return [latest]
    return group.filter(TrainingLog.weight >= 0, TrainingLog.reps.between(1, 100)).order_by(TrainingLog.set_number, TrainingLog.id).all()


def exercise_recipe(user_id, exercise_id):
    item = MenuItem.query.join(WorkoutDay).filter(
        WorkoutDay.user_id == user_id, WorkoutDay.is_archived.is_(False),
        or_(MenuItem.exercise_id == exercise_id,
            MenuItem.alternatives.any(MenuItemAlternative.exercise_id == exercise_id)),
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
        snapshot = WorkoutSessionItem(
            exercise_id=exercise_id, muscle_group=exercise.muscle_group, position=position,
            sets=item.sets if item else max((log.set_number for log in saved), default=1),
            rep_min=item.rep_min if item else 8, rep_max=item.rep_max if item else 12,
        )
        choices = [exercise] + ([alternative.exercise for alternative in item.alternatives] if item else [])
        snapshot.options = [WorkoutSessionOption(exercise_id=choice.id, muscle_group=choice.muscle_group)
                            for choice in choices]
        workout_session.items.append(snapshot)
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
    logs = [saved for saved in logs if saved.recording_mode == log.recording_mode]
    guidance = progression_targets(logs, **recipe, mode=log.recording_mode)
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
        TrainingLog.date >= datetime.combine(japan_today() - timedelta(days=14), time.min),
        TrainingLog.date <= japan_now(),
        or_(TrainingLog.session_id.is_(None), WorkoutSession.completed_at.isnot(None)),
    ).group_by(Exercise.muscle_group).all()
    for group, trained_at in rows:
        add_recovery_check(user_id, group, trained_at.date())
    db.session.commit()


def build_chart_data(logs):
    return [
        {"date": log.date.strftime("%Y-%m-%d"), "lift": history_key(log),
         "rm": record_metric(log)[0], "metric": record_metric(log)[1]}
        for log in reversed(logs)
    ]


def daily_history(logs):
    groups = {}
    for log in logs:
        if log.lift_type == "その他":
            continue
        groups.setdefault((log.lift_type, log.recording_mode, log.date.date()), []).append(log)
    entries = []
    for saved in groups.values():
        best = max(saved, key=lambda log: (record_metric(log)[0], log.reps, -log.id))
        entries.append({"best": best, "sets": sorted(saved, key=lambda log: (log.date, log.id))})
    return sorted(entries, key=lambda row: (row["best"].date, row["best"].id), reverse=True)


@app.route("/register", methods=["GET", "POST"])
def register():
    if get_current_user() is not None:
        return redirect(url_for("home"))

    if request.method == "POST":
        username = request.form.get("username", "").strip().lower()
        password = request.form.get("password", "")
        password_confirmation = request.form.get("password_confirmation", "")

        limited = auth_rate_limit("register")
        if limited is not None:
            return limited

        if not 3 <= len(username) <= 40:
            flash("ユーザー名は3〜40文字で入力してください。", "error")
        elif User.query.filter_by(username=username).first() is not None:
            flash("このユーザー名はすでに使われています。", "error")
        elif len(password) < 8:
            flash("パスワードは8文字以上にしてください。", "error")
        elif len(password) > 128:
            flash("パスワードは128文字以内にしてください。", "error")
        elif password != password_confirmation:
            flash("確認用パスワードが一致しません。", "error")
        else:
            is_first_user = User.query.count() == 0
            user = User(username=username, password_hash=generate_password_hash(password))
            db.session.add(user)
            try:
                db.session.flush()
            except IntegrityError:
                db.session.rollback()
                flash("このユーザー名はすでに使われています。", "error")
                return render_template("auth.html", mode="register"), 400
            if is_first_user:
                # 現在の7件を、ユーザー指定どおり最初のアカウントへ引き継ぐ。
                TrainingLog.query.filter_by(user_id=None).update(
                    {TrainingLog.user_id: user.id}, synchronize_session=False
                )
            ensure_user_setup(user)
            session.clear()
            session["user_id"] = user.id
            session["auth_version"] = user.auth_version
            session.permanent = True
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
        limited = auth_rate_limit("login", username)
        if limited is not None:
            return limited
        user = User.query.filter_by(username=username).first()
        if len(username) > 40 or len(password) > 128 or user is None or not check_password_hash(user.password_hash, password):
            flash("ユーザー名またはパスワードが違います。", "error")
        else:
            session.clear()
            session["user_id"] = user.id
            session["auth_version"] = user.auth_version
            session.permanent = True
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
                values, error = validate_training_form({**request.form.to_dict(), "lift_type": lift, **row, "target_reps": str(rep_max)}, allowed_exercises, exercise.recording_mode if exercise else "weight")
                if error:
                    break
                validated.append(values)
        if not error:
            batch = TrainingBatch(user_id=user.id, sets=expected_sets, rep_min=rep_min, rep_max=rep_max)
            db.session.add(batch)
            db.session.flush()
            recorded_at = japan_now()
            new_logs = []
            for number, values in enumerate(validated, 1):
                log = TrainingLog(user_id=user.id, batch_id=batch.id, set_number=number, date=recorded_at,
                    lift_type=lift, weight=values["weight"], reps=values["reps"],
                    one_rm=round(calculate_1rm(values["weight"], values["reps"], lift), 1),
                    target_reps=rep_max, target_weight=values["weight"])
                apply_record_values(log, values)
                db.session.add(log)
                new_logs.append(log)
            db.session.flush()
            refresh_log_targets(new_logs[0])
            add_recovery_check(user.id, exercise.muscle_group, recorded_at.date())
            sync_growth_records(user.id)
            db.session.commit()
            flash(f"{lift}の{len(new_logs)}セットを保存しました。", "success")
            return redirect(url_for("home", saved_batch=batch.id))

    guidance_by_exercise = {}
    for exercise in exercises:
        recipe = exercise_recipe(user.id, exercise.id)
        previous = previous_exercise_logs(user.id, exercise.name)
        guidance_by_exercise[exercise.name] = {
            "exercise_id": exercise.id,
            **recipe, **progression_targets(previous, **recipe, mode=exercise.recording_mode),
            "previous": [{"set_number": log.set_number, "weight": log.weight, "reps": log.reps} for log in previous],
            "previous_date": previous[0].date.strftime("%Y-%m-%d") if previous else None,
        }
    all_logs = TrainingLog.query.filter_by(user_id=user.id).order_by(TrainingLog.date.desc(), TrainingLog.id.desc()).all()
    history = daily_history(all_logs)
    history_logs = [row["best"] for row in history]
    history_lifts = sorted({exercise.name if exercise.recording_mode == "weight" else f"{exercise.name}（{RECORDING_MODES[exercise.recording_mode]}）"
                            for exercise in exercises if exercise.name != "その他"}
                           | {history_key(log) for log in history_logs})
    if history_logs:
        recent_lift = history_key(history_logs[0])
        history_lifts = [recent_lift] + [lift for lift in history_lifts if lift != recent_lift]
    ongoing_sessions = WorkoutSession.query.filter_by(user_id=user.id, completed_at=None).order_by(WorkoutSession.started_at.desc()).all()
    next_guidance = None
    saved_batch_id = request.args.get("saved_batch", type=int)
    if saved_batch_id:
        batch = TrainingBatch.query.filter_by(id=saved_batch_id, user_id=user.id).first()
        saved = TrainingLog.query.filter_by(batch_id=saved_batch_id, user_id=user.id).order_by(TrainingLog.set_number).all()
        if batch and saved:
            next_guidance = {"lift_type": saved[0].lift_type, **progression_targets(saved, batch.sets, batch.rep_min, batch.rep_max, mode=saved[0].recording_mode)}
    return render_template(
        "index.html", logs=history_logs, history=history, history_lifts=history_lifts,
        error=error, form_data=request.form if error else {},
        set_rows=set_rows if error else [], exercises=exercises, guidance_by_exercise=guidance_by_exercise,
        next_guidance=next_guidance, ongoing_sessions=ongoing_sessions, chart_data=build_chart_data(history_logs),
    ), 400 if error else 200


@app.route("/guidance/<int:exercise_id>")
@login_required
def exercise_guidance(exercise_id):
    user = get_current_user()
    exercise = Exercise.query.filter_by(id=exercise_id, user_id=user.id, is_archived=False).first_or_404()
    recipe = exercise_recipe(user.id, exercise_id)
    try:
        recipe = {key: int(request.args.get(key, value)) for key, value in recipe.items()}
        if not 1 <= recipe["sets"] <= 20 or not 1 <= recipe["rep_min"] <= recipe["rep_max"] <= 100:
            raise ValueError
    except (TypeError, ValueError):
        return jsonify({"error": "回数の下限・上限とセット数を確認してください。"}), 400
    previous = previous_exercise_logs(user.id, exercise.name)
    return jsonify({**recipe, "recording_mode": exercise.recording_mode, **progression_targets(previous, **recipe, mode=exercise.recording_mode),
        "previous": [{"set_number": log.set_number, "weight": log.weight, "reps": log.reps} for log in previous],
        "previous_date": previous[0].date.strftime("%Y-%m-%d") if previous else None})

# --- 5. データベースから削除 ---
@app.route("/delete/<int:log_id>", methods=["POST"])
@login_required
def delete_log(log_id):
    # 送られてきたIDと一致する記録をデータベースから探し出す
    log_to_delete = TrainingLog.query.filter_by(id=log_id, user_id=get_current_user().id).first()
    calendar_month = log_to_delete.date.strftime("%Y-%m") if log_to_delete else japan_today().strftime("%Y-%m")
    
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
        sync_growth_records(get_current_user().id)
        db.session.commit()              # 確定して保存
        
    # 削除が終わったら、home関数に自動で戻る
    if request.form.get("return_to") == "calendar":
        return redirect(url_for("calendar_page", month=calendar_month))
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
    if not any(exercise.name == log_to_edit.lift_type for exercise in exercises):
        recorded_exercise = Exercise.query.filter_by(user_id=user.id, name=log_to_edit.lift_type).first()
        if recorded_exercise:
            exercises.append(recorded_exercise)
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
        values, error = validate_training_form(edit_values, [exercise.name for exercise in exercises], request.form.get("recording_mode", log_to_edit.recording_mode))
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
        apply_record_values(log_to_edit, values)
        log_to_edit.target_reps = new_target
        log_to_edit.date = new_date
        log_to_edit.user_id = get_current_user().id
        if log_to_edit.batch_id:
            batch = db.session.get(TrainingBatch, log_to_edit.batch_id)
            batch.rep_max = new_target
            batch.rep_min = min(batch.rep_min, new_target)
        db.session.flush()
        refresh_log_targets(log_to_edit)
        sync_growth_records(user.id)

        # データベースの変更を確定して保存する
        db.session.commit()     # 確定して保存

        return redirect(url_for('home'))

    else:
        # === 編集ボタンが押された直後 ===
        # まだ作っていませんが、編集専用の画面に log_to_edit を渡して開く
        return render_template(
            "edit.html", log=log_to_edit, error=None, form_data={}, exercises=exercises, edit_upper=edit_upper
        )


def clear_completed_schedules(user_id):
    """Detach finished workouts and remove their booking, including same-day duplicates."""
    removed = 0
    for schedule in WorkoutSchedule.query.filter_by(user_id=user_id).all():
        workout = schedule.session
        if workout is not None:
            if workout.completed_at is None or TrainingLog.query.filter_by(
                    user_id=user_id, session_id=workout.id).first() is None:
                continue
        else:
            finished = WorkoutSession.query.join(TrainingLog, TrainingLog.session_id == WorkoutSession.id).filter(
                WorkoutSession.user_id == user_id, TrainingLog.user_id == user_id,
                WorkoutSession.workout_day_id == schedule.workout_day_id,
                WorkoutSession.completed_at.isnot(None),
                WorkoutSession.started_at >= datetime.combine(schedule.scheduled_date, time.min),
                WorkoutSession.started_at < datetime.combine(schedule.scheduled_date + timedelta(days=1), time.min),
            )
            if schedule.created_at is not None:
                finished = finished.filter(WorkoutSession.completed_at >= schedule.created_at)
            workout = finished.first()
            if workout is None:
                continue
        schedule.session = None
        db.session.delete(schedule)
        removed += 1
    db.session.flush()
    return removed


@app.route("/calendar")
@login_required
def calendar_page():
    user = get_current_user()
    ensure_user_setup(user)
    if clear_completed_schedules(user.id):
        db.session.commit()
    sync_recent_recovery_checks(user.id)
    today = japan_today()
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

    session_ids = {log.session_id for log in logs if log.session_id is not None}
    workouts = WorkoutSession.query.filter(WorkoutSession.user_id == user.id, or_(
        WorkoutSession.id.in_(session_ids),
        (WorkoutSession.started_at >= datetime.combine(month_start, time.min)) &
        (WorkoutSession.started_at < datetime.combine(next_month_start, time.min)),
    )).order_by(WorkoutSession.started_at).all()
    workout_by_id = {workout.id: workout for workout in workouts}
    sessions_by_day = {}
    compact = {}

    def calendar_entry(day_key, key, label, recorded):
        entries = compact.setdefault(day_key, {})
        if key not in entries or recorded:
            entries[key] = {"label": label, "recorded": recorded}

    def calendar_session(day_key, workout):
        entries = sessions_by_day.setdefault(day_key, {})
        entries[workout.id] = workout
        calendar_entry(day_key, f"menu:{workout.workout_day_id}:{workout.display_name}", workout.display_name, True)

    for schedule in schedules:
        if schedule.status != "cancelled":
            calendar_entry(schedule.scheduled_date.isoformat(), f"menu:{schedule.workout_day_id}:{schedule.workout_day.name}",
                schedule.workout_day.name, schedule.session is not None or schedule.status in ("in_progress", "completed"))
    for workout in workouts:
        if month_start <= workout.started_at.date() <= month_end:
            calendar_session(workout.started_at.date().isoformat(), workout)
    for log in logs:
        workout = workout_by_id.get(log.session_id)
        if workout:
            calendar_session(log.date.date().isoformat(), workout)
        else:
            calendar_entry(log.date.date().isoformat(), "quick", "記録", True)

    weeks = [
        [date(month_start.year, month_start.month, day) if day else None for day in week]
        for week in month_calendar.Calendar(firstweekday=6).monthdayscalendar(
            month_start.year, month_start.month
        )
    ]
    previous_month = (month_start - timedelta(days=1)).strftime("%Y-%m")
    following_month = next_month_start.strftime("%Y-%m")
    workout_days = WorkoutDay.query.filter_by(user_id=user.id, is_archived=False).order_by(WorkoutDay.id).all()
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
        groups = sorted({exercise.muscle_group for item in workout_day.items
                         for exercise in [item.exercise] + [alternative.exercise for alternative in item.alternatives]})
        candidate_dates = [today]
        for group in groups:
            last_date = muscle_last_dates.get(group)
            if last_date is not None:
                recovery_days = estimates.get(group, {}).get("days", 2.0)
                recovered = MuscleRecoveryLog.query.filter_by(user_id=user.id, muscle_group=group, trained_date=last_date).order_by(MuscleRecoveryLog.recovered_date.desc()).first()
                candidate_dates.append(recovered.recovered_date if recovered else last_date + timedelta(days=max(1, math.ceil(recovery_days))))
            still_sore = RecoveryCheck.query.filter_by(user_id=user.id, muscle_group=group, recovery_log_id=None, dismissed=False).filter(
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
        calendar_entries={key: list(entries.values()) for key, entries in compact.items()},
        sessions_by_day={key: list(entries.values()) for key, entries in sessions_by_day.items()},
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

    workout_day = WorkoutDay.query.filter_by(id=workout_day_id, user_id=user.id, is_archived=False).first()
    if workout_day is None:
        flash("登録済みのメニューを選択してください。", "error")
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

    workout_day = WorkoutDay.query.filter_by(id=workout_day_id, user_id=user.id, is_archived=False).first_or_404()
    schedule = None
    if schedule_id is not None:
        schedule = WorkoutSchedule.query.filter_by(id=schedule_id, user_id=user.id).first_or_404()
        if schedule.workout_day_id != workout_day.id:
            abort(400)
        if schedule.session is not None:
            schedule.session.paused_at = None
            db.session.commit()
            return redirect(url_for("workout_session_page", session_id=schedule.session.id))

    ongoing = WorkoutSession.query.filter_by(user_id=user.id, workout_day_id=workout_day.id, completed_at=None).first()
    if ongoing:
        ongoing.paused_at = None
        if schedule is not None and ongoing.schedule is None:
            ongoing.schedule = schedule
            schedule.status = "in_progress"
        db.session.commit()
        flash("実施中のトレーニングを再開します。", "success")
        return redirect(url_for("workout_session_page", session_id=ongoing.id))
    if not workout_day.items:
        flash("このメニューには種目がありません。先に種目を登録してください。", "error")
        return redirect(url_for("menu_page"))

    workout_session = WorkoutSession(
        user_id=user.id, workout_day_id=workout_day.id, schedule_id=schedule_id, menu_name=workout_day.name
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
        previous_sets = previous_exercise_logs(user.id, item.exercise.name, before=workout_session.started_at, exclude_session=session_id, mode="all")
        guidance = progression_targets([log for log in previous_sets if log.recording_mode == item.exercise.recording_mode], item.sets, item.rep_min, item.rep_max, mode=item.exercise.recording_mode)
        targets = []
        for number in range(1, item.sets + 1):
            mode = item.exercise.recording_mode
            same_mode = [log for log in previous_sets if log.recording_mode == mode]
            target = progression_targets(same_mode, item.sets, item.rep_min, item.rep_max, mode=mode)["targets"][number - 1]
            recent_body = next((log for log in reversed(session_logs)
                                if log.lift_type == item.exercise.name and log.recording_mode == "bodyweight"), None)
            if mode == "bodyweight" and recent_body:
                target["body_weight"] = recent_body.body_weight
            target["recording_mode"] = mode
            targets.append(target)
        completed_sets = {
            log.set_number: log for log in session_logs if log.lift_type == item.exercise.name
        }
        workout_items.append({
            "item": item,
            "previous_log": previous_sets[0] if previous_sets else None,
            "previous_sets": previous_sets,
            "targets": targets,
            "next_reps": guidance["targets"][0]["reps"],
            "target_weight": guidance["targets"][0]["weight"],
            "target_reason": guidance["reason"],
            "completed_sets": completed_sets,
            "options": item.options,
            "minimum_sets": max(completed_sets, default=1),
        })

    db.session.commit()
    return render_template(
        "workout.html", workout_session=workout_session, workout_items=workout_items,
        exercises=exercises, session_logs=session_logs, muscle_groups=MUSCLE_GROUPS,
        available_exercises=[exercise for exercise in exercises
                             if exercise.id not in {entry["item"].exercise_id for entry in workout_items}],
    )


@app.route("/workouts/<int:session_id>/sets", methods=["POST"])
@login_required
def add_workout_set(session_id):
    return save_workout_sets(session_id, [request.form.to_dict()])


@app.route("/workouts/<int:session_id>/sets/bulk", methods=["POST"])
@login_required
def add_workout_sets_bulk(session_id):
    try:
        rows = json.loads(request.form.get("rows", ""))
    except (TypeError, ValueError):
        return workout_save_error(session_id, "保存するセットを選択してください。")
    if not isinstance(rows, list) or not 1 <= len(rows) <= 100:
        return workout_save_error(session_id, "一度に保存するセットは1〜100セットにしてください。")
    return save_workout_sets(session_id, rows)


def workout_save_error(session_id, message, item_id=None, set_number=None):
    if request.headers.get("X-Requested-With") == "fetch":
        return jsonify({"error": message}), 400
    flash(message, "error")
    anchor = f"set-{item_id}-{set_number}" if item_id and set_number else None
    return redirect(url_for("workout_session_page", session_id=session_id, _anchor=anchor))


def workout_save_response(workout, saved_items, message):
    if request.headers.get("X-Requested-With") == "fetch":
        return jsonify({"message": message, "saved": [
            {"row_id": f"set-{item.id}-{log.set_number}",
             "html": render_template("workout_saved_set.html", saved=log, workout_session=workout)}
            for item, log in saved_items]})
    flash(message, "success")
    item, log = saved_items[-1]
    return redirect(url_for("workout_session_page", session_id=workout.id,
                            _anchor=f"set-{item.id}-{log.set_number}"))


def save_workout_sets(session_id, rows):
    # Acquire SQLite's write lock before checking existing sets, including requests from another tab.
    db.session.execute(db.update(WorkoutSession).where(
        WorkoutSession.id == session_id, WorkoutSession.user_id == get_current_user().id
    ).values(started_at=WorkoutSession.started_at))
    workout = active_workout(session_id)
    items = {item.exercise_id: item for item in ensure_session_items(workout)}
    existing = {(log.lift_type, log.set_number) for log in TrainingLog.query.filter_by(
        user_id=workout.user_id, session_id=session_id).all()}
    validated = []
    selected = set()
    for row in rows:
        if not isinstance(row, dict):
            return workout_save_error(session_id, "セットの入力内容を確認してください。")
        try:
            item = items[int(row.get("exercise_id", ""))]
            number = int(row.get("set_number", ""))
        except (KeyError, TypeError, ValueError):
            return workout_save_error(session_id, "種目とセット番号を確認してください。")
        if item.is_skipped or not 1 <= number <= item.sets:
            return workout_save_error(session_id, "セット数やスキップ状態が変更されています。画面を再読み込みしてください。", item.id, number)
        identity = (item.exercise.name, number)
        if identity in existing or identity in selected:
            return workout_save_error(session_id, f"{item.exercise.name}の{number}セット目は記録済み、または重複しています。再読み込みして確認してください。", item.id, number)
        mode = row.get("recording_mode", item.exercise.recording_mode)
        values, error = validate_training_form({**row, "lift_type": item.exercise.name,
                                               "target_reps": str(item.rep_max)}, [item.exercise.name], mode)
        if error:
            return workout_save_error(session_id, f"{item.exercise.name}の{number}セット目：{error}", item.id, number)
        selected.add(identity)
        validated.append((item, number, values))
    # Validate the entire selection before writing any of its sets.
    saved_items = []
    for item, number, values in validated:
        log = TrainingLog(user_id=workout.user_id, session_id=session_id, set_number=number,
                          lift_type=item.exercise.name, target_reps=item.rep_max, target_weight=values["weight"])
        apply_record_values(log, values)
        db.session.add(log)
        saved_items.append((item, log))
    db.session.flush()
    for _, log in saved_items:
        refresh_log_targets(log)
    db.session.commit()
    return workout_save_response(workout, saved_items, f"{len(saved_items)}セットを記録しました。")


def active_workout(session_id):
    workout = WorkoutSession.query.filter_by(id=session_id, user_id=get_current_user().id).first_or_404()
    if workout.completed_at is not None:
        abort(400, description="完了したトレーニングの構成は変更できません。")
    if workout.paused_at is not None:
        abort(400, description="トレーニングを再開してから変更してください。")
    return workout


def current_workout_item(workout, item_id):
    item = next((saved for saved in ensure_session_items(workout) if saved.id == item_id), None)
    if item is None:
        abort(404)
    return item


@app.route("/workouts/<int:session_id>/items/<int:item_id>/move", methods=["POST"])
@login_required
def move_workout_item(session_id, item_id):
    workout = active_workout(session_id)
    item = current_workout_item(workout, item_id)
    direction = request.form.get("direction", "")
    if direction not in ("up", "down"):
        abort(400, description="並べ替えの方向を選択してください。")
    ordered = sorted(workout.items, key=lambda saved: (saved.position, saved.id))
    index = next(number for number, saved in enumerate(ordered) if saved.id == item.id)
    target = index + (-1 if direction == "up" else 1)
    if 0 <= target < len(ordered):
        ordered[index], ordered[target] = ordered[target], ordered[index]
        for position, saved in enumerate(ordered):
            saved.position = position
        db.session.commit()
        flash("今回のトレーニングの種目の順番を保存しました。", "success")
    return redirect(url_for("workout_session_page", session_id=session_id, _anchor=f"exercise-{item.id}"))


@app.route("/workouts/<int:session_id>/sets/<int:log_id>/edit", methods=["POST"])
@login_required
def edit_workout_set(session_id, log_id):
    workout = active_workout(session_id)
    log = TrainingLog.query.filter_by(id=log_id, user_id=workout.user_id, session_id=session_id).first_or_404()
    item = next((saved for saved in ensure_session_items(workout) if saved.exercise.name == log.lift_type), None)
    if item is None:
        abort(404)
    mode = request.form.get("recording_mode", log.recording_mode)
    values, error = validate_training_form({**request.form.to_dict(), "lift_type": log.lift_type,
        "target_reps": str(item.rep_max)}, [log.lift_type], mode)
    if error:
        return workout_save_error(session_id, error, item.id, log.set_number)
    else:
        apply_record_values(log, values)
        db.session.flush()
        refresh_log_targets(log)
        sync_growth_records(workout.user_id)
        db.session.commit()
    return workout_save_response(workout, [(item, log)], f"{log.lift_type}の{log.set_number}セット目を修正しました。")


@app.route("/workouts/<int:session_id>/sets/<int:log_id>/delete", methods=["POST"])
@login_required
def delete_workout_set(session_id, log_id):
    workout = active_workout(session_id)
    log = TrainingLog.query.filter_by(id=log_id, user_id=workout.user_id, session_id=session_id).first_or_404()
    item = next((saved for saved in ensure_session_items(workout) if saved.exercise.name == log.lift_type), None)
    if item is None:
        abort(404)
    lift = log.lift_type
    db.session.delete(log)
    db.session.flush()
    remaining = TrainingLog.query.filter_by(user_id=workout.user_id, session_id=session_id, lift_type=lift).first()
    if remaining is not None:
        refresh_log_targets(remaining)
    sync_growth_records(workout.user_id)
    db.session.commit()
    flash("このセットの記録を削除しました。もう一度入力できます。", "success")
    return redirect(url_for("workout_session_page", session_id=session_id, _anchor=f"exercise-{item.id}"))


@app.route("/workouts/<int:session_id>/items/<int:item_id>/sets", methods=["POST"])
@login_required
def change_workout_sets(session_id, item_id):
    workout = active_workout(session_id)
    item = current_workout_item(workout, item_id)
    count = request.form.get("sets", type=int)
    logs = TrainingLog.query.filter_by(user_id=workout.user_id, session_id=session_id, lift_type=item.exercise.name).all()
    minimum = max((log.set_number for log in logs), default=1)
    if count is None or not 1 <= count <= 20:
        flash("セット数は1〜20で入力してください。", "error")
    elif count < minimum:
        flash(f"{minimum}セット目まで記録があります。先に不要なセット記録を削除してください。", "error")
    else:
        item.sets = count
        db.session.flush()
        if logs:
            refresh_log_targets(logs[0])
        db.session.commit()
        flash(f"今回のセット数を{count}に変更しました。", "success")
    return redirect(url_for("workout_session_page", session_id=session_id, _anchor=f"exercise-{item.id}"))


@app.route("/workouts/<int:session_id>/items/<int:item_id>/skip", methods=["POST"])
@login_required
def skip_workout_item(session_id, item_id):
    workout = active_workout(session_id)
    item = current_workout_item(workout, item_id)
    action = request.form.get("action", "")
    if action not in ("skip", "restore"):
        abort(400)
    item.is_skipped = action == "skip"
    db.session.commit()
    flash("残りのセットをスキップしました。保存済みの記録は残ります。" if item.is_skipped else
          "この種目を再開できます。", "success")
    return redirect(url_for("workout_session_page", session_id=session_id, _anchor=f"exercise-{item.id}"))


@app.route("/workouts/<int:session_id>/items", methods=["POST"])
@login_required
def add_workout_item(session_id):
    workout = active_workout(session_id)
    items = ensure_session_items(workout)
    if len(items) >= 100:
        flash("1回のトレーニングに追加できる種目は100種目までです。", "error")
        return redirect(url_for("workout_session_page", session_id=session_id))
    try:
        sets = int(request.form.get("sets", "3"))
        rep_min = int(request.form.get("rep_min", "8"))
        rep_max = int(request.form.get("rep_max", "12"))
    except (TypeError, ValueError):
        flash("セット数・回数を正しく入力してください。", "error")
        return redirect(url_for("workout_session_page", session_id=session_id, _anchor="add-exercise"))
    if not 1 <= sets <= 20 or not 1 <= rep_min <= rep_max <= 100:
        flash("セット数は1〜20、回数は1〜100で下限が上限以下になるよう入力してください。", "error")
        return redirect(url_for("workout_session_page", session_id=session_id, _anchor="add-exercise"))
    source = request.form.get("source", "existing")
    if source == "new":
        name = request.form.get("name", "").strip()
        muscle_group = request.form.get("muscle_group", "")
        mode = request.form.get("recording_mode", "weight")
        if not 1 <= len(name) <= 80 or muscle_group not in MUSCLE_GROUPS or mode not in RECORDING_MODES:
            flash("種目名（1〜80文字）と部位を確認してください。", "error")
            return redirect(url_for("workout_session_page", session_id=session_id, _anchor="add-exercise"))
        exercise = Exercise.query.filter_by(user_id=workout.user_id, name=name).first()
        if exercise is not None and not exercise.is_archived:
            flash("この種目は登録済みです。登録済み種目から選んでください。", "error")
            return redirect(url_for("workout_session_page", session_id=session_id, _anchor="add-exercise"))
        if exercise is None:
            exercise = Exercise(user_id=workout.user_id, name=name, muscle_group=muscle_group)
            db.session.add(exercise)
        else:
            exercise.is_archived = False
            exercise.muscle_group = muscle_group
        exercise.recording_mode = mode
        try:
            db.session.flush()
        except IntegrityError:
            db.session.rollback()
            flash("同じ名前の種目が登録されました。登録済み種目から選んでください。", "error")
            return redirect(url_for("workout_session_page", session_id=session_id, _anchor="add-exercise"))
    elif source == "existing":
        exercise = Exercise.query.filter_by(id=request.form.get("exercise_id", type=int),
            user_id=workout.user_id, is_archived=False).first()
        if exercise is None:
            flash("追加する種目を選択してください。", "error")
            return redirect(url_for("workout_session_page", session_id=session_id, _anchor="add-exercise"))
    else:
        abort(400)
    if any(item.exercise_id == exercise.id for item in items):
        db.session.rollback()
        flash("この種目は今回のトレーニングにあります。スキップ中なら再開してください。", "error")
        return redirect(url_for("workout_session_page", session_id=session_id, _anchor="add-exercise"))
    item = WorkoutSessionItem(session_id=workout.id, exercise=exercise, muscle_group=exercise.muscle_group,
        position=max((saved.position for saved in items), default=-1) + 1,
        sets=sets, rep_min=rep_min, rep_max=rep_max)
    item.options = [WorkoutSessionOption(exercise=exercise, muscle_group=exercise.muscle_group)]
    db.session.add(item)
    db.session.commit()
    flash(f"今回のトレーニングに「{exercise.name}」を追加しました。", "success")
    return redirect(url_for("workout_session_page", session_id=session_id, _anchor=f"exercise-{item.id}"))


@app.route("/workouts/<int:session_id>/finish", methods=["POST"])
@login_required
def finish_workout(session_id):
    user = get_current_user()
    workout_session = WorkoutSession.query.filter_by(id=session_id, user_id=user.id).first_or_404()
    if workout_session.paused_at is not None:
        abort(400, description="中断中です。再開してから完了してください。")
    if workout_session.completed_at is None:
        saved_logs = TrainingLog.query.filter_by(user_id=user.id, session_id=session_id).all()
        if not saved_logs:
            flash("セットを1つ以上記録してからトレーニングを完了してください。", "error")
            return redirect(url_for("workout_session_page", session_id=session_id))
        groups = {item.exercise.name: item.muscle_group for item in ensure_session_items(workout_session)}
        for log in saved_logs:
            if log.lift_type in groups:
                add_recovery_check(user.id, groups[log.lift_type], log.date.date())
        workout_session.completed_at = japan_now()
        clear_completed_schedules(user.id)
        sync_growth_records(user.id)
        db.session.commit()
        flash("トレーニングを完了しました。対応する予定を削除し、実施記録を残しました。", "success")
    return redirect(url_for("workout_session_page", session_id=session_id))


@app.route("/workouts/<int:session_id>/pause", methods=["POST"])
@login_required
def pause_workout(session_id):
    workout = WorkoutSession.query.filter_by(id=session_id, user_id=get_current_user().id).first_or_404()
    if workout.completed_at is not None:
        abort(400)
    workout.paused_at = japan_now()
    db.session.commit()
    flash("トレーニングを中断しました。記録を残して、後から再開できます。", "success")
    return redirect(url_for("home"))


@app.route("/workouts/<int:session_id>/resume", methods=["POST"])
@login_required
def resume_workout(session_id):
    workout = WorkoutSession.query.filter_by(id=session_id, user_id=get_current_user().id).first_or_404()
    if workout.completed_at is not None:
        abort(400)
    workout.paused_at = None
    db.session.commit()
    return redirect(url_for("workout_session_page", session_id=session_id))


@app.route("/workouts/<int:session_id>/delete", methods=["POST"])
@login_required
def delete_workout(session_id):
    user = get_current_user()
    workout = WorkoutSession.query.filter_by(id=session_id, user_id=user.id).first_or_404()
    completed = workout.completed_at is not None
    calendar_month = workout.started_at.strftime("%Y-%m")
    removed = TrainingLog.query.filter_by(user_id=user.id, session_id=workout.id).all()
    muscle_groups = {item.exercise.name: item.muscle_group for item in ensure_session_items(workout)}
    affected = {(muscle_groups[log.lift_type], log.date.date()) for log in removed
                if log.lift_type in muscle_groups}
    TrainingLog.query.filter_by(user_id=user.id, session_id=workout.id).delete(synchronize_session=False)
    if workout.schedule is not None:
        schedule = workout.schedule
        schedule.session = None
        if completed:
            db.session.delete(schedule)
        else:
            schedule.status = "planned"
    db.session.delete(workout)
    db.session.flush()
    for muscle_group, trained_date in affected:
        remaining = TrainingLog.query.join(Exercise, TrainingLog.lift_type == Exercise.name).outerjoin(
            WorkoutSession, TrainingLog.session_id == WorkoutSession.id).filter(
            TrainingLog.user_id == user.id, Exercise.user_id == user.id,
            Exercise.muscle_group == muscle_group,
            TrainingLog.date >= datetime.combine(trained_date, time.min),
            TrainingLog.date < datetime.combine(trained_date + timedelta(days=1), time.min),
            or_(TrainingLog.session_id.is_(None), WorkoutSession.completed_at.isnot(None)),
        ).first()
        if remaining is None:
            RecoveryCheck.query.filter_by(user_id=user.id, muscle_group=muscle_group,
                trained_date=trained_date, recovery_log_id=None).update(
                {RecoveryCheck.dismissed: True, RecoveryCheck.last_sore_date: None}, synchronize_session=False)
    sync_growth_records(user.id)
    db.session.commit()
    flash("トレーニングと、そのセット記録を削除しました。", "success")
    if completed or request.form.get("return_to") == "calendar":
        return redirect(url_for("calendar_page", month=calendar_month))
    return redirect(url_for("home"))


@app.route("/workouts/<int:session_id>/items/<int:item_id>/exercise", methods=["POST"])
@login_required
def choose_workout_exercise(session_id, item_id):
    user = get_current_user()
    workout = WorkoutSession.query.filter_by(id=session_id, user_id=user.id).first_or_404()
    if workout.completed_at is not None or workout.paused_at is not None:
        abort(400)
    item = next((item for item in ensure_session_items(workout) if item.id == item_id), None)
    if item is None:
        abort(404)
    if item.is_skipped:
        abort(400, description="スキップを解除してから種目を選択してください。")
    exercise_id = request.form.get("exercise_id", type=int)
    option = next((choice for choice in item.options if choice.exercise_id == exercise_id), None)
    if option is None or option.exercise.user_id != user.id:
        abort(400)
    if TrainingLog.query.filter_by(user_id=user.id, session_id=session_id, lift_type=item.exercise.name).first():
        flash("セット記録後は種目を切り替えられません。最初のセット前に選んでください。", "error")
    elif any(other.id != item.id and other.exercise_id == exercise_id for other in workout.items):
        flash("このトレーニングでは、同じ種目を別の枠にも選ぶことはできません。", "error")
    else:
        item.exercise = option.exercise
        item.muscle_group = option.muscle_group
        db.session.commit()
        flash(f"「{option.exercise.name}」で記録します。前回記録と目標もこの種目に切り替えました。", "success")
    return redirect(url_for("workout_session_page", session_id=session_id))


@app.route("/menu")
@login_required
def menu_page():
    user = get_current_user()
    exercises = ensure_user_setup(user)
    plans = WorkoutDay.query.filter_by(user_id=user.id, is_archived=False).order_by(WorkoutDay.id).all()
    selected_menu = request.args.get("menu", type=int)
    if not any(day.id == selected_menu for day in plans):
        selected_menu = plans[0].id if plans else None
    return render_template(
        "menu.html", plans=plans, exercises=exercises, muscle_groups=MUSCLE_GROUPS,
        selected_menu=selected_menu, show_exercises=request.args.get("manage") == "1"
    )


@app.route("/menus", methods=["POST"])
@login_required
def create_menu():
    user = get_current_user()
    ensure_user_setup(user)
    name = request.form.get("name", "").strip()
    if not 1 <= len(name) <= 30:
        flash("メニュー名は1〜30文字で入力してください。", "error")
        return redirect(url_for("menu_page"))
    if WorkoutDay.query.filter_by(user_id=user.id, name=name, is_archived=False).first():
        flash("同じ名前のメニューが登録されています。別の名前を付けてください。", "error")
        return redirect(url_for("menu_page"))
    menu = WorkoutDay(user_id=user.id, split_code="m" + token_hex(4), name=name)
    db.session.add(menu)
    db.session.commit()
    flash(f"「{name}」を保存しました。種目とorの候補を追加してください。", "success")
    return redirect(url_for("menu_page", menu=menu.id, _anchor=f"menu-{menu.id}"))


@app.route("/menus/<int:day_id>/name", methods=["POST"])
@login_required
def rename_menu(day_id):
    user = get_current_user()
    menu = WorkoutDay.query.filter_by(id=day_id, user_id=user.id, is_archived=False).first_or_404()
    name = request.form.get("name", "").strip()
    if not 1 <= len(name) <= 30:
        flash("メニュー名は1〜30文字で入力してください。", "error")
    elif WorkoutDay.query.filter(WorkoutDay.user_id == user.id, WorkoutDay.id != day_id,
                                 WorkoutDay.name == name, WorkoutDay.is_archived.is_(False)).first():
        flash("同じ名前のメニューが登録されています。", "error")
    else:
        WorkoutSession.query.filter_by(user_id=user.id, workout_day_id=day_id,
            menu_name=None).update({WorkoutSession.menu_name: menu.name}, synchronize_session=False)
        menu.name = name
        db.session.commit()
        flash("メニュー名を保存しました。開始済みのトレーニングは以前の名前で残ります。", "success")
    return redirect(url_for("menu_page", menu=day_id, _anchor=f"menu-{day_id}"))


@app.route("/menus/<int:day_id>/delete", methods=["POST"])
@login_required
def delete_menu(day_id):
    user = get_current_user()
    menu = WorkoutDay.query.filter_by(id=day_id, user_id=user.id, is_archived=False).first_or_404()
    # Keep snapshots and identities needed by past or ongoing workouts.
    for workout in WorkoutSession.query.filter_by(user_id=user.id, workout_day_id=day_id).all():
        if workout.menu_name is None:
            workout.menu_name = menu.name
        ensure_session_items(workout)
    for schedule in WorkoutSchedule.query.filter_by(user_id=user.id, workout_day_id=day_id).all():
        schedule.session = None
        db.session.delete(schedule)
    menu.is_archived = True
    db.session.commit()
    flash(f"「{menu.name}」を削除しました。関連する予定を削除し、過去・実施中のトレーニング記録は残しました。", "success")
    return redirect(url_for("menu_page"))


@app.route("/exercises", methods=["POST"])
@login_required
def add_exercise():
    name = request.form.get("name", "").strip()
    muscle_group = request.form.get("muscle_group", "")
    mode = request.form.get("recording_mode", "weight")
    user = get_current_user()
    existing = Exercise.query.filter_by(user_id=user.id, name=name).first()

    if not 1 <= len(name) <= 80:
        flash("種目名は1〜80文字で入力してください。", "error")
    elif muscle_group not in MUSCLE_GROUPS:
        flash("部位を選択してください。", "error")
    elif mode not in RECORDING_MODES:
        flash("記録方式を選択してください。", "error")
    elif existing is not None and not existing.is_archived:
        flash("この種目はすでに登録されています。", "error")
    else:
        if existing is not None:
            existing.is_archived = False
            existing.muscle_group = muscle_group
            existing.recording_mode = mode
        else:
            db.session.add(Exercise(user_id=user.id, name=name, muscle_group=muscle_group, recording_mode=mode))
        db.session.commit()
        flash(f"「{name}」を種目に追加しました。", "success")
    return redirect(url_for("menu_page", manage=1, menu=request.form.get("menu_id", type=int)))


@app.route("/exercises/<int:exercise_id>/recording-mode", methods=["POST"])
@login_required
def change_recording_mode(exercise_id):
    user = get_current_user()
    exercise = Exercise.query.filter_by(id=exercise_id, user_id=user.id, is_archived=False).first_or_404()
    workout_id = request.form.get("session_id", type=int)
    if workout_id is not None:
        workout = active_workout(workout_id)
        if not any(item.exercise_id == exercise.id for item in ensure_session_items(workout)):
            abort(400)
    mode = request.form.get("recording_mode", "")
    if mode not in RECORDING_MODES:
        abort(400, description="記録方式を選択してください。")
    exercise.recording_mode = mode
    db.session.commit()
    flash("記録方式を保存しました。今後の入力に反映します。過去の記録は当時の方式で残ります。", "success")
    if workout_id is not None:
        return redirect(url_for("workout_session_page", session_id=workout_id))
    return redirect(url_for("menu_page", manage=1, menu=request.form.get("menu_id", type=int)))


@app.route("/exercises/<int:exercise_id>/delete", methods=["POST"])
@login_required
def delete_exercise(exercise_id):
    user = get_current_user()
    exercise = Exercise.query.filter_by(id=exercise_id, user_id=user.id, is_archived=False).first_or_404()
    for item in MenuItem.query.join(WorkoutDay).filter(WorkoutDay.user_id == user.id).all():
        item.alternatives[:] = [alternative for alternative in item.alternatives
                                if alternative.exercise_id != exercise.id]
        if item.exercise_id == exercise.id:
            if item.alternatives:
                replacement = item.alternatives.pop(0)
                item.exercise = replacement.exercise
            else:
                db.session.delete(item)
    # Keep the identity for recorded workouts and competition snapshots.
    exercise.is_archived = True
    db.session.commit()
    flash(f"「{exercise.name}」を登録済み種目とメニューから削除しました。過去の記録は残ります。", "success")
    return redirect(url_for("menu_page", manage=1, menu=request.form.get("menu_id", type=int)))


def menu_alternatives(workout_day, primary, values, exclude_item_id=None):
    try:
        ids = {int(value) for value in values} - {primary.id}
    except (TypeError, ValueError):
        raise ValueError("orの候補を正しく選択してください。")
    if len(ids) > 10:
        raise ValueError("orの候補は10種目まで選択できます。")
    choices = Exercise.query.filter(Exercise.user_id == workout_day.user_id,
        Exercise.is_archived.is_(False), Exercise.id.in_(ids)).order_by(Exercise.name).all()
    if len(choices) != len(ids):
        raise ValueError("登録済みの自分の種目をorの候補に選んでください。")
    used = set()
    for item in workout_day.items:
        if item.id != exclude_item_id:
            used.add(item.exercise_id)
            used.update(alternative.exercise_id for alternative in item.alternatives)
    if used & (ids | {primary.id}):
        raise ValueError("同じ種目は、このメニュー内の別の枠やorの候補に重複して登録できません。")
    return choices


@app.route("/menu/<int:day_id>/items", methods=["POST"])
@login_required
def add_menu_item(day_id):
    user = get_current_user()
    workout_day = WorkoutDay.query.filter_by(id=day_id, user_id=user.id, is_archived=False).first_or_404()
    try:
        exercise_id = int(request.form.get("exercise_id", ""))
        sets = int(request.form.get("sets", "3"))
        rep_min = int(request.form.get("rep_min", "8"))
        rep_max = int(request.form.get("rep_max", "12"))
    except (TypeError, ValueError):
        flash("種目、セット数、回数を正しく入力してください。", "error")
        return redirect(url_for("menu_page", menu=day_id, _anchor=f"menu-{day_id}"))

    exercise = Exercise.query.filter_by(id=exercise_id, user_id=user.id, is_archived=False).first()
    if exercise is None:
        flash("種目を選択してください。", "error")
    elif not 1 <= sets <= 20:
        flash("セット数は1〜20で入力してください。", "error")
    elif not (1 <= rep_min <= rep_max <= 100):
        flash("回数範囲は1〜100で、下限が上限以下になるよう入力してください。", "error")
    elif MenuItem.query.filter_by(workout_day_id=workout_day.id, exercise_id=exercise.id).first():
        flash("この種目はすでにメニューにあります。", "error")
    else:
        try:
            alternatives = menu_alternatives(workout_day, exercise, request.form.getlist("alternative_ids"))
        except ValueError as error:
            flash(str(error), "error")
            return redirect(url_for("menu_page", menu=day_id, _anchor=f"menu-{day_id}"))
        position = max((item.position for item in workout_day.items), default=-1) + 1
        item = MenuItem(
            workout_day_id=workout_day.id, exercise_id=exercise.id,
            position=position, sets=sets, rep_min=rep_min, rep_max=rep_max,
        )
        item.alternatives = [MenuItemAlternative(exercise_id=choice.id) for choice in alternatives]
        db.session.add(item)
        db.session.commit()
        flash(f"「{exercise.name}」を{workout_day.name}メニューに追加しました。", "success")
    return redirect(url_for("menu_page", menu=day_id, _anchor=f"menu-{day_id}"))


@app.route("/menu/items/<int:item_id>/alternatives", methods=["POST"])
@login_required
def update_menu_alternatives(item_id):
    item = MenuItem.query.join(WorkoutDay).filter(MenuItem.id == item_id,
        WorkoutDay.user_id == get_current_user().id, WorkoutDay.is_archived.is_(False)).first_or_404()
    try:
        choices = menu_alternatives(item.workout_day, item.exercise,
                                   request.form.getlist("alternative_ids"), exclude_item_id=item.id)
    except ValueError as error:
        flash(str(error), "error")
    else:
        retained = {alternative.exercise_id: alternative for alternative in item.alternatives}
        item.alternatives[:] = [retained[choice.id] if choice.id in retained else
                               MenuItemAlternative(exercise_id=choice.id) for choice in choices]
        db.session.commit()
        flash("orの候補を保存しました。次に開始するトレーニングから選べます。", "success")
    return redirect(url_for("menu_page", menu=item.workout_day_id, _anchor=f"menu-{item.workout_day_id}"))


def preserve_menu_sessions(workout_day):
    for workout in WorkoutSession.query.filter_by(user_id=workout_day.user_id,
                                                  workout_day_id=workout_day.id).all():
        if not workout.items:
            ensure_session_items(workout)


@app.route("/menu/items/<int:item_id>/edit", methods=["POST"])
@login_required
def edit_menu_item(item_id):
    item = MenuItem.query.join(WorkoutDay).filter(MenuItem.id == item_id,
        WorkoutDay.user_id == get_current_user().id, WorkoutDay.is_archived.is_(False)).first_or_404()
    day_id = item.workout_day_id
    try:
        exercise_id = int(request.form.get("exercise_id", ""))
        sets = int(request.form.get("sets", ""))
        rep_min = int(request.form.get("rep_min", ""))
        rep_max = int(request.form.get("rep_max", ""))
    except (TypeError, ValueError):
        flash("種目・セット数・回数を正しく入力してください。", "error")
        return redirect(url_for("menu_page", menu=day_id, _anchor=f"menu-{day_id}"))
    try:
        if not 1 <= sets <= 20 or not 1 <= rep_min <= rep_max <= 100:
            raise ValueError("セット数は1〜20、回数は1〜100で下限が上限以下になるよう入力してください。")
        exercise = Exercise.query.filter_by(id=exercise_id, user_id=item.workout_day.user_id,
                                            is_archived=False).first()
        if exercise is None:
            raise ValueError("登録済みの自分の種目を選択してください。")
        choices = menu_alternatives(item.workout_day, exercise,
                                    request.form.getlist("alternative_ids"), exclude_item_id=item.id)
    except ValueError as error:
        flash(str(error), "error")
        return redirect(url_for("menu_page", menu=day_id, _anchor=f"menu-{day_id}"))
    preserve_menu_sessions(item.workout_day)
    item.exercise = exercise
    item.sets, item.rep_min, item.rep_max = sets, rep_min, rep_max
    retained = {alternative.exercise_id: alternative for alternative in item.alternatives}
    item.alternatives[:] = [retained[choice.id] if choice.id in retained else
                           MenuItemAlternative(exercise_id=choice.id) for choice in choices]
    db.session.commit()
    flash("種目・セット数・回数範囲・orの候補を保存しました。", "success")
    return redirect(url_for("menu_page", menu=day_id, _anchor=f"menu-{day_id}"))


@app.route("/menu/items/<int:item_id>/move", methods=["POST"])
@login_required
def move_menu_item(item_id):
    item = MenuItem.query.join(WorkoutDay).filter(MenuItem.id == item_id,
        WorkoutDay.user_id == get_current_user().id, WorkoutDay.is_archived.is_(False)).first_or_404()
    day_id = item.workout_day_id
    direction = request.form.get("direction", "")
    if direction not in ("up", "down"):
        abort(400, description="並べ替えの方向を選択してください。")
    ordered = sorted(item.workout_day.items, key=lambda saved: (saved.position, saved.id))
    index = next(number for number, saved in enumerate(ordered) if saved.id == item.id)
    target = index + (-1 if direction == "up" else 1)
    if 0 <= target < len(ordered):
        preserve_menu_sessions(item.workout_day)
        ordered[index], ordered[target] = ordered[target], ordered[index]
        for position, saved in enumerate(ordered):
            saved.position = position
        db.session.commit()
        flash("このメニューの種目の順番を保存しました。", "success")
    return redirect(url_for("menu_page", menu=day_id, _anchor=f"menu-{day_id}"))


@app.route("/menu/items/<int:item_id>/delete", methods=["POST"])
@login_required
def delete_menu_item(item_id):
    user = get_current_user()
    item = MenuItem.query.join(WorkoutDay).filter(
        MenuItem.id == item_id, WorkoutDay.user_id == user.id, WorkoutDay.is_archived.is_(False)
    ).first_or_404()
    day_id = item.workout_day_id
    db.session.delete(item)
    db.session.commit()
    flash("メニューから種目を削除しました。", "success")
    return redirect(url_for("menu_page", menu=day_id, _anchor=f"menu-{day_id}"))


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
        elif trained_date > japan_today() or recovered_date > japan_today():
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
        pending_checks=RecoveryCheck.query.filter_by(user_id=user.id, recovery_log_id=None, dismissed=False).order_by(RecoveryCheck.trained_date.desc(), RecoveryCheck.id).all(),
        recovery_estimates=recovery_estimates(user.id),
        muscle_groups=MUSCLE_GROUPS, today=japan_today(),
    )


@app.route("/recovery/checks/<int:check_id>", methods=["POST"])
@login_required
def update_recovery_check(check_id):
    check = RecoveryCheck.query.filter_by(id=check_id, user_id=get_current_user().id, dismissed=False).first_or_404()
    status = request.form.get("status")
    if status not in ("sore", "recovered") or check.trained_date > japan_today():
        abort(400)
    if check.recovery_log_id is not None:
        flash("この部位の回復日はすでに記録済みです。", "success")
        return redirect(url_for("recovery_page"))
    if status == "sore":
        check.last_sore_date = japan_today()
        flash(f"{check.muscle_group}はまだ筋肉痛があると記録しました。", "success")
    else:
        recovery_log = MuscleRecoveryLog.query.filter_by(user_id=check.user_id,
            muscle_group=check.muscle_group, trained_date=check.trained_date).first()
        if recovery_log is None:
            recovery_log = MuscleRecoveryLog(user_id=check.user_id, muscle_group=check.muscle_group,
                trained_date=check.trained_date, recovered_date=japan_today())
            db.session.add(recovery_log)
            db.session.flush()
        check.recovery_log_id = recovery_log.id
        flash(f"{check.muscle_group}の回復日を今日で記録しました。", "success")
    db.session.commit()
    return redirect(url_for("recovery_page"))


@app.route("/recovery/checks/<int:check_id>/delete", methods=["POST"])
@login_required
def delete_recovery_check(check_id):
    check = RecoveryCheck.query.filter_by(id=check_id, user_id=get_current_user().id,
        recovery_log_id=None, dismissed=False).first_or_404()
    # A tombstone prevents the recent-record backfill from recreating the same check.
    check.dismissed = True
    check.last_sore_date = None
    db.session.commit()
    flash("この部位・トレーニング日の回復チェックを削除しました。", "success")
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

register_growth(app, db, User, Exercise, TrainingLog, TrainingBatch, WorkoutSession,
                login_required, get_current_user, ensure_user_setup)

if __name__ == "__main__":
    # ローカル開発で必要なときだけ FLASK_DEBUG=1 を設定する。
    app.run(debug=not IS_PRODUCTION and os.environ.get("FLASK_DEBUG") == "1")
