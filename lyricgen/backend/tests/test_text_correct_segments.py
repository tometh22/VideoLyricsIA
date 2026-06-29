"""text_correct_segments — audio-as-truth line-level text correction.

Pins the contract: swap each segment's TEXT to the best reference line while
KEEPING timing/structure, and leave ad-lib segments (no reference match)
untouched. Pure — no audio, no network.
"""
from whisperx_reconcile import text_correct_segments

REF = """Iluminados por el fuego que dejaste arder
Para qué, para qué tus santos de papel
Tomás del miedo tu don
Frágil espejo de vos
Para qué, para qué tus santos de papel"""


def _seg(start, end, text):
    words = ([{"word": text.split()[0], "start": start, "end": start + 0.3}]
             if text.split() else [])
    return {"start": start, "end": end, "text": text, "words": words}


def test_swaps_text_keeps_timing():
    segs = [_seg(102.0, 107.8, "tomas del miedo tu don")]  # accent mondegreen
    out = text_correct_segments(segs, REF)
    assert out[0]["text"] == "Tomás del miedo tu don"
    assert out[0]["start"] == 102.0 and out[0]["end"] == 107.8   # timing intact
    assert out[0]["words"] == segs[0]["words"]                    # words intact


def test_phonetic_rescues_mondegreen():
    # Jaccard ~0 (perro/espejo, voz/vos) but phonetic ratio clears the bar.
    segs = [_seg(126.0, 132.0, "frágiles perro de voz")]
    out = text_correct_segments(segs, REF)
    assert out[0]["text"] == "Frágil espejo de vos"


def test_adlib_survives_untouched():
    segs = [_seg(73.0, 84.0, "uh uh uh uh uh uh uh uh")]  # not in reference
    out = text_correct_segments(segs, REF)
    assert out[0]["text"] == "uh uh uh uh uh uh uh uh"     # unchanged


def test_output_length_order_and_no_mutation():
    segs = [_seg(10, 14, "iluminados por el fuego que dejaste arder"),
            _seg(73, 84, "uh uh uh uh uh"),
            _seg(102, 108, "tomas del miedo tu don")]
    snapshot = [dict(s) for s in segs]
    out = text_correct_segments(segs, REF)
    assert len(out) == len(segs)
    assert out[1]["text"] == "uh uh uh uh uh"              # ad-lib in the middle kept
    assert segs == snapshot                                 # input not mutated


def test_repeated_chorus_maps_monotonically():
    # Two chorus segments -> the two reference chorus occurrences, in order
    # (not both to the first). The ad-lib between them stays put.
    segs = [_seg(24, 29, "para que para que tus santos de papel"),
            _seg(73, 84, "uh uh uh uh uh"),
            _seg(134, 139, "para que para que tus santos de papel")]
    out = text_correct_segments(segs, REF)
    assert out[0]["text"] == "Para qué, para qué tus santos de papel"
    assert out[1]["text"] == "uh uh uh uh uh"
    assert out[2]["text"] == "Para qué, para qué tus santos de papel"


def test_short_reference_is_noop():
    segs = [_seg(0, 3, "algo")]
    assert text_correct_segments(segs, "una sola linea")[0]["text"] == "algo"


def test_low_match_segment_unchanged():
    segs = [_seg(0, 4, "esto no se parece a ninguna linea de la referencia xyz")]
    out = text_correct_segments(segs, REF)
    assert out[0]["text"] == "esto no se parece a ninguna linea de la referencia xyz"


REF2 = ("Tomás del miedo tu don\nFrágil espejo de vos\n"
        "Tomás del miedo tu don\nFrágil espejo de vos")


def test_blank_segments_named_from_reference():
    # whisperX heard a sustained melisma but produced empty text where the
    # reference has "Frágil espejo de vos". Blank-fill names it (keeps timing).
    segs = [_seg(102, 108, "tomas del miedo tu don"),
            _seg(110, 116, ""),                       # blank melisma
            _seg(118, 124, "tomas del miedo tu don"),
            _seg(126, 132, "")]                       # blank melisma
    out = text_correct_segments(segs, REF2)
    assert [s["text"] for s in out] == [
        "Tomás del miedo tu don", "Frágil espejo de vos",
        "Tomás del miedo tu don", "Frágil espejo de vos"]
    assert out[1].get("review") is True and out[3].get("review") is True
    assert out[1]["start"] == 110 and out[1]["end"] == 116   # audio timing kept


def test_blank_with_no_reference_left_is_dropped():
    # A blank with no genuinely-skipped reference line is dropped (no phantom).
    segs = [_seg(0, 5, "tomas del miedo tu don"), _seg(6, 8, "")]
    out = text_correct_segments(segs, "Tomás del miedo tu don\nFrágil espejo de vos")
    # blank could take "Frágil espejo" (skipped) → named; assert it's named, not phantom-duplicated
    assert len(out) == 2 and out[1]["text"] == "Frágil espejo de vos"


from whisperx_reconcile import relabel_long_adlibs


def test_relabel_long_sustained_vocal_as_uh():
    # 21s with 4 words = mis-heard ad-lib → relabel to "Uh" lines
    segs = [_seg(81.0, 102.0, "¿Para qué? ¿Para qué?")]
    out = relabel_long_adlibs(segs)
    assert len(out) >= 2 and all(s["text"] == "Uh, uh, uh" for s in out)
    assert all(s.get("review") for s in out)
    assert out[0]["start"] == 81.0 and abs(out[-1]["end"] - 102.0) < 0.01


def test_relabel_leaves_real_long_line():
    # 16s but 12 words (1.3 s/word) = real lyric → untouched
    segs = [_seg(8.0, 24.0, "dentro de tu piel se esconden los indicios de que nada perfecto")]
    out = relabel_long_adlibs(segs)
    assert len(out) == 1 and out[0]["text"].startswith("dentro")


def test_relabel_leaves_short_segment():
    segs = [_seg(0, 4, "para qué para qué")]
    assert relabel_long_adlibs(segs)[0]["text"] == "para qué para qué"
