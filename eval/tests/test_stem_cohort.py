import numpy as np

from eval.stem_cohort_audit import cross_correlation_offset, select_pairs
from eval.stem_cohort_report import cohort_ids


def test_pair_selection_is_shortest_and_deterministic():
    manifest = {"cases": [
        {"song_id": "old", "origin": "local_demucs_exact_model_name", "duration_s": 1},
        {"song_id": "b", "origin": "runpod_demucs_exact_model_name", "duration_s": 3},
        {"song_id": "a", "origin": "runpod_demucs_exact_model_name", "duration_s": 3},
        {"song_id": "c", "origin": "runpod_demucs_exact_model_name", "duration_s": 2},
    ]}
    assert [row["song_id"] for row in select_pairs(manifest, 2)] == ["c", "a"]


def test_cohort_split_includes_legacy_in_previous():
    manifest = {"cases": [
        {"song_id": "legacy"},
        {"song_id": "local", "origin": "local_demucs_exact_model_name"},
        {"song_id": "new", "origin": "runpod_demucs_exact_model_name"},
    ]}
    assert cohort_ids(manifest) == {
        "previous_26": {"legacy", "local"}, "runpod_15": {"new"},
    }


def test_cross_correlation_offset_sign_and_value():
    rng = np.random.default_rng(7)
    reference = rng.normal(size=80000)
    delayed = np.concatenate([np.zeros(24), reference[:-24]])
    result = cross_correlation_offset(reference, delayed, 8000)
    assert result["local_minus_runpod_samples_at_8khz"] == 24
    assert result["local_minus_runpod_ms"] == 3.0
    assert result["aligned_correlation"] > 0.99
