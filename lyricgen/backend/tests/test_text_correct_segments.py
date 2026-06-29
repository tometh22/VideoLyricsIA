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
    return {"start": start, "end": end, "text": text,
            "words": [{"word": text.split()[0], "start": start, "end": start + 0.3}]}


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
