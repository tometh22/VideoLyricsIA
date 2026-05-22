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


def test_destretch_trims_ballooned_trailing_word():
    """Hermanos pattern: the model stretches the last word to fill the
    instrumental gap (line held 12 s). De-stretch caps the end back near
    where the word actually started + a normal tail."""
    # Median word ~0.3s; "vos" stretched 0.6 -> 12.0 (11.4s).
    words = [
        _w("Yo", 122.6, 122.9), _w("solo", 122.9, 123.3), _w("quiero", 123.3, 123.8),
        _w("vagar", 123.8, 124.3), _w("con", 124.3, 124.6), _w("vos", 124.6, 134.8),
        _w("Yo", 134.8, 135.1), _w("solo", 135.1, 135.5),  # next line words (median)
    ]
    segs = fa.wordstamps_to_segments(words, ["Yo solo quiero vagar con vos", "Yo solo"])
    line14 = segs[0]
    dur = line14["end"] - line14["start"]
    assert dur < 3.0, f"line should be de-stretched, got {dur:.1f}s"
    assert line14["start"] == 122.6           # sung start preserved
    assert line14["end"] <= 134.8             # never extends past the word


def test_destretch_leaves_normal_lines_untouched():
    words = [
        _w("hola", 1.0, 1.4), _w("mundo", 1.4, 2.0),
        _w("chau", 3.0, 3.4), _w("amigo", 3.4, 4.0),
    ]
    segs = fa.wordstamps_to_segments(words, ["hola mundo", "chau amigo"])
    assert segs[0] == {"start": 1.0, "end": 2.0, "text": "hola mundo"}
    assert segs[1] == {"start": 3.0, "end": 4.0, "text": "chau amigo"}


def test_compress_for_upload_falls_back_on_bad_input():
    """ffmpeg can't transcode a nonexistent file → graceful fallback to the
    original path (never raises, never leaves a temp behind)."""
    path, is_temp = fa._compress_for_upload("/nonexistent/audio.wav")
    assert path == "/nonexistent/audio.wav"
    assert is_temp is False


def test_wordstamps_reanchors_when_model_inserts_extra_word():
    # The model tokenises "rocknroll" as THREE stamps (rock/n/roll) where the
    # lyric line has it as one word. A naive positional walk would consume the
    # wrong stamps from here on (drift). Re-anchoring must keep later lines
    # aligned by matching their tokens forward in the stream.
    lines = ["amo el rocknroll", "vivo de noche", "canto sin parar"]
    words = [
        _w("amo", 1.0, 1.3), _w("el", 1.3, 1.6),
        _w("rock", 1.6, 1.9), _w("n", 1.9, 2.0), _w("roll", 2.0, 2.4),
        _w("vivo", 3.0, 3.3), _w("de", 3.3, 3.5), _w("noche", 3.5, 4.0),
        _w("canto", 5.0, 5.3), _w("sin", 5.3, 5.6), _w("parar", 5.6, 6.2),
    ]
    segs = fa.wordstamps_to_segments(words, lines)
    # Despite the extra token, the LATER lines must land on their real stamps.
    assert len(segs) == 3
    assert segs[1]["text"] == "vivo de noche"
    assert segs[1]["start"] == 3.0 and segs[1]["end"] == 4.0
    assert segs[2]["text"] == "canto sin parar"
    assert segs[2]["start"] == 5.0 and segs[2]["end"] == 6.2


def test_wordstamps_reanchors_when_model_drops_word():
    # The model dropped "muy" — fewer stamps than the lyric word count. The
    # next line must still anchor on its real stamps, not slide late.
    lines = ["hola mundo muy lindo", "adios para siempre"]
    words = [
        _w("hola", 1.0, 1.3), _w("mundo", 1.3, 1.7), _w("lindo", 1.7, 2.2),
        _w("adios", 3.0, 3.4), _w("para", 3.4, 3.7), _w("siempre", 3.7, 4.3),
    ]
    segs = fa.wordstamps_to_segments(words, lines)
    assert len(segs) == 2
    assert segs[1]["text"] == "adios para siempre"
    assert segs[1]["start"] == 3.0 and segs[1]["end"] == 4.3


def test_wordstamps_aborts_on_sustained_drift():
    # The word stream is unrelated noise (nothing matches the lyric lines for
    # several consecutive lines). The detector must give up (return []) so the
    # caller falls back, rather than emit a confidently-wrong drifting result.
    lines = ["primera linea cantada", "segunda linea distinta",
             "tercera estrofa nueva", "cuarta parte final",
             "quinta y ultima aqui"]
    words = [_w(f"ruido{i}", i, i + 0.4) for i in range(25)]
    segs = fa.wordstamps_to_segments(words, lines)
    assert segs == []


def test_forced_align_lyrics_returns_none_when_disabled(monkeypatch):
    monkeypatch.delenv("FORCED_ALIGNER_ENABLED", raising=False)
    assert fa.forced_align_lyrics("/tmp/x.mp3", "a\nb\nc\nd") is None


def test_forced_align_lyrics_returns_none_for_short_lyrics(monkeypatch):
    monkeypatch.setenv("FORCED_ALIGNER_ENABLED", "1")
    monkeypatch.setenv("REPLICATE_API_TOKEN", "r8_x")
    # < 4 lines → skip (not worth a call), returns None without network.
    assert fa.forced_align_lyrics("/tmp/x.mp3", "una\ndos") is None
