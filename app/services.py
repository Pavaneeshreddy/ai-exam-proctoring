"""Business logic shared by the routes: storage, events, scoring, grading."""

import os
import random
import re
import uuid
from datetime import timedelta

from flask import current_app, request
from flask_login import current_user

from .ai import engine
from .ai.pipeline import encode_jpeg
from .extensions import db
from .models import (
    utcnow,
    Answer,
    AuditLog,
    ExamSession,
    ProctorEvent,
    ProctorMessage,
    ScorePoint,
    Setting,
    Snapshot,
)

# ---------------------------------------------------------------- storage


def storage_path(rel):
    root = os.path.abspath(current_app.config["STORAGE_DIR"])
    full = os.path.abspath(os.path.join(root, rel))
    if not full.startswith(root + os.sep):
        raise ValueError("invalid storage path")
    return full


def save_file(rel, data):
    """Save bytes or a BGR image under STORAGE_DIR; returns the relative path."""
    if data is not None and not isinstance(data, (bytes, bytearray)):
        data = encode_jpeg(data)
    if not data:
        return None
    full = storage_path(rel)
    os.makedirs(os.path.dirname(full), exist_ok=True)
    with open(full, "wb") as fh:
        fh.write(data)
    return rel


def stamp():
    return utcnow().strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:6]


# ------------------------------------------------------------------ audit


def audit(action, target=None, details=None, user=None):
    user = user or (current_user if current_user and current_user.is_authenticated else None)
    db.session.add(
        AuditLog(
            user_id=user.id if user else None,
            action=action,
            target=target,
            details=details,
            ip_address=_client_ip(),
        )
    )


def _client_ip():
    try:
        return (request.headers.get("X-Forwarded-For") or request.remote_addr or "").split(",")[0].strip()
    except RuntimeError:
        return None


# ----------------------------------------------------------------- events


def event_weights():
    return Setting.get("event_weights", {}) or {}


def record_event(session, event_type, confidence=1.0, details=None, source="client", image=None, message=None, now=None, correlate=True):
    """Store a proctoring event (respecting per-type cooldowns) and update the score.

    Returns the new ProctorEvent, or None if suppressed by the cooldown.
    """
    now = now or utcnow()
    weights = event_weights()
    entry = engine.catalog_entry(event_type, weights)
    cooldowns = {**engine.DEFAULT_COOLDOWN_SECONDS, **(Setting.get("event_cooldowns", {}) or {})}
    cooldown = cooldowns.get(event_type, 8)
    last = (
        ProctorEvent.query.filter_by(session_id=session.id, event_type=event_type)
        .order_by(ProctorEvent.created_at.desc())
        .first()
    )
    if last and (now - last.created_at).total_seconds() < cooldown:
        return None

    ev = ProctorEvent(
        session_id=session.id,
        event_type=event_type,
        category=entry["category"],
        severity=entry["severity"],
        weight=entry["weight"],
        confidence=round(float(confidence if confidence is not None else 1.0), 3),
        source=source,
        message=(message or entry["label"])[:300],
        details=details or {},
        created_at=now,
    )
    if image is not None and engine.SEVERITY_ORDER.index(entry["severity"]) >= 2:
        rel = save_file(f"sessions/{session.id}/evidence/{stamp()}-{event_type}.jpg", image)
        if rel:
            ev.evidence_path = rel
            db.session.add(Snapshot(session_id=session.id, path=rel, kind="evidence", created_at=now))
    db.session.add(ev)
    db.session.flush()

    if correlate:
        window_start = now - timedelta(seconds=60)
        recent = (
            ProctorEvent.query.filter(ProctorEvent.session_id == session.id, ProctorEvent.created_at >= window_start)
            .with_entities(ProctorEvent.event_type, ProctorEvent.created_at)
            .all()
        )
        recent_pairs = [(t, ts) for t, ts in recent]
        composites = {t for t, _ in recent_pairs if engine.catalog_entry(t)["category"] == "composite"}
        for composite, description in engine.find_correlations(recent_pairs, event_type, now, composites):
            record_event(
                session,
                composite,
                confidence=0.8,
                details={"trigger": event_type, "rule": description},
                source="system",
                image=image,
                message=f"{engine.catalog_entry(composite)['label']}: {description}",
                now=now,
                correlate=False,
            )

    recompute_score(session, now)
    enforce_violation_limit(session, now)
    return ev


# Events that count as a warning ("strike") for the student. System noise such as a
# dropped connection, composite events (already counted via their parts) and info-level
# events do not count.
STRIKE_SEVERITIES = ("medium", "high", "critical")
NON_STRIKE_CATEGORIES = ("system", "composite", "manual")


def is_strike(event):
    return (
        event.review_status != "dismissed"
        and event.severity in STRIKE_SEVERITIES
        and event.category not in NON_STRIKE_CATEGORIES
    )


def violation_count(session):
    return sum(1 for e in ProctorEvent.query.filter_by(session_id=session.id).all() if is_strike(e))


def enforce_violation_limit(session, now=None):
    limit = session.exam.max_violations or 0
    if session.status != "in_progress" or limit <= 0:
        return
    if violation_count(session) >= limit:
        finish_session(
            session,
            "terminated",
            reason=f"Exam ended automatically: {limit} integrity warnings were recorded "
            "(the maximum allowed for this exam). A proctor will review the evidence.",
            now=now,
        )


def score_time(session, now=None):
    """Finished sessions are scored as of submission so decay stops at the end of the exam."""
    if session.is_finished and session.submitted_at:
        return session.submitted_at
    return now or utcnow()


def recompute_score(session, now=None):
    now = score_time(session, now)
    events = ProctorEvent.query.filter_by(session_id=session.id).all()
    score, _ = engine.compute_score(events, now, event_weights())
    exam = session.exam
    previous_level = session.risk_level
    session.fraud_score = score
    session.peak_score = max(session.peak_score or 0.0, score)
    session.risk_level = engine.risk_level(score, exam.warn_threshold, exam.critical_threshold)

    last_point = (
        ScorePoint.query.filter_by(session_id=session.id).order_by(ScorePoint.created_at.desc()).first()
    )
    if session.is_finished:
        pass
    elif last_point is None or abs(last_point.score - score) >= 0.5 or (now - last_point.created_at).total_seconds() > 60:
        db.session.add(ScorePoint(session_id=session.id, score=score, created_at=now))

    levels = ["low", "medium", "high", "critical"]
    if session.status == "in_progress" and levels.index(session.risk_level) > levels.index(previous_level or "low"):
        if session.risk_level in ("high", "critical"):
            send_message(
                session,
                "Unusual activity has been detected and reported to your proctor. "
                "Keep your face in view, stay on the exam screen and make sure you are alone.",
                kind="system",
            )
    if (
        session.status == "in_progress"
        and exam.auto_terminate
        and score >= exam.critical_threshold
    ):
        finish_session(session, "terminated", reason=f"Automatically terminated: fraud score {score} reached the critical threshold ({exam.critical_threshold}).")
    return score


def send_message(session, body, kind="warning", sender=None):
    msg = ProctorMessage(session_id=session.id, body=body, kind=kind, sender_id=sender.id if sender else None)
    db.session.add(msg)
    if kind == "warning":
        session.warnings_sent = (session.warnings_sent or 0) + 1
    return msg


# --------------------------------------------------------------- sessions


def build_question_order(exam):
    questions = list(exam.questions)
    if exam.shuffle_questions:
        random.shuffle(questions)
    option_orders = {}
    for q in questions:
        idx = list(range(len(q.options or [])))
        if exam.shuffle_options and q.qtype in ("mcq", "multi"):
            random.shuffle(idx)
        option_orders[str(q.id)] = idx
    return [q.id for q in questions], option_orders


def finish_session(session, status="submitted", reason=None, now=None):
    if session.is_finished:
        return session
    now = now or utcnow()
    session.status = status
    session.submitted_at = now
    if reason:
        session.terminated_reason = reason
    grade_session(session)
    if status == "terminated":
        send_message(session, reason or "Your exam has been terminated by the proctor.", kind="system")
    return session


def sweep_sessions(now=None):
    """Auto-submit expired sessions and flag clients that stopped sending heartbeats."""
    now = now or utcnow()
    timeout = current_app.config["HEARTBEAT_TIMEOUT_SECONDS"]
    changed = False
    for s in ExamSession.query.filter(ExamSession.status == "in_progress").all():
        if s.remaining_seconds(now) <= 0:
            finish_session(s, "auto_submitted", now=now)
            changed = True
            continue
        stale = s.last_heartbeat and (now - s.last_heartbeat).total_seconds() > timeout
        if stale and not s.connection_lost:
            s.connection_lost = True
            record_event(s, "connection_lost", 1.0, {"last_heartbeat": s.last_heartbeat.isoformat()}, source="system", now=now)
            changed = True
    if changed:
        db.session.commit()


# ---------------------------------------------------------------- grading

_space = re.compile(r"\s+")


def _norm(text):
    return _space.sub(" ", str(text or "").strip().lower())


def grade_answer(question, response):
    """Return (marks, is_correct) or (None, None) if it needs manual grading."""
    if not question.is_auto_graded:
        return None, None
    marks = question.marks or 0
    key = question.answer
    if response is None or response == "" or response == []:
        return 0.0, False
    if question.qtype == "mcq":
        ok = str(response) == str(key)
        return (marks if ok else 0.0), ok
    if question.qtype == "truefalse":
        ok = str(response).lower() == str(key).lower()
        return (marks if ok else 0.0), ok
    if question.qtype == "multi":
        correct = {int(x) for x in (key or [])}
        chosen = {int(x) for x in (response if isinstance(response, list) else [response])}
        if not correct:
            return 0.0, False
        hits = len(chosen & correct)
        wrong = len(chosen - correct)
        frac = max(0.0, (hits - wrong) / len(correct))
        return round(marks * frac, 2), chosen == correct
    if question.qtype == "short":
        accepted = {_norm(a) for a in (key or [])}
        ok = _norm(response) in accepted
        return (marks if ok else 0.0), ok
    return None, None


def grade_session(session):
    exam = session.exam
    answers = {a.question_id: a for a in session.answers}
    total = 0.0
    manual = False
    for q in exam.questions:
        ans = answers.get(q.id)
        if exam.mode == "written" or not q.is_auto_graded:
            if ans is None or ans.marks_awarded is None:
                manual = True
            else:
                total += ans.marks_awarded
            continue
        if ans is None:
            ans = Answer(session_id=session.id, question_id=q.id, response=None)
            db.session.add(ans)
        marks, ok = grade_answer(q, ans.response)
        ans.marks_awarded = marks
        ans.is_correct = ok
        total += marks or 0
    session.max_score = exam.total_marks
    session.needs_manual_grading = manual
    session.score = round(total, 2)
    return session


# --------------------------------------------------------------- identity


def extract_single_face(img):
    """Return (embedding, error_message). Requires exactly one clear face."""
    from .ai import face_analyzer

    analyzer = face_analyzer()
    if img is None:
        return None, "We could not read the photo. Please try again."
    faces = analyzer.detect(img)
    if not faces:
        return None, "No face detected. Face the camera in good light and try again."
    main = faces[0]
    others = [f for f in faces[1:] if f.area >= 0.25 * main.area]
    if others:
        return None, "More than one face is visible. Make sure you are alone in the frame."
    h, w = img.shape[:2]
    if main.area < 0.02 * w * h:
        return None, "Your face is too small in the frame. Move closer to the camera."
    emb = analyzer.embed(img, main)
    if emb is None:
        return None, "Could not extract facial features. Try again with better lighting."
    return emb, None


def enroll_face(user, img):
    """Store the registered profile photo and face template for ``user``."""
    from .ai import face_analyzer

    emb, error = extract_single_face(img)
    if error:
        return False, error
    user.photo_path = save_file(f"users/{user.id}/profile-{stamp()}.jpg", img)
    user.face_embedding = emb
    user.face_backend = face_analyzer().backend
    return True, "Face enrolled successfully."
