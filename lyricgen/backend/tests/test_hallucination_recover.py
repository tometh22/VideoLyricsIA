"""Tests for the Whisper hallucination detector + auto-recover helpers
that ship with commit "feat: auto-recover from lrclib plain when Whisper
hallucinates" (May 2026).

The path under test handles outlier songs like "El Plan de la Mariposa
— El Riesgo" where Whisper drifts into synonym loops or instrumental-
passage mega-segments and only returns 3 segments for a 4+ minute song.
When this happens AND lrclib has plain lyrics, we replace Whisper's
output with lines distributed evenly across the audio; operator nudges
the timestamps in the editor before approving the render.
"""

from pipeline import (
    _detect_hallucination,
    _has_fuzzy_intra_loop,
    _synthesize_segments_from_plain,
)


def _seg(start, end, text):
    return {"start": start, "end": end, "text": text}


# ----- _has_fuzzy_intra_loop ----------------------------------------------

def test_fuzzy_loop_clean_text_returns_false():
    text = ("Dijo todo bien ya se va a pasar la sombra que cruce que me "
            "anime a ver lo que viene después")
    assert _has_fuzzy_intra_loop(text) is False


def test_fuzzy_loop_exact_repeat_returns_true():
    # Same exact phrase repeated 3 times — what `_truncate_intra_loop` already
    # catches via `==`. New helper must keep catching this case.
    text = ("estaba haciendo y que podia reflexionar "
            "estaba haciendo y que podia reflexionar "
            "estaba haciendo y que podia reflexionar")
    assert _has_fuzzy_intra_loop(text) is True


def test_fuzzy_loop_synonym_swap_returns_true():
    # The "El Plan de la Mariposa" failure mode — same loop with one synonym
    # swap per repetition (reflexionar ↔ pensar). Old `==` check missed this.
    text = ("que podia reflexionar sobre lo que estaba haciendo y "
            "que podia pensar sobre lo que estaba haciendo y "
            "que podia reflexionar sobre lo que estaba haciendo y "
            "que podia pensar sobre lo que estaba haciendo")
    assert _has_fuzzy_intra_loop(text) is True


def test_fuzzy_loop_short_text_returns_false():
    # Below the 12-word minimum — no detection regardless of content.
    assert _has_fuzzy_intra_loop("yo soy yo soy yo soy") is False


# ----- _detect_hallucination ----------------------------------------------

def test_detect_clean_segments_returns_false():
    # 30 plausibly-sized segments over a 240 s song — clearly fine.
    segments = [_seg(i * 8, i * 8 + 7.5, f"line {i}") for i in range(30)]
    is_hall, reason = _detect_hallucination(segments, audio_duration=240.0)
    assert is_hall is False
    assert reason == ""


def test_detect_low_count_for_long_audio_returns_true():
    # 3 segments for 240 s — the El Plan de la Mariposa failure shape.
    segments = [
        _seg(0, 30, "Dijo todo bien"),
        _seg(60, 90, "Que podia reflexionar"),
        _seg(90, 240, "estaba haciendo"),
    ]
    is_hall, reason = _detect_hallucination(segments, audio_duration=240.0)
    assert is_hall is True
    assert "low count" in reason or "implausible" in reason


def test_detect_implausible_long_segment_returns_true():
    # One mega-segment > 15 s with > 40 words — instrumental-passage trap.
    long_text = " ".join(["palabra"] * 50)
    segments = [
        _seg(0, 5, "Intro corto y normal aquí va"),
        _seg(5, 60, long_text),  # 55 s × 50 words → tripped.
    ]
    is_hall, reason = _detect_hallucination(segments, audio_duration=240.0)
    assert is_hall is True
    assert "implausible" in reason or "fuzzy" in reason or "low count" in reason


def test_detect_synonym_intra_loop_returns_true():
    # Plenty of segments + reasonable durations — the only red flag is the
    # synonym intra-loop inside one segment. Loop must contain 3+ near-
    # duplicate windows; we craft 4 to give the Jaccard search clean
    # alignment (real Whisper hallucinations show 4-7 repeats).
    loop = ("que podia reflexionar sobre lo que estaba haciendo y "
            "que podia pensar sobre lo que estaba haciendo y "
            "que podia reflexionar sobre lo que estaba haciendo y "
            "que podia pensar sobre lo que estaba haciendo")
    segments = [_seg(i * 5, i * 5 + 4, f"line {i}") for i in range(20)]
    # 14 s segment with 35 words — under both single-segment thresholds,
    # so only signal 3 (fuzzy intra-loop) can fire.
    segments.insert(8, _seg(45, 59, loop))
    is_hall, reason = _detect_hallucination(segments, audio_duration=240.0)
    assert is_hall is True
    assert "fuzzy" in reason or "intra" in reason


def test_detect_empty_segments_returns_true():
    is_hall, reason = _detect_hallucination([], audio_duration=240.0)
    assert is_hall is True
    assert "empty" in reason


def test_detect_no_audio_duration_skips_count_check():
    # When we don't know the audio duration, the count signal is silent.
    # Two clean segments shouldn't false-positive.
    segments = [_seg(0, 4, "line 1"), _seg(5, 9, "line 2")]
    is_hall, _ = _detect_hallucination(segments, audio_duration=None)
    assert is_hall is False


# ----- _synthesize_segments_from_plain ------------------------------------

PLAIN = (
    "[Verso 1]\n"
    "Dijo todo bien\n"
    "Ya se va a pasar la sombra\n"
    "Que cruce, que me anime a ver\n"
    "\n"
    "[Coro]\n"
    "Que podía reflexionar\n"
    "Sobre lo que estaba haciendo\n"
)


def test_synthesize_full_distribution_starts_at_zero():
    segs = _synthesize_segments_from_plain(PLAIN, audio_duration=100.0)
    # 5 lyric lines, section markers stripped.
    assert len(segs) == 5
    assert segs[0]["text"] == "Dijo todo bien"
    assert segs[0]["start"] == 0.0
    # Last segment ends at or before audio_duration.
    assert segs[-1]["end"] <= 100.0
    # Monotonically increasing.
    for a, b in zip(segs, segs[1:]):
        assert a["end"] <= b["start"] + 0.01


def test_synthesize_anchored_starts_at_anchor():
    segs = _synthesize_segments_from_plain(
        PLAIN, audio_duration=100.0, anchor_start=4.2,
    )
    assert segs[0]["start"] == 4.2
    # 5 lines into 95.8 s of usable audio → ~19.16 s per line.
    assert abs((segs[1]["start"] - segs[0]["start"]) - 19.16) < 0.1


def test_synthesize_strips_section_markers():
    segs = _synthesize_segments_from_plain(PLAIN, audio_duration=60.0)
    texts = [s["text"] for s in segs]
    assert all("[" not in t for t in texts)
    assert "Dijo todo bien" in texts
    assert "Que podía reflexionar" in texts


def test_synthesize_empty_plain_returns_empty_list():
    assert _synthesize_segments_from_plain("", audio_duration=100.0) == []
    assert _synthesize_segments_from_plain("[Verso]\n", audio_duration=100.0) == []


def test_synthesize_no_audio_duration_returns_empty_list():
    assert _synthesize_segments_from_plain(PLAIN, audio_duration=0) == []
    assert _synthesize_segments_from_plain(PLAIN, audio_duration=None) == []


def test_synthesize_clamps_anchor_when_too_close_to_end():
    # Anchor inside the last second falls back to 0 — defensive against
    # bad anchor values from a noisy first Whisper segment.
    segs = _synthesize_segments_from_plain(
        PLAIN, audio_duration=10.0, anchor_start=9.5,
    )
    assert segs[0]["start"] == 0.0
