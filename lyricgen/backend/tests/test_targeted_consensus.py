import targeted_consensus as tc


def words(text, start=10.0, step=0.7):
    return [
        {"word": token, "start": start + i * step, "end": start + i * step + 0.5}
        for i, token in enumerate(text.split())
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
    assert stats["lines_inserted"] >= 1
    assert enforced["segments"][0]["review"] is True
