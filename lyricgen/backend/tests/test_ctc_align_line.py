"""align_line declina sin romper: nunca propone si no puede alinear."""
from __future__ import annotations

import ctc_align


def test_declines_when_ctc_disabled(monkeypatch, tmp_path):
    monkeypatch.setenv("CTC_ALIGN_ENABLED", "0")
    audio = tmp_path / "a.wav"
    audio.write_bytes(b"x")
    result = ctc_align.align_line(str(audio), "hola", 1.0, 3.0)
    assert result["status"] == "declined" and result["reason"] == "ctc_disabled"


def test_declines_missing_audio_empty_text_and_bad_window(monkeypatch, tmp_path):
    monkeypatch.setenv("CTC_ALIGN_ENABLED", "1")
    assert ctc_align.align_line(str(tmp_path / "nope.wav"), "hola", 1, 3)["reason"] == "audio_missing"
    audio = tmp_path / "a.wav"
    audio.write_bytes(b"x")
    assert ctc_align.align_line(str(audio), "  ", 1, 3)["reason"] == "empty_text"
    assert ctc_align.align_line(str(audio), "hola", 1.0, 1.2)["reason"] == "window_out_of_range"
    assert ctc_align.align_line(str(audio), "hola", 0.0, 60.0)["reason"] == "window_out_of_range"
