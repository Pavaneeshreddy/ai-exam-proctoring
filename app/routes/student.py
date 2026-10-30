
from flask import Blueprint, abort, flash, jsonify, redirect, render_template, request, session as flask_session, url_for
from flask_login import current_user

from ..ai import face_analyzer
from ..ai.pipeline import decode_image
from ..extensions import db
from ..models import Exam, ExamSession, Snapshot, utcnow
from ..services import audit, build_question_order, extract_single_face, record_event, save_file
from ..utils import student_required

bp = Blueprint("student", __name__)

MAX_IDENTITY_ATTEMPTS = 3


def _attempts(exam):
    return ExamSession.query.filter_by(exam_id=exam.id, student_id=current_user.id).all()


@bp.route("/")
@student_required
def dashboard():
    exams = Exam.query.filter_by(status="published").order_by(Exam.start_at.is_(None), Exam.start_at).all()
    my_sessions = (
        ExamSession.query.filter_by(student_id=current_user.id).order_by(ExamSession.started_at.desc()).all()
    )
    by_exam = {}
    for s in my_sessions:
        by_exam.setdefault(s.exam_id, []).append(s)
    cards = []
    for e in exams:
        attempts = by_exam.get(e.id, [])
        active = next((s for s in attempts if s.is_active), None)
        state = e.availability()
        if active:
            state = "resume"
        elif len(attempts) >= (e.max_attempts or 1):
            state = "completed"
        cards.append({"exam": e, "state": state, "attempts": attempts, "active": active})
    stats = {
        "completed": sum(1 for s in my_sessions if s.is_finished),
        "upcoming": sum(1 for c in cards if c["state"] in ("open", "upcoming")),
        "average": _avg([s.percent for s in my_sessions if s.percent is not None and s.exam.show_results]),
    }
    return render_template("student/dashboard.html", cards=cards, history=my_sessions, stats=stats)


def _avg(values):
    return round(sum(values) / len(values), 1) if values else None


@bp.route("/exam/<int:exam_id>/prepare")
@student_required
def prepare(exam_id):
    exam = db.get_or_404(Exam, exam_id)
    attempts = _attempts(exam)
    active = next((s for s in attempts if s.is_active), None)
    if active:
        return redirect(url_for("student.room", session_id=active.id))
    if not exam.is_open():
        flash("This exam is not open right now.", "error")
        return redirect(url_for("student.dashboard"))
    if len(attempts) >= (exam.max_attempts or 1):
        flash("You have used all attempts for this exam.", "error")
        return redirect(url_for("student.dashboard"))
    return render_template(
        "student/prepare.html",
        exam=exam,
        has_enrolled_face=current_user.face_embedding is not None,
        backend=face_analyzer().backend,
        attempts_left=MAX_IDENTITY_ATTEMPTS - flask_session.get(f"idfail-{exam.id}", 0),
    )


@bp.route("/exam/<int:exam_id>/begin", methods=["POST"])
@student_required
def begin(exam_id):
    """Identity check with the start-of-exam photo, then create the exam session."""
    exam = db.get_or_404(Exam, exam_id)
    payload = request.get_json(silent=True) or {}
    attempts = _attempts(exam)
    active = next((s for s in attempts if s.is_active), None)
    if active:
        return jsonify(ok=True, redirect=url_for("student.room", session_id=active.id))
    if not exam.is_open():
        return jsonify(ok=False, error="This exam is not open right now."), 400
    if len(attempts) >= (exam.max_attempts or 1):
        return jsonify(ok=False, error="No attempts left for this exam."), 400
    if exam.access_code and (payload.get("access_code") or "").strip() != exam.access_code:
        return jsonify(ok=False, error="The exam access code is incorrect.", field="access_code"), 400

    img = decode_image(payload.get("photo"))
    emb, error = extract_single_face(img)
    if error:
        return jsonify(ok=False, error=error, retry=True), 400

    analyzer = face_analyzer()
    fail_key = f"idfail-{exam.id}"
    similarity = None
    verified = True
    enrolled = current_user.face_embedding
    if enrolled is not None and current_user.face_backend == analyzer.backend:
        similarity = analyzer.similarity(enrolled, emb)
        verified = analyzer.is_match(similarity)
    elif enrolled is None:
        verified = not exam.require_identity  # no profile photo -> proctor must approve when identity is required

    liveness = payload.get("liveness") or {}
    liveness_passed = liveness.get("passed")
    if exam.require_liveness and liveness_passed is False:
        verified = False

    if not verified and enrolled is not None:
        fails = flask_session.get(fail_key, 0) + 1
        flask_session[fail_key] = fails
        if fails < MAX_IDENTITY_ATTEMPTS:
            reason = (
                "Liveness check was not completed."
                if liveness_passed is False
                else "Your face does not match your registered photo."
            )
            return jsonify(
                ok=False,
                error=f"{reason} Remove glasses/masks, improve lighting and try again. "
                f"{MAX_IDENTITY_ATTEMPTS - fails} attempt(s) left before a proctor must approve you.",
                retry=True,
                similarity=similarity,
            ), 400

    order, option_orders = build_question_order(exam)
    s = ExamSession(
        exam_id=exam.id,
        student_id=current_user.id,
        status="in_progress" if verified else "pending_approval",
        started_at=utcnow(),
        last_heartbeat=utcnow(),
        question_order=order,
        option_orders=option_orders,
        identity_backend=analyzer.backend,
        identity_score=similarity,
        identity_verified=verified,
        liveness_passed=liveness_passed,
        ip_address=(request.headers.get("X-Forwarded-For") or request.remote_addr or "").split(",")[0],
        user_agent=(request.user_agent.string or "")[:400],
        device_info=payload.get("device") or {},
    )
    s.reference_embedding = emb
    db.session.add(s)
    db.session.flush()
    s.reference_photo = save_file(f"sessions/{s.id}/reference.jpg", img)
    db.session.add(Snapshot(session_id=s.id, path=s.reference_photo, kind="reference", face_count=1, identity_similarity=similarity))
    if not verified:
        if enrolled is None:
            msg = "No registered face photo on file — proctor approval required"
            record_event(s, "identity_verification_failed", 0.5, {"reason": "no_enrolled_photo"}, source="server", message=msg)
        else:
            record_event(
                s,
                "identity_verification_failed",
                0.9,
                {"similarity": similarity, "threshold": analyzer.threshold, "liveness": liveness_passed},
                source="server",
                image=img,
            )
    if liveness_passed is False:
        record_event(s, "liveness_failed", 0.8, liveness, source="client")
    flask_session.pop(fail_key, None)
    audit("exam_started", f"session:{s.id}", f"exam {exam.id}, verified={verified}, similarity={similarity}")
    db.session.commit()
    return jsonify(ok=True, redirect=url_for("student.room", session_id=s.id), verified=verified, similarity=similarity)


def _own_session(session_id):
    s = db.get_or_404(ExamSession, session_id)
    if s.student_id != current_user.id:
        abort(403)
    return s


@bp.route("/session/<int:session_id>")
@student_required
def room(session_id):
    s = _own_session(session_id)
    if s.is_finished:
        return redirect(url_for("student.result", session_id=s.id))
    if s.status == "pending_approval":
        return render_template("student/waiting.html", s=s)
    questions = {q.id: q for q in s.exam.questions}
    answers = {a.question_id: a for a in s.answers}
    payload = []
    for qid in s.question_order or [q.id for q in s.exam.questions]:
        q = questions.get(qid)
        if q is None:
            continue
        order = (s.option_orders or {}).get(str(q.id)) or list(range(len(q.options or [])))
        a = answers.get(q.id)
        payload.append(
            {
                "id": q.id,
                "type": q.qtype,
                "text": q.text,
                "marks": q.marks,
                "options": [{"value": i, "text": q.options[i]} for i in order if i < len(q.options or [])],
                "response": a.response if a else None,
                "flagged": bool(a and a.flagged),
            }
        )
    from flask import current_app

    config = {
        "sessionId": s.id,
        "examMode": s.exam.mode,
        "remaining": s.remaining_seconds(),
        "frameInterval": current_app.config["FRAME_INTERVAL_SECONDS"],
        "monitorAudio": s.exam.monitor_audio,
        "monitorObjects": s.exam.monitor_objects,
        "enforceFullscreen": s.exam.enforce_fullscreen,
        "referencePhoto": url_for("main.media", rel=s.reference_photo) if s.reference_photo else None,
        "sheets": [{"path": p, "url": url_for("main.media", rel=p)} for p in (s.answer_sheets or [])],
    }
    return render_template("student/room.html", s=s, exam=s.exam, questions=payload, config=config)


@bp.route("/session/<int:session_id>/result")
@student_required
def result(session_id):
    s = _own_session(session_id)
    if s.is_active:
        return redirect(url_for("student.room", session_id=s.id))
    rows = []
    if s.exam.show_results and not s.needs_manual_grading:
        answers = {a.question_id: a for a in s.answers}
        for q in s.exam.questions:
            rows.append({"q": q, "a": answers.get(q.id)})
    return render_template("student/result.html", s=s, rows=rows)

