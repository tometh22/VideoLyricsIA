import numpy as np

from timing_review_suggestions import (
    AcousticTrack, TimingReviewPolicy, build_timing_review_candidates,
)


def _track(pitched):
    count = len(pitched)
    return AcousticTrack(
        frame_seconds=.05,
        times=np.arange(count, dtype=np.float32) * .05,
        rms=np.ones(count, dtype=np.float32),
        active=np.ones(count, dtype=bool),
        f0=np.full(count, 220.0, dtype=np.float32),
        voiced_probability=np.ones(count, dtype=np.float32),
        pitched=np.asarray(pitched, dtype=bool),
        energy_threshold=.1,
    )


def test_builds_review_only_pitch_candidate_with_perceptual_lead():
    pitched = np.zeros(120, dtype=bool)
    pitched[20:55] = True  # 1.00 through 2.75 seconds.
    segments = [
        {"_id": 1, "start": 1.0, "end": 2.0, "text": "hola",
         "words": [{"word": "hola", "start": 1.1, "end": 2.7}]},
        {"_id": 2, "start": 3.0, "end": 4.0, "text": "mundo"},
    ]

    candidates, report = build_timing_review_candidates(segments, _track(pitched))

    assert segments[0]["end"] == 2.0
    assert candidates[0]["suggestion_type"] == "timing"
    assert candidates[0]["proposed_end"] == 2.65
    assert candidates[0]["proposed_segments"][0]["end"] == 2.65
    assert candidates[0]["automatic_apply_allowed"] is False
    assert report["mutated_segments"] is False


def test_locked_lines_and_large_occurrence_jumps_abstain():
    pitched = np.ones(300, dtype=bool)
    segments = [
        {"start": 1.0, "end": 2.0, "text": "locked", "locked": True},
        {"start": 3.0, "end": 4.0, "text": "jump"},
        {"start": 13.0, "end": 14.0, "text": "next"},
    ]

    candidates, report = build_timing_review_candidates(
        segments, _track(pitched),
        policy=TimingReviewPolicy(maximum_visible_delta_s=6.0),
    )

    assert not any(item["current_segments"][0]["text"] == "locked" for item in candidates)
    assert not any(item["current_segments"][0]["text"] == "jump" for item in candidates)
    assert report["abstention_reasons"]["operator_locked"] == 1
    assert report["abstention_reasons"]["occurrence_jump_veto"] >= 1


def test_never_crosses_the_next_line_start():
    pitched = np.ones(100, dtype=bool)
    segments = [
        {"start": .5, "end": 1.0, "text": "one"},
        {"start": 1.5, "end": 2.0, "text": "two"},
    ]

    candidates, _ = build_timing_review_candidates(segments, _track(pitched))

    first = next(item for item in candidates if item["current_segments"][0]["text"] == "one")
    assert first["proposed_end"] <= 1.48
