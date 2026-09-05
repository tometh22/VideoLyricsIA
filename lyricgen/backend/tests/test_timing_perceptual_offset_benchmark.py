import numpy as np

from scripts.benchmark_timing_perceptual_offset import evaluate_offsets
from timing_review_suggestions import AcousticTrack


def _track() -> AcousticTrack:
    count = 100
    pitched = np.zeros(count, dtype=bool)
    pitched[20:61] = True
    return AcousticTrack(
        frame_seconds=.05,
        times=np.arange(count, dtype=np.float32) * .05,
        rms=np.ones(count, dtype=np.float32),
        active=np.ones(count, dtype=bool),
        f0=np.full(count, 220.0, dtype=np.float32),
        voiced_probability=np.ones(count, dtype=np.float32),
        pitched=pitched,
        energy_threshold=.1,
    )


def test_compares_raw_and_100ms_offset_on_same_operator_gold_without_mutation():
    baseline = [
        {"start": 1.0, "end": 2.0, "text": "one"},
        {"start": 4.0, "end": 4.5, "text": "two"},
    ]
    gold = [
        {"start": 1.0, "end": 3.05, "text": "one", "locked": True},
        {"start": 4.0, "end": 4.5, "text": "two"},
    ]

    result = evaluate_offsets(baseline, gold, _track())

    assert result["gold_line_count"] == 1
    assert result["offsets"]["offset_0ms"]["median_absolute_error_s"] == 0.0
    assert result["offsets"]["offset_100ms"]["median_absolute_error_s"] == 0.1
    assert result["automatic_mutations"] == 0
    assert baseline[0]["end"] == 2.0
