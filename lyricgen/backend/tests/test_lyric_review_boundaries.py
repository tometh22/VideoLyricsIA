"""Revisión rápida: reglas determinísticas de timing y de frontera de frase.

Letras sintéticas (inventadas para el test), en español y portugués.
"""
import pytest

from lyric_review import build_review
from lyric_review_boundaries import (
    boundary_items,
    language,
    phrase_items,
    timing_items,
)


def _words(text, start, end):
    tokens = text.split()
    step = (end - start) / len(tokens)
    return [{"word": t, "start": round(start + k * step, 2),
             "end": round(start + (k + 1) * step - 0.02, 2), "score": 0.9}
            for k, t in enumerate(tokens)]


def _line(text, start, end, *, words_until=None, sid=None, **extra):
    row = {"text": text, "start": start, "end": end,
           "words": _words(text, start, words_until if words_until is not None else end - 0.05)}
    if sid:
        row["segment_id"] = sid
    row.update(extra)
    return row


def _witness(pairs):
    return [{"word": w, "start": s, "end": e} for w, s, e in pairs]


def _evidence(lang=None, witness=None):
    evidence = {"hypotheses_by_family": []}
    if lang:
        evidence["decisions"] = {"quality": {"metrics": {"language": lang}}}
    if witness:
        evidence["hypotheses_by_family"].append({
            "role": "independent", "family": "openai/whisper-1", "kind": "word_stream",
            "events": [{"word": w, "start": s, "end": e} for w, s, e in witness]})
    return evidence


def _lines_flagged(items, kind=None):
    return [i["line"] for i in items if kind is None or i["kind"] == kind]


# --------------------------------------------------------------------------
# Timing.
# --------------------------------------------------------------------------

def test_line_that_leaves_before_its_last_word_ends_gets_a_one_click_fix():
    segs = [
        _line("Camino solo por la vereda", 1.0, 2.0, words_until=4.2),
        _line("Mirando el cielo gris", 6.0, 8.0),
    ]
    [item] = timing_items(segs)
    assert item["line"] == 0 and item["kind"] == "timing" and item["required"] is False
    assert item["fix"] == {"type": "timing", "end": 4.28}  # última palabra + 0,1 s
    assert item["action"] == "Alargar"
    assert "«vereda»" in item["why"]


def test_word_end_within_slack_is_not_flagged():
    segs = [_line("Camino solo por la vereda", 1.0, 3.0, words_until=3.25),
            _line("Mirando el cielo gris", 6.0, 8.0)]
    assert timing_items(segs) == []


def test_fix_is_clamped_to_the_next_line_and_skipped_without_room():
    segs = [_line("Camino solo por la vereda", 1.0, 2.0, words_until=4.2),
            _line("Mirando el cielo gris", 3.0, 5.0)]
    [item] = timing_items(segs)
    assert item["fix"]["end"] == pytest.approx(2.95)
    crowded = [_line("Camino solo por la vereda", 1.0, 2.0, words_until=4.2),
               _line("Mirando el cielo gris", 2.1, 5.0)]
    assert timing_items(crowded) == []


def test_stale_words_that_do_not_end_with_the_line_text_are_ignored():
    seg = _line("Camino solo por la vereda", 1.0, 2.0, words_until=4.2)
    seg["text"] = "Camino solo por la calle"  # una persona cambió el texto
    assert timing_items([seg, _line("Mirando el cielo gris", 6.0, 8.0)]) == []


def test_locked_lines_keep_the_timing_a_person_chose():
    segs = [_line("Camino solo por la vereda", 1.0, 2.0, words_until=4.2, locked=True),
            _line("Mirando el cielo gris", 6.0, 8.0)]
    assert timing_items(segs) == []


def test_provider_hearing_singing_well_after_the_line_end_asks_to_listen():
    seg = _line("Desculpa por te deixar sozinha", 10.0, 12.0)
    seg["provider_evidence"] = {"start": 10.1, "end": 13.6, "text": "desculpa por te deixar sozinha."}
    [item] = timing_items([seg, _line("Voltei pra casa cedo", 14.5, 16.0)])
    assert item["listen"] is True and item["required"] is False
    assert item["fix"] == {"type": "inspect_structure"}
    assert "13.6" in item["why"]


@pytest.mark.parametrize("evidence", [
    {"start": 10.1, "end": 12.5, "text": "desculpa por te deixar sozinha"},  # < 1 s
    {"start": 12.5, "end": 14.0, "text": "desculpa por te deixar sozinha"},  # no se solapa
    {"start": 10.1, "end": 13.6, "text": "outra frase completamente distinta"},  # texto viejo
])
def test_provider_evidence_negatives(evidence):
    seg = _line("Desculpa por te deixar sozinha", 10.0, 12.0)
    seg["provider_evidence"] = evidence
    assert timing_items([seg, _line("Voltei pra casa cedo", 14.5, 16.0)]) == []


def test_compressed_line_is_flagged_only_with_many_words_in_under_a_second():
    fast = _line("Uno dos tres cuatro cinco", 5.0, 5.6)
    assert _lines_flagged(timing_items([fast, _line("Otra línea normal", 8.0, 10.0)])) == [0]
    three = _line("Uno dos tres", 5.0, 5.6)
    slow = _line("Uno dos tres cuatro cinco", 5.0, 6.5)
    assert timing_items([three, _line("Otra línea normal", 8.0, 10.0)]) == []
    assert timing_items([slow, _line("Otra línea normal", 8.0, 10.0)]) == []


def test_singing_left_uncovered_after_the_last_line_flags_the_last_line():
    segs = [_line("Primera línea inventada", 1.0, 3.0),
            _line("Y la última termina acá", 4.0, 6.0)]
    tail = _witness([("y", 6.5, 6.8), ("sigue", 6.8, 7.3), ("cantando", 7.3, 8.1), ("más", 8.1, 8.6)])
    [item] = timing_items(segs, tail)
    assert item["line"] == 1 and item["listen"] is True
    assert item["sources"] == {"witness"}
    assert "sigue cantando" in item["why"]


@pytest.mark.parametrize("tail", [
    # Testigo en bucle: duración cero al final del audio.
    [("y", 9.9, 9.9), ("sigue", 9.9, 9.9), ("cantando", 9.9, 9.9), ("más", 9.9, 9.9)],
    # Dos palabras sueltas no son canto sin línea.
    [("eh", 6.5, 6.8), ("ah", 7.0, 7.4)],
    # Dentro de la última línea: está cubierto.
    [("y", 5.0, 5.3), ("la", 5.3, 5.5), ("última", 5.5, 6.0)],
])
def test_tail_negatives(tail):
    segs = [_line("Primera línea inventada", 1.0, 3.0),
            _line("Y la última termina acá", 4.0, 6.0)]
    assert timing_items(segs, _witness(tail)) == []


# --------------------------------------------------------------------------
# Frontera de frase.
# --------------------------------------------------------------------------

def _song(*texts, start=2.0, step=3.0):
    segs = [_line("Esta línea abre la canción", start, start + 2.5)]
    for k, text in enumerate(texts, 1):
        # Una palabra sola dura lo que se tarda en decirla, no una nota sostenida.
        length = 0.8 if len(text.split()) == 1 else 2.5
        segs.append(_line(text, start + k * step, start + k * step + length))
    segs.append(_line("Y esta otra la cierra", start + (len(texts) + 1) * step,
                      start + (len(texts) + 1) * step + 2.5))
    return segs


@pytest.mark.parametrize("text", [
    "Yo te espero en la",        # artículo
    "Me fui caminando con",      # preposición
    "Y cuando vuelvas me",       # pronombre átono
    "Dijiste que no era para el",
])
def test_spanish_line_ending_on_a_function_word_is_flagged(text):
    [item] = phrase_items(_song(text), lang="es")
    assert item["line"] == 1 and item["kind"] == "phrase_structure"
    assert item["required"] is False and item["listen"] is True
    assert item["title"] == "Revisar división de frases"
    assert f"«{text.split()[-1]}»" in item["why"]


@pytest.mark.parametrize("text", [
    "Hoje eu acordei e me",      # pronome átono
    "Vou voltar pra casa do",    # contração
    "Fiquei pensando na",
    "Sinto mais forte quem",
])
def test_portuguese_line_ending_on_a_function_word_is_flagged(text):
    assert _lines_flagged(phrase_items(_song(text), lang="pt")) == [1]


@pytest.mark.parametrize("text,lang", [
    ("Te quiero a ti", "es"),
    ("Ahora te digo que no", "es"),      # "no" en español cierra frase
    ("Yo ya no se", "es"),               # "no sé" sin tilde
    ("Y canto la la la", "es"),          # sílabas repetidas
    ("¿Y ahora con quién?", "es"),       # pregunta
    ("Me dijo que sí", "es"),            # tónica
    ("Somos los dos", None),             # "dos" sólo es átona en portugués
    ("Eu não sei mais", "pt"),
    ("Ninguém sabe o que é", "pt"),      # "é" es verbo
])
def test_complete_endings_are_not_flagged(text, lang):
    assert phrase_items(_song(text), lang=lang) == []


def test_portuguese_contraction_no_is_flagged_only_in_portuguese():
    segs = _song("Eu vou dormir no")
    assert _lines_flagged(phrase_items(segs, lang="pt")) == [1]
    assert phrase_items(segs, lang="es") == []
    assert phrase_items(segs, lang=None) == []


def test_single_word_line_is_flagged():
    [item] = phrase_items(_song("Siempre"), lang="es")
    assert item["title"] == "Palabra sola en pantalla"
    assert "«Siempre»" in item["why"] and item["required"] is False


@pytest.mark.parametrize("text", ["Oh", "Yeah", "Uuuh", "Ey", "(Aham)", "¡Fuego!"])
def test_interjections_and_adlibs_alone_are_fine(text):
    assert phrase_items(_song(text), lang="es") == []


def test_single_word_chorus_repeated_is_fine():
    assert phrase_items(_song("Saudade", "Uma frase do meio", "Saudade"), lang="pt") == []


def test_held_single_word_is_fine():
    segs = _song("Siempre")
    segs[1]["end"] = segs[1]["start"] + 1.6
    assert phrase_items(segs, lang="es") == []


def test_edges_cut_by_a_fragment_are_skipped():
    segs = [_line("Del", 0.0, 0.5), _line("Una línea completa y bien", 1.0, 3.0),
            _line("Y el final queda en la", 3.5, 5.0)]
    assert phrase_items(segs, lang="es") == []


def test_orphan_word_is_left_to_the_required_orphan_rule():
    segs = _song("que")
    assert phrase_items(segs, lang="es", orphan_words={"que"}) == []
    review = build_review(segs)
    kinds = [(i["kind"], i["required"]) for i in review["items"]
             if any(o["line_index"] == 1 for o in i["occurrences"])]
    assert kinds == [("orphan_word", True)]


# --------------------------------------------------------------------------
# Idioma y cableado con la Revisión rápida.
# --------------------------------------------------------------------------

def test_language_prefers_the_pipeline_decision_then_guesses():
    es = [_line("Yo no sé si el amor está acá", 0, 1), _line("Pero también es tuyo", 1, 2)]
    pt = [_line("Eu não sei se você vai", 0, 1), _line("É um amor com isso", 1, 2)]
    assert language(_evidence("pt"), es) == "pt"
    assert language(None, es) == "es"
    assert language(None, pt) == "pt"
    assert language(_evidence("en"), es) is None
    assert language(None, [_line("Hola", 0, 1)]) is None


def test_build_review_shows_them_as_suggestions_in_every_mode(monkeypatch):
    segs = _song("Yo te espero en la", "Siempre")
    segs[-1]["words"] = _words(segs[-1]["text"], segs[-1]["start"], segs[-1]["end"] + 2.0)
    for review_mode in ("enforce", "observe"):
        monkeypatch.setenv("LYRIC_REVIEW_MODE", review_mode)
        review = build_review(segs, machine_evidence=_evidence("es"))
        ours = [i for i in review["items"] if i["kind"] in {"timing", "phrase_structure"}]
        assert {(i["kind"], i["group"]) for i in ours} == {("timing", "timing"), ("phrase_structure", "layout")}
        assert len(ours) == 3
        assert all(not i["required"] for i in ours)
        assert review["required_count"] == 0
        assert review["mode"] == review_mode


def test_dismissed_points_stay_hidden():
    segs = _song("Yo te espero en la")
    [item] = [i for i in build_review(segs, machine_evidence=_evidence("es"))["items"]
              if i["kind"] == "phrase_structure"]
    segs[1]["qa_dismissed"] = item["keys"]
    assert [i for i in build_review(segs, machine_evidence=_evidence("es"))["items"]
            if i["kind"] == "phrase_structure"] == []


def test_kill_switch_turns_both_rules_off(monkeypatch):
    segs = _song("Yo te espero en la")
    segs[1]["words"] = _words(segs[1]["text"], segs[1]["start"], segs[1]["end"] + 1.0)
    assert boundary_items(segs, machine_evidence=_evidence("es"))
    monkeypatch.setenv("LYRIC_REVIEW_BOUNDARY_RULES", "0")
    assert boundary_items(segs, machine_evidence=_evidence("es")) == []


# --------------------------------------------------------------------------
# Cada aparición se revisa y se descarta por separado.
# --------------------------------------------------------------------------

def _chorus_twice():
    return [
        _line("Esta línea abre la canción", 2.0, 4.5, sid="a"),
        _line("Yo te espero en la", 5.0, 7.5, sid="b"),
        _line("Una estrofa en el medio", 8.0, 10.5, sid="c"),
        _line("Yo te espero en la", 11.0, 13.5, sid="d"),
        _line("Y esta otra la cierra", 14.0, 16.5, sid="e"),
    ]


def _ours(review, kind):
    return [i for i in review["items"] if i["kind"] == kind]


def _sid(item):
    return item["occurrences"][0]["line_segment_id"]


def test_repeated_unfinished_phrase_gives_one_item_per_chorus():
    items = _ours(build_review(_chorus_twice(), machine_evidence=_evidence("es")), "phrase_structure")
    assert sorted(_sid(i) for i in items) == ["b", "d"]
    assert all(len(i["occurrences"]) == 1 for i in items)
    assert len({k for i in items for k in i["keys"]}) == 2


def test_dismissing_one_chorus_keeps_the_other_and_retiming_reopens_it():
    segs = _chorus_twice()
    first = next(i for i in _ours(build_review(segs, machine_evidence=_evidence("es")), "phrase_structure")
                 if _sid(i) == "b")
    segs[1]["qa_dismissed"] = first["keys"]
    left = _ours(build_review(segs, machine_evidence=_evidence("es")), "phrase_structure")
    assert [_sid(i) for i in left] == ["d"]
    segs[1]["end"] = 7.2  # alguien movió los tiempos: hay que volver a mirarla
    again = _ours(build_review(segs, machine_evidence=_evidence("es")), "phrase_structure")
    assert sorted(_sid(i) for i in again) == ["b", "d"]


def test_repeated_line_ending_early_gives_independent_timing_items():
    segs = _chorus_twice()
    for k in (1, 3):
        segs[k]["text"] = "Cantamos juntos esta vez"
        segs[k]["words"] = _words(segs[k]["text"], segs[k]["start"], segs[k]["end"] + 0.4)
        segs[k]["end"] -= 0.6
    items = _ours(build_review(segs, machine_evidence=_evidence("es")), "timing")
    assert sorted(_sid(i) for i in items) == ["b", "d"]
    assert all(len(i["occurrences"]) == 1 for i in items)
    segs[1]["qa_dismissed"] = next(i for i in items if _sid(i) == "b")["keys"]
    left = _ours(build_review(segs, machine_evidence=_evidence("es")), "timing")
    assert [_sid(i) for i in left] == ["d"]
