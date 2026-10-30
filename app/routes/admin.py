"""Administrator pages: analytics, users, AI engine settings, audit log."""

import secrets
from collections import Counter
from datetime import timedelta

from flask import Blueprint, abort, flash, redirect, render_template, request, url_for
from flask_login import current_user
from sqlalchemy import func

from ..ai import engine, face_analyzer, object_detector
from ..ai.pipeline import decode_image
from ..extensions import db
from ..models import AuditLog, Exam, ExamSession, ProctorEvent, Setting, User, utcnow
from ..services import audit, enroll_face, sweep_sessions
from ..utils import admin_required
from .auth import EMAIL_RE

bp = Blueprint("admin", __name__)


@bp.route("/")
@admin_required
def dashboard():
    sweep_sessions()
    now = utcnow()
    since = now - timedelta(days=13)
    users_by_role = dict(db.session.query(User.role, func.count(User.id)).group_by(User.role).all())
    sessions = ExamSession.query.all()
    finished = [s for s in sessions if s.is_finished]
    stats = {
        "students": users_by_role.get("student", 0),
        "proctors": users_by_role.get("proctor", 0),
        "exams": Exam.query.count(),
        "published": Exam.query.filter_by(status="published").count(),
        "live": sum(1 for s in sessions if s.status == "in_progress"),
        "sessions": len(sessions),
        "flagged": sum(1 for s in sessions if (s.peak_score or 0) >= s.exam.warn_threshold),
        "malpractice": sum(1 for s in sessions if s.verdict == "malpractice"),
        "avg_fraud": round(sum(s.peak_score or 0 for s in finished) / len(finished), 1) if finished else 0,
        "events": ProctorEvent.query.count(),
    }
    days = [(since + timedelta(days=i)).date() for i in range(14)]
    per_day = Counter(s.started_at.date() for s in sessions if s.started_at and s.started_at >= since)
    flagged_day = Counter(
        s.started_at.date() for s in sessions if s.started_at and s.started_at >= since and (s.peak_score or 0) >= s.exam.warn_threshold
    )
    event_rows = (
        db.session.query(ProctorEvent.event_type, func.count(ProctorEvent.id))
        .filter(ProctorEvent.review_status != "dismissed")
        .group_by(ProctorEvent.event_type)
        .order_by(func.count(ProctorEvent.id).desc())
        .limit(10)
        .all()
    )
    category_rows = Counter()
    for etype, n in db.session.query(ProctorEvent.event_type, func.count(ProctorEvent.id)).group_by(ProctorEvent.event_type):
        category_rows[engine.catalog_entry(etype)["category"]] += n
    charts = {
        "days": [d.strftime("%d %b") for d in days],
        "sessions": [per_day.get(d, 0) for d in days],
        "flagged": [flagged_day.get(d, 0) for d in days],
        "events": {"labels": [engine.catalog_entry(t)["label"] for t, _ in event_rows], "values": [n for _, n in event_rows]},
        "risk": [sum(1 for s in sessions if s.risk_level == lvl) for lvl in ("low", "medium", "high", "critical")],
        "categories": {"labels": list(category_rows.keys()), "values": list(category_rows.values())},
    }
    exam_rows = []
    for e in Exam.query.order_by(Exam.created_at.desc()).limit(8):
        ss = e.sessions
        exam_rows.append(
            {
                "exam": e,
                "attempts": len(ss),
                "flagged": sum(1 for s in ss if (s.peak_score or 0) >= e.warn_threshold),
                "avg_fraud": round(sum(s.peak_score or 0 for s in ss) / len(ss), 1) if ss else 0,
            }
        )
    recent = AuditLog.query.order_by(AuditLog.created_at.desc()).limit(8).all()
    return render_template("admin/dashboard.html", stats=stats, charts=charts, exam_rows=exam_rows, recent=recent)


# ---------------------------------------------------------------------- users


@bp.route("/users")
@admin_required
def users():
    role = request.args.get("role", "")
    q = request.args.get("q", "").strip()
    query = User.query
    if role in User.ROLES:
        query = query.filter_by(role=role)
    if q:
        like = f"%{q}%"
        query = query.filter(User.name.ilike(like) | User.email.ilike(like) | User.student_code.ilike(like))
    rows = query.order_by(User.role, User.name).all()
    return render_template("admin/users.html", rows=rows, role=role, q=q)


@bp.route("/users/new", methods=["GET", "POST"])
@bp.route("/users/<int:user_id>", methods=["GET", "POST"])
@admin_required
def user_edit(user_id=None):
    user = db.get_or_404(User, user_id) if user_id else User(role="student")
    if request.method == "POST":
        action = request.form.get("action", "save")
        if action == "toggle" and user.id:
            if user.id == current_user.id:
                flash("You cannot deactivate your own account.", "error")
            else:
                user.is_active_flag = not user.is_active_flag
                audit("user_toggled", f"user:{user.id}", "active" if user.is_active_flag else "inactive")
                db.session.commit()
                flash(f"Account {'activated' if user.is_active_flag else 'deactivated'}.", "success")
            return redirect(url_for("admin.user_edit", user_id=user.id))
        if action == "reset" and user.id:
            temp = secrets.token_urlsafe(8)
            user.set_password(temp)
            audit("password_reset", f"user:{user.id}")
            db.session.commit()
            flash(f"Temporary password for {user.email}: {temp}", "success")
            return redirect(url_for("admin.user_edit", user_id=user.id))
        if action == "photo" and user.id:
            ok, msg = enroll_face(user, decode_image(request.form.get("photo")))
            if ok:
                audit("face_enrolled", f"user:{user.id}", "by admin")
                db.session.commit()
            flash(msg, "success" if ok else "error")
            return redirect(url_for("admin.user_edit", user_id=user.id))
        if action == "clear_face" and user.id:
            user.face_embedding_raw = None
            user.photo_path = None
            user.face_backend = None
            audit("face_cleared", f"user:{user.id}")
            db.session.commit()
            flash("Face profile removed. The student will need proctor approval or re-enrolment.", "info")
            return redirect(url_for("admin.user_edit", user_id=user.id))

        name = request.form.get("name", "").strip()
        email = request.form.get("email", "").strip().lower()
        role = request.form.get("role", "student")
        errors = []
        if len(name) < 2:
            errors.append("Name is required.")
        if not EMAIL_RE.match(email):
            errors.append("A valid email is required.")
        clash = User.query.filter(User.email == email, User.id != (user.id or 0)).first()
        if clash:
            errors.append("Another account uses this email.")
        if role not in User.ROLES:
            errors.append("Invalid role.")
        if user.id == current_user.id and role != "admin":
            errors.append("You cannot remove your own admin role.")
        password = request.form.get("password", "")
        if not user.id and len(password) < 8:
            errors.append("Set an initial password of at least 8 characters.")
        if errors:
            for e in errors:
                flash(e, "error")
            return render_template("admin/user_edit.html", user=user, form=request.form), 400
        user.name, user.email, user.role = name, email, role
        user.student_code = request.form.get("student_code", "").strip() or None
        user.department = request.form.get("department", "").strip() or None
        if password:
            user.set_password(password)
        if not user.id:
            db.session.add(user)
            db.session.flush()
            if request.form.get("photo"):
                ok, msg = enroll_face(user, decode_image(request.form.get("photo")))
                if not ok:
                    flash(f"User created, but face enrolment failed: {msg}", "error")
        audit("user_saved", f"user:{user.id}", f"{email} ({role})")
        db.session.commit()
        flash("User saved.", "success")
        return redirect(url_for("admin.user_edit", user_id=user.id))
    history = (
        ExamSession.query.filter_by(student_id=user.id).order_by(ExamSession.started_at.desc()).limit(20).all()
        if user.id
        else []
    )
    return render_template("admin/user_edit.html", user=user, form={}, history=history)


# ------------------------------------------------------------------ settings


@bp.route("/settings", methods=["GET", "POST"])
@admin_required
def settings():
    weights = Setting.get("event_weights", {}) or {}
    cooldowns = Setting.get("event_cooldowns", {}) or {}
    if request.method == "POST":
        if request.form.get("action") == "reset":
            Setting.put("event_weights", {})
            Setting.put("event_cooldowns", {})
            audit("settings_reset", "engine")
            db.session.commit()
            flash("AI engine weights restored to defaults.", "success")
            return redirect(url_for("admin.settings"))
        new_w, new_c = {}, {}
        for etype in engine.EVENT_CATALOG:
            default = engine.EVENT_CATALOG[etype][3]
            w = request.form.get(f"w-{etype}", type=float)
            if w is not None and 0 <= w <= 100 and w != default:
                new_w[etype] = w
            c = request.form.get(f"c-{etype}", type=int)
            default_c = engine.DEFAULT_COOLDOWN_SECONDS.get(etype, 8)
            if c is not None and 0 <= c <= 3600 and c != default_c:
                new_c[etype] = c
        Setting.put("event_weights", new_w)
        Setting.put("event_cooldowns", new_c)
        audit("settings_saved", "engine", f"{len(new_w)} weight overrides, {len(new_c)} cooldown overrides")
        db.session.commit()
        flash("AI engine settings saved. New weights apply to all scores from now on.", "success")
        return redirect(url_for("admin.settings"))
    rows = []
    for etype, (label, category, severity, weight, decays) in engine.EVENT_CATALOG.items():
        rows.append(
            {
                "type": etype, "label": label, "category": category, "severity": severity, "default": weight,
                "weight": weights.get(etype, weight), "decays": decays,
                "cooldown": cooldowns.get(etype, engine.DEFAULT_COOLDOWN_SECONDS.get(etype, 8)),
                "default_cooldown": engine.DEFAULT_COOLDOWN_SECONDS.get(etype, 8),
            }
        )
    rows.sort(key=lambda r: (r["category"], -r["default"]))
    return render_template(
        "admin/settings.html",
        rows=rows,
        half_life=engine.HALF_LIFE_SECONDS,
        saturation=engine.SATURATION,
        face_backend=face_analyzer().backend,
        face_threshold=face_analyzer().threshold,
        yolo=object_detector().available,
    )


@bp.route("/audit")
@admin_required
def audit_log():
    page = max(1, request.args.get("page", 1, type=int))
    action = request.args.get("action", "")
    q = AuditLog.query
    if action:
        q = q.filter(AuditLog.action == action)
    pagination = q.order_by(AuditLog.created_at.desc()).paginate(page=page, per_page=50, error_out=False)
    actions = [a for (a,) in db.session.query(AuditLog.action).distinct().order_by(AuditLog.action)]
    return render_template("admin/audit.html", pagination=pagination, actions=actions, action=action)


@bp.route("/users/<int:user_id>/delete", methods=["POST"])
@admin_required
def user_delete(user_id):
    user = db.get_or_404(User, user_id)
    if user.id == current_user.id:
        abort(400)
    if ExamSession.query.filter_by(student_id=user.id).count():
        user.is_active_flag = False
        flash("User has exam records, so the account was deactivated instead of deleted.", "info")
    else:
        db.session.delete(user)
        flash("User deleted.", "success")
    audit("user_deleted", f"user:{user_id}")
    db.session.commit()
    return redirect(url_for("admin.users"))
