"""Demo data so the dashboards have something to show on first run."""

import random
from datetime import timedelta

from .extensions import db
from .models import Answer, Exam, ExamSession, ProctorEvent, Question, ScorePoint, User, utcnow
from .services import grade_session, recompute_score

AI_QUESTIONS = [
    ("mcq", "Which activation function outputs values strictly between 0 and 1?", ["ReLU", "Sigmoid", "Tanh", "Leaky ReLU"], 1),
    ("mcq", "YOLO is primarily used for which computer-vision task?", ["Image captioning", "Object detection", "Speech recognition", "Text classification"], 1),
    ("mcq", "Which loss is most common for multi-class classification?", ["Mean squared error", "Hinge loss", "Categorical cross-entropy", "Huber loss"], 2),
    ("multi", "Select all techniques that reduce overfitting.", ["Dropout", "Data augmentation", "Increasing model size without data", "L2 regularisation"], [0, 1, 3]),
    ("truefalse", "A convolutional layer shares its weights across spatial positions.", [], True),
    ("mcq", "What does the 'k' in k-NN stand for?", ["Number of layers", "Number of nearest neighbours", "Kernel size", "Number of clusters"], 1),
    ("short", "Name the optimisation algorithm that combines momentum and RMSProp.", [], ["adam", "adam optimizer", "adam optimiser"]),
    ("mcq", "Which metric is best for a highly imbalanced binary classification problem?", ["Accuracy", "F1-score", "Mean absolute error", "R²"], 1),
    ("truefalse", "Batch normalisation can only be used in recurrent networks.", [], False),
    ("essay", "Explain how head-pose estimation and eye-gaze tracking can be combined to detect a student looking away from the screen. Mention one source of false positives.", [], None),
]

WRITTEN_QUESTIONS = [
    ("essay", "Write an algorithm to reverse a singly linked list in place and state its time complexity.", [], None),
    ("essay", "Compare BFS and DFS. Give one application where each is preferred.", [], None),
    ("essay", "Insert 50, 30, 70, 20, 40, 60, 80 into an empty BST and draw the result. Show the in-order traversal.", [], None),
]

DEMO_STUDENTS = [
    ("Aarav Sharma", "CS2101"), ("Diya Patel", "CS2102"), ("Rohan Iyer", "CS2103"), ("Meera Nair", "CS2104"),
    ("Kabir Singh", "CS2105"), ("Ananya Rao", "CS2106"), ("Vikram Reddy", "CS2107"), ("Sara Khan", "CS2108"),
]


def seed_demo():
    if User.query.first() is not None:
        return
    rnd = random.Random(42)
    admin = _user("System Administrator", "admin@proctor.ai", "admin123", "admin")
    proctor = _user("Dr. Priya Menon", "proctor@proctor.ai", "proctor123", "proctor", department="Computer Science")
    _user("Demo Student", "student@proctor.ai", "student123", "student", code="CS2100", department="Computer Science")
    students = [
        _user(n, f"{n.split()[0].lower()}@proctor.ai", "student123", "student", code=c, department="Computer Science")
        for n, c in DEMO_STUDENTS
    ]
    db.session.flush()

    now = utcnow()
    ai_exam = Exam(
        title="AI & Machine Learning Fundamentals",
        subject="CS401 · Artificial Intelligence",
        description="Mid-semester assessment covering neural networks, computer vision and model evaluation.",
        instructions="Answer all questions. Keep your face visible, stay in full-screen and do not use other devices.",
        mode="online", duration_minutes=45, status="published", pass_percent=50,
        start_at=now - timedelta(days=3), end_at=now + timedelta(days=30),
        created_by_id=admin.id, auto_terminate=False,
    )
    ai_exam.proctors = [proctor]
    for pos, (qt, text, opts, ans) in enumerate(AI_QUESTIONS):
        ai_exam.questions.append(Question(qtype=qt, text=text, options=opts or (["True", "False"] if qt == "truefalse" else []), answer=ans, marks=5 if qt == "essay" else 2, position=pos))

    written = Exam(
        title="Data Structures — Written Paper",
        subject="CS202 · Data Structures",
        description="Answers are written on paper while the webcam monitors you. Photograph and upload every answer page before submitting.",
        instructions="Use blank A4 sheets. Write your student ID on every page. Keep your hands and paper visible to the camera.",
        mode="written", duration_minutes=60, status="published", pass_percent=40,
        start_at=now - timedelta(days=1), end_at=now + timedelta(days=30),
        created_by_id=proctor.id, monitor_objects=True,
    )
    for pos, (qt, text, opts, ans) in enumerate(WRITTEN_QUESTIONS):
        written.questions.append(Question(qtype=qt, text=text, options=[], answer=None, marks=10, position=pos))

    upcoming = Exam(
        title="Computer Networks — Final",
        subject="CS305 · Networks",
        description="Final exam. Opens next week.",
        mode="online", duration_minutes=90, status="published",
        start_at=now + timedelta(days=7), end_at=now + timedelta(days=7, hours=3),
        created_by_id=proctor.id,
    )
    upcoming.questions.append(Question(qtype="mcq", text="Which layer of the OSI model handles routing?", options=["Data link", "Network", "Transport", "Session"], answer=1, marks=2))
    db.session.add_all([ai_exam, written, upcoming])
    db.session.flush()

    profiles = [
        [],
        [("gaze_away", 3), ("head_turned", 2)],
        [("tab_switch", 2), ("focus_lost", 3), ("clipboard", 1)],
        [("voice_detected", 2), ("multiple_voices", 1), ("head_turned", 3)],
        [("phone_detected", 1), ("face_missing", 2), ("head_turned", 2)],
        [("multiple_faces", 1), ("identity_mismatch", 1), ("face_missing", 3)],
        [("gaze_away", 1)],
        [("fullscreen_exit", 2), ("tab_switch", 3), ("devtools_open", 1)],
    ]
    for i, (student, profile) in enumerate(zip(students, profiles)):
        started = now - timedelta(days=rnd.randint(0, 2), hours=rnd.randint(1, 8))
        s = ExamSession(
            exam_id=ai_exam.id, student_id=student.id, status="submitted", started_at=started,
            submitted_at=started + timedelta(minutes=rnd.randint(25, 44)), last_heartbeat=started,
            question_order=[q.id for q in ai_exam.questions], option_orders={}, identity_verified=True,
            identity_backend="demo", identity_score=round(rnd.uniform(0.55, 0.8), 2), liveness_passed=True,
        )
        db.session.add(s)
        db.session.flush()
        for q in ai_exam.questions:
            correct = rnd.random() < 0.72
            if q.qtype == "mcq":
                resp = q.answer if correct else (q.answer + 1) % len(q.options)
            elif q.qtype == "multi":
                resp = q.answer if correct else q.answer[:1]
            elif q.qtype == "truefalse":
                resp = q.answer if correct else (not q.answer)
            elif q.qtype == "short":
                resp = "Adam" if correct else "SGD"
            else:
                resp = "Head pose gives the direction of the face while gaze gives the eyes' direction; combining both..."
            db.session.add(Answer(session_id=s.id, question_id=q.id, response=resp))
        t = started
        for etype, count in profile:
            for _ in range(count):
                t = t + timedelta(minutes=rnd.randint(1, 6))
                db.session.add(ProctorEvent(
                    session_id=s.id, event_type=etype, category=None, severity=_sev(etype), weight=0,
                    confidence=round(rnd.uniform(0.7, 0.95), 2), source="server" if etype in ("identity_mismatch", "phone_detected") else "client",
                    message=None, details={"demo": True}, created_at=min(t, s.submitted_at),
                ))
                db.session.flush()
                db.session.add(ScorePoint(session_id=s.id, score=0, created_at=min(t, s.submitted_at)))
        db.session.flush()
        db.session.refresh(s)
        for ev in s.events:
            from .ai.engine import catalog_entry

            entry = catalog_entry(ev.event_type)
            ev.category, ev.weight, ev.message = entry["category"], entry["weight"], entry["label"]
        _rebuild_points(s)
        grade_session(s)
        if i == 4:
            s.verdict, s.verdict_notes, s.reviewed_by_id, s.reviewed_at = "malpractice", "Phone clearly visible in evidence frame.", proctor.id, now
        elif i == 0:
            s.verdict, s.reviewed_by_id, s.reviewed_at = "cleared", proctor.id, now
    db.session.commit()


def _rebuild_points(s):
    from .ai.engine import compute_score

    ScorePoint.query.filter_by(session_id=s.id).delete()
    events = sorted(s.events, key=lambda e: e.created_at)
    db.session.add(ScorePoint(session_id=s.id, score=0, created_at=s.started_at))
    for i, ev in enumerate(events):
        score, _ = compute_score(events[: i + 1], ev.created_at)
        db.session.add(ScorePoint(session_id=s.id, score=score, created_at=ev.created_at))
    recompute_score(s)


def _sev(etype):
    from .ai.engine import catalog_entry

    return catalog_entry(etype)["severity"]


def _user(name, email, password, role, code=None, department=None):
    u = User(name=name, email=email, role=role, student_code=code, department=department)
    u.set_password(password)
    db.session.add(u)
    return u
