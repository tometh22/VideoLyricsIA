from datetime import datetime, timezone
from types import SimpleNamespace

from quality_shadow import build_shadow_event


def test_shadow_event_is_deterministic_and_contains_no_lyrics_or_audio():
    job = SimpleNamespace(job_id="abc123", tenant_id="tenant", user_id=2)
    quality = {
        "evaluated_revision": 4,
        "segments_hash": "a" * 64,
        "quality_fingerprint": "b" * 64,
        "pipeline_release": "release",
        "pipeline_config_fingerprint": "c" * 16,
        "policy_version": "quality-gate-v5",
        "decision": "review_required",
        "timing_source": "acoustic_dp_ctc_v1",
        "shadow_decision": {
            "eligible": True,
            "would_approve": False,
            "reason_codes": ["strong_unassigned_vocal_events"],
        },
    }
    moment = datetime(2026, 1, 2, tzinfo=timezone.utc)
    first = build_shadow_event(job, quality, occurred_at=moment)
    second = build_shadow_event(job, quality, occurred_at=moment)
    assert first == second
    assert first["properties"]["eligible"] is True
    assert first["properties"]["would_approve"] is False
    assert first["properties"]["evaluation_stage"] == "terminal"
    assert len(first["properties"]["decision_id"]) == 64
    serialized = str(first).lower()
    assert "segments" not in serialized.replace("segments_hash", "")
    assert "audio_path" not in serialized

    later = build_shadow_event(
        job, {**quality, "quality_fingerprint": "d" * 64},
        evaluation_stage="terminal", occurred_at=moment,
    )
    assert (
        later["properties"]["decision_id"]
        == first["properties"]["decision_id"]
    )
