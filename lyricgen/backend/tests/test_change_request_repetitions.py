"""Un pedido que corrige una línea de estribillo propone el mismo cambio en
las otras repeticiones de esa línea, cada una por separado y destildable."""
from change_request_proposals import apply_operations, build_proposal


def segment(start, end, text, **extra):
    return {"start": start, "end": end, "text": text, **extra}


CHORUS = [
    segment(10, 13, "Ven y súmate a la ronda de los martes"),
    segment(14, 16, "Que la noche ya empezó"),
    segment(40, 43, "Ven y súmate a la ronda de los martes"),
    segment(44, 46, "Que la noche terminó"),
    segment(70, 73, "Ven y súmate a la ronda de los martes,"),
]
COMMENT = '0:11 debe decir "Vení sumate a la ronda de los martes"'


def operations(proposal):
    return [row for row in proposal["operations"] if row["applicable"]]


def test_the_requested_line_and_its_identical_repetitions_are_proposed_separately():
    proposal = build_proposal(comment=COMMENT, segments=CHORUS, base_revision=4)
    rows = operations(proposal)
    assert [(row["start"], row.get("repetition_of") is not None) for row in rows] == [(10, False), (40, True)]
    primary, repetition = rows
    assert repetition["repetition_of"] == primary["id"]
    assert repetition["repetition_of_start"] == 10
    assert repetition["proposed_segments"][0]["text"] == "Vení sumate a la ronda de los martes"
    # El cliente no nombró la repetición: nunca se aplica sola.
    assert repetition["automatic_apply_allowed"] is False
    assert "repetition_of_requested_line" in repetition["warnings"]


def test_a_variant_with_other_punctuation_is_not_touched():
    proposal = build_proposal(comment=COMMENT, segments=CHORUS, base_revision=4)
    assert 70 not in [row["start"] for row in operations(proposal)]


def test_the_operator_can_leave_one_repetition_out():
    proposal = build_proposal(comment=COMMENT, segments=CHORUS, base_revision=4)
    primary, _repetition = operations(proposal)
    result, _ = apply_operations(CHORUS, proposal, [primary["id"]])
    assert [row["text"] for row in result][:3] == [
        "Vení sumate a la ronda de los martes", "Que la noche ya empezó", "Ven y súmate a la ronda de los martes"]


def test_applying_both_changes_both_and_keeps_the_timeline():
    proposal = build_proposal(comment=COMMENT, segments=CHORUS, base_revision=4)
    result, _ = apply_operations(CHORUS, proposal, [row["id"] for row in operations(proposal)])
    assert [row["text"] for row in result] == [
        "Vení sumate a la ronda de los martes", "Que la noche ya empezó",
        "Vení sumate a la ronda de los martes", "Que la noche terminó",
        "Ven y súmate a la ronda de los martes,"]
    assert [(row["start"], row["end"]) for row in result] == [(row["start"], row["end"]) for row in CHORUS]


def test_explicit_all_matching_requests_are_not_duplicated():
    segments = [segment(0, 2, "Vodka con naranja"), segment(4, 6, "Vodka con naranja")]
    proposal = build_proposal(
        comment='En todas las apariciones cambiar "Vodka con naranja" por "Vodka con Gancia"',
        segments=segments, base_revision=1)
    rows = operations(proposal)
    assert len(rows) == 2
    assert not any(row.get("repetition_of") for row in rows)


def test_a_repetition_the_client_also_cited_is_not_proposed_twice():
    comment = '0:11 debe decir "Vení sumate a la ronda de los martes" // 0:41 debe decir "Vení sumate a la ronda de los martes"'
    proposal = build_proposal(comment=comment, segments=CHORUS, base_revision=4)
    rows = operations(proposal)
    assert [row["start"] for row in rows] == [10, 40]
    assert not any(row.get("repetition_of") for row in rows)
