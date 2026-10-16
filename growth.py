"""Group growth competitions with versioned scoring and frozen results."""
import calendar
import math
from secrets import token_hex

from flask import Blueprint, abort, flash, redirect, render_template, request, url_for
from sqlalchemy import UniqueConstraint, func
from sqlalchemy.exc import IntegrityError
from time_utils import japan_now


def growth_now():
    return japan_now()


def add_calendar_months(value, months):
    year, month_zero = divmod(value.year * 12 + value.month - 1 + months, 12)
    month = month_zero + 1
    return value.replace(year=year, month=month,
                         day=min(value.day, calendar.monthrange(year, month)[1]))


def growth_points(baseline, performance, reference, exponent=.5, lower=.5, upper=2.0):
    factor = min(upper, max(lower, (baseline / reference) ** exponent))
    return 100 * (performance - baseline) / reference * factor


def register_growth(app, db, User, Exercise, TrainingLog, TrainingBatch, WorkoutSession,
                    login_required, get_current_user, ensure_user_setup):
    class GrowthGroup(db.Model):
        id = db.Column(db.Integer, primary_key=True)
        name = db.Column(db.String(60), nullable=False)
        owner_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False)
        invite_code = db.Column(db.String(12), unique=True, nullable=False)
        created_at = db.Column(db.DateTime, nullable=False, default=growth_now)

    class GrowthMembership(db.Model):
        id = db.Column(db.Integer, primary_key=True)
        group_id = db.Column(db.Integer, db.ForeignKey("growth_group.id"), nullable=False, index=True)
        user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False, index=True)
        joined_at = db.Column(db.DateTime, nullable=False, default=growth_now)
        user = db.relationship(User)
        __table_args__ = (UniqueConstraint("group_id", "user_id", name="uq_growth_membership"),)

    class GrowthChallenge(db.Model):
        id = db.Column(db.Integer, primary_key=True)
        group_id = db.Column(db.Integer, db.ForeignKey("growth_group.id"), nullable=False, index=True)
        name = db.Column(db.String(80), nullable=False)
        started_at = db.Column(db.DateTime, nullable=False)
        rounds = db.relationship("GrowthRound", order_by="GrowthRound.id")

    class GrowthRound(db.Model):
        id = db.Column(db.Integer, primary_key=True)
        challenge_id = db.Column(db.Integer, db.ForeignKey("growth_challenge.id"), nullable=False, index=True)
        exercise_name = db.Column(db.String(80), nullable=False)
        months = db.Column(db.Integer, nullable=False)
        starts_at = db.Column(db.DateTime, nullable=False)
        ends_at = db.Column(db.DateTime, nullable=False)
        # Separate cursors keep existing entries/batches out even if log IDs are reused.
        log_cursor = db.Column(db.Integer, nullable=False)
        batch_cursor = db.Column(db.Integer, nullable=False)
        metric = db.Column(db.String(20), nullable=False)
        target_reps = db.Column(db.Integer, nullable=True)
        rm_divisor = db.Column(db.Float, nullable=False)
        reference_weight = db.Column(db.Float, nullable=False)
        formula_version = db.Column(db.String(20), nullable=False, default="sqrt-v1")
        exponent = db.Column(db.Float, nullable=False, default=.5)
        factor_lower = db.Column(db.Float, nullable=False, default=.5)
        factor_upper = db.Column(db.Float, nullable=False, default=2.0)
        finalized_at = db.Column(db.DateTime, nullable=True)
        participants = db.relationship("GrowthParticipant", order_by="GrowthParticipant.id")

    class GrowthParticipant(db.Model):
        id = db.Column(db.Integer, primary_key=True)
        round_id = db.Column(db.Integer, db.ForeignKey("growth_round.id"), nullable=False, index=True)
        user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False, index=True)
        username = db.Column(db.String(40), nullable=False)
        exercise_id = db.Column(db.Integer, db.ForeignKey("exercise.id"), nullable=True)
        exercise = db.relationship(Exercise)
        round = db.relationship("GrowthRound", back_populates="participants")
        observations = db.relationship("GrowthObservation", order_by="GrowthObservation.observed_at",
                                      cascade="all, delete-orphan")
        result = db.relationship("GrowthResult", uselist=False)
        __table_args__ = (UniqueConstraint("round_id", "user_id", name="uq_growth_participant"),)

    class GrowthObservation(db.Model):
        id = db.Column(db.Integer, primary_key=True)
        participant_id = db.Column(db.Integer, db.ForeignKey("growth_participant.id"), nullable=False, index=True)
        # No foreign key to mutable training records: published results survive edits/deletes.
        source_key = db.Column(db.String(40), nullable=False)
        observed_at = db.Column(db.DateTime, nullable=False)
        recorded_at = db.Column(db.DateTime, nullable=False)
        weight = db.Column(db.Float, nullable=False)
        reps = db.Column(db.Integer, nullable=False)
        performance = db.Column(db.Float, nullable=False)
        __table_args__ = (UniqueConstraint("participant_id", "source_key", name="uq_growth_observation"),)

    class GrowthResult(db.Model):
        id = db.Column(db.Integer, primary_key=True)
        participant_id = db.Column(db.Integer, db.ForeignKey("growth_participant.id"), nullable=False, unique=True)
        status = db.Column(db.String(20), nullable=False)
        baseline = db.Column(db.Float, nullable=True)
        final_performance = db.Column(db.Float, nullable=True)
        weight_gain = db.Column(db.Float, nullable=True)
        points = db.Column(db.Float, nullable=True)
        rank = db.Column(db.Integer, nullable=True)

    bp = Blueprint("growth", __name__, url_prefix="/growth")

    def member_group(group_id):
        group = db.session.get(GrowthGroup, group_id)
        if group is None or not GrowthMembership.query.filter_by(
                group_id=group_id, user_id=get_current_user().id).first():
            abort(404)
        return group

    def points_for(round_, baseline, performance):
        return growth_points(baseline, performance, round_.reference_weight,
                             round_.exponent, round_.factor_lower, round_.factor_upper)

    def performance_for(round_, log):
        # Recording modes with different meanings must not enter the same ranking.
        if log.recording_mode != "weight":
            return None
        if not math.isfinite(log.weight) or not 0 < log.weight <= 1000:
            return None
        if round_.metric == "fixed_reps":
            return log.weight if log.reps == round_.target_reps else None
        if not 1 <= log.reps <= 12:
            return None
        # Use one frozen exercise-specific equation for every participant, including aliases.
        return log.weight if log.reps == 1 else log.weight * (1 + log.reps / round_.rm_divisor)

    def sync_user(user_id):
        """Called inside the same transaction as recording/editing a workout."""
        now = growth_now()
        participants = GrowthParticipant.query.join(GrowthRound).filter(
            GrowthParticipant.user_id == user_id, GrowthParticipant.exercise_id.isnot(None),
            GrowthRound.finalized_at.is_(None), GrowthRound.ends_at > now,
        ).all()
        if not participants:
            return
        earliest = min(p.round.starts_at for p in participants)
        logs = TrainingLog.query.filter(
            TrainingLog.user_id == user_id, TrainingLog.date >= earliest,
            TrainingLog.date <= now,
        ).order_by(TrainingLog.date, TrainingLog.id).all()
        workout_ids = {log.session_id for log in logs if log.session_id is not None}
        workouts = {w.id: w for w in WorkoutSession.query.filter(
            WorkoutSession.user_id == user_id, WorkoutSession.id.in_(workout_ids)
        ).all()} if workout_ids else {}
        grouped = {}
        for log in logs:
            kind = "session" if log.session_id is not None else "batch" if log.batch_id is not None else "log"
            source_id = log.session_id if kind == "session" else log.batch_id if kind == "batch" else log.id
            grouped.setdefault((log.lift_type, f"{kind}:{source_id}"), []).append(log)
        for participant in participants:
            round_ = participant.round
            exercise = participant.exercise
            if exercise is None or exercise.user_id != user_id:
                continue
            observations = {o.source_key: o for o in participant.observations}
            selected = set()
            for (lift_name, source_key), source_logs in grouped.items():
                if lift_name != exercise.name:
                    continue
                # The whole batch/session must start after competition setup.
                if source_key.startswith("session:"):
                    workout = workouts.get(source_logs[0].session_id)
                    if workout is None or workout.completed_at is None or workout.started_at < round_.starts_at:
                        continue
                    observed_at = workout.completed_at
                else:
                    existing = (source_logs[0].batch_id <= round_.batch_cursor
                                if source_key.startswith("batch:") else
                                source_logs[0].id <= round_.log_cursor)
                    if existing:
                        continue
                    observed_at = min(log.date for log in source_logs)
                if not round_.starts_at <= observed_at < round_.ends_at or observed_at > now:
                    continue
                valid = [(performance_for(round_, log), log) for log in source_logs
                         if round_.starts_at <= log.date < round_.ends_at and log.date <= now]
                valid = [(value, log) for value, log in valid if value is not None]
                if not valid:
                    continue
                # Representative performance is the best eligible set in a completed workout.
                value, best = max(valid, key=lambda item: (item[0], item[1].weight, -item[1].id))
                selected.add(source_key)
                observation = observations.get(source_key)
                if observation is None:
                    observation = GrowthObservation(participant_id=participant.id, source_key=source_key)
                    db.session.add(observation)
                observation.observed_at = observed_at
                observation.recorded_at = best.date
                observation.weight = best.weight
                observation.reps = best.reps
                observation.performance = value
            for key, observation in observations.items():
                if key not in selected:
                    db.session.delete(observation)
        db.session.flush()
        for participant in participants:
            db.session.expire(participant, ["observations"])

    def standings(round_):
        entries = []
        for participant in round_.participants:
            observations = sorted(participant.observations, key=lambda o: (o.observed_at, o.id))
            first = observations[0] if observations else None
            last = observations[-1] if observations else None
            ready = len(observations) >= 2
            result = participant.result if round_.finalized_at is not None else None
            status = result.status if result else "ranked" if ready else "baseline_only" if first else "no_record"
            points = result.points if result else round(points_for(round_, first.performance, last.performance), 2) if ready else None
            baseline = result.baseline if result else first.performance if first else None
            final_performance = result.final_performance if result else last.performance if ready else None
            weight_gain = result.weight_gain if result else last.weight - first.weight if ready else None
            entries.append({"participant": participant, "observations": observations,
                            "first": first, "last": last if ready else None, "status": status,
                            "baseline": baseline, "final_performance": final_performance,
                            "performance_gain": final_performance - baseline if final_performance is not None else None,
                            "weight_gain": weight_gain, "points": points,
                            "rank": result.rank if result else None})
        entries.sort(key=lambda row: (row["points"] is None, -(row["points"] or 0), row["participant"].id))
        previous_points, previous_rank = None, None
        for index, row in enumerate(entries, 1):
            if row["points"] is None:
                continue
            rank = previous_rank if row["points"] == previous_points else index
            if round_.finalized_at is None:
                row["rank"] = rank
            previous_points, previous_rank = row["points"], rank
        return entries

    def finalize_due(group_id):
        now = growth_now()
        due = GrowthRound.query.join(GrowthChallenge).filter(
            GrowthChallenge.group_id == group_id, GrowthRound.ends_at <= now,
            GrowthRound.finalized_at.is_(None),
        ).all()
        for round_ in due:
            rows = standings(round_)
            # Claim atomically: a concurrent page view must not publish duplicate results.
            claimed = GrowthRound.query.filter_by(id=round_.id, finalized_at=None).update(
                {GrowthRound.finalized_at: now}, synchronize_session=False)
            if not claimed:
                db.session.expire(round_)
                continue
            for row in rows:
                db.session.add(GrowthResult(participant_id=row["participant"].id,
                    status=row["status"], baseline=row["baseline"], final_performance=row["final_performance"],
                    weight_gain=row["weight_gain"], points=row["points"], rank=row["rank"]))
            round_.finalized_at = now
        db.session.commit()
        if due:
            db.session.expire_all()

    @bp.route("/")
    @login_required
    def index():
        user = get_current_user()
        groups = GrowthGroup.query.join(GrowthMembership).filter(
            GrowthMembership.user_id == user.id).order_by(GrowthGroup.created_at.desc()).all()
        return render_template("growth/index.html", groups=groups, user=user)

    @bp.route("/groups", methods=["POST"])
    @login_required
    def create_group():
        name = request.form.get("name", "").strip()
        if not 1 <= len(name) <= 60:
            flash("グループ名は1〜60文字で入力してください。", "error")
            return redirect(url_for("growth.index"))
        user = get_current_user()
        group = GrowthGroup(name=name, owner_id=user.id, invite_code=token_hex(6).upper())
        db.session.add(group)
        db.session.flush()
        db.session.add(GrowthMembership(group_id=group.id, user_id=user.id))
        db.session.commit()
        flash("グループを作成しました。招待コードを仲間に共有してください。", "success")
        return redirect(url_for("growth.group_page", group_id=group.id))

    @bp.route("/join", methods=["POST"])
    @login_required
    def join_group():
        code = request.form.get("invite_code", "").strip().upper()
        group = GrowthGroup.query.filter_by(invite_code=code).first() if len(code) == 12 else None
        if group is None:
            flash("招待コードが見つかりません。入力を確認してください。", "error")
            return redirect(url_for("growth.index"))
        user = get_current_user()
        if GrowthMembership.query.filter_by(group_id=group.id, user_id=user.id).first():
            flash("このグループには参加済みです。", "success")
        else:
            db.session.add(GrowthMembership(group_id=group.id, user_id=user.id))
            try:
                db.session.commit()
            except IntegrityError:
                db.session.rollback()
            flash("グループに参加しました。開始済みの対戦には追加されず、次の対戦から参加できます。", "success")
        return redirect(url_for("growth.group_page", group_id=group.id))

    @bp.route("/groups/<int:group_id>")
    @login_required
    def group_page(group_id):
        group = member_group(group_id)
        user = get_current_user()
        exercises = ensure_user_setup(user)
        sync_user(user.id)
        finalize_due(group_id)
        memberships = GrowthMembership.query.filter_by(group_id=group.id).order_by(GrowthMembership.id).all()
        challenges = GrowthChallenge.query.filter_by(group_id=group.id).order_by(GrowthChallenge.id.desc()).all()
        defaults = {exercise.id: 100 if exercise.name == "スクワット" else 60 for exercise in exercises}
        return render_template("growth/group.html", group=group, user=user, exercises=exercises,
                               memberships=memberships, challenges=challenges, defaults=defaults,
                               now=growth_now())

    @bp.route("/groups/<int:group_id>/challenges", methods=["POST"])
    @login_required
    def create_challenge(group_id):
        group = member_group(group_id)
        user = get_current_user()
        if group.owner_id != user.id:
            abort(403)
        name = request.form.get("name", "").strip()
        selected = request.form.getlist("exercise_ids")
        metric = request.form.get("metric", "estimated_1rm")
        try:
            if not 1 <= len(name) <= 80 or not 1 <= len(selected) <= 20 or len(set(selected)) != len(selected):
                raise ValueError("対戦名と、1〜20種目の選択を確認してください。")
            if metric not in ("estimated_1rm", "fixed_reps"):
                raise ValueError("採点方法を選んでください。")
            target_reps = int(request.form.get("target_reps", "5")) if metric == "fixed_reps" else None
            if target_reps is not None and not 1 <= target_reps <= 20:
                raise ValueError("比較する回数は1〜20回で入力してください。")
            settings = []
            for selected_id in selected:
                exercise_id = int(selected_id)
                exercise = Exercise.query.filter_by(id=exercise_id, user_id=user.id, is_archived=False).first()
                if exercise is None:
                    raise ValueError("自分のメニューにある種目を選んでください。")
                if exercise.recording_mode != "weight":
                    raise ValueError("現在の成長対戦では「重量＋回数」の種目を選んでください。")
                months = int(request.form.get(f"months_{exercise_id}", ""))
                reference = float(request.form.get(f"reference_{exercise_id}", ""))
                if not 1 <= months <= 12 or not math.isfinite(reference) or not .1 <= reference <= 1000:
                    raise ValueError("期間は1〜12カ月、配点基準は0.1〜1000kgで入力してください。")
                settings.append((exercise, months, reference))
        except (ValueError, TypeError) as error:
            flash(str(error) if isinstance(error, ValueError) and not str(error).startswith(("invalid", "could"))
                  else "種目・期間・配点基準を正しく入力してください。", "error")
            return redirect(url_for("growth.group_page", group_id=group_id))
        started_at = growth_now()
        cursor = db.session.query(func.max(TrainingLog.id)).scalar() or 0
        batch_cursor = db.session.query(func.max(TrainingBatch.id)).scalar() or 0
        challenge = GrowthChallenge(group_id=group.id, name=name, started_at=started_at)
        db.session.add(challenge)
        db.session.flush()
        memberships = GrowthMembership.query.filter_by(group_id=group.id).all()
        member_ids = [membership.user_id for membership in memberships]
        personal_exercises = {(exercise.user_id, exercise.name): exercise.id for exercise in
                              Exercise.query.filter(Exercise.user_id.in_(member_ids), Exercise.is_archived.is_(False), Exercise.recording_mode == "weight").all()}
        for exercise, months, reference in settings:
            round_ = GrowthRound(challenge_id=challenge.id, exercise_name=exercise.name,
                months=months, starts_at=started_at, ends_at=add_calendar_months(started_at, months),
                log_cursor=cursor, batch_cursor=batch_cursor, metric=metric,
                target_reps=target_reps, reference_weight=reference,
                rm_divisor=33.3 if exercise.name == "スクワット" else 40.0)
            db.session.add(round_)
            db.session.flush()
            for membership in memberships:
                db.session.add(GrowthParticipant(round_id=round_.id, user_id=membership.user_id,
                    username=membership.user.username,
                    exercise_id=personal_exercises.get((membership.user_id, exercise.name))))
        db.session.commit()
        flash("対戦を開始しました。次に完了する対象種目のトレーニングから自動で集計します。", "success")
        return redirect(url_for("growth.challenge_page", challenge_id=challenge.id))

    @bp.route("/challenges/<int:challenge_id>")
    @login_required
    def challenge_page(challenge_id):
        challenge = db.session.get(GrowthChallenge, challenge_id)
        if challenge is None:
            abort(404)
        group = member_group(challenge.group_id)
        user = get_current_user()
        exercises = ensure_user_setup(user)
        sync_user(user.id)
        finalize_due(group.id)
        cards = []
        chart_data = {}
        for round_ in challenge.rounds:
            rows = standings(round_)
            mine = next((row for row in rows if row["participant"].user_id == user.id), None)
            cards.append({"round": round_, "rows": rows, "mine": mine})
            if round_.finalized_at is not None:
                series = []
                for row in rows:
                    observations = row["observations"]
                    if not observations:
                        continue
                    baseline = row["baseline"]
                    row["history"] = [{"observation": o, "points": round(
                        points_for(round_, baseline, o.performance), 2)} for o in observations]
                    series.append({"name": row["participant"].username,
                        "mine": row["participant"].user_id == user.id,
                        "points": [{"date": o.observed_at.isoformat() + "+09:00", "day": o.observed_at.strftime("%Y-%m-%d"),
                                    "calendar_day": o.recorded_at.strftime("%Y-%m-%d"),
                                    "value": round(points_for(round_, baseline, o.performance), 2),
                                    "weight": o.weight, "reps": o.reps, "performance": round(o.performance, 2)}
                                   for o in observations]})
                chart_data[str(round_.id)] = {"series": series,
                    "starts_at": round_.starts_at.isoformat() + "+09:00",
                    "ends_at": round_.ends_at.isoformat() + "+09:00"}
        return render_template("growth/challenge.html", challenge=challenge, group=group, user=user,
                               cards=cards, exercises=exercises, chart_data=chart_data)

    @bp.route("/participants/<int:participant_id>/exercise", methods=["POST"])
    @login_required
    def map_exercise(participant_id):
        user = get_current_user()
        participant = GrowthParticipant.query.filter_by(id=participant_id, user_id=user.id).first_or_404()
        challenge = db.session.get(GrowthChallenge, participant.round.challenge_id)
        member_group(challenge.group_id)
        if participant.round.ends_at <= growth_now() or participant.observations:
            flash("最初の成績が登録された後、または期間終了後は種目の対応を変更できません。", "error")
        else:
            exercise_id = request.form.get("exercise_id", type=int)
            exercise = Exercise.query.filter_by(id=exercise_id, user_id=user.id, is_archived=False).first()
            if exercise is None or exercise.recording_mode != "weight":
                flash("「重量＋回数」で記録する自分の種目を選んでください。", "error")
            else:
                participant.exercise_id = exercise.id
                participant.exercise = exercise
                db.session.flush()
                sync_user(user.id)
                db.session.commit()
                flash("この対戦で使う自分の種目を設定しました。同じ種目・器具で記録してください。", "success")
        return redirect(url_for("growth.challenge_page", challenge_id=challenge.id))

    # Add only new tables; the existing workout tables and data remain intact.
    with app.app_context():
        db.create_all()
    app.register_blueprint(bp)
    app.extensions["growth"] = {"sync_user": sync_user,
        "models": {model.__name__: model for model in (GrowthGroup, GrowthMembership, GrowthChallenge,
                    GrowthRound, GrowthParticipant, GrowthObservation, GrowthResult)}}
