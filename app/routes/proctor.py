"""Staff pages (admins and proctors): exams, live monitoring, review, grading, reports."""

import csv
import io
import json
from datetime import datetime

from flask import Blueprint, Response, abort, flash, redirect, render_template, request, url_for
from flask_login import current_user

from ..ai import engine, face_analyzer, object_detector
from ..extensions import db
from ..models import Answer, Exam, ExamSession, ProctorEvent, Question, User, utcnow
from ..services import audit, event_weights, grade_session, recompute_score, score_time, sweep_sessions
from ..utils import can_access_exam, staff_required, visible_exams_query
from .api import _visible_sessions_query

bp = Blueprint("proctor", __name__)


def _exam_or_403(exam_id):
    exam = db.get_or_404(Exam, exam_id)
    if not can_access_exam(current_user, exam):
        abort(403)
    return exam


def _session_or_403(session_id):
    s = db.get_or_404(ExamSession, session_id)
    if not can_access_exam(current_user, s.exam):
        abort(403)
    return s


# ------------------------------------------------------------------ dashboard


@bp.route("/")
@staff_required
def dashboard():
    sweep_sessions()
    exams = visible_exams_query(current_user).order_by(Exam.created_at.desc()).all()
    sessions_q = _visible_sessions_query()
    live = sessions_q.filter(ExamSession.status.in_(["in_progress", "pending_approval"])).all()
    to_review = (
        sessions_q.filter(ExamSession.status.in_(["submitted", "auto_submitted", "terminated"]))
        .filter(ExamSession.verdict.is_(None))
        .filter(ExamSession.risk_level.in_(["high", "critical"]) | (ExamSession.peak_score >= 40))
        .order_by(ExamSession.peak_score.desc())
        .limit(8)
        .all()
    )
    to_grade = sessions_q.filter(ExamSession.needs_manual_grading.is_(True)).count()
    recent_alerts = (
        ProctorEvent.query.filter(ProctorEvent.session_id.in_(sessions_q.with_entities(ExamSession.id)))
        .filter(ProctorEvent.severity.in_(["high", "critical"]))
        .order_by(ProctorEvent.created_at.desc())
        .limit(10)
        .all()
    )
    stats = {
        "exams": len(exams),
        "live": sum(1 for s in live if s.status == "in_progress"),
        "pending": sum(1 for s in live if s.status == "pending_approval"),
        "flagged_live": sum(1 for s in live if s.risk_level in ("high", "critical")),
        "to_review": len(to_review),
        "to_grade": to_grade,
    }
    return render_template(
        "proctor/dashboard.html", exams=exams[:6], live=live, to_review=to_review, alerts=recent_alerts, stats=stats
    )


@bp.route("/live")
@staff_required
def live():
    exams = visible_exams_query(current_user).filter(Exam.status == "published").order_by(Exam.title).all()
    return render_template("proctor/live.html", exams=exams, selected=request.args.get("exam_id", type=int))


# -------------------------------------------------------------------- review


@bp.route("/sessions")
@staff_required
def sessions():
    q = _visible_sessions_query()
    f = {k: request.args.get(k, "") for k in ("exam_id", "risk", "status", "verdict", "q")}
    if f["exam_id"].isdigit():
        q = q.filter(ExamSession.exam_id == int(f["exam_id"]))
    if f["risk"]:
        q = q.filter(ExamSession.risk_level == f["risk"])
    if f["status"]:
        q = q.filter(ExamSession.status == f["status"])
    if f["verdict"] == "none":
        q = q.filter(ExamSession.verdict.is_(None))
    elif f["verdict"]:
        q = q.filter(ExamSession.verdict == f["verdict"])
    if f["q"]:
        like = f"%{f['q']}%"
        q = q.join(User, User.id == ExamSession.student_id).filter(
            User.name.ilike(like) | User.email.ilike(like) | User.student_code.ilike(like)
        )
    rows = q.order_by(ExamSession.started_at.desc()).limit(500).all()
    exams = visible_exams_query(current_user).order_by(Exam.title).all()
    return render_template("proctor/sessions.html", rows=rows, exams=exams, f=f, statuses=ExamSession.STATUSES)


@bp.route("/session/<int:session_id>")
@staff_required
def session_detail(session_id):
    s = _session_or_403(session_id)
    weights = event_weights()
    _, breakdown = engine.compute_score(s.events, score_time(s), weights)
    summary = engine.summarize(s.events, weights)
    snapshots = [sn for sn in s.snapshots if sn.kind in ("periodic", "evidence")]
    snapshots.sort(key=lambda sn: sn.created_at, reverse=True)
    return render_template(
        "proctor/session.html",
        s=s,
        breakdown=breakdown,
        summary=summary,
        snapshots=snapshots[:60],
        backend=face_analyzer().backend,
        threshold=face_analyzer().threshold,
    )


@bp.route("/session/<int:session_id>/verdict", methods=["POST"])
@staff_required
def verdict(session_id):
    s = _session_or_403(session_id)
    v = request.form.get("verdict")
    if v not in ExamSession.VERDICTS:
        flash("Choose a verdict.", "error")
    else:
        s.verdict = v
        s.verdict_notes = request.form.get("notes", "").strip() or None
        s.reviewed_by_id = current_user.id
        s.reviewed_at = utcnow()
        if request.form.get("confirm_pending") and v != "cleared":
            for ev in s.events:
                if ev.review_status == "pending" and ev.severity in ("high", "critical"):
                    ev.review_status = "confirmed"
        recompute_score(s)
        audit("verdict", f"session:{s.id}", v)
        db.session.commit()
        flash(f"Verdict saved: {ExamSession.VERDICTS[v]}.", "success")
    return redirect(url_for("proctor.session_detail", session_id=s.id))


@bp.route("/session/<int:session_id>/report")
@staff_required
def session_report(session_id):
    s = _session_or_403(session_id)
    weights = event_weights()
    _, breakdown = engine.compute_score(s.events, score_time(s), weights)
    evidence = [e for e in s.events if e.evidence_path and e.review_status != "dismissed"][:24]
    return render_template(
        "proctor/report.html", s=s, breakdown=breakdown, summary=engine.summarize(s.events, weights), evidence=evidence
    )


@bp.route("/session/<int:session_id>/grade", methods=["GET", "POST"])
@staff_required
def grade(session_id):
    s = _session_or_403(session_id)
    answers = {a.question_id: a for a in s.answers}
    manual = [q for q in s.exam.questions if s.exam.mode == "written" or not q.is_auto_graded]
    if request.method == "POST":
        for q in manual:
            raw = request.form.get(f"marks-{q.id}", "").strip()
            if raw == "":
                continue
            try:
                marks = max(0.0, min(float(raw), q.marks))
            except ValueError:
                continue
            a = answers.get(q.id)
            if a is None:
                a = Answer(session_id=s.id, question_id=q.id)
                db.session.add(a)
                answers[q.id] = a
            a.marks_awarded = marks
            a.feedback = request.form.get(f"feedback-{q.id}", "").strip() or None
        db.session.flush()
        db.session.refresh(s)
        grade_session(s)
        audit("graded", f"session:{s.id}", f"score={s.score}")
        db.session.commit()
        flash("Marks saved." + (" Some questions are still ungraded." if s.needs_manual_grading else ""), "success")
        return redirect(url_for("proctor.grade", session_id=s.id))
    return render_template("proctor/grade.html", s=s, manual=manual, answers=answers)


# --------------------------------------------------------------------- exams


@bp.route("/exams")
@staff_required
def exams():
    rows = visible_exams_query(current_user).order_by(Exam.created_at.desc()).all()
    return render_template("proctor/exams.html", exams=rows)


def _parse_dt(value):
    if not value:
        return None
    for fmt in ("%Y-%m-%dT%H:%M", "%Y-%m-%d %H:%M", "%Y-%m-%dT%H:%M:%S"):
        try:
            return datetime.strptime(value, fmt)
        except ValueError:
            continue
    return None


def _validate_questions(raw):
    try:
        items = json.loads(raw or "[]")
    except ValueError:
        return None, ["Questions could not be read."]
    errors, clean = [], []
    for n, item in enumerate(items, 1):
        qtype = item.get("type")
        text = (item.get("text") or "").strip()
        if qtype not in Question.TYPES:
            errors.append(f"Question {n}: unknown type.")
            continue
        if not text:
            errors.append(f"Question {n}: text is empty.")
            continue
        raw_options = [str(o).strip() for o in (item.get("options") or [])]
        remap = {old: new for new, old in enumerate(i for i, o in enumerate(raw_options) if o)}
        options = [o for o in raw_options if o]
        answer = item.get("answer")
        if qtype == "mcq" and isinstance(answer, int):
            answer = remap.get(answer)
        elif qtype == "multi":
            answer = [remap[a] for a in (answer or []) if isinstance(a, int) and a in remap]
        if qtype in ("mcq", "multi"):
            if len(options) < 2:
                errors.append(f"Question {n}: add at least two options.")
                continue
            if qtype == "mcq":
                if not isinstance(answer, int) or not 0 <= answer < len(options):
                    errors.append(f"Question {n}: mark the correct option.")
                    continue
            else:
                answer = sorted({int(a) for a in (answer or []) if isinstance(a, int) and 0 <= a < len(options)})
                if not answer:
                    errors.append(f"Question {n}: mark at least one correct option.")
                    continue
        elif qtype == "truefalse":
            options = ["True", "False"]
            answer = bool(answer)
        elif qtype == "short":
            options = []
            answer = [a.strip() for a in (answer or []) if isinstance(a, str) and a.strip()]
            if not answer:
                errors.append(f"Question {n}: add at least one accepted answer.")
                continue
        else:
            options, answer = [], None
        try:
            marks = max(0.0, float(item.get("marks") or 1))
        except (TypeError, ValueError):
            marks = 1.0
        clean.append(
            {"id": item.get("id"), "qtype": qtype, "text": text, "options": options, "answer": answer, "marks": marks,
             "explanation": (item.get("explanation") or "").strip() or None}
        )
    return clean, errors


def _apply_exam_form(exam, form):
    exam.title = form.get("title", "").strip()
    exam.subject = form.get("subject", "").strip() or None
    exam.description = form.get("description", "").strip() or None
    exam.instructions = form.get("instructions", "").strip() or None
    exam.mode = form.get("mode") if form.get("mode") in Exam.MODES else "online"
    exam.duration_minutes = max(1, form.get("duration_minutes", type=int) or 60)
    exam.start_at = _parse_dt(form.get("start_at"))
    exam.end_at = _parse_dt(form.get("end_at"))
    exam.pass_percent = min(100, max(0, form.get("pass_percent", type=int) or 40))
    exam.status = form.get("status") if form.get("status") in ("draft", "published", "archived") else "draft"
    exam.access_code = form.get("access_code", "").strip() or None
    exam.max_attempts = max(1, form.get("max_attempts", type=int) or 1)
    for flag in (
        "shuffle_questions", "shuffle_options", "show_results", "require_identity", "require_liveness",
        "monitor_audio", "monitor_objects", "enforce_fullscreen", "auto_terminate",
    ):
        setattr(exam, flag, bool(form.get(flag)))
    exam.max_violations = min(100, max(0, form.get("max_violations", type=int) or 0))
    exam.warn_threshold = min(100, max(5, form.get("warn_threshold", type=int) or 40))
    exam.critical_threshold = min(100, max(exam.warn_threshold + 1, form.get("critical_threshold", type=int) or 75))


@bp.route("/exams/new", methods=["GET", "POST"])
@bp.route("/exams/<int:exam_id>/edit", methods=["GET", "POST"])
@staff_required
def exam_edit(exam_id=None):
    exam = _exam_or_403(exam_id) if exam_id else Exam(created_by_id=current_user.id)
    proctors = User.query.filter_by(role="proctor", is_active_flag=True).order_by(User.name).all()
    if request.method == "POST":
        _apply_exam_form(exam, request.form)
        questions, errors = _validate_questions(request.form.get("questions_json"))
        if not exam.title:
            errors.insert(0, "Exam title is required.")
        if exam.start_at and exam.end_at and exam.end_at <= exam.start_at:
            errors.append("The end time must be after the start time.")
        if exam.status == "published" and not questions:
            errors.append("Add at least one question before publishing.")
        if errors:
            for e in errors:
                flash(e, "error")
            db.session.rollback()
            return render_template(
                "proctor/exam_edit.html", exam=exam, proctors=proctors, questions_json=request.form.get("questions_json", "[]")
            ), 400
        if exam.id is None:
            db.session.add(exam)
            db.session.flush()
        selected = set(request.form.getlist("proctors", type=int))
        exam.proctors = [p for p in proctors if p.id in selected]
        has_sessions = ExamSession.query.filter_by(exam_id=exam.id).count() > 0
        existing = {q.id: q for q in exam.questions}
        keep = set()
        for pos, item in enumerate(questions):
            q = existing.get(item["id"]) if isinstance(item["id"], int) else None
            if q is None:
                q = Question(exam_id=exam.id)
                exam.questions.append(q)
            q.qtype, q.text, q.options, q.answer = item["qtype"], item["text"], item["options"], item["answer"]
            q.marks, q.position, q.explanation = item["marks"], pos, item["explanation"]
            if q.id:
                keep.add(q.id)
        for qid, q in existing.items():
            if qid not in keep:
                if has_sessions:
                    flash("Questions with recorded attempts cannot be deleted; they were kept.", "info")
                    continue
                exam.questions.remove(q)
        audit("exam_saved", f"exam:{exam.id}", exam.title)
        db.session.commit()
        flash("Exam saved.", "success")
        return redirect(url_for("proctor.exam_edit", exam_id=exam.id))
    questions_json = json.dumps(
        [
            {"id": q.id, "type": q.qtype, "text": q.text, "options": q.options or [], "answer": q.answer,
             "marks": q.marks, "explanation": q.explanation or ""}
            for q in exam.questions
        ]
    )
    return render_template("proctor/exam_edit.html", exam=exam, proctors=proctors, questions_json=questions_json)


@bp.route("/exams/<int:exam_id>/delete", methods=["POST"])
@staff_required
def exam_delete(exam_id):
    exam = _exam_or_403(exam_id)
    if any(s.is_active for s in exam.sessions):
        flash("This exam has candidates writing right now and cannot be deleted.", "error")
        return redirect(url_for("proctor.exams"))
    if exam.sessions:
        exam.status = "archived"
        flash("Exam has attempts, so it was archived instead of deleted.", "info")
    else:
        db.session.delete(exam)
        flash("Exam deleted.", "success")
    audit("exam_deleted", f"exam:{exam_id}")
    db.session.commit()
    return redirect(url_for("proctor.exams"))


@bp.route("/exams/<int:exam_id>/duplicate", methods=["POST"])
@staff_required
def exam_duplicate(exam_id):
    src = _exam_or_403(exam_id)
    copy = Exam(created_by_id=current_user.id, status="draft")
    for col in Exam.__table__.columns:
        if col.name not in ("id", "created_by_id", "status", "created_at", "updated_at"):
            setattr(copy, col.name, getattr(src, col.name))
    copy.title = f"{src.title} (copy)"
    for q in src.questions:
        copy.questions.append(
            Question(qtype=q.qtype, text=q.text, options=q.options, answer=q.answer, marks=q.marks, position=q.position, explanation=q.explanation)
        )
    db.session.add(copy)
    db.session.commit()
    flash("Exam duplicated as a draft.", "success")
    return redirect(url_for("proctor.exam_edit", exam_id=copy.id))


@bp.route("/exams/<int:exam_id>/import", methods=["POST"])
@staff_required
def exam_import(exam_id):
    """CSV import: type,text,options (| separated),answer,marks."""
    exam = _exam_or_403(exam_id)
    upload = request.files.get("file")
    if not upload:
        flash("Choose a CSV file.", "error")
        return redirect(url_for("proctor.exam_edit", exam_id=exam.id))
    text = upload.read().decode("utf-8-sig", errors="replace")
    reader = csv.DictReader(io.StringIO(text))
    added, items = 0, []
    for row in reader:
        row = {(k or "").strip().lower(): (v or "").strip() for k, v in row.items()}
        qtype = row.get("type", "mcq") or "mcq"
        options = [o.strip() for o in row.get("options", "").split("|") if o.strip()]
        ans = row.get("answer", "")
        if qtype == "mcq":
            answer = _option_index(ans, options)
        elif qtype == "multi":
            answer = [i for i in (_option_index(a, options) for a in ans.split("|")) if i is not None]
        elif qtype == "truefalse":
            answer = ans.lower() in ("true", "t", "1", "yes")
        elif qtype == "short":
            answer = [a.strip() for a in ans.split("|") if a.strip()]
        else:
            answer = None
        items.append({"type": qtype, "text": row.get("text", ""), "options": options, "answer": answer, "marks": row.get("marks") or 1})
    clean, errors = _validate_questions(json.dumps(items))
    start = len(exam.questions)
    for pos, item in enumerate(clean):
        exam.questions.append(
            Question(qtype=item["qtype"], text=item["text"], options=item["options"], answer=item["answer"], marks=item["marks"], position=start + pos)
        )
        added += 1
    db.session.commit()
    for e in errors[:5]:
        flash(e, "error")
    flash(f"Imported {added} question(s).", "success" if added else "info")
    return redirect(url_for("proctor.exam_edit", exam_id=exam.id))


def _option_index(value, options):
    value = value.strip()
    if value.isdigit():
        i = int(value) - 1  # 1-based in CSV
        return i if 0 <= i < len(options) else None
    if len(value) == 1 and value.isalpha():
        i = ord(value.upper()) - 65
        return i if 0 <= i < len(options) else None
    lowered = [o.lower() for o in options]
    return lowered.index(value.lower()) if value.lower() in lowered else None


@bp.route("/exams/<int:exam_id>/report")
@staff_required
def exam_report(exam_id):
    exam = _exam_or_403(exam_id)
    rows = sorted(exam.sessions, key=lambda s: s.started_at, reverse=True)
    finished = [s for s in rows if s.is_finished]
    graded = [s for s in finished if s.percent is not None and not s.needs_manual_grading]
    event_counts = {}
    for s in rows:
        for e in s.events:
            if e.review_status != "dismissed":
                event_counts[e.event_type] = event_counts.get(e.event_type, 0) + 1
    stats = {
        "attempts": len(rows),
        "finished": len(finished),
        "avg_score": round(sum(s.percent for s in graded) / len(graded), 1) if graded else None,
        "pass_rate": round(100 * sum(1 for s in graded if s.passed) / len(graded)) if graded else None,
        "avg_fraud": round(sum(s.peak_score or 0 for s in rows) / len(rows), 1) if rows else None,
        "flagged": sum(1 for s in rows if s.risk_level in ("high", "critical") or (s.peak_score or 0) >= exam.warn_threshold),
        "terminated": sum(1 for s in rows if s.status == "terminated"),
        "malpractice": sum(1 for s in rows if s.verdict == "malpractice"),
    }
    top_events = sorted(event_counts.items(), key=lambda kv: -kv[1])[:10]
    return render_template("proctor/exam_report.html", exam=exam, rows=rows, stats=stats, top_events=top_events)


@bp.route("/exams/<int:exam_id>/export.csv")
@staff_required
def exam_export(exam_id):
    exam = _exam_or_403(exam_id)
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow([
        "Session", "Student", "Email", "Student ID", "Status", "Started", "Submitted", "Score", "Max", "Percent",
        "Passed", "Fraud score", "Peak fraud score", "Risk", "Events", "Identity verified", "Verdict",
    ])
    for s in exam.sessions:
        w.writerow([
            s.id, s.student.name, s.student.email, s.student.student_code or "", s.status_label,
            s.started_at.isoformat() if s.started_at else "", s.submitted_at.isoformat() if s.submitted_at else "",
            s.score if s.score is not None else "", s.max_score or "", s.percent if s.percent is not None else "",
            {True: "yes", False: "no", None: ""}[s.passed], s.fraud_score, s.peak_score, s.risk_level,
            len(s.events), "yes" if s.identity_verified else "no", s.verdict or "",
        ])
    audit("export", f"exam:{exam.id}")
    db.session.commit()
    name = "".join(c if c.isalnum() else "-" for c in exam.title.lower())[:40]
    return Response(buf.getvalue(), mimetype="text/csv", headers={"Content-Disposition": f"attachment; filename={name}-results.csv"})


@bp.route("/system")
@staff_required
def system():
    return render_template(
        "proctor/system.html",
        face_backend=face_analyzer().backend,
        face_threshold=face_analyzer().threshold,
        yolo=object_detector().available,
    )
