"""Verificación punto por punto de un pedido contra la letra publicada.

Los casos imitan los patrones de los pedidos UMG sep-nov 2026 que volvieron
como segunda ronda (letras inventadas, mismas formas)."""
from types import SimpleNamespace
from datetime import datetime, timedelta, timezone

import change_request_verification as v


def seg(start, end, text):
    return {"start": start, "end": end, "text": text}


CHORUS_BEFORE = [
    seg(150.0, 153.5, "Ven y súmate a la ronda de los martes"),
    seg(154.0, 157.0, "Que la noche ya empezó"),
    seg(159.0, 162.5, "Ven y súmate a la ronda de los martes"),
    seg(163.0, 166.0, "Que la noche terminó"),
]


def statuses(result):
    return [(p["kind"], p["status"]) for p in result["points"]]


def test_a_chorus_fixed_only_once_fails_the_other_cited_occurrence():
    after = [dict(row) for row in CHORUS_BEFORE]
    after[0]["text"] = "Vení sumate a la ronda de los martes"
    comment = "2:30 Vení sumate a la ronda de los martes // 2:39 Vení sumate a la ronda de los martes"
    result = v.verify(comment, after, before_segments=CHORUS_BEFORE)
    assert statuses(result) == [("text", "ok"), ("text", "fail")]
    failing = result["points"][1]
    assert failing["reason"] == "found_only_elsewhere"
    assert failing["line"]["text"] == "Ven y súmate a la ronda de los martes"
    assert result["blocking"] is True


def test_the_publish_guard_text_check_would_have_let_that_close():
    """Por qué existe este módulo: el control anterior acepta la frase en
    cualquier parte de la canción."""
    import change_request_publish_guard as guard
    after = [dict(row) for row in CHORUS_BEFORE]
    after[0]["text"] = "Vení sumate a la ronda de los martes"
    comment = "2:30 Vení sumate a la ronda de los martes // 2:39 Vení sumate a la ronda de los martes"
    assert guard.check_requested_text(comment, after)["status"] == "found"


def test_every_point_fixed_passes():
    after = [dict(row) for row in CHORUS_BEFORE]
    after[0]["text"] = after[2]["text"] = "Vení sumate a la ronda de los martes"
    comment = "2:30 Vení sumate a la ronda de los martes // 2:39 Vení sumate a la ronda de los martes"
    result = v.verify(comment, after, before_segments=CHORUS_BEFORE)
    assert statuses(result) == [("text", "ok"), ("text", "ok")]
    assert result["blocking"] is False


def test_two_times_for_the_same_phrase_need_two_occurrences():
    after = [seg(14.0, 16.0, "A esperarte en la esquina"), seg(20.5, 22.0, "a esperarme en la esquina")]
    result = v.verify("0:16 y 0:20 A esperarte en la esquina", after)
    assert statuses(result) == [("text", "ok"), ("text", "fail")]
    assert result["points"][1]["reason"] == "same_occurrence_as_other_time"


def test_what_the_client_said_is_wrong_must_be_gone():
    after = [seg(160.0, 163.0, "Cuando el viento pero nos da igual")]
    ok = v.verify('2:41 dice "que nos da", es "pero nos da"', after)
    assert statuses(ok) == [("text", "ok")]
    still = [seg(160.0, 163.0, "Si me ves, si me vas")]
    result = v.verify('2:41 dice "si me VES", es "si me VAS"', still)
    assert statuses(result) == [("text", "fail")]
    assert result["points"][0]["reason"] == "current_text_still_present"


def test_accents_count_only_when_the_client_writes_them():
    asked = v.verify("0:11 te cantás una de blues", [seg(10.0, 13.0, "Te cantas una de blues")])
    assert statuses(asked) == [("text", "fail")]
    omitted = v.verify("0:11 te cantas una de blues", [seg(10.0, 13.0, "Te cantás una de blues")])
    assert statuses(omitted) == [("text", "ok")]


def test_a_word_in_capitals_is_strict_about_its_accent():
    result = v.verify("2:41 TU casa vacía", [seg(160.0, 163.0, "Tú casa vacía")])
    assert statuses(result) == [("text", "fail")]
    fixed = v.verify("2:41 TU casa vacía", [seg(160.0, 163.0, "Tu casa vacía")])
    assert statuses(fixed) == [("text", "ok")]


def test_removing_an_accent_is_checked_in_both_directions():
    result = v.verify('1:47 quitar tilde a palabra "Esperé"', [seg(105.0, 108.0, "Me dicen que la esperé en el puerto")])
    assert statuses(result) == [("text", "fail")]
    fixed = v.verify('1:47 quitar tilde a palabra "Esperé"', [seg(105.0, 108.0, "Me dicen que la espere en el puerto")])
    assert statuses(fixed) == [("text", "ok")]


def test_explicit_interval_is_checked_against_the_requested_start_and_end():
    before = [seg(15.0, 17.0, "Otra frase"), seg(20.5, 24.0, "La tarde se volvió de vidrio")]
    good = [seg(15.0, 16.8, "Otra frase"), seg(17.2, 21.0, "La tarde se volvió de vidrio")]
    result = v.verify("Desde 0:17 hasta 0:21: La tarde se volvió de vidrio", good, before_segments=before)
    assert statuses(result) == [("timing", "ok")]
    result = v.verify("Desde 0:17 hasta 0:21: La tarde se volvió de vidrio", before, before_segments=before)
    assert statuses(result) == [("timing", "fail")]
    assert result["points"][0]["reason"] == "interval_mismatch"


def test_prose_timing_must_move_the_line_the_way_the_client_asked():
    before = [seg(10.0, 13.0, "Primera línea"), seg(13.5, 15.0, "Segunda línea")]
    comment = "0:12 la primera línea entra tarde"
    assert statuses(v.verify(comment, before, before_segments=before)) == [("timing", "fail")]
    earlier = [seg(9.4, 13.0, "Primera línea"), seg(13.5, 15.0, "Segunda línea")]
    assert statuses(v.verify(comment, earlier, before_segments=before)) == [("timing", "ok")]
    later = [seg(10.6, 13.0, "Primera línea"), seg(13.5, 15.0, "Segunda línea")]
    result = v.verify(comment, later, before_segments=before)
    assert statuses(result) == [("timing", "fail")]
    assert result["points"][0]["reason"] == "timing_moved_wrong_way"


def test_prose_timing_without_the_version_the_client_saw_is_manual():
    after = [seg(10.0, 13.0, "Primera línea")]
    result = v.verify("0:12 la primera línea entra tarde", after)
    assert statuses(result) == [("timing", "manual")]


def test_extend_until_a_time_needs_the_new_end():
    before = [seg(60.0, 64.0, "Corazón de madera")]
    short = [seg(60.0, 64.5, "Corazón de madera")]
    result = v.verify("1:02 alargar tiempo de esa línea hasta 1:10.", short, before_segments=before)
    assert result["points"][0]["reason"] == "end_short_of_request"
    long = [seg(60.0, 69.5, "Corazón de madera")]
    assert statuses(v.verify("1:02 alargar tiempo de esa línea hasta 1:10.", long, before_segments=before)) == [("timing", "ok")]


def test_metadata_is_compared_with_accents():
    result = v.verify("Corregir artista: Nico Pérez", [], metadata={"artist": "Nico Perez", "song_title": "x"})
    assert statuses(result) == [("metadata", "fail")]
    result = v.verify("Corregir artista: Nico Pérez", [], metadata={"artist": "Nico Pérez", "song_title": "x"})
    assert statuses(result) == [("metadata", "ok")]
    title = v.verify('Solo cambiar el título "Banda - Cuando Llegue El Tren"', [],
                     metadata={"artist": "Banda", "song_title": "Cuando Llegue El Tren"})
    assert statuses(title) == [("metadata", "ok")]


def test_question_marks_and_periods():
    after = [seg(94.0, 97.0, "¿Ves la luz?"), seg(98.0, 100.0, "Ya se fue.")]
    assert statuses(v.verify("1:34 Sacarle los signos de pregunta", after)) == [("punctuation", "fail")]
    assert statuses(v.verify("1:38 sacar el punto final", after)) == [("punctuation", "fail")]
    clean = [seg(94.0, 97.0, "Ves la luz"), seg(98.0, 100.0, "Ya se fue")]
    assert statuses(v.verify("1:34 Sacarle los signos de pregunta", clean)) == [("punctuation", "ok")]


def test_background_and_free_prose_are_left_to_the_operator():
    result = v.verify("Sacar el arma que aparece en el video // Se ve muy oscuro, ¿podrían revisar?", [seg(0, 2, "x")])
    assert [p["status"] for p in result["points"]] == ["manual", "manual"]
    assert result["blocking"] is False


def test_unbalanced_quotes_are_manual_instead_of_a_wrong_check():
    result = v.verify('1:12 agregar tilde a "MÍO uno tiene tilde, dice "MÍO MIO"', [seg(70, 73, "Mío mío")])
    assert statuses(result) == [("manual", "manual")]


def test_a_correction_asked_for_every_repetition_fails_while_one_is_pending():
    after = [dict(row) for row in CHORUS_BEFORE]
    after[0]["text"] = "Vení sumate a la ronda de los martes"
    comment = "2:30 Vení sumate a la ronda de los martes, corregir en todas las partes"
    result = v.verify(comment, after, before_segments=CHORUS_BEFORE)
    assert statuses(result) == [("text", "fail")]
    assert result["points"][0]["reason"] == "repetitions_not_fixed"
    assert result["points"][0]["repetitions_pending"][0]["start"] == 159.0


def test_an_unrequested_repetition_left_behind_is_a_warning_not_a_failure():
    after = [dict(row) for row in CHORUS_BEFORE]
    after[0]["text"] = "Vení sumate a la ronda de los martes"
    result = v.verify("2:30 Vení sumate a la ronda de los martes", after, before_segments=CHORUS_BEFORE)
    assert statuses(result) == [("text", "ok")]
    assert result["points"][0]["repetitions_pending"][0]["text"] == "Ven y súmate a la ronda de los martes"


def test_overrides_must_cover_every_failing_point_and_stay_internal():
    after = [dict(row) for row in CHORUS_BEFORE]
    after[0]["text"] = "Vení sumate a la ronda de los martes"
    comment = "2:30 Vení sumate a la ronda de los martes // 2:39 Vení sumate a la ronda de los martes"
    result = v.verify(comment, after, before_segments=CHORUS_BEFORE)
    [failing] = v.failing_ids(result)
    assert v.missing_overrides(result, {}) == [failing]
    assert v.missing_overrides(result, {failing: "   "}) == [failing]
    overrides = v.parse_overrides({failing: "El cliente pidió dejar la segunda como está", "otro": "x", 3: "y"})
    assert v.missing_overrides(result, overrides) == []
    assert v.clean_overrides(result, overrides) == {failing: "El cliente pidió dejar la segunda como está"}
    audit = v.audit_summary(result, v.clean_overrides(result, overrides))
    assert [p.get("override") for p in audit["points"] if p["status"] == "fail"] == [
        "El cliente pidió dejar la segunda como está"]


def test_point_ids_are_stable_for_the_same_comment():
    comment = "0:16 A esperarte en la esquina // Desde 0:20 hasta 0:22: Dejame salir temprano"
    assert [p["id"] for p in v.extract_points(comment)] == [p["id"] for p in v.extract_points(comment)]


def test_a_new_request_repeating_a_closed_point_is_a_recurrence():
    base = datetime(2026, 10, 2, tzinfo=timezone.utc)
    rows = [
        SimpleNamespace(id=1, delivery_id=7, comment="0:14 Dejame salir temprano // 0:58 no me pidas PERDÓN",
                        submitted_at=base, resolved_at=base + timedelta(hours=3)),
        SimpleNamespace(id=2, delivery_id=7, comment="0:15 Dejame salir temprano",
                        submitted_at=base + timedelta(days=2), resolved_at=None),
        SimpleNamespace(id=3, delivery_id=7, comment="2:00 otra cosa distinta",
                        submitted_at=base + timedelta(days=3), resolved_at=None),
        # Abierto cuando llegó el nuevo: no es reincidencia (no se había cerrado).
        SimpleNamespace(id=4, delivery_id=8, comment="0:14 Dejame salir temprano", submitted_at=base, resolved_at=None),
        SimpleNamespace(id=5, delivery_id=8, comment="0:14 Dejame salir temprano",
                        submitted_at=base + timedelta(days=1), resolved_at=None),
    ]
    found = v.recurrences_by_request(rows)
    assert list(found) == [2]
    assert found[2][0]["request_id"] == 1


def test_the_kill_switch(monkeypatch):
    assert v.enabled() is True
    monkeypatch.setenv("CHANGE_REQUEST_CLOSE_VERIFICATION", "0")
    assert v.enabled() is False
