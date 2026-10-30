# ProctorAI: AI-Based Online Exam Fraud Detection Using Deep Learning

ProctorAI is an online proctoring platform for remote exams. A photo is taken when the exam starts, and the AI checks throughout the exam that **the same person stays in front of the camera**. While the student writes, it also monitors head pose, eye gaze, prohibited objects, voices and browser activity. All of these events feed an explainable **fraud score**, and proctors see the score, the alerts and the evidence on a live dashboard.

Built with **Python · Flask · OpenCV · YOLOv8 · MediaPipe · SQLAlchemy (SQLite or MySQL)**.

---

## Quick start

```bash
cd exam-proctor
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
python scripts/download_models.py --web   # deep face models + self-hosted browser AI (recommended)
python run.py                             # http://127.0.0.1:5000
```

Demo accounts are created on first run:

| Role | Email | Password |
| --- | --- | --- |
| Admin | admin@proctor.ai | admin123 |
| Proctor | proctor@proctor.ai | proctor123 |
| Student | student@proctor.ai | student123 |

You can also register a new student at `/register`. Registration captures a face photo with the webcam.

> Browsers only allow camera access on `https://` or `localhost`. To test from another device on your network, run `flask --app run run --host 0.0.0.0 --cert=adhoc` (requires `pip install pyopenssl`).

**Optional server-side YOLOv8:** `pip install -r requirements-ai.txt` (this installs PyTorch). Without it, object detection runs only in the browser.

**MySQL:** set `DATABASE_URL=mysql+pymysql://user:pass@host/exam_proctor`. Tables are created automatically. See `.env.example` for the other settings.

---

## How an exam works

1. **System check.** The app tests the camera, microphone, browser support, displays (it blocks the exam if several monitors are connected), network latency and on-device AI.
2. **Rules and consent.** The student reads the exam rules and agrees to monitoring.
3. **Identity and liveness.** The student completes a random liveness challenge (blink, turn, smile or nod) so a printed photo cannot pass. Then the **start-of-exam photo** is matched against the photo the student registered with. After 3 failed matches, a proctor has to approve the student manually before the timer starts.
4. **Monitored exam.** The exam runs full-screen with autosave, a question palette, mark-for-review, keyboard shortcuts and a server-authoritative timer. In **written mode**, students answer on paper while the camera watches, then capture photos of their answer sheets.
5. **Review.** Proctors watch live tiles, receive alerts, warn students, extend time, flag or terminate sessions, confirm or dismiss each event, record a verdict, grade essays and answer sheets, and export reports.

## What the AI detects

| Modality | Where | Detects |
| --- | --- | --- |
| **Identity continuity** | Server (YuNet + SFace, OpenCV) | The face in each sampled frame is compared with the start photo. Two consecutive mismatches raise *Different person detected* (critical), with a screenshot as evidence. |
| Face presence | Browser (MediaPipe) and server | No face, face gone for more than 15 s (*left camera view*), multiple faces, face too far |
| Head pose and gaze | Browser (MediaPipe landmarks + blendshapes) | Head turned away, repeated head rotation (5 or more per minute), eyes off screen. A personal baseline is calibrated first. Looking down is allowed in written mode. |
| Objects | Browser (EfficientDet) and server (YOLOv8) | Phone, book or notes, laptop or second screen, extra person |
| Camera | Server (OpenCV) and browser | Lens covered, too dark, camera unplugged or permission revoked |
| Audio | Browser VAD → server analysis | Speech, and **multiple voices** detected by clustering pitch. Raw audio is never uploaded. |
| Browser | Browser | Tab switch, focus loss, leaving full-screen, copy/paste, blocked shortcuts, developer tools, multiple monitors, resizing, going offline, lost heartbeat |

### Fraud score (decision engine, `app/ai/engine.py`)

```
points = Σ weight(type) × repeat(1×, 1.25×, … ≤ 2×) × confidence × decay
score  = 100 × (1 − e^(−points / 50))
```

* **Severity:** each event type has a weight. Admins can tune the weights and cooldowns under **AI engine**.
* **Frequency:** repeated behaviour counts progressively more.
* **Recency:** behavioural events halve every 10 minutes. Hard evidence (phone, another person, impersonation) never decays.
* **Correlation rules:** some combinations add a composite event, for example *looked away + speech within 30 s → suspected assistance*, or *left the window + looked away → suspected lookup*.
* **Human in the loop:** events a proctor dismisses are excluded, and events a proctor confirms count at full confidence.

Each exam sets a **warning threshold**, which notifies the student and alerts proctors, and a **critical threshold**, which can optionally terminate the exam automatically. Evidence screenshots are saved for every medium-severity or higher event.

## Face model backends

| Backend | When | Notes |
| --- | --- | --- |
| `sface` | After `scripts/download_models.py` | YuNet detector + SFace recogniser (recommended, accurate) |
| `dlib` | If `face_recognition` is installed | Alternative recogniser |
| `basic` | Always available | Haar cascade + LBP histograms. Lower accuracy, so the threshold is stricter and its mismatches are down-weighted |

The **AI system status** page shows which backend is active.

## Project layout

```
exam-proctor/
├── app/
│   ├── ai/            engine.py (fraud score) · face.py · pipeline.py (frame analysis) · objects.py (YOLOv8) · audio.py
│   ├── routes/        auth · student · proctor · admin · api (JSON for the exam room and live monitor)
│   ├── static/js/     monitor.js (proctoring client) · vision.js (MediaPipe) · room.js · prepare.js · live.js · session.js
│   ├── templates/     landing, auth, student, proctor, admin pages
│   ├── models.py      Users, Exams, Questions, Sessions, Answers, Events, Snapshots, Messages, Audit log, Settings
│   └── services.py    event recording, scoring, grading, storage
├── scripts/download_models.py
└── tests/             engine, end-to-end API flows (including impersonation), page rendering
```

## Tests

```bash
pip install -r requirements-dev.txt
python -m pytest -q
```

## Privacy and fairness

* Raw audio never leaves the device. Only pitch and loudness summaries are sent.
* Photos and evidence are served only to exam staff and to the student they belong to.
* The fraud score supports human judgement; it is not a verdict on its own. Proctors review the evidence and record the final decision, and every action is written to the audit log.
