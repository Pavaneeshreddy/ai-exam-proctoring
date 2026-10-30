import json
from datetime import datetime, timedelta, timezone

import numpy as np
from flask_login import UserMixin
from werkzeug.security import check_password_hash, generate_password_hash

from .extensions import db, login_manager


def utcnow():
    """Naive UTC timestamp (the database stores UTC without tz info)."""
    return datetime.now(timezone.utc).replace(tzinfo=None)


class JSONText(db.TypeDecorator):
    """Stores Python objects as JSON text (portable across SQLite and MySQL)."""

    impl = db.Text
    cache_ok = True

    def process_bind_param(self, value, dialect):
        return None if value is None else json.dumps(value)

    def process_result_value(self, value, dialect):
        return None if value is None else json.loads(value)


def _encode_embedding(vec):
    if vec is None:
        return None
    return np.asarray(vec, dtype=np.float32).tobytes()


def _decode_embedding(raw):
    if not raw:
        return None
    return np.frombuffer(raw, dtype=np.float32)


exam_proctors = db.Table(
    "exam_proctors",
    db.Column("exam_id", db.Integer, db.ForeignKey("exams.id", ondelete="CASCADE"), primary_key=True),
    db.Column("user_id", db.Integer, db.ForeignKey("users.id", ondelete="CASCADE"), primary_key=True),
)


class User(UserMixin, db.Model):
    __tablename__ = "users"

    ROLES = ("admin", "proctor", "student")

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(120), nullable=False)
    email = db.Column(db.String(160), unique=True, nullable=False, index=True)
    password_hash = db.Column(db.String(255), nullable=False)
    role = db.Column(db.String(20), nullable=False, default="student", index=True)
    student_code = db.Column(db.String(40))
    department = db.Column(db.String(120))
    photo_path = db.Column(db.String(255))
    face_embedding_raw = db.Column(db.LargeBinary)
    face_backend = db.Column(db.String(30))
    is_active_flag = db.Column(db.Boolean, default=True, nullable=False)
    created_at = db.Column(db.DateTime, default=utcnow)
    last_login_at = db.Column(db.DateTime)

    sessions = db.relationship("ExamSession", back_populates="student", foreign_keys="ExamSession.student_id")

    def set_password(self, password):
        self.password_hash = generate_password_hash(password)

    def check_password(self, password):
        return check_password_hash(self.password_hash, password)

    @property
    def is_active(self):
        return self.is_active_flag

    @property
    def face_embedding(self):
        return _decode_embedding(self.face_embedding_raw)

    @face_embedding.setter
    def face_embedding(self, vec):
        self.face_embedding_raw = _encode_embedding(vec)

    @property
    def is_staff(self):
        return self.role in ("admin", "proctor")

    @property
    def initials(self):
        parts = [p for p in self.name.split() if p]
        return "".join(p[0] for p in parts[:2]).upper() or "?"


@login_manager.user_loader
def load_user(user_id):
    return db.session.get(User, int(user_id))


class Exam(db.Model):
    __tablename__ = "exams"

    MODES = {"online": "Online answers", "written": "Written on paper (camera-monitored)"}

    id = db.Column(db.Integer, primary_key=True)
    title = db.Column(db.String(200), nullable=False)
    subject = db.Column(db.String(120))
    description = db.Column(db.Text)
    instructions = db.Column(db.Text)
    mode = db.Column(db.String(20), default="online", nullable=False)
    duration_minutes = db.Column(db.Integer, default=60, nullable=False)
    start_at = db.Column(db.DateTime)
    end_at = db.Column(db.DateTime)
    pass_percent = db.Column(db.Integer, default=40)
    status = db.Column(db.String(20), default="draft", index=True)  # draft|published|archived
    access_code = db.Column(db.String(40))
    shuffle_questions = db.Column(db.Boolean, default=True)
    shuffle_options = db.Column(db.Boolean, default=True)
    show_results = db.Column(db.Boolean, default=True)
    max_attempts = db.Column(db.Integer, default=1)

    # Proctoring policy
    require_identity = db.Column(db.Boolean, default=True)
    require_liveness = db.Column(db.Boolean, default=True)
    monitor_audio = db.Column(db.Boolean, default=True)
    monitor_objects = db.Column(db.Boolean, default=True)
    enforce_fullscreen = db.Column(db.Boolean, default=True)
    warn_threshold = db.Column(db.Integer, default=40)
    critical_threshold = db.Column(db.Integer, default=75)
    auto_terminate = db.Column(db.Boolean, default=False)

    created_by_id = db.Column(db.Integer, db.ForeignKey("users.id"))
    created_at = db.Column(db.DateTime, default=utcnow)
    updated_at = db.Column(db.DateTime, default=utcnow, onupdate=utcnow)

    created_by = db.relationship("User", foreign_keys=[created_by_id])
    proctors = db.relationship("User", secondary=exam_proctors, lazy="subquery")
    questions = db.relationship(
        "Question", back_populates="exam", cascade="all, delete-orphan", order_by="Question.position"
    )
    sessions = db.relationship("ExamSession", back_populates="exam", cascade="all, delete-orphan")

    @property
    def total_marks(self):
        return sum(q.marks for q in self.questions)

    def is_open(self, now=None):
        now = now or utcnow()
        if self.status != "published":
            return False
        if self.start_at and now < self.start_at:
            return False
        if self.end_at and now > self.end_at:
            return False
        return True

    def availability(self, now=None):
        now = now or utcnow()
        if self.status != "published":
            return self.status
        if self.start_at and now < self.start_at:
            return "upcoming"
        if self.end_at and now > self.end_at:
            return "closed"
        return "open"


class Question(db.Model):
    __tablename__ = "questions"

    TYPES = {
        "mcq": "Single choice",
        "multi": "Multiple choice",
        "truefalse": "True / False",
        "short": "Short answer",
        "essay": "Essay / long answer",
    }

    id = db.Column(db.Integer, primary_key=True)
    exam_id = db.Column(db.Integer, db.ForeignKey("exams.id", ondelete="CASCADE"), nullable=False, index=True)
    qtype = db.Column(db.String(20), default="mcq", nullable=False)
    text = db.Column(db.Text, nullable=False)
    options = db.Column(JSONText, default=list)  # list[str]
    answer = db.Column(JSONText)  # mcq: int, multi: list[int], truefalse: bool, short: list[str]
    marks = db.Column(db.Float, default=1.0)
    position = db.Column(db.Integer, default=0)
    explanation = db.Column(db.Text)

    exam = db.relationship("Exam", back_populates="questions")

    @property
    def is_auto_graded(self):
        return self.qtype in ("mcq", "multi", "truefalse", "short")


class ExamSession(db.Model):
    __tablename__ = "exam_sessions"

    STATUSES = {
        "pending_approval": "Awaiting proctor approval",
        "in_progress": "In progress",
        "submitted": "Submitted",
        "auto_submitted": "Auto-submitted (time up)",
        "terminated": "Terminated",
    }
    VERDICTS = {"cleared": "Cleared", "suspicious": "Suspicious", "malpractice": "Malpractice confirmed"}

    id = db.Column(db.Integer, primary_key=True)
    exam_id = db.Column(db.Integer, db.ForeignKey("exams.id", ondelete="CASCADE"), nullable=False, index=True)
    student_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False, index=True)
    status = db.Column(db.String(30), default="in_progress", index=True)
    started_at = db.Column(db.DateTime, default=utcnow)
    submitted_at = db.Column(db.DateTime)
    last_heartbeat = db.Column(db.DateTime, default=utcnow)
    question_order = db.Column(JSONText)
    option_orders = db.Column(JSONText)  # {question_id: [original indices]}

    reference_photo = db.Column(db.String(255))
    reference_embedding_raw = db.Column(db.LargeBinary)
    identity_backend = db.Column(db.String(30))
    identity_score = db.Column(db.Float)  # similarity vs enrolled photo at start
    identity_verified = db.Column(db.Boolean, default=False)
    liveness_passed = db.Column(db.Boolean)
    last_identity_similarity = db.Column(db.Float)
    identity_mismatch_streak = db.Column(db.Integer, default=0)
    last_face_count = db.Column(db.Integer)
    last_frame_at = db.Column(db.DateTime)
    last_snapshot_at = db.Column(db.DateTime)

    fraud_score = db.Column(db.Float, default=0.0)
    peak_score = db.Column(db.Float, default=0.0)
    risk_level = db.Column(db.String(20), default="low", index=True)
    warnings_sent = db.Column(db.Integer, default=0)
    extra_minutes = db.Column(db.Integer, default=0)
    connection_lost = db.Column(db.Boolean, default=False)

    score = db.Column(db.Float)
    max_score = db.Column(db.Float)
    needs_manual_grading = db.Column(db.Boolean, default=False)
    answer_sheets = db.Column(JSONText, default=list)

    verdict = db.Column(db.String(20))
    verdict_notes = db.Column(db.Text)
    reviewed_by_id = db.Column(db.Integer, db.ForeignKey("users.id"))
    reviewed_at = db.Column(db.DateTime)
    terminated_reason = db.Column(db.Text)

    ip_address = db.Column(db.String(64))
    user_agent = db.Column(db.String(400))
    device_info = db.Column(JSONText)

    exam = db.relationship("Exam", back_populates="sessions")
    student = db.relationship("User", back_populates="sessions", foreign_keys=[student_id])
    reviewed_by = db.relationship("User", foreign_keys=[reviewed_by_id])
    answers = db.relationship("Answer", back_populates="session", cascade="all, delete-orphan")
    events = db.relationship(
        "ProctorEvent", back_populates="session", cascade="all, delete-orphan", order_by="ProctorEvent.created_at"
    )
    snapshots = db.relationship("Snapshot", back_populates="session", cascade="all, delete-orphan")
    messages = db.relationship("ProctorMessage", back_populates="session", cascade="all, delete-orphan")
    score_points = db.relationship("ScorePoint", back_populates="session", cascade="all, delete-orphan")

    @property
    def reference_embedding(self):
        return _decode_embedding(self.reference_embedding_raw)

    @reference_embedding.setter
    def reference_embedding(self, vec):
        self.reference_embedding_raw = _encode_embedding(vec)

    @property
    def is_active(self):
        return self.status in ("in_progress", "pending_approval")

    @property
    def is_finished(self):
        return self.status in ("submitted", "auto_submitted", "terminated")

    @property
    def deadline(self):
        end = self.started_at + timedelta(minutes=self.exam.duration_minutes + (self.extra_minutes or 0))
        if self.exam.end_at and self.exam.end_at + timedelta(minutes=self.extra_minutes or 0) < end:
            end = self.exam.end_at + timedelta(minutes=self.extra_minutes or 0)
        return end

    def remaining_seconds(self, now=None):
        now = now or utcnow()
        return max(0, int((self.deadline - now).total_seconds()))

    @property
    def percent(self):
        if self.score is None or not self.max_score:
            return None
        return round(100.0 * self.score / self.max_score, 1)

    @property
    def passed(self):
        pct = self.percent
        return None if pct is None else pct >= (self.exam.pass_percent or 0)

    @property
    def status_label(self):
        return self.STATUSES.get(self.status, self.status)


class Answer(db.Model):
    __tablename__ = "answers"
    __table_args__ = (db.UniqueConstraint("session_id", "question_id", name="uq_answer_session_question"),)

    id = db.Column(db.Integer, primary_key=True)
    session_id = db.Column(db.Integer, db.ForeignKey("exam_sessions.id", ondelete="CASCADE"), nullable=False)
    question_id = db.Column(db.Integer, db.ForeignKey("questions.id", ondelete="CASCADE"), nullable=False)
    response = db.Column(JSONText)
    flagged = db.Column(db.Boolean, default=False)
    marks_awarded = db.Column(db.Float)
    is_correct = db.Column(db.Boolean)
    feedback = db.Column(db.Text)
    updated_at = db.Column(db.DateTime, default=utcnow, onupdate=utcnow)

    session = db.relationship("ExamSession", back_populates="answers")
    question = db.relationship("Question")


class ProctorEvent(db.Model):
    __tablename__ = "proctor_events"

    id = db.Column(db.Integer, primary_key=True)
    session_id = db.Column(db.Integer, db.ForeignKey("exam_sessions.id", ondelete="CASCADE"), nullable=False, index=True)
    event_type = db.Column(db.String(40), nullable=False, index=True)
    category = db.Column(db.String(20))
    severity = db.Column(db.String(10), nullable=False)  # info|low|medium|high|critical
    weight = db.Column(db.Float, default=0.0)
    confidence = db.Column(db.Float, default=1.0)
    source = db.Column(db.String(20), default="client")  # client|server|system|proctor
    message = db.Column(db.String(300))
    details = db.Column(JSONText)
    evidence_path = db.Column(db.String(255))
    created_at = db.Column(db.DateTime, default=utcnow, index=True)
    review_status = db.Column(db.String(20), default="pending")  # pending|confirmed|dismissed
    reviewed_by_id = db.Column(db.Integer, db.ForeignKey("users.id"))
    reviewed_at = db.Column(db.DateTime)

    session = db.relationship("ExamSession", back_populates="events")
    reviewed_by = db.relationship("User")


class Snapshot(db.Model):
    __tablename__ = "snapshots"

    id = db.Column(db.Integer, primary_key=True)
    session_id = db.Column(db.Integer, db.ForeignKey("exam_sessions.id", ondelete="CASCADE"), nullable=False, index=True)
    path = db.Column(db.String(255), nullable=False)
    kind = db.Column(db.String(20), default="periodic")  # reference|periodic|evidence|answer_sheet
    face_count = db.Column(db.Integer)
    identity_similarity = db.Column(db.Float)
    created_at = db.Column(db.DateTime, default=utcnow)

    session = db.relationship("ExamSession", back_populates="snapshots")


class ScorePoint(db.Model):
    __tablename__ = "score_points"

    id = db.Column(db.Integer, primary_key=True)
    session_id = db.Column(db.Integer, db.ForeignKey("exam_sessions.id", ondelete="CASCADE"), nullable=False, index=True)
    score = db.Column(db.Float, nullable=False)
    created_at = db.Column(db.DateTime, default=utcnow)

    session = db.relationship("ExamSession", back_populates="score_points")


class ProctorMessage(db.Model):
    __tablename__ = "proctor_messages"

    id = db.Column(db.Integer, primary_key=True)
    session_id = db.Column(db.Integer, db.ForeignKey("exam_sessions.id", ondelete="CASCADE"), nullable=False, index=True)
    sender_id = db.Column(db.Integer, db.ForeignKey("users.id"))
    kind = db.Column(db.String(20), default="warning")  # warning|info|system|reply
    body = db.Column(db.Text, nullable=False)
    created_at = db.Column(db.DateTime, default=utcnow)
    delivered_at = db.Column(db.DateTime)

    session = db.relationship("ExamSession", back_populates="messages")
    sender = db.relationship("User")


class AuditLog(db.Model):
    __tablename__ = "audit_logs"

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"))
    action = db.Column(db.String(80), nullable=False)
    target = db.Column(db.String(120))
    details = db.Column(db.Text)
    ip_address = db.Column(db.String(64))
    created_at = db.Column(db.DateTime, default=utcnow, index=True)

    user = db.relationship("User")


class Setting(db.Model):
    __tablename__ = "settings"

    key = db.Column(db.String(80), primary_key=True)
    value = db.Column(JSONText)

    @staticmethod
    def get(key, default=None):
        row = db.session.get(Setting, key)
        return default if row is None else row.value

    @staticmethod
    def put(key, value):
        row = db.session.get(Setting, key)
        if row is None:
            row = Setting(key=key, value=value)
            db.session.add(row)
        else:
            row.value = value
