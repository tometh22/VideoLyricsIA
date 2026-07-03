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


def _seg_w(start, end, text, word_spans):
    return {"start": start, "end": end, "text": text,
            "words": [{"word": w, "start": ws, "end": we} for (w, ws, we) in word_spans]}


def test_relabel_wordless_tail_becomes_uh_keeps_head():
    # words end at 70 but audio runs to 81 → 11s wordless tail → "Uh"; head kept
    seg = _seg_w(66, 81, "¿Para qué? Tus santos de papel",
                 [("¿Para", 66, 67), ("qué", 67, 68), ("Tus", 68, 68.5),
                  ("santos", 68.5, 69), ("de", 69, 69.5), ("papel", 69.5, 70)])
    out = relabel_long_adlibs([seg])
    assert out[0]["text"] == "¿Para qué? Tus santos de papel"   # head survives (recovers chorus)
    assert out[0]["end"] == 70.0                                 # trimmed to last word
    assert len(out) >= 2 and all(o["text"].startswith("Uh") for o in out[1:])
    assert abs(out[-1]["end"] - 81) < 0.01                       # uh covers the tail


def test_relabel_leaves_real_line_when_words_span_segment():
    # words run to the segment end → no wordless tail → untouched
    seg = _seg_w(8, 16, "dentro de tu piel se esconden los indicios",
                 [("dentro", 8, 9), ("indicios", 15.4, 16)])
    out = relabel_long_adlibs([seg])
    assert len(out) == 1 and out[0]["text"].startswith("dentro")


def test_relabel_skips_segments_without_words():
    seg = {"start": 0, "end": 20, "text": "algo", "words": []}
    assert relabel_long_adlibs([seg]) == [seg]


# ── match2: un segmento que abarca DOS líneas de referencia (Amanda Pujó, 03/07) ──

REF_SANTOS = """Dentro de tu piel se esconden los indicios
de que nada es perfecto
iluminados por el fuego que dejaste arder
para qué para qué tus santos de papel
Como tantos vas buscando las respuestas
que nada te responden
acariciando tus ideas algo en qué creer"""


def _w(text, start, end):
    """Word-stamps sintéticos repartidos uniformemente (como whisper-1)."""
    ws = text.split()
    dur = (end - start) / len(ws)
    return [{"word": w, "start": round(start + i * dur, 2),
             "end": round(start + (i + 1) * dur, 2)} for i, w in enumerate(ws)]


def test_segment_spanning_two_ref_lines_keeps_both():
    """Caso real: whisper oyó '…los indicios de que nada es perfecto' en UN
    segmento; la referencia lo tiene en DOS líneas. Con matching 1:1 la
    segunda desaparecía del video (frase cantada sin subtítulo)."""
    from whisperx_reconcile import text_correct_segments
    txt = "Dentro de tu piel se esconden los indicios de que nada es perfecto"
    segs = [
        {"text": txt, "start": 8.0, "end": 15.5, "words": _w(txt, 8.0, 15.5)},
        {"text": "iluminados por el fuego que dejaste arder", "start": 16.9, "end": 21.4},
    ]
    out = text_correct_segments(segs, REF_SANTOS)
    texts = [s["text"] for s in out]
    assert "de que nada es perfecto" in texts          # la frase YA NO se pierde
    assert "Dentro de tu piel se esconden los indicios" in texts
    i = texts.index("Dentro de tu piel se esconden los indicios")
    a, b = out[i], out[i + 1]
    assert a["end"] <= b["start"] + 0.01               # partido en orden
    assert 10.0 < a["end"] < 14.5                      # corte adentro del segmento
    assert b["end"] == 15.5                            # fin original conservado


def test_second_real_case_que_nada_te_responden():
    from whisperx_reconcile import text_correct_segments
    txt = "que en nada te responden acariciando tus ideas, algo en que creer"
    segs = [
        {"text": "Como tantos vas buscando las respuestas", "start": 41.0, "end": 45.7},
        {"text": txt, "start": 45.8, "end": 54.6, "words": _w(txt, 45.8, 54.6)},
    ]
    out = text_correct_segments(segs, REF_SANTOS)
    texts = [s["text"] for s in out]
    assert "que nada te responden" in texts
    assert "acariciando tus ideas algo en qué creer" in texts


def test_single_line_match_not_split_on_tie():
    """Una línea sana que matchea 1:1 no se parte en dos por empate."""
    from whisperx_reconcile import text_correct_segments
    segs = [{"text": "para qué para qué tus santos de papel",
             "start": 24.0, "end": 29.0}]
    out = text_correct_segments(segs, REF_SANTOS)
    assert len(out) == 1
    assert out[0]["text"] == "para qué para qué tus santos de papel"
