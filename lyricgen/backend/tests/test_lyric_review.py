"""Revisión rápida: casos reales de los pedidos UMG #112-#131 (29-09-2026)."""
from types import SimpleNamespace

import pytest

from lyric_review import LyricReviewPending, build_review
from lyric_review_sources import mine_correction_pairs, require_resolved


def _line(text, start, end, segment_id=None, words=None, **extra):
    row = {"text": text, "start": start, "end": end, **extra}
    if segment_id:
        row["segment_id"] = segment_id
    if words is not None:
        row["words"] = words
    return row


def _words(text, start, step=0.3, score=0.9):
    out, t = [], start
    for token in text.split():
        out.append({"word": token, "start": round(t, 2), "end": round(t + step - 0.05, 2), "score": score})
        t += step
    return out


def _evidence(witness_words=None, gemini=None):
    hyps = []
    if witness_words:
        hyps.append({"role": "candidate", "family": "openai/whisper-1", "kind": "word_stream",
                     "transformation": "live_independent_verify_raw",
                     "events": [{k: w[k] for k in ("word", "start", "end")} for w in witness_words]})
    if gemini:
        hyps.append({"role": "primary", "family": "google/gemini-2.5-flash-audio", "kind": "text",
                     "transformation": "gemini_reference_hypothesis_raw", "events": [{"text": gemini}]})
    return {"hypotheses_by_family": hyps}


def _kinds(review):
    return [(i["kind"], i["required"]) for i in review["items"]]


BERSUIT = [
    ("Quilombos espontáneos de semana", 100.0),
    ("Revueltas furibundas en la cama", 102.0),
    ("Ingestas continuadas sin estilus", 104.0),
    ("Pero todo lo que tuvo que pasar", 106.0),
    ("De uno u otro modo ya ha pasado", 108.0),
]


def _bersuit(screen_line="Ingestas continuadas sin estilus"):
    segs = [_line(t if t != BERSUIT[2][0] else screen_line, s, s + 1.8, f"s{k}") for k, (t, s) in enumerate(BERSUIT)]
    heard = [(t.replace("estilus", "estilo"), s) for t, s in BERSUIT]
    witness = [w for t, s in heard for w in _words(t, s)]
    gemini = " ".join(t for t, _ in heard)
    return segs, _evidence(witness, gemini)


def test_two_ears_agreeing_against_the_screen_is_required():
    # La Flor De Mis Heridas: "estilus" en pantalla, Gemini y el testigo
    # oyeron "estilo" (pedido #123).
    segs, ev = _bersuit()
    review = build_review(segs, original_segments=segs, machine_evidence=ev, title="La Flor De Mis Heridas")
    [item] = [i for i in review["items"] if i["kind"] == "heard_different"]
    assert item["required"] is True
    assert item["occurrences"][0]["after"] == "Ingestas continuadas sin estilo"
    assert item["sources"] == ["gemini", "witness"]


def test_a_single_ear_is_only_a_suggestion():
    segs, ev = _bersuit()
    ev["hypotheses_by_family"] = [h for h in ev["hypotheses_by_family"] if h["kind"] == "text"]
    review = build_review(segs, original_segments=segs, machine_evidence=ev, title="")
    [item] = [i for i in review["items"] if i["kind"] == "heard_different"]
    assert item["required"] is False
    assert review["required_count"] == 0


def test_a_word_a_person_already_changed_is_not_forced_back():
    # "reloco" lo pidió el cliente: los oídos oyen "reloj", pero la revisora
    # lo cambió a propósito, así que queda como sugerencia.
    original = [_line("Tu garantía de reloj se fundió", 43.5, 47.8, "a"),
                _line("Te lo pido por favor", 48.7, 51.1, "b"),
                _line("Y no lo consigo nunca", 52.0, 54.0, "c")]
    screen = [dict(original[0], text="Tu garantía de reloco se fundió"), original[1], original[2]]
    heard = "Tu garantía de reloj se fundió Te lo pido por favor Y no lo consigo nunca"
    witness = _words("Tu garantía de reloj se fundió", 43.6) + _words("Te lo pido por favor", 48.7) \
        + _words("Y no lo consigo nunca", 52.0)
    review = build_review(screen, original_segments=original, machine_evidence=_evidence(witness, heard))
    [item] = [i for i in review["items"] if i["kind"] == "heard_different"]
    assert item["required"] is False


def test_question_marks_follow_what_umg_asked():
    segs = [_line("¿Cuando vuelvas?", 10, 12, "a"), _line("¿Qué lindo añorar la zamba de ayer?", 13, 15, "b"),
            _line("¿Me querés?", 16, 17, "c"), _line("¿Qué hacés acá?", 18, 19, "d"),
            _line("¿A ver si sos tan macho?", 20, 21, "e")]
    review = build_review(segs, title="")
    fixes = {o["line_segment_id"]: o["after"] for i in review["items"] for o in i["occurrences"]}
    assert fixes == {
        "a": "Cuando vuelvas",
        "b": "¡Qué lindo añorar la zamba de ayer!",
        "e": "A ver si sos tan macho",
    }


def test_repeated_fix_is_one_item_for_every_chorus():
    segs = [_line("¿Cuando vuelvas?", t, t + 1, f"c{t}") for t in (10, 30, 50)]
    review = build_review(segs, title="")
    [item] = review["items"]
    assert len(item["occurrences"]) == 3
    assert review["required_count"] == 1


def test_title_spelling_only_the_two_umg_cases():
    segs = [_line("¿Cuando vuelva quiero verte", 1, 2, "a"),
            _line("Completamente enamorados", 3, 4, "b")]
    review = build_review(segs, title="Cuando Vuelvas")
    fixes = [(i["kind"], o["after"]) for i in review["items"] for o in i["occurrences"]]
    assert ("title_spelling", "¿Cuando vuelvas quiero verte") in fixes
    assert build_review([_line("Porque te tengo que olvidar", 1, 3, "a")],
                        title="¿POR QUÉ TE TENGO QUE OLVIDAR?")["items"][0]["occurrences"][0]["after"] \
        == "Por qué te tengo que olvidar"
    assert build_review([_line("Completamente enamorados", 1, 2, "a")],
                        title="Completamente Enamorado")["items"] == []


def test_accents_and_joined_words():
    segs = [_line("Mío, mío", 1, 2, "a"), _line("Mio en la noche", 3, 4, "b"),
            _line("Bajo tu influencia", 5, 6, "c"), _line("Bajó la noche", 7, 8, "d"),
            _line("logroEntender que puedes", 9, 10, "e"), _line("Desnúdate ya", 11, 12, "f"),
            _line("Vuelvo, vuelvo,vuelvo", 13, 14, "g")]
    gemini = "mío mío mío en la noche bajo tu influencia bajó la noche logro entender que puedes desnúdate ya vuelvo vuelvo vuelvo"
    review = build_review(segs, machine_evidence=_evidence(gemini=gemini), title="")
    fixes = {o["line_segment_id"]: o["after"] for i in review["items"] for o in i["occurrences"]}
    assert fixes["b"] == "Mío en la noche"
    assert fixes["e"] == "logro entender que puedes"
    assert fixes["g"] == "Vuelvo, vuelvo, vuelvo"
    assert "c" not in fixes and "d" not in fixes and "f" not in fixes


def test_lonely_word_merges_into_the_closest_line():
    segs = [_line("Al menos cuando he logrado llegar", 140, 149, "a"),
            _line("Que", 150.9, 151.2, "b"), _line("Dejó el temor de tener que olvidar", 151.3, 154, "c")]
    [item] = build_review(segs, title="")["items"]
    assert item["kind"] == "orphan_word"
    assert item["occurrences"][0]["fix"] == {"type": "merge", "direction": "next"}


def test_regional_apocope_and_silent_h_are_not_errors():
    segs = [_line("Pero nunca pasa na'", 1, 3, "a"), _line("Ay Navidad de Aimogasta", 4, 6, "b"),
            _line("Otra línea cualquiera de relleno", 7, 9, "c")]
    heard = "Pero nunca pasa nada Hay Navidad de Aimogasta Otra línea cualquiera de relleno"
    witness = _words("Pero nunca pasa nada", 1) + _words("Hay Navidad de Aimogasta", 4) \
        + _words("Otra línea cualquiera de relleno", 7)
    review = build_review(segs, original_segments=segs, machine_evidence=_evidence(witness, heard))
    assert [i for i in review["items"] if i["kind"] == "heard_different"] == []


def test_dismissed_items_stay_dismissed():
    segs = [_line("¿Cuando vuelvas?", 10, 12, "a")]
    [item] = build_review(segs, title="")["items"]
    segs[0]["qa_dismissed"] = item["keys"]
    assert build_review(segs, title="")["items"] == []


def test_correction_memory_learns_from_the_same_artist():
    docs = [([_line("Es un mambo de chuchu", 1, 2)], [_line("Es un mambo de Xuxú", 1, 2)]),
            ([_line("Estaba toda la noche", 1, 2)], [_line("Estaba todo la noche", 1, 2)])]
    pairs = mine_correction_pairs(docs)
    assert pairs == {"chuchu": "Xuxú"}  # "toda/todo" es concordancia, no oído
    segs = [_line("Un mambo de chuchu otra vez", 5, 8, "a")]
    [item] = build_review(segs, memory_pairs=pairs, title="")["items"]
    assert item["kind"] == "correction_memory"
    assert item["occurrences"][0]["after"] == "Un mambo de Xuxú otra vez"


def test_official_lyrics_only_insert_from_a_live_version_is_ignored():
    segs = [_line(t, s, s + 1.8, f"s{k}") for k, (t, s) in enumerate(BERSUIT)]
    official = "Para Pricila y su anvil y el facho " + " ".join(t for t, _ in BERSUIT)
    review = build_review(segs, official_text=official, title="")
    assert [i for i in review["items"] if i["kind"] == "missing"] == []


def test_approval_gate(monkeypatch):
    monkeypatch.delenv("LYRIC_REVIEW_MODE", raising=False)
    segs = [_line("¿Cuando vuelvas?", 10, 12, "a")]
    document = SimpleNamespace(job_id="j", current_segments=segs, original_segments=segs, machine_evidence=None)
    job = SimpleNamespace(job_id="j", artist="", song_title="", campaign_item_id=None)

    class _Query:
        def filter(self, *a, **k):
            return self

        def first(self):
            return None

    db = SimpleNamespace(query=lambda *a, **k: _Query())
    with pytest.raises(LyricReviewPending) as exc:
        require_resolved(db, document, job)
    assert exc.value.review["required_count"] == 1
    monkeypatch.setenv("LYRIC_REVIEW_MODE", "observe")
    require_resolved(db, document, job)


def test_chorus_fixed_in_one_repetition_suggests_the_others():
    # Como Caramelo De Limón (#76): se corrigió una repetición del coro y las
    # otras quedaron con la versión de la máquina.
    original = [_line("Y con él mi corazón", t, t + 2, f"c{t}") for t in (10, 40, 70)]
    screen = [dict(original[0], text="Y con él tu corazón"), original[1], original[2]]
    [item] = [i for i in build_review(screen, original_segments=original, title="")["items"]
              if i["kind"] == "chorus_propagate"]
    assert item["required"] is False
    assert [o["after"] for o in item["occurrences"]] == ["Y con él tu corazón"] * 2


def test_merged_lines_are_not_a_chorus_correction():
    original = [_line("Justo a tiempo ya", t, t + 2, f"c{t}") for t in (10, 40)]
    screen = [dict(original[0], text="Ehy, quiebro razón justo a tiempo ya ahora"), original[1]]
    review = build_review(screen, original_segments=original, title="")
    assert [i for i in review["items"] if i["kind"] == "chorus_propagate"] == []
