import numpy as np

from timing_review_suggestions import (
    AcousticTrack, TimingReviewPolicy, build_timing_review_candidates,
    automatic_tail_enabled, extend_line_ends_to_stable_pitch,
)


def test_stable_pitch_tail_automatic_mutation_is_default_off(monkeypatch):
    monkeypatch.delenv("STABLE_PITCH_TAIL_ENABLED", raising=False)
    assert automatic_tail_enabled() is False

    monkeypatch.setenv("STABLE_PITCH_TAIL_ENABLED", "1")
    assert automatic_tail_enabled() is True


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


def test_builds_review_only_pitch_candidate_without_unvalidated_end_discount():
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
    assert candidates[0]["proposed_end"] == 2.75
    assert candidates[0]["proposed_segments"][0]["end"] == 2.75
    assert candidates[0]["raw_acoustic_end"] == 2.75
    assert candidates[0]["perceptual_end_offset_s"] == 0.0
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


def test_extends_word_endpoint_through_contiguous_stable_pitch_tail():
    pitched = np.zeros(120, dtype=bool)
    pitched[38:61] = True  # Stable, energy-backed vowel from 1.90s to 3.05s.
    segments = [
        {"start": 1.0, "end": 2.0, "text": "amor",
         "words": [{"word": "amor", "start": 1.2, "end": 2.0}]},
        {"start": 3.5, "end": 4.0, "text": "next"},
    ]

    output, report = extend_line_ends_to_stable_pitch(
        segments, _track(pitched),
    )

    assert segments[0]["end"] == 2.0
    assert output[0]["end"] == 3.05
    assert output[0]["stable_pitch_tail_extended"] is True
    assert report["extended_count"] == 1
    assert report["maximum_pitch_distance_cents"] == 200.0


def test_stable_pitch_tail_stops_at_pitch_jump_and_next_line_guard():
    pitched = np.zeros(120, dtype=bool)
    pitched[38:70] = True
    track = _track(pitched)
    track.f0[50:] = 440.0  # One octave jump: outside the ±2 semitone rule.
    segment = {
        "start": 1.0, "end": 2.0, "text": "amor",
        "words": [{"word": "amor", "start": 1.2, "end": 2.0}],
    }

    output, _ = extend_line_ends_to_stable_pitch(
        [segment, {"start": 4.0, "end": 5.0, "text": "next"}], track,
    )
    assert output[0]["end"] == 2.5

    track.f0[:] = 220.0
    capped, _ = extend_line_ends_to_stable_pitch(
        [segment, {"start": 2.7, "end": 3.2, "text": "next"}], track,
    )
    assert capped[0]["end"] == 2.68


def test_stable_pitch_tail_never_retimes_operator_locked_line():
    pitched = np.ones(100, dtype=bool)
    segment = {
        "start": 1.0, "end": 2.0, "text": "manual", "locked": True,
        "words": [{"word": "manual", "start": 1.2, "end": 2.0}],
    }

    output, report = extend_line_ends_to_stable_pitch(
        [segment], _track(pitched),
    )

    assert output[0] == segment
    assert report["extended_count"] == 0
    assert report["abstention_reasons"]["operator_locked"] == 1
