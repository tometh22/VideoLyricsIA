import targeted_consensus as tc
import vocal_sep


def words(text, start=10.0, step=0.7):
    return [
        {"word": token, "start": start + i * step, "end": start + i * step + 0.5}
        for i, token in enumerate(text.split())
    ]


def event_words(lines, starts):
    out = []
    for line, start in zip(lines, starts):
        out.extend(words(line, start=start, step=0.35))
    return out


def gemini_events(lines, starts):
    return [
        {"start": start, "end": start + 1.2, "text": line, "kind": "sung"}
        for line, start in zip(lines, starts)
    ]


def test_consensus_requires_stem_and_a_second_source():
    agreed, evidence = tc.choose_consensus(
        words("hoy temprano pienso"), words("hoy temprano pienso"), []
    )
    assert agreed
    assert evidence["sources"] == ["stem", "mix"]
    assert tc.choose_consensus(words("hoy temprano pienso"), words("otra cosa distinta"), [])[0] is None
    assert tc.choose_consensus(
        words("hoy temprano estuve pensando en vos"),
        words("muy temprano estuve pensando en vos"), [],
    )[0] is None


def test_slowed_stem_can_win_only_with_independent_corroboration():
    normal = words("muy temprano pienso")
    slowed = words("hoy temprano pienso")
    mix = words("hoy temprano pienso")
    agreed, evidence = tc.choose_consensus(
        normal, mix, [], slowed_words=slowed,
    )
    assert agreed == slowed
    assert evidence["sources"] == ["slowed_stem", "mix"]
    assert tc.choose_consensus(
        normal, [], [], slowed_words=slowed,
    )[0] is None
    agreed, evidence = tc.choose_consensus(
        [], [], [], slowed_words=slowed,
        witness_words=words("hoy temprano pienso"),
    )
    assert agreed == slowed
    assert evidence["sources"] == ["slowed_stem", "witness"]


def test_slowed_timestamp_mapping_returns_to_original_clock(tmp_path, monkeypatch):
    stem = tmp_path / "stem.wav"
    stem.write_bytes(b"audio")

    monkeypatch.setattr(tc.subprocess, "run", lambda *_a, **_k: None)
    heard = words("hoy temprano pienso", start=1.0, step=1.0)
    mapped = tc._transcribe_slowed_window(
        str(stem), 40.0, 10.0, 0.88, "es", "job",
        lambda *_args: heard,
    )
    assert mapped[0]["start"] == 40.88
    assert mapped[0]["end"] == 41.32


def test_reprocess_suggests_but_never_destroys_a_mismatched_line():
    result = {
        "segments": [{"start": 10, "end": 13, "text": "texto totalmente malo"}],
        "_asr_words": words("hoy temprano pienso"),
    }

    def transcribe(path, *_args):
        return words("hoy temprano pienso") if path in {"stem.wav", "mix.wav"} else []

    output, stats = tc.reprocess(
        result, "mix.wav",
        [{"start": 9, "end": 14, "segment_indices": [0]}],
        transcribe_fn=transcribe, stem_path="stem.wav",
    )
    assert output["segments"][0]["text"] == "texto totalmente malo"
    assert output["segments"][0]["consensus_suggestion"] == "hoy temprano pienso"
    assert output["segments"][0]["review"] is True
    assert stats["lines_replaced"] == 0
    assert stats["lines_suggested"] == 1
    assert stats["asr_calls"] == 2
    assert stats["provider_attempts"] == 2
    assert stats["submitted_audio_seconds"] == 10.0


def test_reprocess_respects_cost_budget(monkeypatch):
    monkeypatch.setenv("TARGETED_CONSENSUS_MAX_BILLED_SECONDS", "5")
    result = {"segments": [], "_asr_words": []}
    output, stats = tc.reprocess(
        result, "mix.wav", [{"start": 0, "end": 10}],
        transcribe_fn=lambda *_: [], stem_path="stem.wav",
    )
    assert output["segments"] == result["segments"]
    assert stats["asr_calls"] == 0
    assert "cost_budget" in stats["declined"]


def test_reprocess_reports_windows_clipped_by_hard_limit(monkeypatch):
    monkeypatch.setenv("TARGETED_CONSENSUS_MAX_CLIP_SECONDS", "10")
    _output, stats = tc.reprocess(
        {"segments": [], "_asr_words": []}, "mix.wav",
        [{"start": 0, "end": 20}],
        transcribe_fn=lambda *_: [], stem_path="stem.wav",
    )
    assert stats["truncated_windows"] == 1
    assert "window_truncated" in stats["declined"]


def test_gap_candidate_is_dark_in_observe_and_reviewable_in_enforce(monkeypatch):
    window = [{"start": 9, "end": 14, "reasons": ["voiced_gap"]}]
    def transcribe(*_args):
        return words("hoy temprano pienso")

    monkeypatch.setenv("TRANSCRIPTION_QUALITY_MODE", "observe")
    observed, stats = tc.reprocess(
        {"segments": [], "_asr_words": []}, "mix.wav", window,
        transcribe_fn=transcribe, stem_path="stem.wav",
    )
    assert observed["segments"] == []
    assert stats["lines_suggested"] >= 1

    monkeypatch.setenv("TRANSCRIPTION_QUALITY_MODE", "enforce")
    enforced, stats = tc.reprocess(
        {"segments": [], "_asr_words": []}, "mix.wav", window,
        transcribe_fn=transcribe, stem_path="stem.wav",
    )
    assert stats["lines_inserted"] == 0
    assert stats["lines_suggested"] >= 1
    assert enforced["segments"] == []


def test_slowed_and_same_model_witness_only_suggest_insertion(
        monkeypatch):
    recovered = words("real wow wow", start=60.0)
    monkeypatch.setenv("TRANSCRIPTION_QUALITY_MODE", "enforce")
    monkeypatch.setenv("TARGETED_SLOW_STEM_ENABLED", "1")
    monkeypatch.setattr(
        tc, "_transcribe_slowed_window", lambda *_a, **_k: recovered,
    )
    result = {
        "segments": [], "_asr_words": [],
        "_independent_asr_words": recovered,
    }
    out, stats = tc.reprocess(
        result, "mix.wav",
        [{"start": 59, "end": 64,
          "reasons": ["independent_uncovered_asr"]}],
        transcribe_fn=lambda *_a, **_k: [], stem_path="stem.wav",
    )
    assert stats["lines_inserted"] == 0
    assert stats["lines_suggested"] == 1
    assert out["segments"] == []


def test_cross_model_primary_can_confirm_bounded_insertion(monkeypatch):
    recovered = words("real wow wow", start=60.0)
    monkeypatch.setenv("TRANSCRIPTION_QUALITY_MODE", "enforce")
    result = {
        "segments": [], "_asr_words": recovered,
        "live_audio_truth": True,
    }
    out, stats = tc.reprocess(
        result, "mix.wav",
        [{"start": 59, "end": 64,
          "reasons": ["uncovered_asr"]}],
        transcribe_fn=lambda *_a, **_k: recovered, stem_path="stem.wav",
    )
    assert stats["lines_inserted"] == 1
    assert out["segments"][0]["text"] == "real wow wow"
    assert "primary" in out["segments"][0]["consensus_sources"]


def test_full_song_witness_reserves_budget_before_targeted_windows(monkeypatch):
    monkeypatch.setenv("LIVE_ASR_MAX_BILLED_SECONDS", "600")
    result = {
        "segments": [], "_asr_words": [],
        "postpass_stats": {
            "word_vote": {"audio_seconds_billed": 590.0},
        },
    }
    out, stats = tc.reprocess(
        result, "mix.wav", [{"start": 0, "end": 10}],
        transcribe_fn=lambda *_a, **_k: (_ for _ in ()).throw(
            AssertionError("reserved budget must prevent another ASR call")
        ),
        stem_path="stem.wav",
    )
    assert out["segments"] == []
    assert stats["prior_audio_seconds_billed"] == 590.0
    assert "cost_budget" in stats["declined"]


def test_owned_cached_stem_is_removed_after_retry(tmp_path, monkeypatch):
    stem = tmp_path / "cached-stem.wav"
    stem.write_bytes(b"stem")
    monkeypatch.setattr(
        vocal_sep, "separate_vocals", lambda *_a, **_k: str(stem),
    )
    tc.reprocess(
        {"segments": [], "_asr_words": []}, "mix.wav",
        [{"start": 0, "end": 5}], transcribe_fn=lambda *_a, **_k: [],
    )
    assert not stem.exists()


def test_cross_model_cardinality_repairs_repeated_motif_monotonically():
    starts = [60.8, 64.0, 67.2, 73.2]
    lines = ["Real wow wow"] * 4
    segments = [
        {"start": start, "end": start + 1.4,
         "text": "Real", "live_structural_suggestion": "Real wow wow"}
        for start in starts
    ] + [{
        "start": 88.0, "end": 89.0, "text": "Real",
        "live_structural_suggestion": "Real wow wow",
    }]
    slow = event_words(lines, starts)
    support = event_words(["Real oh oh"] * 4, starts)
    repaired, stats = tc._repair_structural_repetition(
        segments, slow, gemini_events(lines, starts), support,
        window_start=59.0, window_end=85.0, enforce=True,
    )
    assert stats["applied"] is True
    assert stats["events"] == 4
    assert len(repaired) == 5
    assert [segment["start"] for segment in repaired[:4]] == starts
    assert all(segment["text"] == "Real wow wow" for segment in repaired[:4])
    assert repaired[4]["text"] == "Real"  # unmatched event is never deleted
    assert all(segment["consensus_sources"] == [
        "slowed_stem_whisper_1", "gemini_audio",
    ] for segment in repaired[:4])


def test_structural_repair_declines_when_models_disagree_on_repeat_count():
    starts = [60.8, 64.0, 67.2, 73.2]
    lines = ["Real wow wow"] * 4
    segments = [{
        "start": 62.0, "end": 67.0, "text": "Real real",
        "live_structural_suggestion": "Real wow wow",
    }]
    original = [dict(segment) for segment in segments]
    repaired, stats = tc._repair_structural_repetition(
        segments, event_words(lines, starts),
        gemini_events(lines[:3], starts[:3]), event_words(lines, starts),
        window_start=59.0, window_end=85.0, enforce=True,
    )
    assert repaired == original
    assert stats["applied"] is False
    assert stats["reason"] == "cardinality_disagreement"


def test_structural_repair_is_suggestion_only_in_observe_mode():
    starts = [60.8, 64.0]
    lines = ["Real wow wow"] * 2
    segments = [{
        "start": 62.0, "end": 67.0, "text": "Real real",
        "live_structural_suggestion": "Real wow wow",
    }]
    repaired, stats = tc._repair_structural_repetition(
        segments, event_words(lines, starts), gemini_events(lines, starts),
        event_words(lines, starts), window_start=59.0, window_end=70.0,
        enforce=False,
    )
    assert repaired == segments
    assert stats["suggested"] is True
    assert stats["applied"] is False


def test_reprocess_applies_gemini_verified_structural_block(monkeypatch):
    starts = [60.8, 64.0, 67.2, 73.2]
    lines = ["Real wow wow"] * 4
    slow = event_words(lines, starts)
    support = event_words(["Real oh oh"] * 4, starts)
    monkeypatch.setenv("TRANSCRIPTION_QUALITY_MODE", "enforce")
    monkeypatch.setenv("TARGETED_SLOW_STEM_ENABLED", "1")
    monkeypatch.setenv("TARGETED_GEMINI_VERIFY_ENABLED", "1")
    monkeypatch.setenv("TARGETED_STRUCTURAL_AUTOREPAIR_ENABLED", "1")
    monkeypatch.setenv("TARGETED_CONSENSUS_MAX_BILLED_SECONDS", "180")
    monkeypatch.setattr(
        tc, "_transcribe_slowed_window", lambda *_a, **_k: slow,
    )
    result = {
        "segments": [
            {"start": start, "end": start + 1.4,
             "text": "Real", "live_structural_suggestion": "Real wow wow"}
            for start in starts
        ],
        "_asr_words": support,
    }
    out, stats = tc.reprocess(
        result, "mix.wav",
        [{"start": 59, "end": 85,
          "reasons": ["live_structural_disagreement"]}],
        transcribe_fn=lambda *_a, **_k: support,
        gemini_fn=lambda *_a, **_k: gemini_events(lines, starts),
        stem_path="stem.wav",
    )
    assert stats["structural_repairs"] == 1
    assert stats["structural_events"] == 4
    assert len(out["segments"]) == 4
