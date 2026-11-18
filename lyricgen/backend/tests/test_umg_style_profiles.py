"""Perfiles de estilo por portal de UMG en la Revisión rápida.

Casos sacados de los pedidos de UMG de sep-nov 2026 (anonimizados al
mínimo: sólo la línea que el cliente marcó)."""
from lyric_review import build_review, required_for_scope


def _line(text, start=0.0, sid=None):
    return {"text": text, "start": start, "end": start + 2, "segment_id": sid or f"s{start}"}


def _items(lines, portal, kind=None):
    segs = [_line(t, i * 3.0) for i, t in enumerate(lines)]
    items = build_review(segs, title="", portal=portal)["items"]
    return [i for i in items if kind is None or i["kind"] == kind]


def _afters(item):
    return [o["after"] for o in item["occurrences"]]


# --- Punto final -----------------------------------------------------------

def test_terminal_period_is_required_and_grouped_for_both_portals():
    for portal in ("argentina", "chile", "umg"):
        [item] = _items(["Para no volver.", "Lejos de vos.", "Y nada más"], portal, "terminal_period")
        assert item["required"] is True and item["style_rule"] is True
        assert _afters(item) == ["Para no volver", "Lejos de vos"]


def test_terminal_period_keeps_ellipsis_and_initials():
    assert _items(["Y me fui...", "Loco lindo V.S.D.", "Sigo así…"], "argentina",
                  "terminal_period") == []


def test_no_portal_means_no_profile_rules():
    review = build_review([_line("Para no volver.")], title="")
    assert review["profile"] is None
    assert [i for i in review["items"] if i["kind"] == "terminal_period"] == []


# --- Signos de pregunta ----------------------------------------------------

def test_question_without_opening_mark_is_removed_by_default():
    [item] = _items(["Si ya no tengo las caricias de mi madre?"], "chile", "question_marks")
    assert item["required"] is True
    assert _afters(item) == ["Si ya no tengo las caricias de mi madre"]
    assert item["alternatives"][0]["fix"]["text"] == "¿Si ya no tengo las caricias de mi madre?"


def test_open_question_with_interrogative_is_completed():
    [item] = _items(["Pero, ¿qué hago yo acá"], "argentina", "question_marks")
    assert _afters(item) == ["Pero, ¿qué hago yo acá?"]
    [item] = _items(['Yo le pregunté: "nena qué vas a hacer?"'], "argentina", "question_marks")
    assert _afters(item) == ['Yo le pregunté: "nena ¿qué vas a hacer?"']


def test_open_question_keeps_the_no_question_option_one_click_away():
    # "¿Por qué soy wacho amor": UMG pidió sacar los signos.
    [item] = _items(["¿Por qué soy wacho amor"], "argentina", "question_marks")
    assert _afters(item) == ["¿Por qué soy wacho amor?"]
    assert item["alternatives"][0]["fix"]["text"] == "Por qué soy wacho amor"


def test_real_questions_are_never_touched():
    lines = ["¿Qué hacé, boludo?", "¿Boludo yo?", "¿Me querés?", "Si no tengo, ¿de dónde saco?",
             "¿Por qué, por qué los ricos?"]
    for portal in ("argentina", "chile"):
        assert _items(lines, portal, "question_marks") == []


def test_exclamation_follows_the_portal():
    [ar] = _items(["¿Qué lindo añorar la zamba de ayer?"], "argentina", "question_marks")
    assert ar["required"] is True
    assert _afters(ar) == ["Qué lindo añorar la zamba de ayer"]
    assert ar["alternatives"][0]["fix"]["mode"] == "exclaim"
    [cl] = _items(["¿Qué lindo añorar la zamba de ayer?"], "chile", "question_marks")
    assert _afters(cl) == ["¡Qué lindo añorar la zamba de ayer!"]


def test_relative_cuando_question_still_required_with_profile():
    [item] = _items(["¿Cuando la belleza pase?"], "argentina", "question_marks")
    assert item["required"] is True and item["style_rule"] is True
    assert _afters(item) == ["Cuando la belleza pase"]


# --- Paréntesis (Argentina) -------------------------------------------------

def test_parentheses_suggested_only_in_argentina():
    [item] = _items(["(Cargada del amor)", "Le cortaron la cosita (ay, ay, ay)"],
                    "argentina", "parentheses")
    assert item["required"] is False
    assert _afters(item) == ["Cargada del amor", "Le cortaron la cosita ay, ay, ay"]
    assert _items(["(Cargada del amor)"], "chile", "parentheses") == []


# --- Tildes que faltan ------------------------------------------------------

def test_missing_accents_are_required_and_keep_case():
    cases = {
        "MENEANDO, PIERNA Y CADERA, ARDIAN COMO CANDELA": "MENEANDO, PIERNA Y CADERA, ARDÍAN COMO CANDELA",
        "Descubriendose el hombre": "Descubriéndose el hombre",
        "Nos explican de que esta de más": "Nos explican de que está de más",
        "La gracia y la bondad siempre daran sus frutos": "La gracia y la bondad siempre darán sus frutos",
        "Y fue gol, pasion y locura": "Y fue gol, pasión y locura",
    }
    for before, after in cases.items():
        [item] = _items([before], "chile", "accent_missing")
        assert item["required"] is True
        assert _afters(item) == [after]


def test_accent_rule_skips_ambiguous_words_and_english():
    lines = ["Esta noche es para vos", "Esta canción", "Sabia es la vida", "Mas no te vayas",
             "I need your passion and your love", "Hacia el sol", "Y esta, la más linda",
             "Se vació la casa", "Lo publico mañana"]
    assert _items(lines, "argentina", "accent_missing") == []
    [item] = _items(["¿Dónde esta?"], "argentina", "accent_missing")
    assert _afters(item) == ["¿Dónde está?"]


# --- Alcance: re-render por pedido de cambio -----------------------------------

def test_style_points_block_change_request_rerender_but_suggestions_do_not():
    segs = [_line("Para no volver.", 0), _line("(Oh, oh)", 3)]
    review = build_review(segs, title="", portal="argentina")
    blocking = required_for_scope(review, "missing_only")
    assert [i["kind"] for i in blocking] == ["terminal_period"]
