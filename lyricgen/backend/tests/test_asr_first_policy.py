"""Regression tests for the audio-first transcription policy."""

from pathlib import Path

from pipeline import (
    _anchored_recovery_is_safe,
    _fetch_lrclib_by_audio_evidence,
    _initial_asr_lyrics_hint,
    _plain_lyrics_aligner_enabled,
    _strip_leading_reference_credits,
)


_MAIN_PATH = Path(__file__).resolve().parent.parent / "main.py"


def test_reference_prompt_is_off_by_default(monkeypatch):
    monkeypatch.delenv("WHISPER_REFERENCE_PROMPT_MODE", raising=False)
    assert _initial_asr_lyrics_hint("known lyrics that must not prime ASR") is None


def test_reference_prompt_has_explicit_short_and_full_rollback(monkeypatch):
    reference = "x" * 300
    monkeypatch.setenv("WHISPER_REFERENCE_PROMPT_MODE", "short")
    assert _initial_asr_lyrics_hint(reference) == reference[:120]
    monkeypatch.setenv("WHISPER_REFERENCE_PROMPT_MODE", "full")
    assert _initial_asr_lyrics_hint(reference) == reference


def test_plain_lyrics_aligner_is_on_by_default_with_kill_switch(monkeypatch):
    monkeypatch.delenv("LRCLIB_PLAIN_ALIGNER_ENABLED", raising=False)
    assert _plain_lyrics_aligner_enabled() is True
    monkeypatch.setenv("LRCLIB_PLAIN_ALIGNER_ENABLED", "0")
    assert _plain_lyrics_aligner_enabled() is False


def test_detached_catalogue_credit_is_removed_before_alignment():
    plain = (
        "Primer tema compuesto por el Potro\n\n"
        "Sentado, fumando en un bar y pensando\n"
        "Escribo, mirando tus fotos y extraño"
    )
    cleaned, removed = _strip_leading_reference_credits(plain)
    assert removed == ["Primer tema compuesto por el Potro"]
    assert cleaned.startswith("Sentado, fumando")
    assert "Primer tema" not in cleaned


def test_reference_credit_filter_does_not_delete_real_lyrics():
    for plain in (
        "Esta canción la escribí por vos\nY todavía te espero",
        "Compuesto por piezas rotas\nSigo caminando",
        "Written by the sea\nI found my way home",
    ):
        assert _strip_leading_reference_credits(plain) == (plain, [])


def test_orchestrator_routes_references_through_policy_and_requests_words():
    src = _MAIN_PATH.read_text()
    assert "lyrics_hint=initial_hint" in src
    assert "lyrics_hint=_initial_asr_lyrics_hint(_gemini_pre)" in src
    assert src.count("return_words=True") >= 3
    assert "lyrics_hint=_gemini_pre or None" not in src
    assert "transcribe_path, lang, plain" not in src
    assert "split_long_lines=False" in src


def test_synthetic_recovery_rejects_two_anchors_for_full_song():
    plain = "\n".join(f"line {i}" for i in range(31))
    anchors = [(0, 15.0), (12, 80.0)]
    recovered = [
        {"start": i * 7.0, "end": i * 7.0 + 6.0, "text": f"line {i}"}
        for i in range(31)
    ]
    ok, reason = _anchored_recovery_is_safe(plain, anchors, recovered)
    assert ok is False
    assert "insufficient anchors" in reason


def test_synthetic_recovery_accepts_distributed_short_segments():
    plain = "\n".join(f"line {i}" for i in range(12))
    anchors = [(0, 10.0), (3, 30.0), (7, 70.0), (11, 110.0)]
    recovered = [
        {"start": i * 9.0, "end": i * 9.0 + 5.0, "text": f"line {i}"}
        for i in range(12)
    ]
    assert _anchored_recovery_is_safe(plain, anchors, recovered) == (True, "ok")


def test_synthetic_recovery_rejects_mega_line_even_with_anchors():
    plain = "\n".join(f"line {i}" for i in range(12))
    anchors = [(0, 10.0), (3, 30.0), (7, 70.0), (11, 110.0)]
    recovered = [
        {"start": i * 9.0, "end": i * 9.0 + 5.0, "text": f"line {i}"}
        for i in range(12)
    ]
    recovered[6]["end"] = recovered[6]["start"] + 40.0
    ok, reason = _anchored_recovery_is_safe(plain, anchors, recovered)
    assert ok is False
    assert "too long" in reason


def test_duration_aware_collapse_retries_vad_without_reference(monkeypatch):
    """A 6-segment/4-minute result used to bypass the <=2 local gate."""
    import pipeline

    monkeypatch.setenv("OPENAI_API_KEY", "sk-test")
    monkeypatch.setenv("VAD_CHUNK_ENABLED", "1")
    monkeypatch.delenv("TRANSCRIBE_VAD_FIRST", raising=False)
    monkeypatch.setattr(pipeline, "_audio_duration", lambda _p: 240.0)

    sparse = [
        {"start": i * 30.0, "end": i * 30.0 + 4.0, "text": f"unique line {i}"}
        for i in range(6)
    ]
    healthy = [
        {"start": i * 10.0, "end": i * 10.0 + 4.0, "text": f"healthy line {i}"}
        for i in range(20)
    ]
    monkeypatch.setattr(
        pipeline, "_transcribe_via_openai_api", lambda *a, **k: sparse,
    )
    captured = {}

    def fake_vad(*args, **kwargs):
        captured.update(kwargs)
        return healthy

    monkeypatch.setattr(pipeline, "_vad_chunk_transcribe", fake_vad)
    out = pipeline.transcribe(
        "/tmp/fake.mp3", language="es", lyrics_hint="poison reference",
    )
    assert len(out) == 20
    assert captured["lyrics_hint"] is None


def test_audio_evidence_recovers_reference_for_truncated_artist(monkeypatch):
    import pipeline

    words = (
        "one bright morning we walk beside the river and remember every "
        "promise that the summer carried through the open city windows "
    ).split() * 4
    heard = " ".join(words)
    candidate = {
        "trackName": "The River Picture",
        "artistName": "Rodrigo Example",
        "duration": 227.0,
        "plainLyrics": heard,
        "syncedLyrics": None,
    }
    monkeypatch.setattr(
        pipeline, "_try_lrclib_title_search", lambda _song: [candidate],
    )
    out = _fetch_lrclib_by_audio_evidence(
        "Rodrig", "The River Picture",
        [{"start": 10.0, "end": 200.0, "text": heard}],
        230.0,
    )
    assert out is not None
    assert out["plain"] == heard
    assert out["_matched_artist"] == "Rodrigo Example"
    assert out["_audio_evidence_score"] >= 0.72


def test_audio_evidence_rejects_same_title_with_wrong_words(monkeypatch):
    import pipeline

    heard = " ".join(f"heard{i}" for i in range(80))
    wrong = " ".join(f"other{i}" for i in range(80))
    candidate = {
        "trackName": "Common Title",
        "artistName": "Some Artist",
        "duration": 230.0,
        "plainLyrics": wrong,
        "syncedLyrics": None,
    }
    monkeypatch.setattr(
        pipeline, "_try_lrclib_title_search", lambda _song: [candidate],
    )
    assert _fetch_lrclib_by_audio_evidence(
        "Some", "Common Title",
        [{"start": 10.0, "end": 200.0, "text": heard}],
        230.0,
    ) is None


def test_audio_evidence_rejects_repetitive_asr_before_search(monkeypatch):
    import pipeline

    called = False

    def fake_search(_song):
        nonlocal called
        called = True
        return []

    monkeypatch.setattr(pipeline, "_try_lrclib_title_search", fake_search)
    loop = "same phrase " * 50
    assert _fetch_lrclib_by_audio_evidence(
        "Artist", "Title",
        [{"start": 0.0, "end": 200.0, "text": loop}],
        200.0,
    ) is None
    assert called is False
