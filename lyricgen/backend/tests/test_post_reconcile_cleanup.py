"""Unit tests for post_reconcile helpers."""

from post_reconcile import (
    _is_adlib_loop,
    _split_adlib_by_time,
    _split_at_word_gaps,
    post_reconcile_cleanup,
    ADLIB_MIN_DUR,
    ADLIB_CHUNK_DUR,
    LONG_LINE_MIN_WORDS,
    LONG_LINE_MIN_DUR,
    LONG_LINE_GAP_THRESH,
    STRETCH_MARGIN,
)


def _w(word, start, end=None):
    return {"word": word, "start": start, "end": (end or start + 0.3)}


def _seg(text, start, end, words=None):
    s = {"text": text, "start": start, "end": end}
    if words is not None:
        s["words"] = words
    return s


# ──────────────────────────────────────────────────────────────────────────────
# _split_adlib_by_time
# ──────────────────────────────────────────────────────────────────────────────

def test_adlib_chunker_splits_26s_block():
    """The archetypal 'No Hay Santos' pattern: one 26-second uh-block."""
    words = [_w("uh", 69.04 + i * 1.87) for i in range(14)]
    words.append(_w("uh-oh-oh-oh-oh-oh-oh", 94.2, 95.22))
    seg = _seg("Uh, uh, uh, uh, uh, uh, uh, uh, uh, uh, uh, uh, uh, uh., uh-oh-oh-oh-oh-oh-oh",
               69.04, 95.22, words)
    chunks = _split_adlib_by_time(seg, words)
    assert len(chunks) >= 4, f"expected ≥4 chunks, got {len(chunks)}"
    # Each chunk should be roughly ADLIB_CHUNK_DUR long (within 2.5×).
    for c in chunks:
        dur = c["end"] - c["start"]
        assert dur < ADLIB_CHUNK_DUR * 2.5, f"chunk too long: {dur:.1f}s"
    # No timing overlaps.
    for i in range(len(chunks) - 1):
        assert chunks[i]["end"] <= chunks[i + 1]["start"] + 0.01
    # Total span preserved.
    assert chunks[0]["start"] == seg["start"]
    assert chunks[-1]["end"] == seg["end"]


def test_adlib_chunker_no_words_falls_back_to_equal_split():
    # Each chunk must get a SHORT text slice (≤ ~3 tokens), NOT the full
    # mega-text — otherwise _has_fuzzy_intra_loop still fires on 15-token text.
    seg = _seg("uh uh uh uh uh uh uh uh uh uh", 70.0, 90.0, words=None)
    chunks = _split_adlib_by_time(seg, [])
    assert len(chunks) >= 4
    # Every chunk must have fewer tokens than the full segment text.
    full_tokens = len(seg["text"].split())
    for c in chunks:
        assert len(c["text"].split()) < full_tokens, (
            f"chunk text has same token count as full text: {c['text']!r}"
        )


def test_adlib_chunker_short_segment_not_split():
    """A 4-second ad-lib block should stay as-is (< ADLIB_MIN_DUR)."""
    words = [_w("uh", 70.0 + i * 0.5) for i in range(8)]
    seg = _seg("uh uh uh uh uh uh uh uh", 70.0, 74.0, words)
    # The post_reconcile_cleanup gate would not call the splitter here,
    # but verify the splitter itself still returns a reasonable count.
    chunks = _split_adlib_by_time(seg, words)
    # 4 seconds / 3.5 chunk ≈ 1-2 chunks — should not over-fragment.
    assert len(chunks) <= 2


# ──────────────────────────────────────────────────────────────────────────────
# _split_at_word_gaps
# ──────────────────────────────────────────────────────────────────────────────

def test_gap_splitter_two_phrases():
    """'Iluminados por el fuego | que dejaste arder' with 0.8s gap."""
    words = [
        _w("iluminados", 17.0, 17.6),
        _w("por", 17.6, 17.8),
        _w("el", 17.8, 17.9),
        _w("fuego", 17.9, 18.4),      # 0.8s gap after here
        _w("que", 19.2, 19.4),
        _w("dejaste", 19.4, 19.9),
        _w("arder", 19.9, 20.5),
    ]
    seg = _seg("Iluminados por el fuego que dejaste arder", 17.0, 22.34, words)
    parts = _split_at_word_gaps(seg, words)
    assert len(parts) == 2
    assert parts[0]["text"] == "Iluminados por el fuego"
    assert parts[1]["text"] == "que dejaste arder"
    assert parts[0]["end"] == words[3]["end"]    # ends at "fuego"
    assert parts[1]["start"] == words[4]["start"]  # starts at "que"


def test_gap_splitter_three_phrases():
    """'Como tantos vas | buscando las respuestas | que nada te responden.'"""
    words = [
        _w("como", 41.06, 41.3),
        _w("tantos", 41.3, 41.8),
        _w("vas", 41.8, 42.1),         # 0.8s gap
        _w("buscando", 42.9, 43.5),
        _w("las", 43.5, 43.7),
        _w("respuestas", 43.7, 44.4),  # 0.8s gap
        _w("que", 45.2, 45.4),
        _w("nada", 45.4, 45.7),
        _w("te", 45.7, 45.9),
        _w("responden", 45.9, 46.5),
    ]
    seg = _seg("Como tantos vas buscando las respuestas que nada te responden.",
               41.06, 48.34, words)
    parts = _split_at_word_gaps(seg, words)
    assert len(parts) == 3
    assert parts[0]["text"] == "Como tantos vas"
    assert parts[1]["text"] == "buscando las respuestas"
    assert "que nada te responden" in parts[2]["text"]


def test_gap_splitter_no_gap_returns_original():
    words = [
        _w("hola", 1.0, 1.3),
        _w("como", 1.3, 1.6),
        _w("estas", 1.6, 1.9),
        _w("che", 1.9, 2.2),
        _w("amigo", 2.2, 2.6),
        _w("mio", 2.6, 2.9),
        _w("querido", 2.9, 3.3),
    ]
    seg = _seg("hola como estas che amigo mio querido", 1.0, 3.5, words)
    parts = _split_at_word_gaps(seg, words)  # max gap = 0.0s between tight words
    assert len(parts) == 1
    assert parts[0] is seg


def test_gap_splitter_too_few_words():
    words = [_w("hola", 1.0), _w("mundo", 1.5)]
    seg = _seg("hola mundo", 1.0, 2.0, words)
    assert _split_at_word_gaps(seg, words) == [seg]


def test_gap_splitter_rejects_words_outside_reconciled_line_bounds():
    """La Foto regression: ordinal word reattachment can cross line bounds.

    Splitting this mapping used to create a fragment whose start was after its
    end; the later global timestamp sort then mixed it into the next line.
    """
    words = [
        _w("no", 35.97, 36.57),
        _w("quiero", 37.63, 38.10),
        _w("pasar", 38.20, 38.70),
        _w("de", 38.75, 38.90),
        _w("valiente", 39.00, 39.50),
        _w("a", 39.55, 39.65),
        _w("cobarde", 41.19, 41.60),  # outside the segment's 41.14 end
    ]
    seg = _seg(
        "no quiero, pasar de valiente a cobarde",
        35.97,
        41.14,
        words,
    )
    assert _split_at_word_gaps(seg, words) == [seg]


# ──────────────────────────────────────────────────────────────────────────────
# post_reconcile_cleanup (full pipeline)
# ──────────────────────────────────────────────────────────────────────────────

def test_cleanup_splits_adlib_mega_block():
    words = [_w("uh", 69.04 + i * 1.87) for i in range(14)]
    words.append(_w("uh-oh-oh-oh-oh-oh-oh", 94.2, 95.22))
    mega = _seg("Uh, uh, uh, uh, uh, uh, uh, uh, uh, uh, uh, uh, uh, uh., uh-oh-oh-oh-oh-oh-oh",
                69.04, 95.22, words)
    normal = _seg("Tomas del miedo tu don", 102.011, 105.511,
                  [_w("tomas", 102.011), _w("del", 102.3), _w("miedo", 102.6),
                   _w("tu", 103.0), _w("don", 103.3, 103.7)])
    out = post_reconcile_cleanup([mega, normal])
    assert len(out) > 2, "mega-block should have been split"
    # Normal segment preserved.
    assert out[-1]["text"] == "Tomas del miedo tu don"


def test_cleanup_splits_merged_phrases():
    words = [
        _w("iluminados", 17.0, 17.6), _w("por", 17.6, 17.8),
        _w("el", 17.8, 17.9), _w("fuego", 17.9, 18.4),
        _w("que", 19.2, 19.4), _w("dejaste", 19.4, 19.9),
        _w("arder", 19.9, 20.5),
    ]
    merged = _seg("Iluminados por el fuego que dejaste arder", 17.0, 22.34, words)
    short = _seg("¿Para qué?", 24.3, 25.02,
                 [_w("para", 24.3), _w("que", 24.6)])
    out = post_reconcile_cleanup([merged, short])
    assert len(out) == 3  # merged → 2 parts + short stays as 1
    assert out[0]["text"] == "Iluminados por el fuego"
    assert out[1]["text"] == "que dejaste arder"
    assert out[2]["text"] == "¿Para qué?"


def test_cleanup_can_preserve_canonical_line_structure():
    words = [
        _w("sentado", 15.87, 16.40),
        _w("fumando", 17.00, 17.60),
        _w("en", 18.40, 18.55),
        _w("un", 18.60, 18.75),
        _w("bar", 18.80, 19.10),
        _w("y", 19.20, 19.30),
        _w("pensando", 19.40, 20.23),
    ]
    canonical = _seg(
        "Sentado, fumando en un bar y pensando",
        15.87,
        20.40,
        words,
    )
    out = post_reconcile_cleanup(
        [canonical],
        split_long_lines=False,
    )
    assert len(out) == 1
    assert out[0]["text"] == canonical["text"]
    # Safe cleanup still tightens the display end to the last acoustic word.
    assert out[0]["end"] == 20.23


def test_cleanup_does_not_split_normal_segments():
    segs = [
        _seg("¿Para qué?", 57.12, 58.57,
             [_w("para", 57.12), _w("que", 57.5)]),
        _seg("¿Para qué tus santos de papel?", 26.08, 29.22,
             [_w("para", 26.08), _w("que", 26.4), _w("tus", 26.7),
              _w("santos", 26.9), _w("de", 27.3), _w("papel", 27.5)]),
    ]
    out = post_reconcile_cleanup(segs)
    assert len(out) == len(segs), "normal segments must not be split"


def test_stretch_trimmer_real_pattern():
    """Whisper's decoder boundary trails the last acoustic word."""
    seg_a = _seg("Para qué tus santos de papel", 26.08, 33.5,
                 [_w("para", 26.08), _w("que", 26.4), _w("tus", 26.7),
                  _w("santos", 26.9), _w("de", 27.3), _w("papel", 27.5, 28.0)])
    seg_b = _seg("Iluminados por el fuego", 36.1, 38.5,
                 [_w("iluminados", 36.1), _w("por", 36.5), _w("el", 36.7), _w("fuego", 36.9)])
    out = post_reconcile_cleanup([seg_a, seg_b])
    # Close on the actual last word, not next_start - margin. The old behavior
    # extended this line to 36.02 s and painted through the silence.
    assert out[0]["end"] == 28.0
    assert out[1]["start"] == 36.1  # next seg untouched
    assert out[0]["start"] == 26.08  # start untouched


def test_stretch_trimmer_small_gap_untouched():
    """Word evidence wins even when the following silence is short."""
    seg_a = _seg("Normal phrase", 10.0, 14.0, [_w("normal", 10.0), _w("phrase", 10.5)])
    seg_b = _seg("Next phrase", 15.0, 17.0, [_w("next", 15.0), _w("phrase", 15.4)])
    out = post_reconcile_cleanup([seg_a, seg_b])
    assert out[0]["end"] == 10.8


def test_cleanup_never_extends_wordless_segment_across_instrumental_gap():
    """ROTOR regression: a line ending at 84 s must not reach the next verse."""
    seg_a = _seg("Last line before instrumental", 77.1, 84.16, words=None)
    seg_b = _seg("Verse returns", 127.60, 132.0, words=None)
    out = post_reconcile_cleanup([seg_a, seg_b])
    assert out[0]["end"] == 84.16
    assert out[1]["start"] == 127.60


def test_cleanup_clamps_true_overlap_without_extending():
    seg_a = _seg("Overlapping line", 10.0, 15.5, words=None)
    seg_b = _seg("Next line", 15.0, 17.0, words=None)
    out = post_reconcile_cleanup([seg_a, seg_b])
    assert out[0]["end"] == round(15.0 - STRETCH_MARGIN, 3)


def test_adlib_loop_all_long_tokens():
    """'Uoh-oh-oh-oh-oh' × 5: all tokens normalise to >4-char 'uohohohohoh'.
    Must be detected as ad-lib (100 % concentration, even though token is long).
    """
    text = "Uoh-oh-oh-oh-oh, uoh-oh-oh-oh-oh, uoh-oh-oh-oh-oh, uoh-oh-oh-oh-oh, uoh-oh-oh-oh-oh"
    assert _is_adlib_loop(text), "pure long-token repetition should be detected as ad-lib"


def test_adlib_loop_mixed_short_and_long():
    """'uh' (short) + 'uoh-oh-oh-oh-oh' (long): concentration of 'uh' ≥ 85 %."""
    # 14 'uh' + 1 long token: 14/15 ≈ 93 % → still True (hits the ≤4-char branch)
    tokens = ["uh"] * 14 + ["uoh-oh-oh-oh-oh"]
    text = " ".join(tokens)
    assert _is_adlib_loop(text)


def test_adlib_loop_mixed_long_token_below_100pct():
    """Two different long tokens: neither is 100 %, neither is ≤4 chars → False."""
    tokens = ["uoh-oh-oh-oh-oh"] * 4 + ["yeah-yeah-yeah"] * 2
    text = " ".join(tokens)
    assert not _is_adlib_loop(text)


def test_adlib_chunker_all_long_tokens():
    """Full pipeline: 9.3-second pure 'Uoh-oh-oh-oh-oh' block must be split."""
    words = [_w("uoh-oh-oh-oh-oh", 69.04 + i * 1.86, 69.04 + i * 1.86 + 1.5)
             for i in range(5)]
    text = "Uoh-oh-oh-oh-oh, uoh-oh-oh-oh-oh, uoh-oh-oh-oh-oh, uoh-oh-oh-oh-oh, uoh-oh-oh-oh-oh"
    mega = _seg(text, 69.04, 78.34, words)
    normal = _seg("Tomas del miedo tu don", 95.22, 98.72,
                  [_w("tomas", 95.22), _w("del", 95.6), _w("miedo", 96.0),
                   _w("tu", 96.5), _w("don", 96.8, 97.1)])
    out = post_reconcile_cleanup([mega, normal])
    assert len(out) > 2, "9.3-second uoh block must be chunked"
    assert out[-1]["text"] == "Tomas del miedo tu don"


def test_cleanup_disabled_via_env(monkeypatch):
    monkeypatch.setenv("POST_RECONCILE_CLEANUP_ENABLED", "0")
    words = [_w("uh", 69.04 + i * 1.87) for i in range(14)]
    words.append(_w("uh-oh-oh-oh-oh-oh-oh", 94.2, 95.22))
    mega = _seg("Uh, uh, uh, uh, uh, uh, uh, uh, uh, uh, uh, uh, uh, uh., uh-oh-oh-oh-oh-oh-oh",
                69.04, 95.22, words)
    out = post_reconcile_cleanup([mega])
    assert len(out) == 1, "when disabled, cleanup must be a no-op"
