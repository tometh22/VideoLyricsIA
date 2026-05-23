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


def test_filter_ghosts_drops_short_oneword_segments():
    # The El Arbol smoke produced an 'Amén' at 5.15s for 0.18s (1 word).
    # That's whisperX false-flagging an instrumental sound as speech.
    segs = [
        {"start": 5.15, "end": 5.33, "text": "Amén"},                   # ghost
        {"start": 53.27, "end": 68.80, "text": "Después de tanto vagar"},  # real
        {"start": 70.0, "end": 70.1, "text": "x"},                       # ghost
    ]
    kept = wx._filter_ghosts(segs)
    assert [s["text"] for s in kept] == ["Después de tanto vagar"]


def test_filter_ghosts_keeps_sustained_chant():
    # A real chanted vocalisation can be one word held longer than 0.5s
    # (e.g., '¡Karol!' in a chorus) — must NOT be filtered.
    segs = [{"start": 10.0, "end": 10.7, "text": "Karol"}]
    assert wx._filter_ghosts(segs) == segs


def _w(word, start, end):
    return {"word": word, "start": start, "end": end}


def test_split_long_segments_at_biggest_gap():
    # Real El Arbol shape: a 25-word segment held for 16s. WhisperX's native
    # segmentation lumps 'Después de tanto vagar por las calles' together
    # with 'La ciudad te parece tan gris'. The split should land at the
    # natural pause between them.
    words = [
        _w("Después", 53.27, 53.6), _w("de", 53.6, 53.7),
        _w("tanto", 53.7, 54.0), _w("vagar", 54.0, 54.5),
        _w("por", 54.5, 54.7), _w("las", 54.7, 54.85),
        _w("calles", 54.85, 56.77),
        # 1.5s instrumental gap here (sung phrase boundary)
        _w("La", 58.30, 58.5), _w("ciudad", 58.5, 58.9),
        _w("te", 58.9, 59.0), _w("parece", 59.0, 59.5),
        _w("tan", 59.5, 59.8), _w("gris", 59.8, 60.5),
    ]
    seg = {"start": 53.27, "end": 60.5,
           "text": "Después de tanto vagar por las calles La ciudad te parece tan gris",
           "words": words}
    # Use a low max_dur to force the split for the test.
    out = wx._split_long_segments([seg], max_dur=5.0, min_split_gap=0.3)
    assert len(out) == 2
    assert out[0]["text"].startswith("Después") and out[0]["text"].endswith("calles")
    assert out[1]["text"].startswith("La") and out[1]["text"].endswith("gris")
    assert out[0]["end"] == 56.77
    assert out[1]["start"] == 58.30


def test_split_long_segments_does_not_split_continuous_speech():
    # No internal pause >= min_split_gap: leave the segment alone even if long.
    words = [_w(f"w{i}", i * 0.3, (i + 1) * 0.3) for i in range(40)]  # ~12s, gaps ≈ 0s
    seg = {"start": 0.0, "end": 12.0, "text": "x" * 40, "words": words}
    out = wx._split_long_segments([seg], max_dur=5.0, min_split_gap=0.3)
    assert out == [seg]


def test_split_long_segments_passes_through_short_or_wordless():
    short = {"start": 0.0, "end": 4.0, "text": "ok", "words": [_w("ok", 0.0, 4.0)]}
    no_words = {"start": 0.0, "end": 20.0, "text": "no word stamps"}
    out = wx._split_long_segments([short, no_words], max_dur=5.0)
    assert out == [short, no_words]


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
