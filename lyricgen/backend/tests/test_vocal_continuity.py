"""Safety cases that distinguish an attached sustain from a later voice."""

from dataclasses import replace
import numpy as np
import pytest
from vocal_continuity import (
    ContinuityPolicy,
    VocalTrack,
    propose_vocal_endings,
    verify_stem_clock,
)


def track():
    times = np.arange(250) * 0.02
    rms = np.zeros(250)
    rms[50:150] = 1.0
    spectra = np.zeros((250, 4))
    spectra[:, 0] = 1.0
    return VocalTrack(
        times, rms, spectra, 0.02, 5.0, {"verified": True, "offset_s": 0.0}
    )


def line(**extra):
    return {
        "start": 1.0,
        "end": 2.0,
        "text": "mi amor",
        "words": [
            {"word": "mi", "start": 1.1, "end": 1.3},
            {"word": "amor", "start": 1.4, "end": 2.0},
        ],
        **extra,
    }


def test_follows_spectral_envelope_without_a_pitch_or_approved_endpoint():
    source = line()
    before = repr(source)
    proposals, report = propose_vocal_endings([source], track())
    assert proposals[0]["proposed_end"] == 3.0
    assert proposals[0]["automatic_apply_allowed"] is False
    assert repr(source) == before
    assert report["proposal_count"] == 1


def test_reverberant_release_with_threshold_disagreement_abstains():
    t = track()
    t.rms[150:180] = 0.25
    candidates, report = propose_vocal_endings([line()], t)
    assert not candidates
    assert report["abstention_reasons"]["unstable_release"] == 1


def test_does_not_jump_silence_to_later_chorus():
    t = track()
    t.rms[120:140] = 0.0
    t.rms[140:180] = 1.0
    candidates, _ = propose_vocal_endings([line()], t)
    assert candidates[0]["proposed_end"] == 2.4


def test_short_dropout_inside_word_is_bridged():
    t = track()
    t.rms[120:123] = 0.0
    candidates, _ = propose_vocal_endings([line()], t)
    assert candidates[0]["proposed_end"] == 3.0


def test_different_spectrum_ends_run_even_if_louder():
    t = track()
    t.spectrum[120:, 0] = 0
    t.spectrum[120:, 1] = 1.0
    t.rms[120:] = 2.0
    candidates, _ = propose_vocal_endings([line()], t)
    assert candidates[0]["proposed_end"] == 2.4


def test_next_line_and_search_limit_are_not_observed_vocal_releases():
    t = track()
    t.rms[:] = 1.0
    candidates, report = propose_vocal_endings(
        [line(), {"start": 2.5, "end": 3.5, "text": "next"}], t
    )
    assert not candidates
    assert report["abstention_reasons"]["censored_by_search_limit"] == 1


@pytest.mark.parametrize(
    "extra",
    [
        {"locked": True},
        {"operator_locked": True},
        {"words": []},
        {"words": [{"word": "another", "start": 1.4, "end": 2.0}]},
        {"words": [{"word": "amor", "start": -1, "end": 2.0}]},
        {"words": [{"word": "amor", "start": 2.0, "end": 1.0}]},
        {"end": float("nan")},
    ],
)
def test_invalid_stale_or_protected_word_anchors_are_not_retimed(extra):
    candidates, _ = propose_vocal_endings([line(**extra)], track())
    assert candidates == []


def test_unverified_clock_and_silent_anchor_abstain():
    t = track()
    t.clock["verified"] = False
    assert propose_vocal_endings([line()], t)[0] == []
    t = track()
    t.rms[70:105] = 0.0
    assert propose_vocal_endings([line()], t)[0] == []


def test_shortening_and_direction_gate():
    t = track()
    t.rms[120:] = 0.0
    source = line(end=2.8)
    candidates, _ = propose_vocal_endings(
        [source], t, ContinuityPolicy(direction="trim")
    )
    assert candidates[0]["proposed_end"] == 2.4
    assert (
        propose_vocal_endings([source], t, ContinuityPolicy(direction="extend"))[0]
        == []
    )


def test_nonzero_clock_offset_is_preserved_in_proposal():
    t = track()
    t = replace(t, times=t.times - 0.025, clock={"verified": True, "offset_s": 0.025})
    assert propose_vocal_endings([line()], t)[0][0]["proposed_end"] == 2.975


def test_clock_detects_offset_and_rejects_drift_and_unrelated_audio():
    rng = np.random.default_rng(73)
    sr = 1000
    voice = rng.normal(size=30000)
    mix = voice + rng.normal(scale=0.4, size=len(voice))
    stem = np.r_[np.zeros(25), voice[:-25]]
    report = verify_stem_clock(mix, stem, sr)
    assert report["verified"] and report["offset_s"] == 0.025
    unrelated = verify_stem_clock(mix, rng.normal(size=len(voice)), sr)
    assert not unrelated["verified"]
    drifting = np.r_[
        voice[:10000], np.roll(voice[10000:20000], 25), np.roll(voice[20000:], 75)
    ]
    assert not verify_stem_clock(mix, drifting, sr)["verified"]


def test_periodic_instrument_cannot_verify_clock_by_duration_alone():
    sr = 1000
    y = np.sin(np.arange(30000) * 2 * np.pi * 50 / sr)
    assert not verify_stem_clock(y, y, sr)["verified"]


def test_short_and_nonfinite_waveforms_fail_closed():
    assert not verify_stem_clock(np.ones(100), np.ones(100), 1000)["verified"]
    assert not verify_stem_clock(np.ones(12000), np.full(12000, np.nan), 1000)[
        "verified"
    ]


def test_shadow_disabled_does_not_load_audio(monkeypatch):
    import vocal_continuity as module

    monkeypatch.delenv("VOCAL_CONTINUITY_SHADOW_ENABLED", raising=False)
    monkeypatch.setattr(
        module, "extract_vocal_track", lambda *a: pytest.fail("must not load")
    )
    assert (
        module.observe_vocal_continuity({"segments": [line()]}, "mix", "stem") is None
    )


def test_shadow_records_candidates_without_retiming_and_protects_approval(monkeypatch):
    import vocal_continuity as module

    monkeypatch.setenv("VOCAL_CONTINUITY_SHADOW_ENABLED", "1")
    monkeypatch.setattr(module, "extract_vocal_track", lambda *a: track())
    monkeypatch.setattr(
        module, "measure_tail_periodicity", lambda *a: {"supported": True}
    )
    result = {"segments": [line()]}
    frozen = repr(result)
    report = module.observe_vocal_continuity(result, "mix", "stem")
    assert report["candidates"][0]["proposed_end"] == 3.0
    assert repr(result) == frozen
    result["approved_text_reuse"] = {"used": True}
    monkeypatch.setattr(
        module, "extract_vocal_track", lambda *a: pytest.fail("approved")
    )
    assert (
        module.observe_vocal_continuity(result, "mix", "stem")["reason"]
        == "approved_document_reused"
    )


def test_shadow_fails_closed_without_stem_or_on_decoder_error(monkeypatch):
    import vocal_continuity as module

    monkeypatch.setenv("VOCAL_CONTINUITY_SHADOW_ENABLED", "1")
    result = {"segments": [line()]}
    assert (
        module.observe_vocal_continuity(result, "mix", None)["reason"]
        == "cached_stem_unavailable"
    )

    def fail(*a):
        raise RuntimeError("private audio path must not enter telemetry")

    monkeypatch.setattr(module, "extract_vocal_track", fail)
    report = module.observe_vocal_continuity(result, "mix", "stem")
    assert report["error_type"] == "RuntimeError"
    assert "private audio" not in repr(report)
    assert result["segments"][0]["end"] == 2.0


def test_worker_shadow_uses_cached_stem_and_leaves_times_unchanged(
    monkeypatch, tmp_path
):
    import sys
    from types import SimpleNamespace
    import audio_coverage
    import transcription_worker
    import vocal_continuity as module

    original = tmp_path / "original.wav"
    original.touch()
    monkeypatch.setenv("VOCAL_CONTINUITY_SHADOW_ENABLED", "1")
    monkeypatch.delenv("STABLE_PITCH_TAIL_ENABLED", raising=False)
    monkeypatch.setitem(
        sys.modules, "pipeline", SimpleNamespace(_audio_duration=lambda _: 5.0)
    )
    calls = []

    def cached(path, *, cache_only):
        assert cache_only is True
        calls.append(path)
        return "/cached-stem.wav"

    monkeypatch.setitem(
        sys.modules, "vocal_sep", SimpleNamespace(separate_vocals=cached)
    )
    monkeypatch.setattr(module, "extract_vocal_track", lambda original, stem: track())
    monkeypatch.setattr(
        module, "measure_tail_periodicity", lambda *a: {"supported": True}
    )
    monkeypatch.setattr(
        audio_coverage,
        "summarize",
        lambda *a, **kw: {
            "audio_coverage": 1.0,
            "uncovered_spans": 0,
            "uncovered_seconds": 0.0,
            "worst_span_s": 0.0,
        },
    )
    monkeypatch.setattr(audio_coverage, "voiced_gaps", lambda *a, **kw: [])
    result = {"segments": [line()], "_asr_words": line()["words"]}
    output = transcription_worker._finalize_vocal_continuity(
        result, str(original), fresh_transcription=True
    )
    assert calls == [str(original)]
    assert output["segments"] == [line()]
    assert (
        output["postpass_stats"]["vocal_continuity"]["candidates"][0]["proposed_end"]
        == 3.0
    )


def test_same_energy_and_spectrum_without_periodicity_are_rejected():
    from vocal_continuity import periodicity_support, screen_vocal_extensions

    t = track()
    raw, _ = propose_vocal_endings([line()], t)
    assert raw[0]["proposed_end"] == 3.0
    # Regression mechanism: all energy/spectral gates agree, but there is no
    # reliable periodic witness in the added interval (as in the real case).
    times = np.arange(200) * 0.02
    evidence = periodicity_support(
        times,
        np.full(200, 130.0),
        np.ones(200, dtype=bool),
        np.full(200, 0.01),
        2.0,
        3.0,
    )
    screened, report = screen_vocal_extensions(raw, t, lambda *a: evidence)
    assert screened == []
    assert report["abstention_reasons"] == {"no_reliable_periodicity_in_extension": 1}
    assert line()["end"] == 2.0


@pytest.mark.parametrize(
    "location", ["before", "after", "isolated", "disconnected", "nan"]
)
def test_non_witness_pitch_cannot_certify_extension(location):
    from vocal_continuity import periodicity_support

    times = np.arange(200) * 0.02
    probability = np.zeros(200)
    if location == "before":
        probability[times < 2.06] = 1.0
    elif location == "after":
        probability[times > 2.94] = 1.0
    elif location == "isolated":
        probability[125] = 1.0
    elif location == "disconnected":
        probability[[120, 130]] = 1.0
    else:
        probability[:] = np.nan
    result = periodicity_support(
        times, np.full(200, 220.0), np.ones(200, dtype=bool), probability, 2.0, 3.0
    )
    assert not result["supported"]


def test_adjacent_periodic_frames_allow_changing_note_and_preserve_input():
    from vocal_continuity import periodicity_support, screen_vocal_extensions

    times = np.arange(200) * 0.02
    f0 = np.linspace(180, 400, 200)
    prob = np.zeros(200)
    prob[120:122] = 0.8
    evidence = periodicity_support(times, f0, np.ones(200, dtype=bool), prob, 2.0, 3.0)
    assert evidence["supported"] and evidence["longest_run_frames"] == 2
    raw, _ = propose_vocal_endings([line()], track())
    frozen = repr(raw)
    screened, _ = screen_vocal_extensions(raw, track(), lambda *a: evidence)
    assert screened[0]["proposed_end"] == 3.0
    assert screened[0]["automatic_apply_allowed"] is False
    assert repr(raw) == frozen


def test_screening_handles_extractor_failure_and_unverified_clock():
    from vocal_continuity import screen_vocal_extensions

    t = track()
    raw, _ = propose_vocal_endings([line()], t)

    def fail(*a):
        raise RuntimeError("private file path")

    screened, report = screen_vocal_extensions(raw, t, fail)
    assert not screened
    assert report["abstention_reasons"] == {"periodicity_extraction_failed": 1}
    assert "private file" not in repr(report)
    t.clock["verified"] = False
    assert (
        screen_vocal_extensions(raw, t, lambda *a: pytest.fail("must not decode"))[0]
        == []
    )


def test_pitch_frames_with_gaps_or_invalid_shapes_fail_closed():
    from vocal_continuity import periodicity_support

    times = np.array([2.2, 2.4])
    assert not periodicity_support(
        times, np.ones(2) * 220, np.ones(2, dtype=bool), np.ones(2), 2.0, 3.0
    )["supported"]
    assert not periodicity_support(
        times, np.ones(3) * 220, np.ones(2, dtype=bool), np.ones(2), 2.0, 3.0
    )["supported"]


def test_local_pitch_probe_converts_stem_clock_and_excludes_context(monkeypatch):
    import librosa
    from pathlib import Path
    from vocal_continuity import measure_tail_periodicity

    calls = []

    def load(path, **kw):
        calls.append(kw)
        return np.ones(40000, dtype=np.float32), 16000

    def pyin(*a, **kw):
        assert kw["frame_length"] == 2048 and kw["hop_length"] == 320
        prob = np.zeros(125)
        prob[60:65] = 0.9
        return np.full(125, 220.0), np.ones(125, dtype=bool), prob

    monkeypatch.setattr(librosa, "load", load)
    monkeypatch.setattr(librosa, "pyin", pyin)
    report = measure_tail_periodicity(Path("stem.wav"), 2.0, 3.0, 0.025)
    assert report["supported"]
    assert calls[0]["offset"] == pytest.approx(1.025)
    assert calls[0]["duration"] == pytest.approx(2.5)


def test_recorded_735ms_regression_keeps_original_endpoint():
    """Recorded spectral false positive, without audio, lyrics or song-ID rules."""
    from pathlib import Path
    from vocal_continuity import periodicity_support, screen_vocal_extensions

    fixture = Path(__file__).parent / "fixtures/vocal_continuity/ambiguous_tail.npz"
    with np.load(fixture, allow_pickle=False) as f:
        t = VocalTrack(
            f["times"],
            f["rms"],
            f["spectrum"],
            0.02,
            181.0,
            {"verified": True, "offset_s": 0.0250625},
        )
        evidence = periodicity_support(
            f["pitch_times"], f["f0"], f["voiced"], f["probability"], 179.06, 179.7949
        )
    segments = [
        {
            "start": 177.44,
            "end": 179.06,
            "text": "token",
            "words": [{"word": "token", "start": 177.62, "end": 178.56}],
        },
        {"start": 180.34, "end": 181.0, "text": "next"},
    ]
    policy = ContinuityPolicy(
        energy_ratio=0.15,
        spectral_similarity=0.65,
        minimum_delta_s=0.5,
        direction="extend",
    )
    raw, _ = propose_vocal_endings(segments, t, policy)
    assert raw[0]["proposed_end"] == 179.7949
    assert raw[0]["boundary_spread_s"] == pytest.approx(0.08)
    final, report = screen_vocal_extensions(raw, t, lambda *a: evidence)
    assert final == []
    assert report["rejected_candidates"][0]["evidence"]["reliable_frames"] == 0
    assert segments[0]["end"] == 179.06
