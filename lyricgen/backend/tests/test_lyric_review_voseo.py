"""Voseo rioplatense en la Revisión rápida (sólo portal Argentina).

UMG Argentina devolvía letras con el voseo pasado a tuteo ("querés" →
"quieres", "vení" → "ven", "anotate" → "anótate", "No te quedés" → "No te
quedes", "Te llevás" → "Te llevas"). Chile tiene su propio voseo y tuteo
legítimo: ahí la regla no corre. Letras inventadas para el test.
"""
import pytest

from lyric_review import build_review, required_for_scope
from voseo_lexicon import PAIRS, VOS_MARKERS, is_vos_marker, lookup


def _segments(lines, step=5.0):
    return [{"text": t, "start": k * step, "end": k * step + step - 0.4, "segment_id": f"s{k}"}
            for k, t in enumerate(lines)]


def _evidence(witness_lines=None, gemini_lines=None, step=5.0, witness_offset=0.0):
    hyps = []
    if witness_lines:
        events = []
        for k, line in enumerate(witness_lines):
            t = k * step + witness_offset
            for word in line.split():
                events.append({"word": word, "start": round(t, 2), "end": round(t + 0.3, 2)})
                t += 0.35
        hyps.append({"role": "independent", "family": "openai/whisper-1", "kind": "word_stream",
                     "events": events})
    if gemini_lines:
        hyps.append({"role": "primary", "family": "google/gemini-2.5-flash-audio", "kind": "text",
                     "transformation": "gemini_reference_hypothesis_raw",
                     "events": [{"text": "\n".join(gemini_lines)}]})
    return {"hypotheses_by_family": hyps}


def _review(lines, portal="argentina", witness=None, gemini=None, official=None, original=None):
    segs = _segments(lines)
    return build_review(segs, original_segments=original if original is not None else segs,
                        machine_evidence=_evidence(witness, gemini), title="",
                        official_text="\n".join(official) if official else None, portal=portal)


def _voseo(review):
    return [i for i in review["items"] if i["kind"] == "voseo"]


SONG = [
    "Caminando por la avenida sin mirar atrás",
    "Te llevas la guitarra y los discos del comedor",
    "La noche se termina cuando sale el colectivo",
    "Las luces del puerto se apagan de a una",
    "Me quedo mirando el río con el mate frío",
    "Y la ciudad entera duerme sin hacer ruido",
]


# --- Evidencia de los oídos ----------------------------------------------------

def test_two_ears_hearing_the_vos_form_make_a_required_one_click_fix():
    heard = list(SONG)
    heard[1] = "Te llevás la guitarra y los discos del comedor"
    review = _review(SONG, witness=heard, official=heard)
    [item] = _voseo(review)
    assert item["required"] is True
    assert item["group"] == "text"
    assert set(item["sources"]) == {"official", "witness"}
    assert item["occurrences"][0]["after"] == "Te llevás la guitarra y los discos del comedor"
    assert "llevás" in item["why"]
    # El voseo no es estilo: no frena un re-render por pedido de cambio.
    assert item not in required_for_scope(review, "missing_only")
    assert item in required_for_scope(review, "full")


def test_one_ear_alone_is_a_suggestion():
    heard = list(SONG)
    heard[1] = "Te llevás la guitarra y los discos del comedor"
    [item] = _voseo(_review(SONG, gemini=heard))
    assert item["required"] is False
    assert item["sources"] == ["gemini"]


def test_one_ear_plus_voseo_elsewhere_in_the_song_is_required():
    lines = list(SONG)
    lines[0] = "Vos sos la luz de la avenida"
    heard = list(lines)
    heard[1] = "Te llevás la guitarra y los discos del comedor"
    [item] = _voseo(_review(lines, official=heard))
    assert item["required"] is True


def test_witness_heard_in_another_repetition_does_not_count():
    heard = list(SONG)
    heard[1] = "Te llevás la guitarra y los discos del comedor"
    segs = _segments(SONG)
    review = build_review(segs, original_segments=segs, title="", portal="argentina",
                          machine_evidence=_evidence(heard, witness_offset=12.0))
    assert _voseo(review) == []


def test_ear_that_writes_tu_in_the_line_cannot_vouch_for_an_unaccented_imperative():
    # Gemini sin tildes: "besame" no es voseo si en la misma línea escribe
    # "quiereme" (tú; el vos sería "quereme").
    lines = list(SONG)
    lines[2] = "Mírame, bésame, quiéreme esta noche"
    heard = list(lines)
    heard[2] = "Mirame, besame, quiereme esta noche"
    assert _voseo(_review(lines, gemini=heard, witness=heard)) == []


def test_ear_hearing_a_different_spelling_is_absorbed_not_duplicated():
    lines = list(SONG)
    lines[4] = "Me dijiste que no quieres volver al barrio"
    heard = list(lines)
    heard[4] = "Me dijiste que no querés volver al barrio"
    review = _review(lines, witness=heard, gemini=heard)
    [item] = [i for i in _voseo(review) if i["occurrences"][0]["line_index"] == 4]
    assert item["required"] is True
    assert item["occurrences"][0]["after"] == "Me dijiste que no querés volver al barrio"
    # Lo que oyeron los oídos cuenta como voseo de la canción: "Te llevas"
    # pasa a sugerencia (sin oído propio, no obligatoria).
    [other] = [i for i in _voseo(review) if i["occurrences"][0]["line_index"] == 1]
    assert other["required"] is False and other["sources"] == []
    text_items = [i for i in review["items"] if i["kind"] in {"heard_different", "missing"}
                  and i["occurrences"][0]["line_index"] == 4]
    assert text_items == []


def test_ven_heard_as_veni_is_one_voseo_point():
    lines = list(SONG)
    lines[3] = "Ven a la plaza del barrio esta noche"
    heard = list(lines)
    heard[3] = "Vení a la plaza del barrio esta noche"
    review = _review(lines, witness=heard, gemini=heard)
    [item] = _voseo(review)
    assert item["required"] is True
    assert item["occurrences"][0]["after"].startswith("Vení a la plaza")
    assert [i for i in review["items"] if i["kind"] == "heard_different"] == []


def test_multiword_ear_block_is_absorbed_by_every_voseo_point_it_covers():
    lines = list(SONG)
    lines[3] = "Tú eres la luz del puerto"
    heard = list(lines)
    heard[3] = "Vos sos la luz del puerto"
    review = _review(lines, witness=heard, gemini=heard)
    items = [i for i in _voseo(review) if i["occurrences"][0]["line_index"] == 3]
    assert {i["occurrences"][0]["after"] for i in items} == {
        "Vos eres la luz del puerto", "Tú sos la luz del puerto"}
    assert all(i["required"] and i["sources"] == ["gemini", "witness"] for i in items)
    assert [i for i in review["items"] if i["kind"] == "heard_different"] == []


# --- Voseo de la propia canción -------------------------------------------------

VOSEO_SONG = [
    "Vos sos la luz que tengo en esta ciudad",
    "Ven y anótate en la lista del club",
    "Ya sé que vos no querés hablar",
    "No te quedes en la puerta que la fiesta va a empezar",
]


def test_song_voseo_suggests_unambiguous_forms_only():
    items = _voseo(_review(VOSEO_SONG))
    after = {i["occurrences"][0]["after"] for i in items}
    assert "Ven y anotate en la lista del club" in after
    assert "No te quedés en la puerta que la fiesta va a empezar" in after
    assert all(i["required"] is False for i in items)
    # "ven" también es "ellos ven": sin un oído no se toca.
    assert not any(a.startswith("Vení") for a in after)


def test_tuteo_song_without_evidence_is_left_alone():
    ballad = [
        "Tú eres el sol de mi mañana",
        "Si me quieres dime que sí",
        "Quédate conmigo hasta el amanecer",
        "Mírame a los ojos una vez más",
    ]
    assert _voseo(_review(ballad)) == []


def test_line_with_explicit_tu_is_not_voseada_by_inference():
    lines = ["Vos sos la luz que tengo", "Tú me quieres y te quedes", "Y sabés que es verdad"]
    assert _voseo(_review(lines)) == []


def test_determiner_before_a_noun_homograph_is_not_a_verb():
    lines = ["Vos sos la luz que tengo", "Pagamos las cuentas del mes",
             "Y tus esperas no terminan"]
    assert _voseo(_review(lines)) == []


def test_official_lyric_in_tu_vetoes_song_inference():
    lines = list(VOSEO_SONG)
    official = list(VOSEO_SONG)
    official[3] = "No te quedes en la puerta que la fiesta va a empezar"
    items = _voseo(_review(lines, official=official))
    assert not any("quedés" in i["occurrences"][0]["after"] for i in items)


def test_human_edit_is_never_required():
    lines = list(SONG)
    original = _segments(SONG)
    original[1] = {**original[1], "text": "Te lleva la guitarra y los discos del comedor"}
    heard = list(SONG)
    heard[1] = "Te llevás la guitarra y los discos del comedor"
    [item] = _voseo(_review(lines, witness=heard, official=heard, original=original))
    assert item["required"] is False


def test_fix_keeps_case_and_punctuation():
    lines = ["VOS SOS LA LUZ DEL BARRIO", "NO TE QUEDES EN LA ESQUINA, CHE"]
    [item] = _voseo(_review(lines))
    assert item["occurrences"][0]["after"] == "NO TE QUEDÉS EN LA ESQUINA, CHE"


# --- Portales --------------------------------------------------------------------

@pytest.mark.parametrize("portal", ["chile", "umg", None])
def test_rule_never_runs_outside_argentina(portal):
    heard = list(SONG)
    heard[1] = "Te llevás la guitarra y los discos del comedor"
    assert _voseo(_review(SONG, portal=portal, witness=heard, official=heard)) == []
    assert _voseo(_review(VOSEO_SONG, portal=portal)) == []


def test_chilean_voseo_and_tuteo_stay_untouched_in_chile():
    chile = ["¿Por qué me das tanta pena?", "Ay, si me querís decime", "Si no sabís lo que tienes po"]
    review = _review(chile, portal="chile", witness=chile, gemini=chile)
    assert _voseo(review) == []


# --- Diccionario -------------------------------------------------------------------

@pytest.mark.parametrize("tu, vos", [
    ("quieres", "querés"), ("tienes", "tenés"), ("puedes", "podés"), ("vienes", "venís"),
    ("eres", "sos"), ("llevas", "llevás"), ("silbas", "silbás"), ("ven", "vení"),
    ("anótate", "anotate"), ("quédate", "quedate"), ("mírame", "mirame"), ("dime", "decime"),
    ("ponte", "ponete"), ("cuéntame", "contame"), ("cuídate", "cuidate"),
    ("escríbeme", "escribime"), ("quedes", "quedés"), ("entregues", "entregués"),
    ("vayas", "vayás"), ("vuelvas", "volvás"), ("tú", "vos"),
])
def test_lexicon_pairs(tu, vos):
    assert lookup(tu).vos == vos
    assert lookup(tu.upper()).vos == vos


@pytest.mark.parametrize("shared", ["estás", "vas", "das", "ves", "verás", "serás", "sabrás", "has"])
def test_forms_shared_by_tu_and_vos_are_not_pairs(shared):
    assert lookup(shared) is None


def test_markers_skip_forms_that_are_other_words():
    assert is_vos_marker("querés") and is_vos_marker("Sos") and is_vos_marker("mirá")
    for word in ("salí", "viví", "tomás", "bebés", "quedate", "estás"):
        assert not is_vos_marker(word)
    assert all(v not in PAIRS for v in VOS_MARKERS)


def test_ambiguous_forms_are_flagged():
    for word in ("ven", "mira", "ganas", "partes", "di", "sal"):
        assert lookup(word).ambiguous
    for word in ("quieres", "anótate", "quedes"):
        assert not lookup(word).ambiguous
