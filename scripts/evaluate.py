"""Reproducible evaluation used in the paper (paper/ieee_paper.tex).

    python scripts/evaluate.py [output_dir]

Writes results.json and figures (PNG) to output_dir (default: paper/results).
Every number in the paper's evaluation section comes from this script or from
the pytest suite; nothing is typed in by hand.
"""

import json
import math
import os
import random
import statistics
import sys
import time
from datetime import datetime, timedelta

import cv2
import numpy as np

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from app.ai import engine  # noqa: E402
from app.ai.audio import analyze_audio_window, estimate_speakers  # noqa: E402
from app.ai.face import FaceAnalyzer, lbp_descriptor  # noqa: E402
from app.ai.pipeline import analyze_frame  # noqa: E402

T0 = datetime(2026, 1, 1, 10, 0, 0)


# ------------------------------------------------------------ decision engine
SCENARIOS = {
    "Honest student": [],
    "Occasional glances": [("gaze_away", 5), ("gaze_away", 22), ("head_turned", 31)],
    "Tab switching": [("tab_switch", 4), ("focus_lost", 9), ("tab_switch", 15), ("tab_switch", 21), ("clipboard", 22)],
    "Talking + looking away": [("voice_detected", 6), ("head_turned", 6.2), ("multiple_voices", 12), ("head_turned", 12.3), ("gaze_away", 18)],
    "Phone usage": [("head_turned", 8), ("phone_detected", 9), ("phone_detected", 25)],
    "Impersonation": [("multiple_faces", 10), ("identity_mismatch", 11), ("face_missing", 12)],
}
DURATION_MIN = 45


def _event(etype, minute):
    return {"event_type": etype, "created_at": T0 + timedelta(minutes=minute), "confidence": 0.85, "review_status": "pending"}


def _with_correlations(raw):
    """Apply the engine's correlation rules the way services.record_event does."""
    events, composites = [], []
    for etype, minute in sorted(raw, key=lambda e: e[1]):
        t = T0 + timedelta(minutes=minute)
        recent = [(e["event_type"], e["created_at"]) for e in events if (t - e["created_at"]).total_seconds() <= 60]
        recent_comp = {c for c, ts in composites if (t - ts).total_seconds() <= 60}
        events.append(_event(etype, minute))
        for comp, _ in engine.find_correlations(recent, etype, t, recent_comp):
            events.append({**_event(comp, minute), "confidence": 0.8})
            composites.append((comp, t))
    return events


def eval_engine():
    out = {}
    for name, raw in SCENARIOS.items():
        events = _with_correlations(raw)
        curve = []
        for m in range(0, DURATION_MIN + 1):
            now = T0 + timedelta(minutes=m)
            score, _ = engine.compute_score([e for e in events if e["created_at"] <= now], now)
            curve.append(score)
        final, breakdown = engine.compute_score(events, T0 + timedelta(minutes=DURATION_MIN))
        peak = max(curve)
        out[name] = {
            "events": len(events),
            "composites": sum(1 for e in events if engine.catalog_entry(e["event_type"])["category"] == "composite"),
            "peak": peak,
            "final": final,
            "risk_at_peak": engine.risk_level(peak),
            "breakdown": breakdown,
            "curve": curve,
        }
    return out


# ---------------------------------------------------------- audio speakers
def synth_pitch_track(n_speakers, rng, n=60):
    """F0 track of a 5 s window. Speakers' mean F0 drawn from adult ranges."""
    if n_speakers == 1:
        mu = rng.uniform(95, 240)
        return list(np.clip(rng.normal(mu, mu * 0.06, n), 70, 400))
    while True:  # two speakers with any realistic pair of voices (may be close!)
        a, b = sorted(rng.uniform(95, 240, 2))
        if b - a > 5:
            break
    share = rng.uniform(0.25, 0.75)
    k = int(n * share)
    return list(np.clip(np.concatenate([rng.normal(a, a * 0.06, k), rng.normal(b, b * 0.06, n - k)]), 70, 400))


def eval_audio(trials=2000, seed=7):
    rng = np.random.default_rng(seed)
    cm = {"1->1": 0, "1->2": 0, "2->1": 0, "2->2": 0}
    by_gap = {}
    for _ in range(trials):
        true = 1 if rng.random() < 0.5 else 2
        track = synth_pitch_track(true, rng)
        pred = estimate_speakers(track)["count"]
        cm[f"{true}->{pred}"] += 1
        if true == 2:
            cents = sorted(track)
            c, _, _ = __import__("app.ai.audio", fromlist=["kmeans_1d"]).kmeans_1d(cents, 2)
            gap = abs(c[1] - c[0]) / min(c)
            bucket = "<20%" if gap < 0.2 else "20-35%" if gap < 0.35 else "35-60%" if gap < 0.6 else ">60%"
            stat = by_gap.setdefault(bucket, [0, 0])
            stat[0] += pred == 2
            stat[1] += 1
    tp, fn, fp, tn = cm["2->2"], cm["2->1"], cm["1->2"], cm["1->1"]
    precision = tp / (tp + fp) if tp + fp else 0
    recall = tp / (tp + fn) if tp + fn else 0
    # voice-activity gate on silence / noise windows
    gate = {
        "silence": analyze_audio_window({"voiced_ratio": 0.02, "level_db": 2, "pitches": []}),
        "background_noise": analyze_audio_window({"voiced_ratio": 0.1, "level_db": 16, "loud_ratio": 0.7, "pitches": []}),
    }
    return {
        "trials": trials,
        "confusion": cm,
        "accuracy": (tp + tn) / trials,
        "precision_multi": precision,
        "recall_multi": recall,
        "f1_multi": 2 * precision * recall / (precision + recall) if precision + recall else 0,
        "false_alarm_rate": fp / (fp + tn) if fp + tn else 0,
        "recall_by_pitch_gap": {k: v[0] / v[1] for k, v in sorted(by_gap.items())},
        "gate": {k: [e[0] for e in v] for k, v in gate.items()},
    }


# -------------------------------------------------------- face (fallback)
def _variant(img, rng):
    h, w = img.shape[:2]
    m = cv2.getRotationMatrix2D((w / 2, h / 2), rng.uniform(-8, 8), rng.uniform(0.75, 1.05))
    m[0, 2] += rng.uniform(-40, 40)
    m[1, 2] += rng.uniform(-20, 20)
    out = cv2.warpAffine(img, m, (w, h), borderMode=cv2.BORDER_REPLICATE).astype(np.float32)
    out = out * rng.uniform(0.7, 1.25) + rng.uniform(-30, 30)
    out += rng.normal(0, 4, out.shape)
    return np.clip(out, 0, 255).astype(np.uint8)


def eval_face(faces_dir, variants=40, seed=3):
    analyzer = FaceAnalyzer(os.path.join(ROOT, "models"))
    rng = np.random.default_rng(seed)
    ids = {}
    for name in sorted(os.listdir(faces_dir)):
        img = cv2.imread(os.path.join(faces_dir, name))
        if img is None:
            continue
        found = analyzer.detect(img)
        if len(found) == 1:
            ids[name] = img
    if len(ids) < 2:
        return {"backend": analyzer.backend, "identities": len(ids), "note": "need >=2 single-face images"}
    embs = {}
    detected = total = 0
    for name, img in ids.items():
        embs[name] = []
        for _ in range(variants):
            v = _variant(img, rng)
            total += 1
            f = analyzer.detect(v)
            if f:
                detected += 1
                embs[name].append(analyzer.embed(v, f[0]))
    genuine, impostor = [], []
    names = list(embs)
    for i, a in enumerate(names):
        ea = embs[a]
        for x in range(len(ea)):
            for y in range(x + 1, len(ea)):
                genuine.append(analyzer.similarity(ea[x], ea[y]))
        for b in names[i + 1:]:
            for u in ea:
                for v in embs[b]:
                    impostor.append(analyzer.similarity(u, v))
    thr = analyzer.threshold
    far = sum(s >= thr for s in impostor) / len(impostor)
    frr = sum(s < thr for s in genuine) / len(genuine)
    # equal error rate
    best = (1.0, None)
    for t in np.linspace(0.8, 1.0, 401):
        a = sum(s >= t for s in impostor) / len(impostor)
        r = sum(s < t for s in genuine) / len(genuine)
        if abs(a - r) < best[0]:
            best = (abs(a - r), (t, (a + r) / 2))
    return {
        "backend": analyzer.backend,
        "identities": len(ids),
        "variants_per_identity": variants,
        "detection_rate": detected / total,
        "genuine_pairs": len(genuine),
        "impostor_pairs": len(impostor),
        "genuine_mean": statistics.mean(genuine),
        "genuine_min": min(genuine),
        "impostor_mean": statistics.mean(impostor),
        "impostor_max": max(impostor),
        "threshold": thr,
        "far_at_threshold": far,
        "frr_at_threshold": frr,
        "eer_threshold": best[1][0],
        "eer": best[1][1],
        "genuine": genuine,
        "impostor": impostor,
    }


# ----------------------------------------------------------------- latency
def eval_latency(faces_dir, reps=60):
    analyzer = FaceAnalyzer(os.path.join(ROOT, "models"))
    img = cv2.imread(os.path.join(faces_dir, sorted(os.listdir(faces_dir))[0]))
    h, w = img.shape[:2]
    s = min(640 / w, 480 / h)
    frame = cv2.copyMakeBorder(cv2.resize(img, (int(w * s), int(h * s))), 0, 0, 0, 0, cv2.BORDER_CONSTANT)
    ref = analyzer.embed(frame, analyzer.detect(frame)[0])

    def timeit(fn):
        times = []
        for _ in range(reps):
            t = time.perf_counter()
            fn()
            times.append((time.perf_counter() - t) * 1000)
        times.sort()
        return {"median_ms": statistics.median(times), "p95_ms": times[int(0.95 * len(times)) - 1]}

    ok, jpg = cv2.imencode(".jpg", frame, [cv2.IMWRITE_JPEG_QUALITY, 72])
    face = analyzer.detect(frame)[0]
    events = [
        {"event_type": random.choice(list(engine.EVENT_CATALOG)), "created_at": T0 + timedelta(seconds=i * 5), "confidence": 0.8, "review_status": "pending"}
        for i in range(500)
    ]
    return {
        "backend": analyzer.backend,
        "frame_size": list(frame.shape[:2][::-1]),
        "jpeg_kb": round(len(jpg) / 1024, 1),
        "decode": timeit(lambda: cv2.imdecode(jpg, cv2.IMREAD_COLOR)),
        "face_detect": timeit(lambda: analyzer.detect(frame)),
        "face_embed": timeit(lambda: analyzer.embed(frame, face)),
        "full_frame_pipeline": timeit(lambda: analyze_frame(frame, analyzer, None, ref, None)),
        "engine_500_events": timeit(lambda: engine.compute_score(events, T0 + timedelta(hours=1))),
    }


# ------------------------------------------------------------------ figures
def figures(res, out):
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    plt.rcParams.update({"font.size": 8, "font.family": "serif"})
    # score trajectories
    fig, ax = plt.subplots(figsize=(3.5, 2.3))
    for name, r in res["engine"].items():
        ax.plot(range(len(r["curve"])), r["curve"], label=name, linewidth=1.3)
    ax.axhline(40, color="#f97316", linestyle="--", linewidth=0.8)
    ax.axhline(75, color="#ef4444", linestyle="--", linewidth=0.8)
    ax.text(DURATION_MIN, 41, "warn", ha="right", va="bottom", fontsize=6.5, color="#f97316")
    ax.text(DURATION_MIN, 76, "critical", ha="right", va="bottom", fontsize=6.5, color="#ef4444")
    ax.set_xlabel("Exam time (min)")
    ax.set_ylabel("Fraud score")
    ax.set_ylim(0, 100)
    ax.legend(fontsize=5.8, loc="upper left", frameon=False, ncol=2)
    fig.tight_layout()
    fig.savefig(os.path.join(out, "fig_scores.png"), dpi=300)
    plt.close(fig)
    # face similarity histogram
    f = res["face"]
    if "genuine" in f:
        fig, ax = plt.subplots(figsize=(3.5, 2.1))
        bins = np.linspace(min(f["impostor"] + f["genuine"]), 1.0, 40)
        ax.hist(f["genuine"], bins=bins, alpha=0.7, label="Same person", color="#10b981")
        ax.hist(f["impostor"], bins=bins, alpha=0.7, label="Different person", color="#ef4444")
        ax.axvline(f["threshold"], color="black", linestyle="--", linewidth=0.8)
        ax.set_xlabel(f"Similarity ({f['backend']} backend)")
        ax.set_ylabel("Pairs")
        ax.legend(fontsize=6.5, frameon=False)
        fig.tight_layout()
        fig.savefig(os.path.join(out, "fig_face.png"), dpi=300)
        plt.close(fig)


def main():
    out = sys.argv[1] if len(sys.argv) > 1 else os.path.join(ROOT, "paper", "results")
    faces = os.environ.get("FACES_DIR", os.path.join(ROOT, "paper", "faces"))
    os.makedirs(out, exist_ok=True)
    random.seed(1)
    res = {"engine": eval_engine(), "audio": eval_audio()}
    res["face"] = eval_face(faces) if os.path.isdir(faces) else {"note": "no FACES_DIR"}
    res["latency"] = eval_latency(faces) if os.path.isdir(faces) else {"note": "no FACES_DIR"}
    figures(res, out)
    slim = json.loads(json.dumps(res, default=float))
    for k in ("genuine", "impostor"):
        slim["face"].pop(k, None)
    with open(os.path.join(out, "results.json"), "w") as fh:
        json.dump(slim, fh, indent=2, default=float)
    print(json.dumps({k: v for k, v in slim.items() if k != "engine"}, indent=1, default=float)[:4000])
    for name, r in slim["engine"].items():
        print(f"{name:26s} events={r['events']:2d} comp={r['composites']} peak={r['peak']:5.1f} final={r['final']:5.1f} {r['risk_at_peak']}")


if __name__ == "__main__":
    main()
