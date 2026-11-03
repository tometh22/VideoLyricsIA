"""Texto "unknown" en un job en español: pasa al CTC solo con la bandera."""
from __future__ import annotations

import ctc_align

UNKNOWN_TEXT = ["Ra ta ta", "Pum pum", "Na na na", "Oh oh oh"]


def _reaches_motif_check(monkeypatch, tmp_path, **kwargs) -> bool:
    audio = tmp_path / "stem.wav"
    audio.write_bytes(b"x")
    reached = []

    def motif(lines):
        reached.append(True)
        return True                           # corta acá: no carga el modelo

    monkeypatch.setattr(ctc_align, "has_short_repeated_motif", motif)
    segments = [{"text": t, "start": i, "end": i + 1} for i, t in enumerate(UNKNOWN_TEXT)]
    assert ctc_align.retime_segments(str(audio), segments, "job", **kwargs) is None
    return bool(reached)


def test_text_language_is_unknown():
    assert ctc_align.guess_text_lang(UNKNOWN_TEXT) == "unknown"


def test_flag_off_keeps_declining_unknown_text(monkeypatch, tmp_path):
    monkeypatch.setenv("CTC_ALIGN_ENABLED", "1")
    monkeypatch.delenv("CTC_ALIGN_UNKNOWN_LANG_JOB_ES", raising=False)
    assert not _reaches_motif_check(monkeypatch, tmp_path, language_hint="es")


def test_flag_on_aligns_unknown_text_only_for_spanish_jobs(monkeypatch, tmp_path):
    monkeypatch.setenv("CTC_ALIGN_ENABLED", "1")
    monkeypatch.setenv("CTC_ALIGN_UNKNOWN_LANG_JOB_ES", "1")
    assert _reaches_motif_check(monkeypatch, tmp_path, language_hint="es")
    assert not _reaches_motif_check(monkeypatch, tmp_path, language_hint="en")
    assert not _reaches_motif_check(monkeypatch, tmp_path, language_hint="")


def test_flag_never_overrides_detected_english(monkeypatch, tmp_path):
    monkeypatch.setenv("CTC_ALIGN_ENABLED", "1")
    monkeypatch.setenv("CTC_ALIGN_UNKNOWN_LANG_JOB_ES", "1")
    audio = tmp_path / "stem.wav"
    audio.write_bytes(b"x")
    english = ["I want you to know", "that I will be there", "and you are the one", "the night is young"]
    assert ctc_align.guess_text_lang(english) == "en"
    called = []
    monkeypatch.setattr(ctc_align, "has_short_repeated_motif", lambda lines: called.append(1) or True)
    segments = [{"text": t, "start": i, "end": i + 1} for i, t in enumerate(english)]
    assert ctc_align.retime_segments(str(audio), segments, "job", language_hint="es") is None
    assert not called


def test_flag_is_part_of_the_pipeline_fingerprint():
    from transcription_quality import _PIPELINE_CONFIG_KEYS

    assert "CTC_ALIGN_UNKNOWN_LANG_JOB_ES" in _PIPELINE_CONFIG_KEYS
