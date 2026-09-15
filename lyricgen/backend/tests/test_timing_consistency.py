"""Final line↔word consistency pass — karaoke_align.enforce_line_word_consistency.

Origin (UMG Chile, 2026-08-13): an operator manually dragged 48 of 50 lines on
one song because the captions disappeared mid-phrase — "líneas que quedaron
cortas respecto a lo que se escucha".

Root cause: several stages rewrite a segment's start/end and/or its words
independently (anchor_align.build_synced_scaffold, ctc_align, word_vote,
phrase_segmenter, gap_rescue, lead_in) and nothing verified at the end that a
line's window still matched the words it displays. The synced_scaffold builder
is the worst offender by construction — it sets `end = next_line.start - 50ms`,
which has no relationship to when singing stops.

Measured over 60 days of production before the fix:
  ctc_align        1402 lines  median 0.25s  p90 1.58s   worst 79.7s
  synced_scaffold   120 lines  median 0.45s  p90 22.18s  worst 34.1s
~10% of all lines (25% on scaffold) ended BEFORE their last word was sung.
"""
import karaoke_align as ka


def _w(word, start, end, score=0.95):
    return {"word": word, "start": start, "end": end, "score": score}


def _seg(start, end, words, **extra):
    return {"start": start, "end": end, "text": " ".join(w["word"] for w in words),
            "words": words, **extra}


# ---------------------------------------------------------------------------
# The defect that started this
# ---------------------------------------------------------------------------

def test_caption_dying_mid_phrase_is_extended_to_cover_its_words():
    """The real production line: "Ahh ahh ¿Cómo estás?" displayed 19.29→22.79
    while the words are sung 21.94→24.88 — the caption vanished 2s before the
    singer finished it. That is the scaffold's `end = next.start - 50ms`."""
    segs = [_seg(19.2885, 22.7885, [_w("Cómo", 21.94, 22.26), _w("estás", 22.28, 24.88)])]
    out = ka.enforce_line_word_consistency(segs)
    assert out is not segs, "a caption cut mid-phrase must be corrected"
    assert out[0]["end"] == 24.88
    assert out[0]["start"] == 19.2885, "start was fine — must not be touched"
    assert out[0]["timing_snapped_to_words"] is True


def test_absurd_overhang_is_trimmed():
    segs = [_seg(10.0, 40.0, [_w("hola", 10.1, 12.0), _w("mundo", 12.1, 13.5)])]
    out = ka.enforce_line_word_consistency(segs)
    assert out[0]["end"] == 13.5


def test_short_deliberate_hold_is_left_alone():
    """A caption lingering ~1s past the last word is a readability hold
    (lead_in.polish does this on purpose) — under the 2.0s overhang threshold
    it must survive untouched, or the pass would churn healthy lines."""
    segs = [_seg(10.0, 14.4, [_w("hola", 10.1, 12.0), _w("mundo", 12.1, 13.5)])]
    out = ka.enforce_line_word_consistency(segs)
    assert out is segs, "healthy lines must be returned as the same object"


def test_small_underrun_within_tolerance_is_left_alone():
    segs = [_seg(10.0, 13.3, [_w("hola", 10.1, 12.0), _w("mundo", 12.1, 13.5)])]
    out = ka.enforce_line_word_consistency(segs)
    assert out is segs


# ---------------------------------------------------------------------------
# Guards — the pass must never make things worse
# ---------------------------------------------------------------------------

def test_garbage_word_is_ignored_when_computing_the_span():
    """Real production shape: the line "Calla, sólo te quiero," carries a
    trailing token scored 0.001 that runs to 38.24s. Snapping to it would
    stretch one caption across a third of the song.

    Note the mean-confidence helper does NOT catch this (0.738+0.974+0.855+
    0.001 averages 0.64, comfortably over its 0.55 floor) — which is why the
    span is filtered per-word instead. The three real words end at 32.68, so
    that is where the caption should end: extended from 29.943, not stretched
    to 38.24."""
    words = [_w("Calla", 27.14, 29.02, 0.738), _w("sólo", 30.4, 31.04, 0.974),
             _w("quiero", 31.52, 32.68, 0.855), _w("ahah", 32.7, 38.24, 0.001)]
    segs = [_seg(26.443, 29.943, words)]
    out = ka.enforce_line_word_consistency(segs)
    assert out[0]["end"] == 32.68, "must use the last TRUSTWORTHY word, not the noise"
    assert out[0]["end"] != 38.24, "a 0.001-score token must never define the window"


def test_bridge_word_spanning_an_instrumental_is_ignored():
    """CTC sometimes binds one word across a whole instrumental break (see
    ctc_align.repair_bridge_words: 'anzuelo' measured 8.3→35.1s). Such a word
    is not evidence of where the phrase ends."""
    words = [_w("hola", 10.0, 10.6), _w("mundo", 10.7, 11.4),
             _w("aaah", 11.5, 33.0, 0.9)]
    segs = [_seg(10.0, 11.0, words)]
    out = ka.enforce_line_word_consistency(segs)
    assert out[0]["end"] == 11.4, "the 21s bridge word must not define the end"


def test_words_aligned_to_a_different_part_of_the_song_are_rejected():
    """_words_fit_window requires >=25% overlap. Words that landed 100s away
    belong to another phrase — dragging the line there would be far worse than
    leaving it."""
    segs = [_seg(10.0, 14.0, [_w("hola", 120.0, 121.0), _w("mundo", 121.1, 122.0)])]
    out = ka.enforce_line_word_consistency(segs)
    assert out[0]["start"] == 10.0 and out[0]["end"] == 14.0
    assert out[0]["review"] is True


def test_existing_review_flag_is_not_duplicated_or_lost():
    words = [_w("x", 120.0, 121.0)]
    segs = [_seg(10.0, 14.0, words, review=True)]
    out = ka.enforce_line_word_consistency(segs)
    assert out is segs, "already flagged + unfixable → nothing to change"


def test_final_word_consistency_never_overrides_manual_timing():
    words = [_w("manual", 10.0, 13.5)]
    segs = [_seg(10.0, 14.0, words, locked=True)]

    out = ka.enforce_line_word_consistency(segs)

    assert out is segs
    assert out[0]["start"] == 10.0
    assert out[0]["end"] == 14.0


def test_segment_without_words_is_untouched():
    segs = [{"start": 1.0, "end": 99.0, "text": "sin palabras"}]
    assert ka.enforce_line_word_consistency(segs) is segs


def test_malformed_input_never_raises():
    for bad in ([], None, [{"text": "x"}], [{"start": "a", "end": "b", "words": []}],
                ["not a dict"], [{"start": 1, "end": 2, "words": [{"word": "x"}]}]):
        ka.enforce_line_word_consistency(bad)  # must not raise


def test_never_emits_a_degenerate_window():
    """A word span that would collapse the caption to zero/negative length is
    refused — a 0-length caption renders as a flash or not at all."""
    segs = [_seg(10.0, 20.0, [_w("x", 5.0, 5.0)])]
    out = ka.enforce_line_word_consistency(segs)
    for s in out:
        assert float(s["end"]) > float(s["start"])


def test_disabled_by_env_is_a_no_op(monkeypatch):
    monkeypatch.setenv("TIMING_CONSISTENCY_ENABLED", "0")
    segs = [_seg(19.28, 22.78, [_w("Cómo", 21.94, 22.26), _w("estás", 22.28, 24.88)])]
    assert ka.enforce_line_word_consistency(segs) is segs


def test_healthy_song_is_returned_untouched():
    """The median production line is within 0.25s — the pass must not churn a
    well-aligned song (that would invalidate operator work for nothing)."""
    segs = [_seg(1.0, 3.0, [_w("una", 1.05, 1.5), _w("linea", 1.6, 2.95)]),
            _seg(4.0, 6.0, [_w("otra", 4.1, 4.6), _w("linea", 4.7, 5.9)])]
    assert ka.enforce_line_word_consistency(segs) is segs
