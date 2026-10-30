"""End-to-end flows through the HTTP API with a deterministic fake face model.

The fake analyser treats the dominant colour of an image as a person's
identity, so a blue image and a red image are 'different people'. This lets
us test the start-photo verification and continuous same-person checks
without shipping real face photos.
"""

import base64
from datetime import timedelta

import cv2
import numpy as np
import pytest

from app.ai.face import Face
from app.extensions import db
from app.models import Exam, ExamSession, ProctorEvent, Question, User, utcnow

from .conftest import login


class FakeAnalyzer:
    backend = "fake"
    threshold = 0.9

    def detect(self, img):
        if img.mean() < 5:
            return []
        if img[0, 0, 1] > 200:  # green marker pixel => two people
            return [Face((50, 50, 200, 200)), Face((300, 50, 180, 180))]
        return [Face((50, 50, 200, 200))]

    def detect_profile(self, img):
        return 0

    def embed(self, img, face):
        v = img.reshape(-1, 3).mean(axis=0).astype(np.float32) + 1.0
        return v / np.linalg.norm(v)

    def similarity(self, a, b):
        return float(np.dot(a, b))

    def is_match(self, s):
        return s is not None and s >= self.threshold

    @staticmethod
    def estimate_pose(face):
        return None


def photo(color, marker=False):
    rng = np.random.default_rng(0)
    img = np.zeros((480, 640, 3), dtype=np.uint8)
    img[:] = color
    img = np.clip(img.astype(int) + rng.integers(-40, 40, img.shape), 0, 255).astype(np.uint8)
    if marker:
        img[0, 0] = (0, 255, 0)
    ok, buf = cv2.imencode(".png", img)
    return "data:image/png;base64," + base64.b64encode(buf.tobytes()).decode()


BLUE, RED = (200, 60, 60), (60, 60, 200)


@pytest.fixture()
def fake_ai(app):
    app.extensions["face_analyzer"] = FakeAnalyzer()
    return app


@pytest.fixture()
def setup(fake_ai):
    admin = User(name="Admin", email="a@x.io", role="admin")
    admin.set_password("password1")
    student = User(name="Stu Dent", email="s@x.io", role="student", face_backend="fake")
    student.set_password("password1")
    student.face_embedding = FakeAnalyzer().embed(cv2.imdecode(np.frombuffer(base64.b64decode(photo(BLUE).split(",")[1]), np.uint8), 1), None)
    exam = Exam(
        title="Test exam", status="published", duration_minutes=30, require_liveness=True,
        shuffle_questions=False, shuffle_options=False, warn_threshold=40, critical_threshold=75,
    )
    exam.questions = [
        Question(qtype="mcq", text="2+2?", options=["3", "4"], answer=1, marks=2, position=0),
        Question(qtype="multi", text="Primes", options=["2", "4", "5"], answer=[0, 2], marks=2, position=1),
        Question(qtype="short", text="Capital of France", answer=["paris"], marks=1, position=2),
        Question(qtype="truefalse", text="Sky is blue", options=["True", "False"], answer=True, marks=1, position=3),
    ]
    db.session.add_all([admin, student, exam])
    db.session.commit()
    return {"admin": admin, "student": student, "exam": exam}


def begin(client, exam, img, **extra):
    return client.post(f"/student/exam/{exam.id}/begin", json={"photo": img, "liveness": {"passed": True}, **extra})


def test_start_photo_must_match_registered_face(client, setup):
    login(client, "s@x.io", "password1")
    r = begin(client, setup["exam"], photo(RED))
    assert r.status_code == 400 and r.json["retry"] is True
    assert ExamSession.query.count() == 0
    r = begin(client, setup["exam"], photo(BLUE))
    assert r.json["ok"] and r.json["verified"]
    s = ExamSession.query.one()
    assert s.status == "in_progress" and s.reference_photo


def test_three_failed_matches_require_proctor_approval(client, setup):
    login(client, "s@x.io", "password1")
    for _ in range(2):
        assert begin(client, setup["exam"], photo(RED)).status_code == 400
    r = begin(client, setup["exam"], photo(RED))
    assert r.json["ok"] and not r.json["verified"]
    s = ExamSession.query.one()
    assert s.status == "pending_approval"
    assert any(e.event_type == "identity_verification_failed" for e in s.events)
    # proctor approves
    client.post("/logout")
    login(client, "a@x.io", "password1")
    assert client.post(f"/api/live/session/{s.id}/approve", json={}).json["ok"]
    db.session.refresh(s)
    assert s.status == "in_progress"


def test_rejects_photo_with_two_people(client, setup):
    login(client, "s@x.io", "password1")
    r = begin(client, setup["exam"], photo(BLUE, marker=True))
    assert r.status_code == 400 and "More than one face" in r.json["error"]


def test_same_person_must_remain_visible(client, setup):
    login(client, "s@x.io", "password1")
    begin(client, setup["exam"], photo(BLUE))
    s = ExamSession.query.one()
    ok = client.post(f"/api/session/{s.id}/frame", json={"image": photo(BLUE)}).json
    assert ok["identity"]["match"] and ok["events"] == []
    # a different person sits down: one mismatching frame is tolerated, the second raises the alarm
    first = client.post(f"/api/session/{s.id}/frame", json={"image": photo(RED)}).json
    assert "identity_mismatch" not in first["events"]
    second = client.post(f"/api/session/{s.id}/frame", json={"image": photo(RED)}).json
    assert "identity_mismatch" in second["events"]
    ev = ProctorEvent.query.filter_by(event_type="identity_mismatch").one()
    assert ev.severity == "critical" and ev.evidence_path
    # nobody in front of the camera
    dark = "data:image/png;base64," + base64.b64encode(cv2.imencode(".png", np.full((480, 640, 3), 2, np.uint8))[1].tobytes()).decode()
    r = client.post(f"/api/session/{s.id}/frame", json={"image": dark}).json
    assert "camera_blocked" in r["events"]


def test_client_events_cooldown_and_whitelist(client, setup):
    login(client, "s@x.io", "password1")
    begin(client, setup["exam"], photo(BLUE))
    s = ExamSession.query.one()
    r = client.post(f"/api/session/{s.id}/events", json={"events": [{"type": "tab_switch"}, {"type": "tab_switch"}, {"type": "identity_mismatch"}]}).json
    assert r["recorded"] == ["tab_switch"]  # duplicate suppressed, server-only type rejected
    assert r["score"] > 0


def test_auto_terminate_at_critical_threshold(client, setup):
    setup["exam"].auto_terminate = True
    db.session.commit()
    login(client, "s@x.io", "password1")
    begin(client, setup["exam"], photo(BLUE))
    s = ExamSession.query.one()
    client.post(f"/api/session/{s.id}/events", json={"events": [{"type": "phone_detected", "confidence": 0.95}, {"type": "multiple_faces", "confidence": 0.95}, {"type": "multiple_persons", "confidence": 0.95}, {"type": "camera_disabled", "confidence": 0.95}]})
    db.session.refresh(s)
    assert s.status == "terminated" and "critical threshold" in s.terminated_reason
    hb = client.post(f"/api/session/{s.id}/heartbeat", json={}).json
    assert hb["status"] == "terminated" and hb["redirect"]


def test_answers_grading_and_results(client, setup):
    login(client, "s@x.io", "password1")
    begin(client, setup["exam"], photo(BLUE))
    s = ExamSession.query.one()
    qs = {q.qtype: q for q in setup["exam"].questions}
    for qtype, resp in (("mcq", 1), ("multi", [0, 1]), ("short", "  PARIS "), ("truefalse", True)):
        assert client.post(f"/api/session/{s.id}/answer", json={"question_id": qs[qtype].id, "response": resp}).json["ok"]
    assert client.post(f"/api/session/{s.id}/answer", json={"question_id": 99999, "response": 1}).status_code == 400
    r = client.post(f"/api/session/{s.id}/submit", json={}).json
    assert r["status"] == "submitted"
    db.session.refresh(s)
    # mcq 2 + multi (1 hit - 1 wrong = 0) + short 1 + tf 1
    assert s.score == 4 and s.max_score == 6
    assert client.get(f"/student/session/{s.id}/result").status_code == 200
    assert client.post(f"/api/session/{s.id}/answer", json={"question_id": qs["mcq"].id, "response": 0}).status_code == 409


def test_time_expiry_auto_submits(client, setup):
    login(client, "s@x.io", "password1")
    begin(client, setup["exam"], photo(BLUE))
    s = ExamSession.query.one()
    s.started_at = utcnow() - timedelta(minutes=31)
    db.session.commit()
    assert client.post(f"/api/session/{s.id}/heartbeat", json={}).json["status"] == "auto_submitted"


def test_students_cannot_access_other_sessions_or_staff_pages(client, setup):
    other = User(name="Other", email="o@x.io", role="student")
    other.set_password("password1")
    db.session.add(other)
    db.session.commit()
    login(client, "s@x.io", "password1")
    begin(client, setup["exam"], photo(BLUE))
    s = ExamSession.query.one()
    client.post("/logout")
    login(client, "o@x.io", "password1")
    assert client.post(f"/api/session/{s.id}/heartbeat", json={}).status_code == 403
    assert client.get(f"/media/sessions/{s.id}/reference.jpg").status_code == 403
    assert client.get("/proctor/").status_code == 403
    assert client.get("/admin/").status_code == 403
    assert client.get("/api/live").status_code == 403


def test_proctor_review_changes_score(client, setup):
    login(client, "s@x.io", "password1")
    begin(client, setup["exam"], photo(BLUE))
    s = ExamSession.query.one()
    client.post(f"/api/session/{s.id}/events", json={"events": [{"type": "phone_detected"}]})
    db.session.refresh(s)
    before = s.fraud_score
    client.post("/logout")
    login(client, "a@x.io", "password1")
    ev = ProctorEvent.query.filter_by(event_type="phone_detected").one()
    r = client.post(f"/api/live/event/{ev.id}/review", json={"status": "dismissed"}).json
    assert r["score"] < before
    live = client.get("/api/live").json
    assert live["total"] == 1 and live["sessions"][0]["student"] == "Stu Dent"
    assert client.post(f"/api/live/session/{s.id}/message", json={"body": "Eyes on screen"}).json["ok"]
    client.post("/logout")
    login(client, "s@x.io", "password1")
    hb = client.post(f"/api/session/{s.id}/heartbeat", json={}).json
    assert [m["body"] for m in hb["messages"] if m["kind"] == "warning"] == ["Eyes on screen"]
