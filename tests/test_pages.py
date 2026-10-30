"""Every page renders for the roles that can see it (uses the demo seed data)."""

import io

from app.models import Exam, ExamSession, User

from .conftest import login


def test_public_pages(client):
    assert client.get("/").status_code == 200
    assert client.get("/login").status_code == 200
    assert client.get("/register").status_code == 200
    assert client.get("/admin/").status_code == 302  # redirect to login


def test_bad_login(client, seeded):
    assert login(client, "admin@proctor.ai", "wrong").status_code == 401


def test_admin_pages(client, seeded):
    assert login(client, "admin@proctor.ai", "admin123").status_code == 302
    s = ExamSession.query.first()
    exam = Exam.query.first()
    student = User.query.filter_by(role="student").first()
    for url in [
        "/admin/", "/admin/users", "/admin/users?role=student&q=a", "/admin/users/new", f"/admin/users/{student.id}",
        "/admin/settings", "/admin/audit", "/proctor/", "/proctor/live", "/proctor/sessions",
        "/proctor/sessions?risk=high&verdict=none", "/proctor/exams", "/proctor/exams/new",
        f"/proctor/exams/{exam.id}/edit", f"/proctor/exams/{exam.id}/report", f"/proctor/session/{s.id}",
        f"/proctor/session/{s.id}/report", f"/proctor/session/{s.id}/grade", "/proctor/system", "/profile",
    ]:
        r = client.get(url)
        assert r.status_code == 200, url
    r = client.get(f"/proctor/exams/{exam.id}/export.csv")
    assert r.status_code == 200 and b"Fraud score" in r.data
    assert client.get("/api/live").status_code == 200
    assert client.get(f"/api/live/session/{s.id}").json["session"]["id"] == s.id
    assert client.get("/api/live/alerts").status_code == 200


def test_admin_engine_settings_roundtrip(client, seeded):
    login(client, "admin@proctor.ai", "admin123")
    r = client.post("/admin/settings", data={"action": "save", "w-tab_switch": "25", "c-tab_switch": "8"})
    assert r.status_code == 302
    from app.models import Setting

    assert Setting.get("event_weights") == {"tab_switch": 25.0}
    client.post("/admin/settings", data={"action": "reset"})
    assert Setting.get("event_weights") == {}


def test_exam_editor_creates_exam_and_imports_csv(client, seeded):
    login(client, "proctor@proctor.ai", "proctor123")
    qjson = '[{"type":"mcq","text":"Q1","options":["a","","b"],"answer":2,"marks":2},{"type":"essay","text":"Explain","marks":5}]'
    r = client.post("/proctor/exams/new", data={
        "title": "Created", "mode": "online", "duration_minutes": "20", "status": "published",
        "questions_json": qjson, "warn_threshold": "30", "critical_threshold": "70", "monitor_audio": "on",
    })
    assert r.status_code == 302
    exam = Exam.query.filter_by(title="Created").one()
    assert len(exam.questions) == 2
    mcq = exam.questions[0]
    assert mcq.options == ["a", "b"] and mcq.answer == 1  # empty option removed, answer remapped
    csv_data = "type,text,options,answer,marks\nmcq,Capital?,Paris|Rome,A,1\ntruefalse,Earth is round,,true,1\nmulti,Evens,1|2|4,B|C,2\n"
    r = client.post(f"/proctor/exams/{exam.id}/import", data={"file": (io.BytesIO(csv_data.encode()), "q.csv")}, content_type="multipart/form-data")
    assert r.status_code == 302
    assert len(exam.questions) == 5
    assert exam.questions[-1].answer == [1, 2]
    # publishing with an invalid question is rejected
    r = client.post("/proctor/exams/new", data={"title": "Bad", "status": "published", "questions_json": '[{"type":"mcq","text":"x","options":["a"],"answer":0}]'})
    assert r.status_code == 400


def test_proctor_scope(client, seeded):
    login(client, "proctor@proctor.ai", "proctor123")
    other = Exam(title="Not mine", status="draft")
    from app.extensions import db

    db.session.add(other)
    db.session.commit()
    assert client.get(f"/proctor/exams/{other.id}/edit").status_code == 403
    assert client.get("/admin/").status_code == 403


def test_student_pages(client, seeded):
    assert login(client, "student@proctor.ai", "student123").status_code == 302
    assert client.get("/student/").status_code == 200
    exam = Exam.query.filter_by(title="AI & Machine Learning Fundamentals").one()
    assert client.get(f"/student/exam/{exam.id}/prepare").status_code == 200
    upcoming = Exam.query.filter_by(title="Computer Networks — Final").one()
    assert client.get(f"/student/exam/{upcoming.id}/prepare").status_code == 302
    assert client.get("/proctor/").status_code == 403


def test_seeded_scores_are_explainable(seeded):
    sessions = ExamSession.query.all()
    clean = min(sessions, key=lambda s: len(s.events))
    worst = max(sessions, key=lambda s: s.fraud_score)
    assert clean.fraud_score == 0
    assert any(e.event_type == "identity_mismatch" for e in worst.events)
    assert worst.risk_level in ("high", "critical")
