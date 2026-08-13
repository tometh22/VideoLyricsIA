"""Tests for `transcribe_postprocess.normalize_words` — the conditional
word-strip policy that protects Bug B (line 3741 of main.py used to
strip per-word stamps unconditionally, killing karaoke + confidence).
"""
from transcribe_postprocess import normalize_words


def test_preserves_words_when_score_present():
    """Bug B regression: FA wordstamps with score MUST NOT be stripped.
    The editor's karaoke + confidence highlighting needs these."""
    segs = [{
        "start": 1.0, "end": 3.0, "text": "hola mundo",
        "words": [
            {"word": "hola", "start": 1.0, "end": 1.5, "score": 0.9},
            {"word": "mundo", "start": 1.5, "end": 3.0, "score": 0.95},
        ],
    }]
    out = normalize_words(segs)
    assert "words" in out[0]
    assert len(out[0]["words"]) == 2
    assert out[0]["words"][0]["score"] == 0.9


def test_strips_words_when_no_score():
    """Whisper-raw words (no score) get stripped — they're redundant
    with the line-level start/end and add ~30% JSON bloat for nothing."""
    segs = [{
        "start": 1.0, "end": 3.0, "text": "hola mundo",
        "words": [
            {"word": "hola", "start": 1.0, "end": 1.5},
            {"word": "mundo", "start": 1.5, "end": 3.0},
        ],
    }]
    out = normalize_words(segs)
    assert "words" not in out[0]
    assert out[0]["text"] == "hola mundo"
    assert out[0]["start"] == 1.0


def test_no_words_passthrough():
    """Segments without `words` (lrclib synced lines, etc.) pass through
    unchanged. No spurious key addition or mutation."""
    segs = [{"start": 0, "end": 1, "text": "x"}]
    out = normalize_words(segs)
    assert out == segs


def test_mixed_score_keeps_words():
    """A FA segment can have some words without `score` (the model
    occasionally omits it for very short words). If ANY word has score,
    keep them all — partial confidence is still useful information."""
    segs = [{
        "start": 1.0, "end": 3.0, "text": "hola mundo",
        "words": [
            {"word": "hola", "start": 1.0, "end": 1.5, "score": 0.9},
            {"word": "mundo", "start": 1.5, "end": 3.0},   # no score
        ],
    }]
    out = normalize_words(segs)
    assert "words" in out[0]
    assert len(out[0]["words"]) == 2


def test_does_not_mutate_input():
    """Pure function — caller can keep using the original list."""
    segs = [{"start": 0, "end": 1, "text": "x",
             "words": [{"word": "x", "start": 0, "end": 1}]}]
    snapshot = [dict(s) for s in segs]
    normalize_words(segs)
    assert segs == snapshot
