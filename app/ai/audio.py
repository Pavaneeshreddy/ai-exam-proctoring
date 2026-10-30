"""Server-side analysis of audio summaries sent by the browser.

The browser never uploads raw audio (privacy and bandwidth). Every few seconds
it sends a window summary: voiced-frame ratio, loudness relative to the
calibrated noise floor and the fundamental-frequency (pitch) estimate of each
voiced frame. Here we decide whether that is speech and whether more than one
speaker is present, by clustering the pitch track: two people almost always
speak at clearly separated fundamental frequencies.
"""

import numpy as np


def kmeans_1d(values, k=2, iterations=25):
    values = np.sort(np.asarray(values, dtype=np.float64))
    centroids = np.percentile(values, np.linspace(20, 80, k))
    labels = np.zeros(len(values), dtype=int)
    for _ in range(iterations):
        labels = np.argmin(np.abs(values[:, None] - centroids[None, :]), axis=1)
        new = np.array([values[labels == i].mean() if np.any(labels == i) else centroids[i] for i in range(k)])
        if np.allclose(new, centroids):
            break
        centroids = new
    return centroids, labels, values


def analyze_audio_window(summary):
    """Return a list of (event_type, confidence, details)."""
    voiced_ratio = float(summary.get("voiced_ratio") or 0)
    level_db = float(summary.get("level_db") or 0)  # dB above noise floor
    loud_ratio = float(summary.get("loud_ratio") or 0)
    pitches = [float(p) for p in (summary.get("pitches") or []) if 70 <= float(p) <= 400]
    events = []

    is_speech = voiced_ratio >= 0.25 and level_db >= 8 and len(pitches) >= 6
    if is_speech:
        details = {"voiced_ratio": round(voiced_ratio, 2), "level_db": round(level_db, 1)}
        speakers = estimate_speakers(pitches)
        details["speakers"] = speakers["count"]
        details["pitch_centroids"] = speakers["centroids"]
        if speakers["count"] >= 2:
            events.append(("multiple_voices", speakers["confidence"], details))
        else:
            conf = min(1.0, 0.5 + voiced_ratio)
            events.append(("voice_detected", round(conf, 2), details))
    elif loud_ratio >= 0.6 and level_db >= 15:
        events.append(("loud_noise", 0.6, {"level_db": round(level_db, 1)}))
    return events


def estimate_speakers(pitches):
    if len(pitches) < 12:
        return {"count": 1, "confidence": 0.0, "centroids": [round(float(np.median(pitches)), 1)] if pitches else []}
    centroids, labels, values = kmeans_1d(pitches, 2)
    lo, hi = sorted(centroids)
    share = min(np.mean(labels == 0), np.mean(labels == 1))
    separation = (hi - lo) / max(lo, 1.0)
    spread = np.mean([values[labels == i].std() for i in range(2) if np.any(labels == i)])
    distinct = separation >= 0.35 and share >= 0.2 and (hi - lo) > 2.5 * max(spread, 1.0)
    if distinct:
        confidence = float(min(1.0, 0.4 + separation * 0.6 + share))
        return {"count": 2, "confidence": round(confidence, 2), "centroids": [round(lo, 1), round(hi, 1)]}
    return {"count": 1, "confidence": 0.0, "centroids": [round(float(np.median(values)), 1)]}
