"""Arreglos de un clic de la Revisión rápida: sólo si son una edición limpia y
completa de un tramo contiguo de palabras.

Corrida sobre las 15 demos actuales (16-11-2026, informe 21): con la ventana de
la letra oficial (#1603) aparecieron tres arreglos rotos:

- GBA: «Dai-me um coração…» → «Dá me»: el reemplazo suena como «Dai-me» y se
  come el «um» que el oído no oyó.
- Menos É Mais: «Tudo Pelos demais de Natanzio e Salvador» → «Tudo menos e
  Salvador»: el reemplazo es la mitad de una frase cuya otra mitad (un agregado
  de la letra oficial) no se propone.
- Midian: «Mas você não será» → «Mas tu não será»: cambia «você→tu» y deja
  «será», porque «serás» cae en el borde del fragmento y no se propone.

En los tres casos el punto queda como "Escuchá este tramo", sin arreglo de un
clic. Textos sintéticos inventados para el test.
"""
import lyric_review
from lyric_review import _drops_neighbour_words, build_review


def _segments(lines, start=0.0, step=5.0):
    return [
        {"text": text, "start": round(start + k * step, 2),
         "end": round(start + k * step + step - 0.4, 2), "segment_id": f"s{k}"}
        for k, text in enumerate(lines)
    ]


def _evidence(witness_lines=None, gemini_lines=None, start=0.0, step=5.0):
    hyps = []
    if witness_lines:
        events = []
        for k, line in enumerate(witness_lines):
            t = start + k * step
            for word in line.split():
                events.append({"word": word, "start": round(t, 2), "end": round(t + 0.3, 2)})
                t += 0.35
        hyps.append({"role": "candidate", "family": "openai/whisper-1", "kind": "word_stream",
                     "transformation": "live_independent_verify_raw", "events": events})
    if gemini_lines:
        hyps.append({"role": "primary", "family": "google/gemini-2.5-flash-audio", "kind": "text",
                     "transformation": "gemini_reference_hypothesis_raw",
                     "events": [{"text": " ".join(gemini_lines)}]})
    return {"hypotheses_by_family": hyps}


def _text_items(review, line):
    return [i for i in review["items"]
            if i["kind"] in {"heard_different", "missing"}
            and i["occurrences"][0]["line_index"] == line]


def _one_click(item):
    return not item["listen"] and item["occurrences"][0]["fix"].get("one_click", True)


# --- GBA: el reemplazo se come una palabra vecina -----------------------------

SCREEN_A = [
    "Camino por la orilla del río plateado",
    "Dai-me um barco pequeno ao porto",
    "Cuento las piedras que dejó la tormenta",
    "El farol de la esquina se quedó dormido",
]


def test_replacement_that_eats_a_neighbour_word_is_listen_only():
    heard = list(SCREEN_A)
    heard[1] = "Dá me barco pequeno ao porto"  # los dos oídos no oyen «um»
    segs = _segments(SCREEN_A)
    review = build_review(segs, original_segments=segs,
                          machine_evidence=_evidence(heard, heard))
    [item] = _text_items(review, 1)
    fix = item["occurrences"][0]["fix"]
    assert (fix["find"], fix["replace"]) == ("Dai-me um", "Dá me")
    # Antes: obligatorio de un clic (testigo + Gemini), borraba «um».
    assert item["listen"] is True and item["required"] is False
    assert fix["one_click"] is False
    assert item["alternatives"] == []


def test_drops_neighbour_words_only_flags_partial_matches():
    assert _drops_neighbour_words({"type": "replace", "find": "Dai-me um", "replace": "Dá me"})
    # «boca à» → «pouca» es UNA palabra mal cortada, no una vecina perdida.
    assert not _drops_neighbour_words({"type": "replace", "find": "boca à", "replace": "pouca"})
    assert not _drops_neighbour_words({"type": "replace", "find": "de ese buque", "replace": "bebé"})
    assert not _drops_neighbour_words({"type": "replace", "find": "que vinho",
                                       "replace": "Kevinho cê acredita"})
    assert not _drops_neighbour_words({"type": "insert", "text": "um"})


def test_clean_multi_word_fix_keeps_its_one_click():
    heard = list(SCREEN_A)
    heard[1] = "Dai-me um barco pequeno ao puerto"
    segs = _segments(SCREEN_A)
    review = build_review(segs, original_segments=segs,
                          machine_evidence=_evidence(heard, heard))
    [item] = _text_items(review, 1)
    assert item["required"] is True and _one_click(item)
    assert item["occurrences"][0]["after"] == "Dai-me um barco pequeno ao puerto"


# --- Menos É Mais: la mitad de una frase de la letra oficial ------------------

SONG_B = [
    "Mi abuela tejía bufandas de colores",
    "Para los gatos que dormían en el patio",
    "Cantaba tangos mientras hervía la sopa",
    "Hoy Menos É Mais e Robertinho em Mendoza solta a voz",
    "Y el reloj de pared marcaba otro siglo",
    "Hoy la casa tiene ventanas cerradas",
    "Pero el jazmín sigue trepando la reja",
]


def test_half_of_an_official_phrase_is_listen_only():
    screen = list(SONG_B)
    screen[3] = "Hoy Pelos demais de Robertito e Mendoza solta a voz"
    # Otra toma (como un fragmento): similitud < 0,85, así que el agregado de
    # la letra oficial («Mais e Robertinho») no se propone.
    screen[5] = "Hoy las cosas tienen puertas abiertas"
    screen[6] = "Pero ningún gato vuelve a la reja"
    segs = _segments(screen)
    review = build_review(segs, original_segments=segs, official_text="\n".join(SONG_B))
    assert 0.45 <= review["sources"]["similarity"]["official"] < 0.85
    [item] = _text_items(review, 3)
    fix = item["occurrences"][0]["fix"]
    assert (fix["find"], fix["replace"]) == ("Pelos demais de Robertito", "Menos")
    # Antes: sugerencia de un clic que dejaba «Hoy Menos e Mendoza…».
    assert item["listen"] is True and item["required"] is False
    assert fix["one_click"] is False


# --- Midian: arreglo incompleto en el borde del fragmento ---------------------

VERSE = [
    "Mi abuela tejía bufandas de colores",
    "Para los gatos que dormían en el patio",
    "Cantaba tangos mientras hervía la sopa",
    "Y el reloj de pared marcaba otro siglo",
    "Hoy la casa tiene ventanas cerradas",
    "Pero tú no vendrás",
    "Cayendo desde el cielo gris",
]
FILLER = [
    "Camino por la orilla del río plateado",
    "Cuento las piedras que dejó la tormenta",
    "El farol de la esquina se quedó dormido",
    "Y un perro sin nombre me sigue los pasos",
    "Guardo en el bolsillo una llave oxidada",
    "Que abre una puerta que ya no existe",
]
SONG_C = FILLER + VERSE + FILLER[::-1]


def test_incomplete_fix_at_the_fragment_edge_is_listen_only(monkeypatch):
    # Demo de 30 s que termina en «Pero usted no vendrá»: la letra dice «Pero
    # tú no vendrás», y «vendrás» cae en el borde (la línea siguiente de la
    # letra entra como margen y ese agregado no se propone).
    screen = VERSE[:5] + ["Pero usted no vendrá"]
    heard = VERSE[:5] + ["Pero tú no vendrás"]
    segs = _segments(screen)
    official = "\n".join(SONG_C)
    review = build_review(segs, original_segments=segs, machine_evidence=_evidence(heard),
                          official_text=official)
    assert review["sources"]["official_window"]["start_line"] == len(FILLER)
    [item] = _text_items(review, 5)
    fix = item["occurrences"][0]["fix"]
    assert (fix["find"], fix["replace"]) == ("usted", "tú")
    assert sorted(item["sources"]) == ["official", "witness"]
    # Antes: obligatorio de un clic que dejaba «Pero tú no vendrá».
    assert item["listen"] is True and item["required"] is False
    assert fix["one_click"] is False

    # Sin el agregado retenido en el borde, el mismo cambio sí es de un clic.
    monkeypatch.setattr(lyric_review, "_fragment_edge_addition", lambda *a, **k: False)
    full = build_review(segs, original_segments=segs, machine_evidence=_evidence(heard),
                        official_text=official)
    tu = [i for i in _text_items(full, 5) if i["occurrences"][0]["fix"].get("find") == "usted"]
    assert tu and _one_click(tu[0])
