"""Unit tests for the rotor_pipeline first-line + run-on guards.

These cover the two staging regressions on Bersuit "Yo Tomo":
  A. a spurious '¿Quién es?' intro fragment at 0:16 (verse really starts 0:35)
  B. run-on lines cramming 3 Rotor lines into one
…and the critical NON-REGRESSION case: a song that opens normally must be
left untouched.
"""
import rotor_pipeline as rp


def _w(word, start, end):
    return {"word": word, "start": start, "end": end}


def _verse_words():
    # "¿Quién sos? ¿Cómo sos? ¿Cuándo venís? ¿Cuándo llegaste?
    #  ¿Por qué te fuiste? ¿Y quién se acabó?" — the staging run-on.
    toks = ["¿Quién", "sos?", "¿Cómo", "sos?", "¿Cuándo", "venís?",
            "¿Cuándo", "llegaste?", "¿Por", "qué", "te", "fuiste?",
            "¿Y", "quién", "se", "acabó?"]
    t = 35.1
    out = []
    for tok in toks:
        out.append(_w(tok, t, t + 0.4))
        t += 0.5
    return out


# ── A. drop_spurious_intro ──────────────────────────────────────────────
def test_drops_short_isolated_intro_fragment():
    segs = [
        {"start": 16.6, "end": 17.1, "text": "¿Quién es?",
         "words": [_w("¿Quién", 16.6, 16.9), _w("es?", 16.9, 17.1)]},
        {"start": 35.3, "end": 38.0, "text": "¿Quién sos? ¿Cómo sos?",
         "words": _verse_words()[:4]},
        {"start": 39.8, "end": 42.0, "text": "¿Por qué te fuiste?",
         "words": _verse_words()[8:12]},
    ]
    out = rp.drop_spurious_intro(segs)
    assert len(out) == 2
    assert abs(out[0]["start"] - 35.3) < 1e-6, "verse must become line 1"


def test_keeps_legit_early_first_line_no_big_gap():
    # First line is short but the body starts right after (small gap) → keep.
    segs = [
        {"start": 2.0, "end": 3.0, "text": "Oh, oh", "words": [_w("Oh,", 2.0, 2.5), _w("oh", 2.5, 3.0)]},
        {"start": 4.5, "end": 7.0, "text": "Empieza el verso ya mismo acá",
         "words": [_w(x, 4.5 + i * 0.4, 4.7 + i * 0.4) for i, x in
                   enumerate("Empieza el verso ya mismo acá".split())]},
        {"start": 8.0, "end": 10.0, "text": "Segunda línea normal del tema",
         "words": [_w(x, 8.0 + i * 0.4, 8.2 + i * 0.4) for i, x in
                   enumerate("Segunda línea normal del tema".split())]},
    ]
    out = rp.drop_spurious_intro(segs)
    assert len(out) == 3, "small gap → first line is legit, keep it"


def test_keeps_substantive_early_line_even_with_gap():
    # Far gap but the first line is a FULL line (not a fragment) → keep.
    long_first = "Esta es una línea entera del estribillo cantada"
    segs = [
        {"start": 5.0, "end": 9.0, "text": long_first,
         "words": [_w(x, 5.0 + i * 0.4, 5.2 + i * 0.4) for i, x in enumerate(long_first.split())]},
        {"start": 30.0, "end": 33.0, "text": "Cuerpo de la canción más tarde",
         "words": [_w(x, 30.0 + i * 0.4, 30.2 + i * 0.4) for i, x in
                   enumerate("Cuerpo de la canción más tarde".split())]},
        {"start": 34.0, "end": 36.0, "text": "Otra línea del cuerpo normal",
         "words": [_w(x, 34.0 + i * 0.4, 34.2 + i * 0.4) for i, x in
                   enumerate("Otra línea del cuerpo normal".split())]},
    ]
    out = rp.drop_spurious_intro(segs)
    assert len(out) == 3, "substantive early line must be kept"


def test_never_empties_short_output():
    segs = [{"start": 16.0, "end": 16.5, "text": "Hey", "words": [_w("Hey", 16.0, 16.5)]},
            {"start": 40.0, "end": 42.0, "text": "Verso", "words": [_w("Verso", 40.0, 42.0)]}]
    assert rp.drop_spurious_intro(segs) == segs, "<3 lines → untouched"


# ── B. split_run_on_lines ───────────────────────────────────────────────
def test_splits_run_on_into_short_lines():
    seg = {"start": 35.1, "end": 43.1, "text": "run on", "words": _verse_words()}
    out = rp.split_run_on_lines([seg], max_words=8)
    assert len(out) >= 2, "16-word run-on must split"
    for o in out:
        assert rp._word_count(o) <= 8
        # each sub-line re-timed from its own words
        assert abs(o["start"] - o["words"][0]["start"]) < 1e-6
        assert abs(o["end"] - o["words"][-1]["end"]) < 1e-6
    # word order + count preserved across the split
    flat = [w["word"] for o in out for w in o["words"]]
    assert flat == [w["word"] for w in _verse_words()]
    # first sub-line starts at the verse onset (Rotor 35.28)
    assert abs(out[0]["start"] - 35.1) < 1e-6


def test_short_line_untouched():
    seg = {"start": 53.4, "end": 56.0, "text": "Tomo para no enamorarme",
           "words": [_w(x, 53.4 + i * 0.4, 53.6 + i * 0.4) for i, x in
                     enumerate("Tomo para no enamorarme".split())]}
    out = rp.split_run_on_lines([seg])
    assert len(out) == 1 and out[0] is seg


def test_clean_join_no_space_before_punct():
    words = [_w("¿Quién", 0, 1), _w("sos?", 1, 2)]
    assert rp._clean_join(words) == "¿Quién sos?"


def test_pack_preserves_all_words():
    words = _verse_words()
    chunks = rp._pack_words(words, max_words=8, min_chunk=3)
    flat = [w for ch in chunks for w in ch]
    assert len(flat) == len(words)


# ── C. acoustic re-timing helpers ───────────────────────────────────────
def test_lines_text_joins_nonempty():
    segs = [{"text": "Una"}, {"text": "  "}, {"text": "Dos"}]
    assert rp._lines_text(segs) == "Una\nDos"


def test_looks_drifty_false_without_regions():
    segs = [{"start": float(i)} for i in range(10)]
    assert rp._looks_drifty(segs, []) is False
    assert rp._looks_drifty(segs, None) is False


def test_looks_drifty_false_when_onsets_on_voice():
    # All line starts sit inside a voiced region → tight, not drifty.
    regions = [(10.0, 14.0), (20.0, 24.0), (30.0, 34.0), (40.0, 44.0),
               (50.0, 54.0), (60.0, 64.0)]
    segs = [{"start": s} for s in (10.5, 20.5, 30.5, 40.5, 50.5, 60.5)]
    assert rp._looks_drifty(segs, regions) is False


def test_looks_drifty_true_when_onsets_off_voice():
    # Voice only early; later starts land in instrumental gaps → drifty.
    regions = [(10.0, 14.0), (20.0, 24.0)]
    segs = [{"start": s} for s in (10.5, 20.5, 33.0, 38.0, 47.0, 56.0)]
    assert rp._looks_drifty(segs, regions) is True


def test_looks_drifty_false_for_short_output():
    regions = [(10.0, 14.0)]
    segs = [{"start": 30.0}, {"start": 40.0}]  # <6 lines → can't measure
    assert rp._looks_drifty(segs, regions) is False


if __name__ == "__main__":
    import sys
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    failed = 0
    for fn in fns:
        try:
            fn()
            print(f"  ok   {fn.__name__}")
        except AssertionError as e:
            failed += 1
            print(f"  FAIL {fn.__name__}: {e}")
        except Exception as e:
            failed += 1
            print(f"  ERR  {fn.__name__}: {type(e).__name__}: {e}")
    print(f"\n{len(fns) - failed}/{len(fns)} passed")
    sys.exit(1 if failed else 0)
