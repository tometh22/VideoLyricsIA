import hashlib

from consensus_review_certificate import (
    OBSERVATION_SCHEMA,
    evaluate_consensus_review_gate,
    validate_observations,
)


def _row(index: int, *, verdict: str = "correct") -> dict:
    digest = hashlib.sha256(str(index).encode()).hexdigest()
    return {
        "schema": OBSERVATION_SCHEMA,
        "window_id": f"window-{index}",
        "song_id": f"song-{index % 10}",
        "verdict": verdict,
        "source_families": ["whisper", "gemini_audio"],
        "candidate_sha256": digest,
        "audio_window_sha256": digest,
    }


def test_declared_gate_passes_50_perfect_windows_across_10_songs():
    result = evaluate_consensus_review_gate(
        [_row(index) for index in range(50)], bootstrap_replicates=500,
    )
    assert result["eligible_for_signed_certificate"] is True
    assert result["reviewed_windows"] == 50
    assert result["songs"] == 10
    assert result["incorrect"] == 0
    assert result["song_bootstrap_precision_lower_95"] == 1.0
    assert result["automatic_apply_allowed"] is False
    assert result["runtime_authorization"] is False


def test_one_incorrect_window_fails_even_when_precision_exceeds_90_percent():
    rows = [_row(index) for index in range(50)]
    rows[-1] = _row(49, verdict="incorrect")
    result = evaluate_consensus_review_gate(rows, bootstrap_replicates=500)
    assert result["eligible_for_signed_certificate"] is False
    assert "incorrect_consensus_approved" in result["blockers"]


def test_uncertain_rows_are_abstentions_and_do_not_pad_reviewed_count():
    rows = [_row(index) for index in range(50)]
    rows[-1] = _row(49, verdict="uncertain")
    result = evaluate_consensus_review_gate(rows, bootstrap_replicates=100)
    assert result["reviewed_windows"] == 49
    assert result["uncertain_excluded"] == 1
    assert "insufficient_reviewed_windows" in result["blockers"]


def test_observations_require_independent_families_and_unique_windows():
    rows = [_row(0), _row(0)]
    rows[0]["source_families"] = ["whisper_raw", "whisper_contextual"]
    errors = validate_observations(rows)
    assert any("two independent families" in error for error in errors)
    assert any("duplicated" in error for error in errors)
