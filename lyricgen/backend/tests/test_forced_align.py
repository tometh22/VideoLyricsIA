"""Unit tests for forced_align — the pure parts (no Replicate network).

The network call (`forced_align_lyrics`) is exercised only for its
disabled/short-circuit branches; the alignment math lives in
`wordstamps_to_segments` and is fully testable offline.
"""

import forced_align as fa


def _w(word, start, end):
    return {"word": word, "start": start, "end": end}


def test_is_enabled_requires_flag_and_token(monkeypatch):
    monkeypatch.delenv("FORCED_ALIGNER_ENABLED", raising=False)
    monkeypatch.delenv("REPLICATE_API_TOKEN", raising=False)
    assert fa.is_enabled() is False
    monkeypatch.setenv("FORCED_ALIGNER_ENABLED", "1")
    assert fa.is_enabled() is False          # flag on but no token
    monkeypatch.setenv("REPLICATE_API_TOKEN", "r8_x")
    assert fa.is_enabled() is True
    monkeypatch.setenv("FORCED_ALIGNER_ENABLED", "0")
    assert fa.is_enabled() is False          # token but flag off


def test_lrc_to_plain_text_strips_timestamps():
    synced = "[00:12.34] primera linea\n[01:05.6] segunda linea\n\n[02:00] tercera"
    assert fa.lrc_to_plain_text(synced) == "primera linea\nsegunda linea\ntercera"
    assert fa.lrc_to_plain_text("") == ""
    assert fa.lrc_to_plain_text(None) == ""


def test_wordstamps_to_segments_reconstructs_lines():
    lines = ["hola mundo", "adios amigo cruel"]
    words = [
        _w("hola", 1.0, 1.4), _w("mundo", 1.4, 2.0),
        _w("adios", 3.0, 3.4), _w("amigo", 3.4, 3.8), _w("cruel", 3.8, 4.5),
    ]
    segs = fa.wordstamps_to_segments(words, lines)
    assert segs == [
        {"start": 1.0, "end": 2.0, "text": "hola mundo"},
        {"start": 3.0, "end": 4.5, "text": "adios amigo cruel"},
    ]


def test_wordstamps_to_segments_clamps_overlap_monotonic():
    lines = ["uno dos", "tres cuatro"]
    # second line starts BEFORE first line's end → clamp first end.
    words = [
        _w("uno", 1.0, 2.5), _w("dos", 2.5, 5.0),
        _w("tres", 4.0, 4.5), _w("cuatro", 4.5, 6.0),
    ]
    segs = fa.wordstamps_to_segments(words, lines)
    assert segs[0]["end"] <= segs[1]["start"]      # no overlap
    assert segs[0]["start"] == 1.0 and segs[1]["end"] == 6.0


def test_wordstamps_to_segments_skips_blank_and_missing():
    lines = ["", "  ", "linea real"]
    words = [_w("linea", 1.0, 1.5), _w("real", 1.5, 2.0)]
    segs = fa.wordstamps_to_segments(words, lines)
    assert segs == [{"start": 1.0, "end": 2.0, "text": "linea real"}]


def test_forced_align_lyrics_returns_none_when_disabled(monkeypatch):
    monkeypatch.delenv("FORCED_ALIGNER_ENABLED", raising=False)
    assert fa.forced_align_lyrics("/tmp/x.mp3", "a\nb\nc\nd") is None


def test_forced_align_lyrics_returns_none_for_short_lyrics(monkeypatch):
    monkeypatch.setenv("FORCED_ALIGNER_ENABLED", "1")
    monkeypatch.setenv("REPLICATE_API_TOKEN", "r8_x")
    # < 4 lines → skip (not worth a call), returns None without network.
    assert fa.forced_align_lyrics("/tmp/x.mp3", "una\ndos") is None
