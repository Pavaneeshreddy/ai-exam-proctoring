from datetime import datetime, timedelta

from app.ai import engine
from app.ai.audio import analyze_audio_window, estimate_speakers


def ev(t, minutes_ago=0, now=None, review="pending", confidence=1.0):
    now = now or datetime(2026, 1, 1, 12, 0, 0)
    return {"event_type": t, "created_at": now - timedelta(minutes=minutes_ago), "confidence": confidence, "review_status": review}


NOW = datetime(2026, 1, 1, 12, 0, 0)


def test_clean_session_scores_zero():
    score, breakdown = engine.compute_score([], NOW)
    assert score == 0 and breakdown == {}


def test_hard_evidence_outweighs_minor_events():
    phone, _ = engine.compute_score([ev("phone_detected")], NOW)
    glances, _ = engine.compute_score([ev("gaze_away"), ev("gaze_away")], NOW)
    assert phone > glances
    assert engine.risk_level(phone) in ("high", "critical")


def test_repetition_increases_score_more_than_linearly():
    one, _ = engine.compute_score([ev("tab_switch")], NOW)
    four, _ = engine.compute_score([ev("tab_switch") for _ in range(4)], NOW)
    raw_one = -engine.SATURATION * __import__("math").log(1 - one / 100)
    raw_four = -engine.SATURATION * __import__("math").log(1 - four / 100)
    assert raw_four > 4 * raw_one


def test_behavioural_events_decay_but_hard_evidence_does_not():
    fresh, _ = engine.compute_score([ev("head_turned")], NOW)
    old, _ = engine.compute_score([ev("head_turned", minutes_ago=30)], NOW)
    assert old < fresh / 4
    phone_fresh, _ = engine.compute_score([ev("phone_detected")], NOW)
    phone_old, _ = engine.compute_score([ev("phone_detected", minutes_ago=30)], NOW)
    assert phone_fresh == phone_old


def test_dismissed_events_are_ignored_and_confirmed_count_fully():
    dismissed, _ = engine.compute_score([ev("multiple_faces", review="dismissed")], NOW)
    assert dismissed == 0
    low_conf, _ = engine.compute_score([ev("multiple_faces", confidence=0.3)], NOW)
    confirmed, _ = engine.compute_score([ev("multiple_faces", confidence=0.3, review="confirmed")], NOW)
    assert confirmed > low_conf


def test_score_is_bounded():
    score, _ = engine.compute_score([ev("identity_mismatch") for _ in range(50)], NOW)
    assert 99 <= score <= 100


def test_admin_weight_override():
    base, _ = engine.compute_score([ev("tab_switch")], NOW)
    heavier, _ = engine.compute_score([ev("tab_switch")], NOW, {"tab_switch": 40})
    assert heavier > base


def test_correlation_detects_assistance():
    recent = [("voice_detected", NOW - timedelta(seconds=10))]
    hits = engine.find_correlations(recent, "head_turned", NOW, set())
    assert hits and hits[0][0] == "suspected_assistance"
    assert engine.find_correlations(recent, "head_turned", NOW, {"suspected_assistance"}) == []
    far = [("voice_detected", NOW - timedelta(seconds=120))]
    assert engine.find_correlations(far, "head_turned", NOW, set()) == []


def test_audio_single_vs_multiple_speakers():
    one = [118 + (i % 7) for i in range(40)]
    two = [115 + (i % 5) for i in range(20)] + [228 + (i % 6) for i in range(20)]
    assert estimate_speakers(one)["count"] == 1
    assert estimate_speakers(two)["count"] == 2
    events = analyze_audio_window({"voiced_ratio": 0.6, "level_db": 18, "pitches": two})
    assert events[0][0] == "multiple_voices"
    events = analyze_audio_window({"voiced_ratio": 0.5, "level_db": 15, "pitches": one})
    assert events[0][0] == "voice_detected"
    assert analyze_audio_window({"voiced_ratio": 0.05, "level_db": 3, "pitches": []}) == []
