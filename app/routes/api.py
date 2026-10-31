"""JSON API used by the exam room (student) and the live monitor (proctor)."""

from datetime import timedelta

from flask import Blueprint, abort, current_app, jsonify, request, url_for
from flask_login import current_user, login_required

from ..ai import engine, face_analyzer, object_detector
from ..ai.audio import analyze_audio_window
from ..ai.pipeline import analyze_frame, decode_image
from ..extensions import db
from ..models import Answer, ExamSession, ProctorEvent, ProctorMessage, ScorePoint, Snapshot, utcnow
from ..services import (
    audit,
    event_weights,
    finish_session,
    recompute_score,
    record_event,
    save_file,
    score_time,
    send_message,
    stamp,
    sweep_sessions,
    violation_count,
)
from ..utils import can_access_exam, staff_required

bp = Blueprint("api", __name__)

# Event types a browser is allowed to report; identity/composite/manual events are server-only.
CLIENT_EVENTS = {
    "face_missing", "left_camera_view", "multiple_faces", "head_turned", "repeated_head_movement", "gaze_away",
    "face_too_far", "phone_detected", "book_detected", "extra_device", "multiple_persons", "camera_blocked",
    "camera_disabled", "low_light", "mic_disabled", "tab_switch", "focus_lost", "fullscreen_exit", "clipboard",
    "blocked_shortcut", "devtools_open", "multiple_monitors", "window_resized", "right_click",
    "screenshot_attempt", "network_offline", "liveness_failed",
}

# consecutive non-matching frontal frames before raising "different person"
IDENTITY_MISMATCH_STREAK = {"sface": 2, "dlib": 2}
DEFAULT_MISMATCH_STREAK = 3
BASELINE_FRAMES = 5  # first frontal frames of a session that calibrate the student's own match level
# a frame must be this far below the student's own baseline (as well as below the global
# threshold scaled down by it) before it counts as a mismatch
BASELINE_MARGIN = {"sface": 0.25, "dlib": 0.06}
DEFAULT_BASELINE_MARGIN = 0.05


def _strikes(s):
    return {
        "violations": violation_count(s),
        "max_violations": s.exam.max_violations or 0,
        "reason": s.terminated_reason,
        "redirect": url_for("student.result", session_id=s.id) if s.is_finished else None,
    }


def _comparable_frame(result, client_pose):
    """Only near-frontal, single-face frames are fair to compare with the start photo."""
    if result["faces"] != 1:
        return False
    pose = result.get("pose") or {}
    if pose.get("profile"):
        return False
    for p in (pose, client_pose or {}):
        if abs(p.get("yaw") or 0) > 20 or abs(p.get("pitch") or 0) > 22:
            return False
    return True


def _check_identity(s, analyzer, identity, events):
    """Same-person check with a per-session baseline.

    The start photo is taken in the same room seconds before the exam, so the student's
    first frontal frames give their own typical similarity. A different person scores far
    below that baseline; natural variation (posture, light changing) does not.
    """
    sim = identity["similarity"]
    s.last_identity_similarity = sim
    backend = analyzer.backend
    margin = BASELINE_MARGIN.get(backend, DEFAULT_BASELINE_MARGIN)
    if (s.identity_samples or 0) < BASELINE_FRAMES and identity["match"]:
        n = s.identity_samples or 0
        s.identity_baseline = ((s.identity_baseline or 0) * n + sim) / (n + 1)
        s.identity_samples = n + 1
    limit = identity["threshold"]
    if s.identity_baseline is not None and (s.identity_samples or 0) >= BASELINE_FRAMES:
        limit = min(limit, s.identity_baseline - margin)
    identity["limit"] = round(limit, 3)
    identity["match"] = sim >= limit
    if identity["match"]:
        s.identity_mismatch_streak = 0
        return
    s.identity_mismatch_streak = (s.identity_mismatch_streak or 0) + 1
    if s.identity_mismatch_streak >= IDENTITY_MISMATCH_STREAK.get(backend, DEFAULT_MISMATCH_STREAK):
        gap = limit - sim
        confidence = min(0.95, 0.6 + gap * 2) * getattr(analyzer, "reliability", 1.0)
        events.append(("identity_mismatch", round(confidence, 2), {**identity, "streak": s.identity_mismatch_streak}))


def _student_session(session_id, allow_pending=False):
    s = db.get_or_404(ExamSession, session_id)
    if s.student_id != current_user.id:
        abort(403)
    return s


def _active_or_error(s):
    if s.status != "in_progress":
        return jsonify(ok=False, status=s.status, error="Session is not active."), 409
    return None


def _media(rel):
    return url_for("main.media", rel=rel) if rel else None


def _event_json(ev):
    return {
        "id": ev.id,
        "type": ev.event_type,
        "label": engine.catalog_entry(ev.event_type)["label"],
        "message": ev.message,
        "severity": ev.severity,
        "category": ev.category,
        "confidence": ev.confidence,
        "source": ev.source,
        "details": ev.details,
        "evidence": _media(ev.evidence_path),
        "review": ev.review_status,
        "at": ev.created_at.isoformat() + "Z",
    }


# ===================================================================== student


@bp.route("/session/<int:session_id>/status")
@login_required
def session_status(session_id):
    s = _student_session(session_id)
    return jsonify(status=s.status, redirect=url_for("student.room", session_id=s.id))


@bp.route("/session/<int:session_id>/heartbeat", methods=["POST"])
@login_required
def heartbeat(session_id):
    s = _student_session(session_id)
    now = utcnow()
    data = request.get_json(silent=True) or {}
    if s.status == "in_progress":
        if s.remaining_seconds(now) <= 0:
            finish_session(s, "auto_submitted", now=now)
        else:
            s.last_heartbeat = now
            s.connection_lost = False
            if data.get("client"):
                s.device_info = {**(s.device_info or {}), "live": data["client"]}
            recompute_score(s, now)
    msgs = (
        ProctorMessage.query.filter_by(session_id=s.id, delivered_at=None)
        .filter(ProctorMessage.kind != "reply")
        .order_by(ProctorMessage.created_at)
        .all()
    )
    out = [{"id": m.id, "kind": m.kind, "body": m.body, "at": m.created_at.isoformat() + "Z"} for m in msgs]
    for m in msgs:
        m.delivered_at = now
    db.session.commit()
    return jsonify(
        ok=True,
        status=s.status,
        remaining=s.remaining_seconds(now),
        risk=s.risk_level,
        score=s.fraud_score,
        messages=out,
        **_strikes(s),
    )


@bp.route("/session/<int:session_id>/events", methods=["POST"])
@login_required
def client_events(session_id):
    s = _student_session(session_id)
    err = _active_or_error(s)
    if err:
        return err
    data = request.get_json(silent=True) or {}
    items = data.get("events") or ([data] if data.get("type") else [])
    recorded = []
    for item in items[:20]:
        etype = item.get("type")
        if etype not in CLIENT_EVENTS:
            continue
        img = decode_image(item["image"]) if item.get("image") else None
        try:
            confidence = float(item.get("confidence", 0.8))
        except (TypeError, ValueError):
            confidence = 0.8
        details = item.get("details") if isinstance(item.get("details"), dict) else {}
        ev = record_event(s, etype, min(confidence, 0.95), details, source="client", image=img)
        if ev:
            recorded.append(ev.event_type)
        if s.status != "in_progress":
            break
    db.session.commit()
    return jsonify(ok=True, recorded=recorded, score=s.fraud_score, risk=s.risk_level, status=s.status, **_strikes(s))


@bp.route("/session/<int:session_id>/frame", methods=["POST"])
@login_required
def frame(session_id):
    s = _student_session(session_id)
    err = _active_or_error(s)
    if err:
        return err
    data = request.get_json(silent=True) or {}
    img = decode_image(data.get("image"))
    if img is None:
        return jsonify(ok=False, error="invalid image"), 400
    now = utcnow()
    analyzer = face_analyzer()
    reference = s.reference_embedding if s.identity_backend == analyzer.backend else None
    detector = object_detector() if s.exam.monitor_objects else None
    result = analyze_frame(img, analyzer, detector, reference, s.exam)

    save_file(f"sessions/{s.id}/latest.jpg", img)
    s.last_frame_at = now
    s.last_face_count = result["faces"]
    s.last_heartbeat = now
    s.connection_lost = False

    client_faces = data.get("faces")
    if isinstance(client_faces, int) and client_faces >= 1 and result["faces"] == 0:
        # browser landmarker still sees the face (e.g. turned or looking down); trust it over the
        # server's frontal-only detector instead of reporting "face missing"
        result["events"] = [e for e in result["events"] if e[0] not in ("face_missing", "head_turned")]

    identity = result.get("identity")
    client_pose = data.get("pose") if isinstance(data.get("pose"), dict) else None
    if identity and _comparable_frame(result, client_pose):
        _check_identity(s, analyzer, identity, result["events"])
    elif identity:
        identity["skipped"] = "face not frontal"  # turned faces score low; head_turned covers them

    keep = current_app.config["SNAPSHOT_KEEP_SECONDS"]
    if s.last_snapshot_at is None or (now - s.last_snapshot_at).total_seconds() >= keep:
        rel = save_file(f"sessions/{s.id}/snapshots/{stamp()}.jpg", img)
        db.session.add(
            Snapshot(
                session_id=s.id,
                path=rel,
                kind="periodic",
                face_count=result["faces"],
                identity_similarity=identity["similarity"] if identity else None,
                created_at=now,
            )
        )
        s.last_snapshot_at = now

    raised = []
    for etype, confidence, details in result["events"]:
        ev = record_event(s, etype, confidence, {"source": "server", **(details or {})}, source="server", image=img, now=now)
        if ev:
            raised.append(etype)
        if s.status != "in_progress":
            break
    if not raised:
        recompute_score(s, now)
    db.session.commit()
    return jsonify(
        ok=True,
        faces=result["faces"],
        identity=identity,
        pose=result["pose"],
        quality=result["quality"],
        objects=result["objects"],
        events=raised,
        detected=[e[0] for e in result["events"]],
        score=s.fraud_score,
        risk=s.risk_level,
        status=s.status,
        **_strikes(s),
    )


@bp.route("/session/<int:session_id>/audio", methods=["POST"])
@login_required
def audio(session_id):
    s = _student_session(session_id)
    err = _active_or_error(s)
    if err:
        return err
    if not s.exam.monitor_audio:
        return jsonify(ok=True, events=[])
    summary = request.get_json(silent=True) or {}
    raised = []
    for etype, confidence, details in analyze_audio_window(summary):
        ev = record_event(s, etype, confidence, details, source="server")
        if ev:
            raised.append(etype)
    db.session.commit()
    return jsonify(ok=True, events=raised, score=s.fraud_score, risk=s.risk_level, status=s.status, **_strikes(s))


@bp.route("/session/<int:session_id>/answer", methods=["POST"])
@login_required
def answer(session_id):
    s = _student_session(session_id)
    err = _active_or_error(s)
    if err:
        return err
    if s.remaining_seconds() <= 0:
        finish_session(s, "auto_submitted")
        db.session.commit()
        return jsonify(ok=False, status=s.status, error="Time is up."), 409
    data = request.get_json(silent=True) or {}
    qid = data.get("question_id")
    if not isinstance(qid, int) or qid not in (s.question_order or []):
        return jsonify(ok=False, error="unknown question"), 400
    ans = Answer.query.filter_by(session_id=s.id, question_id=qid).first()
    if ans is None:
        ans = Answer(session_id=s.id, question_id=qid)
        db.session.add(ans)
    if "response" in data:
        resp = data["response"]
        if isinstance(resp, str):
            resp = resp[:20000]
        ans.response = resp
    if "flagged" in data:
        ans.flagged = bool(data["flagged"])
    ans.updated_at = utcnow()
    db.session.commit()
    return jsonify(ok=True, saved_at=ans.updated_at.isoformat() + "Z")


@bp.route("/session/<int:session_id>/sheet", methods=["POST"])
@login_required
def answer_sheet(session_id):
    s = _student_session(session_id)
    err = _active_or_error(s)
    if err:
        return err
    data = request.get_json(silent=True) or {}
    if data.get("remove") is not None:
        sheets = [p for p in (s.answer_sheets or []) if p != data["remove"]]
        s.answer_sheets = sheets
        db.session.commit()
        return jsonify(ok=True, sheets=[{"path": p, "url": _media(p)} for p in sheets])
    img = decode_image(data.get("image"))
    if img is None:
        return jsonify(ok=False, error="Could not read the image."), 400
    if len(s.answer_sheets or []) >= 30:
        return jsonify(ok=False, error="Maximum 30 pages."), 400
    rel = save_file(f"sessions/{s.id}/sheets/{stamp()}.jpg", img)
    s.answer_sheets = (s.answer_sheets or []) + [rel]
    db.session.add(Snapshot(session_id=s.id, path=rel, kind="answer_sheet"))
    db.session.commit()
    return jsonify(ok=True, sheets=[{"path": p, "url": _media(p)} for p in s.answer_sheets])


@bp.route("/session/<int:session_id>/submit", methods=["POST"])
@login_required
def submit(session_id):
    s = _student_session(session_id)
    if s.status == "in_progress":
        finish_session(s, "submitted")
        audit("exam_submitted", f"session:{s.id}")
        db.session.commit()
    return jsonify(ok=True, status=s.status, redirect=url_for("student.result", session_id=s.id))


# ===================================================================== proctor


def _staff_session(session_id):
    s = db.get_or_404(ExamSession, session_id)
    if not can_access_exam(current_user, s.exam):
        abort(403)
    return s


def _visible_sessions_query():
    from ..models import Exam, exam_proctors

    q = ExamSession.query.join(Exam)
    if current_user.role == "proctor":
        q = q.outerjoin(exam_proctors, exam_proctors.c.exam_id == Exam.id).filter(
            (Exam.created_by_id == current_user.id) | (exam_proctors.c.user_id == current_user.id)
        ).distinct()
    return q


def session_card(s, now=None):
    now = now or utcnow()
    last = s.events[-1] if s.events else None
    return {
        "id": s.id,
        "student": s.student.name,
        "student_code": s.student.student_code,
        "initials": s.student.initials,
        "exam": s.exam.title,
        "exam_id": s.exam_id,
        "status": s.status,
        "status_label": s.status_label,
        "score": s.fraud_score or 0,
        "peak": s.peak_score or 0,
        "risk": s.risk_level,
        "remaining": s.remaining_seconds(now) if s.status == "in_progress" else 0,
        "faces": s.last_face_count,
        "identity": s.last_identity_similarity,
        "identity_verified": s.identity_verified,
        "connection_lost": s.connection_lost,
        "events": len(s.events),
        "last_event": _event_json(last) if last else None,
        "frame": url_for("main.media", rel=f"sessions/{s.id}/latest.jpg", t=int(s.last_frame_at.timestamp())) if s.last_frame_at else None,
        "reference": _media(s.reference_photo),
        "last_seen": int((now - s.last_heartbeat).total_seconds()) if s.last_heartbeat else None,
        "url": url_for("proctor.session_detail", session_id=s.id),
    }


@bp.route("/live")
@staff_required
def live_sessions():
    sweep_sessions()
    q = _visible_sessions_query().filter(ExamSession.status.in_(["in_progress", "pending_approval"]))
    exam_id = request.args.get("exam_id", type=int)
    if exam_id:
        q = q.filter(ExamSession.exam_id == exam_id)
    sessions = q.all()
    now = utcnow()
    cards = sorted((session_card(s, now) for s in sessions), key=lambda c: (c["status"] != "pending_approval", -c["score"]))
    counts = {lvl: sum(1 for c in cards if c["risk"] == lvl) for lvl in ("low", "medium", "high", "critical")}
    return jsonify(sessions=cards, counts=counts, total=len(cards), at=now.isoformat() + "Z")


@bp.route("/live/alerts")
@staff_required
def live_alerts():
    since = request.args.get("since", type=int) or 0
    window = utcnow() - timedelta(hours=6)
    q = (
        ProctorEvent.query.join(ExamSession)
        .filter(ProctorEvent.id > since, ProctorEvent.created_at >= window)
        .filter(ProctorEvent.severity.in_(["high", "critical"]))
        .filter(ExamSession.id.in_(_visible_sessions_query().with_entities(ExamSession.id)))
        .order_by(ProctorEvent.id.desc())
        .limit(40)
    )
    out = []
    for ev in q.all():
        item = _event_json(ev)
        item["session_id"] = ev.session_id
        item["student"] = ev.session.student.name
        item["url"] = url_for("proctor.session_detail", session_id=ev.session_id)
        out.append(item)
    return jsonify(alerts=out)


@bp.route("/live/session/<int:session_id>")
@staff_required
def live_session(session_id):
    s = _staff_session(session_id)
    since = request.args.get("since", type=int) or 0
    events = [e for e in s.events if e.id > since]
    points = ScorePoint.query.filter_by(session_id=s.id).order_by(ScorePoint.created_at).all()
    _, breakdown = engine.compute_score(s.events, score_time(s), event_weights())
    msgs = ProctorMessage.query.filter_by(session_id=s.id).order_by(ProctorMessage.created_at).all()
    return jsonify(
        session=session_card(s),
        events=[_event_json(e) for e in events],
        breakdown=breakdown,
        points=[{"t": p.created_at.isoformat() + "Z", "score": p.score} for p in points],
        messages=[
            {"kind": m.kind, "body": m.body, "at": m.created_at.isoformat() + "Z", "delivered": bool(m.delivered_at), "sender": m.sender.name if m.sender else "System"}
            for m in msgs
        ],
    )


@bp.route("/live/session/<int:session_id>/message", methods=["POST"])
@staff_required
def live_message(session_id):
    s = _staff_session(session_id)
    body = ((request.get_json(silent=True) or {}).get("body") or "").strip()
    if not body:
        return jsonify(ok=False, error="Message is empty."), 400
    send_message(s, body[:1000], kind="warning", sender=current_user)
    audit("warning_sent", f"session:{s.id}", body[:200])
    db.session.commit()
    return jsonify(ok=True)


@bp.route("/live/session/<int:session_id>/terminate", methods=["POST"])
@staff_required
def live_terminate(session_id):
    s = _staff_session(session_id)
    reason = ((request.get_json(silent=True) or {}).get("reason") or "").strip() or "Terminated by the proctor due to suspected malpractice."
    if not s.is_active:
        return jsonify(ok=False, error="Session is not active."), 409
    finish_session(s, "terminated", reason=reason)
    audit("session_terminated", f"session:{s.id}", reason)
    db.session.commit()
    return jsonify(ok=True, status=s.status)


@bp.route("/live/session/<int:session_id>/approve", methods=["POST"])
@staff_required
def live_approve(session_id):
    s = _staff_session(session_id)
    if s.status != "pending_approval":
        return jsonify(ok=False, error="Session is not awaiting approval."), 409
    s.status = "in_progress"
    s.identity_verified = True
    s.started_at = utcnow()  # the clock starts once approved
    s.last_heartbeat = utcnow()
    for ev in s.events:
        if ev.event_type == "identity_verification_failed" and ev.review_status == "pending":
            ev.review_status = "dismissed"
            ev.reviewed_by_id = current_user.id
            ev.reviewed_at = utcnow()
    recompute_score(s)
    send_message(s, "Your identity was verified by the proctor. Good luck!", kind="info", sender=current_user)
    audit("identity_approved", f"session:{s.id}")
    db.session.commit()
    return jsonify(ok=True)


@bp.route("/live/session/<int:session_id>/flag", methods=["POST"])
@staff_required
def live_flag(session_id):
    s = _staff_session(session_id)
    note = ((request.get_json(silent=True) or {}).get("note") or "").strip() or "Flagged by proctor"
    ev = record_event(s, "proctor_flag", 1.0, {"by": current_user.name}, source="proctor", message=note[:300])
    if ev:
        ev.review_status = "confirmed"
        ev.reviewed_by_id = current_user.id
    audit("session_flagged", f"session:{s.id}", note)
    db.session.commit()
    return jsonify(ok=True)


@bp.route("/live/session/<int:session_id>/extend", methods=["POST"])
@staff_required
def live_extend(session_id):
    s = _staff_session(session_id)
    try:
        minutes = int((request.get_json(silent=True) or {}).get("minutes", 0))
    except (TypeError, ValueError):
        minutes = 0
    if not s.is_active or not 1 <= minutes <= 120:
        return jsonify(ok=False, error="Choose 1–120 minutes for an active session."), 400
    s.extra_minutes = (s.extra_minutes or 0) + minutes
    send_message(s, f"You have been given {minutes} extra minute(s).", kind="info", sender=current_user)
    audit("time_extended", f"session:{s.id}", f"+{minutes} min")
    db.session.commit()
    return jsonify(ok=True, remaining=s.remaining_seconds())


@bp.route("/live/event/<int:event_id>/review", methods=["POST"])
@staff_required
def review_event(event_id):
    ev = db.get_or_404(ProctorEvent, event_id)
    s = ev.session
    if not can_access_exam(current_user, s.exam):
        abort(403)
    status = (request.get_json(silent=True) or {}).get("status")
    if status not in ("confirmed", "dismissed", "pending"):
        return jsonify(ok=False, error="invalid status"), 400
    ev.review_status = status
    ev.reviewed_by_id = current_user.id
    ev.reviewed_at = utcnow()
    recompute_score(s)
    audit("event_reviewed", f"event:{ev.id}", status)
    db.session.commit()
    return jsonify(ok=True, score=s.fraud_score, risk=s.risk_level)
