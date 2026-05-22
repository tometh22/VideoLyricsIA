"""Unit tests for whisperx_transcribe — pure mapping + gating (no network)."""

import sys
import types
from unittest.mock import MagicMock

import whisperx_transcribe as wx


def _fake_replicate(monkeypatch, *, run):
    """Inject a stand-in `replicate` module (SDK not installed in test venv)."""
    fake = types.ModuleType("replicate")
    fake.run = run
    monkeypatch.setitem(sys.modules, "replicate", fake)


def test_is_enabled_requires_flag_and_token(monkeypatch):
    monkeypatch.delenv("WHISPERX_ENABLED", raising=False)
    monkeypatch.delenv("REPLICATE_API_TOKEN", raising=False)
    assert wx.is_enabled() is False
    monkeypatch.setenv("WHISPERX_ENABLED", "1")
    assert wx.is_enabled() is False
    monkeypatch.setenv("REPLICATE_API_TOKEN", "r8_x")
    assert wx.is_enabled() is True


def test_map_segments_basic_with_words():
    out = {
        "segments": [
            {"start": 0.0, "end": 2.0, "text": " Hola mundo ",
             "words": [
                 {"word": "Hola", "start": 0.0, "end": 1.0, "score": 0.9},
                 {"word": "mundo", "start": 1.0, "end": 2.0, "score": 0.8},
             ]},
            {"start": 2.0, "end": 4.0, "text": "Chau", "words": []},
        ],
        "detected_language": "es",
    }
    segs = wx._map_segments(out)
    assert len(segs) == 2
    assert segs[0]["text"] == "Hola mundo"
    assert segs[0]["words"] == [
        {"word": "Hola", "start": 0.0, "end": 1.0},
        {"word": "mundo", "start": 1.0, "end": 2.0},
    ]
    # Empty words list → no "words" key.
    assert "words" not in segs[1]


def test_map_segments_skips_blank_and_bad_bounds():
    out = {"segments": [
        {"start": 0.0, "end": 1.0, "text": "   "},          # blank → skip
        {"start": "x", "end": 1.0, "text": "bad start"},     # non-numeric → skip
        {"start": 1.0, "end": 2.0, "text": "ok"},
    ]}
    segs = wx._map_segments(out)
    assert [s["text"] for s in segs] == ["ok"]


def test_map_segments_clamps_inverted_bounds():
    out = {"segments": [{"start": 5.0, "end": 3.0, "text": "raro"}]}
    segs = wx._map_segments(out)
    assert segs[0]["start"] == 5.0 and segs[0]["end"] == 5.0


def test_map_segments_drops_words_missing_stamps():
    out = {"segments": [{"start": 0.0, "end": 2.0, "text": "a b",
                         "words": [
                             {"word": "a", "start": 0.0, "end": 1.0},
                             {"word": "b"},  # no stamps (non-alignable) → drop
                         ]}]}
    segs = wx._map_segments(out)
    assert segs[0]["words"] == [{"word": "a", "start": 0.0, "end": 1.0}]


def test_map_segments_handles_non_dict_output():
    assert wx._map_segments(None) == []
    assert wx._map_segments("nope") == []
    # bare list of segments also accepted
    assert wx._map_segments([{"start": 0, "end": 1, "text": "hi"}])[0]["text"] == "hi"


def test_transcribe_none_when_disabled(monkeypatch, tmp_path):
    monkeypatch.delenv("WHISPERX_ENABLED", raising=False)
    f = tmp_path / "a.mp3"
    f.write_bytes(b"x")
    assert wx.transcribe_whisperx(str(f)) is None


def test_transcribe_none_on_thin_result(monkeypatch, tmp_path):
    monkeypatch.setenv("WHISPERX_ENABLED", "1")
    monkeypatch.setenv("REPLICATE_API_TOKEN", "r8_x")
    f = tmp_path / "a.mp3"
    f.write_bytes(b"x")
    _fake_replicate(monkeypatch, run=MagicMock(return_value={"segments": [
        {"start": 0, "end": 1, "text": "only one"}]}))
    assert wx.transcribe_whisperx(str(f)) is None  # < 2 segs → fall back


def test_transcribe_maps_on_success(monkeypatch, tmp_path):
    monkeypatch.setenv("WHISPERX_ENABLED", "1")
    monkeypatch.setenv("REPLICATE_API_TOKEN", "r8_x")
    f = tmp_path / "a.mp3"
    f.write_bytes(b"x")
    out = {"segments": [
        {"start": 0, "end": 1, "text": "uno"},
        {"start": 1, "end": 2, "text": "dos"},
    ]}
    _fake_replicate(monkeypatch, run=MagicMock(return_value=out))
    segs = wx.transcribe_whisperx(str(f), language="es")
    assert [s["text"] for s in segs] == ["uno", "dos"]
