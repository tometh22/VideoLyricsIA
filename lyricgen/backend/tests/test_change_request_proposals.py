import pytest

from change_request_parser import parse_change_request
from change_request_proposals import (
    apply_operations,
    build_proposal,
    lyrics_preview_context,
)


def segment(start, end, text, **extra):
    return {"start": start, "end": end, "text": text, **extra}


def assert_source_complete(comment, parsed):
    covered = set()
    for row in parsed["instructions"]:
        assert row["source_excerpt"] == comment[row["source_start"]:row["source_end"]]
        covered.update(range(row["source_start"], row["source_end"]))
    assert {i for i, char in enumerate(comment) if not char.isspace()} <= covered


def test_parser_extracts_timestamped_client_text_without_inventing_words():
    comment = '0:13 debe decir "Dicen que soy lo peor"'
    instruction = parse_change_request(comment)["instructions"][0]
    assert instruction["kind"] == "replace_text"
    assert instruction["timecode_seconds"] == 13
    assert instruction["requested_text"] == "Dicen que soy lo peor"
    assert instruction["requested_text"] in comment


def test_parser_extracts_explicit_before_after_pair_and_repeat_scope():
    comment = 'En todas las apariciones, cambiar "Vodka con naranja" por "Vodka con Gancia"'
    instruction = parse_change_request(comment)["instructions"][0]
    assert instruction["current_text"] == "Vodka con naranja"
    assert instruction["requested_text"] == "Vodka con Gancia"
    assert instruction["scope"] == "all_matching"


def test_historical_unquoted_umg_timestamp_list_requires_review_without_losing_source():
    comment = """0:48_ Un caramelo Blanco de limón
1:11 Y Con el tu corazón
1:29 Un caramelo Blanco de limón
3:34 Porque la vida es un bar (revisar que termina antes de que cante)"""
    parsed = parse_change_request(comment)
    assert parsed["has_actionable_text"] is False
    manual = [row for row in parsed["instructions"] if row["kind"] == "manual_review"]
    assert [row["timecode_seconds"] for row in manual] == [48.0, 71.0, 89.0, 214.0]
    assert [row["source_excerpt"] for row in manual] == comment.splitlines()
    assert_source_complete(comment, parsed)
    timing = [row for row in parsed["instructions"] if row["kind"] == "timing_review"]
    assert len(timing) == 1
    assert timing[0]["timecode_seconds"] == 214.0


def test_historical_unquoted_multiline_requests_preserve_every_continuation_for_review():
    comment = """0:03: Yo, Horacio Acavallo
gracias por el homenaje a todos
los boxeadores campeones del mundo
0:48: Víctor Galíndez, Carlos Monzón
Nicolino Loche, Horacio Acavallo
1:25: Víctor Galíndez, Carlos Monzón
Ringo Bonavena, Uby Sacco
1:57: Pinas van
2:01: piñas vienen, piñas van"""

    parsed = parse_change_request(comment)
    assert parsed["has_actionable_text"] is False
    assert all(row["kind"] == "manual_review" for row in parsed["instructions"])
    assert [(row["timecode_seconds"], row["source_excerpt"]) for row in parsed["instructions"]] == [
        (3.0, "0:03: Yo, Horacio Acavallo\ngracias por el homenaje a todos\n"
              "los boxeadores campeones del mundo"),
        (48.0, "0:48: Víctor Galíndez, Carlos Monzón\nNicolino Loche, Horacio Acavallo"),
        (85.0, "1:25: Víctor Galíndez, Carlos Monzón\nRingo Bonavena, Uby Sacco"),
        (117.0, "1:57: Pinas van"),
        (121.0, "2:01: piñas vienen, piñas van"),
    ]
    assert_source_complete(comment, parsed)
    proposal = build_proposal(comment=comment, segments=[segment(2, 6, "Antes")], base_revision=1)
    assert proposal["status"] == "needs_input"
    assert proposal["applicable_count"] == 0
    assert all(not row["applicable"] for row in proposal["operations"])


@pytest.mark.parametrize("quoted", [False, True], ids=["historical-unquoted-manual", "explicit-quoted-patches"])
def test_umg_timestamp_list_requires_explicit_literals_before_building_text_patches(quoted):
    segments = [
        segment(47.0, 49.0, "Un caramelo blanco de lima"),
        segment(70.0, 72.0, "Y con tu corazón"),
        segment(88.0, 90.0, "Un caramelo blanco de lima"),
    ]
    original = """0:48_ Un caramelo Blanco de limón
1:11 Y Con el tu corazón
1:29 Un caramelo Blanco de limón"""
    explicit = '''0:48_ "Un caramelo Blanco de limón"
1:11 "Y Con el tu corazón"
1:29 "Un caramelo Blanco de limón"'''
    comment = explicit if quoted else original
    proposal = build_proposal(
        comment=comment,
        segments=segments, base_revision=3,
    )
    applicable = [row for row in proposal["operations"] if row["applicable"]]
    if not quoted:
        assert proposal["status"] == "needs_input"
        assert applicable == []
        assert_source_complete(comment, parse_change_request(comment))
        return
    assert len(applicable) == 3
    result, _ = apply_operations(segments, proposal, [row["id"] for row in applicable])
    assert [row["text"] for row in result] == [
        "Un caramelo Blanco de limón",
        "Y Con el tu corazón",
        "Un caramelo Blanco de limón",
    ]


def test_parser_keeps_timing_and_structure_review_only():
    parsed = parse_change_request(
        "1:20 mantener por más tiempo y dejar la frase completa en una sola línea"
    )
    assert {row["kind"] for row in parsed["instructions"]} >= {
        "timing_review", "structure_review",
    }


def test_historical_mixed_layout_only_extracts_explicit_quoted_phrases():
    comment = (
        '0:01 y 0:04: "borracho y agresivo"\n'
        '0:42: Y en la damajuana no hay nada que beber\n\n'
        'Revisar que las frases completas esten en 1 sola pantalla'
    )
    parsed = parse_change_request(comment)
    rows = [row for row in parsed["instructions"] if row["kind"] == "merge_phrase"]
    assert [(row["timecode_seconds"], row["requested_text"]) for row in rows] == [
        (1.0, "borracho y agresivo"),
        (4.0, "borracho y agresivo"),
    ]
    assert not any(row["kind"] == "replace_text" for row in parsed["instructions"])
    manual = [row for row in parsed["instructions"] if row["kind"] == "manual_review"]
    assert len(manual) == 1
    assert manual[0]["source_excerpt"] == '0:42: Y en la damajuana no hay nada que beber'
    assert_source_complete(comment, parsed)


def test_complete_phrase_builds_exact_structural_merge_and_applies_it():
    segments = [
        segment(0.8, 1.8, "borracho"),
        segment(1.8, 3.2, "y agresivo"),
        segment(41.8, 43.0, "Y en la damajuana"),
        segment(43.0, 45.2, "no hay nada que beber"),
    ]
    proposal = build_proposal(
        comment=(
            '0:01: "borracho y agresivo"\n'
            '0:42: "Y en la damajuana no hay nada que beber"\n'
            'Revisar que las frases completas esten en 1 sola pantalla'
        ),
        segments=segments,
        base_revision=3,
    )
    applicable = [row for row in proposal["operations"] if row["applicable"]]
    assert proposal["status"] == "ready"
    assert [row["kind"] for row in applicable] == ["merge_phrase", "merge_phrase"]
    assert [len(row["current_segments"]) for row in applicable] == [2, 2]

    result, selected = apply_operations(
        segments, proposal, [row["id"] for row in applicable],
    )
    assert [row["text"] for row in result] == [
        "borracho y agresivo",
        "Y en la damajuana no hay nada que beber",
    ]
    assert [(row["start"], row["end"]) for row in result] == [
        (0.8, 3.2), (41.8, 45.2),
    ]
    assert all(row["automatic_apply_allowed"] is False for row in selected)


@pytest.mark.parametrize("quoted", [False, True], ids=["unquoted-manual", "quoted-unmatched-fragments"])
def test_complete_phrase_stays_manual_when_fragments_do_not_match_exactly(quoted):
    phrase = '"Y en la damajuana no hay nada que beber"' if quoted else 'Y en la damajuana no hay nada que beber'
    proposal = build_proposal(
        comment=(
            f'0:42: {phrase}\n'
            'Revisar que las frases completas esten en 1 sola pantalla'
        ),
        segments=[segment(41.8, 44, "Otra frase")],
        base_revision=1,
    )
    assert proposal["status"] == "needs_input"
    assert proposal["applicable_count"] == 0
    expected = "complete_phrase_fragments_not_found" if quoted else "instruction_requires_manual_interpretation"
    assert proposal["operations"][0]["reason"] == expected


def test_explicit_quoted_phrase_and_timing_note_are_separate_instructions():
    comment = '3:34 "Porque la vida es un bar" (revisar que termina antes de que cante)'
    parsed = parse_change_request(comment)
    replacements = [row for row in parsed["instructions"] if row["kind"] == "replace_text"]
    assert [(row["timecode_seconds"], row["requested_text"]) for row in replacements] == [
        (214.0, "Porque la vida es un bar"),
    ]
    assert len([row for row in parsed["instructions"] if row["kind"] == "timing_review"]) == 1
    assert_source_complete(comment, parsed)


def test_explicit_quoted_full_phrase_preserves_historical_second_and_third_parts():
    full = "Yo, Horacio Acavallo gracias por el homenaje a todos los boxeadores campeones del mundo"
    parsed = parse_change_request(f'0:03: "{full}"')
    assert len(parsed["instructions"]) == 1
    assert parsed["instructions"][0]["requested_text"] == full
    assert parsed["instructions"][0]["kind"] == "replace_text"


def test_build_and_apply_timestamped_text_proposal_preserves_timing_and_metadata():
    segments = [
        segment(10, 12, "Antes"),
        segment(12.5, 15, "Dicen que soy peor", locked=False, pos=[0.5, 0.8]),
        segment(15.5, 18, "Después"),
    ]
    proposal = build_proposal(
        comment='0:13 debe decir "Dicen que soy lo peor"',
        segments=segments, base_revision=4, audio_revision=2, audio_sha256="a" * 64,
    )
    assert proposal["status"] == "ready"
    operation = proposal["operations"][0]
    result, selected = apply_operations(segments, proposal, [operation["id"]])
    assert [row["text"] for row in result] == [
        "Antes", "Dicen que soy lo peor", "Después",
    ]
    assert (result[1]["start"], result[1]["end"], result[1]["pos"]) == (
        12.5, 15, [0.5, 0.8],
    )
    assert selected[0]["kind"] == "replace_text"


def test_all_matching_expands_only_exact_normalized_occurrences():
    segments = [
        segment(0, 2, "Vodka con naranja"),
        segment(4, 6, "Vodka con naranja"),
        segment(8, 10, "Vodka con naranja y hielo"),
    ]
    proposal = build_proposal(
        comment='En todas las apariciones cambiar "Vodka con naranja" por "Vodka con Gancia"',
        segments=segments, base_revision=1,
    )
    applicable = [row for row in proposal["operations"] if row["applicable"]]
    assert len(applicable) == 2
    result, _ = apply_operations(segments, proposal, [row["id"] for row in applicable])
    assert [row["text"] for row in result] == [
        "Vodka con Gancia", "Vodka con Gancia", "Vodka con naranja y hielo",
    ]


def test_terminal_period_is_deterministic_but_locked_line_needs_confirmation():
    proposal = build_proposal(
        comment="Sacar los puntos finales de todas las frases",
        segments=[segment(0, 2, "Hola."), segment(2, 4, "Chau.", locked=True)],
        base_revision=0,
    )
    rows = [row for row in proposal["operations"] if row["applicable"]]
    assert len(rows) == 2
    assert rows[0]["automatic_apply_allowed"] is True
    assert rows[1]["automatic_apply_allowed"] is False


def test_stale_proposal_cannot_apply_to_changed_document():
    segments = [segment(0, 2, "Texto viejo")]
    proposal = build_proposal(
        comment='0:01 debe decir "Texto nuevo"', segments=segments, base_revision=2,
    )
    with pytest.raises(RuntimeError, match="stale"):
        apply_operations(
            [segment(0, 2, "Otro texto")], proposal,
            [proposal["operations"][0]["id"]],
        )


def test_ambiguous_request_never_becomes_applicable_patch():
    proposal = build_proposal(
        comment="Revisar bien esta parte porque suena rara",
        segments=[segment(0, 2, "Texto")], base_revision=0,
    )
    assert proposal["status"] == "needs_input"
    assert proposal["applicable_count"] == 0
    assert all(not row["applicable"] for row in proposal["operations"])


def test_background_request_suggests_editable_prompt_from_request_and_current_context():
    comment = "Cambiar el fondo porfa. Que no aparezcan armas."
    proposal = build_proposal(
        comment=comment,
        segments=[segment(0, 2, "Letra")],
        base_revision=2,
        background_context={
            "background_hint": "Barrio urbano nocturno, cámara lenta",
            "background_mode": "veo",
            "artist": "2 Minutos",
            "song_title": "Tema",
        },
    )
    operation = proposal["operations"][0]

    assert proposal["status"] == "ready"
    assert proposal["applicable_count"] == 0
    assert proposal["visual_action_count"] == 1
    assert operation["visual_action"] == "regenerate_background"
    assert operation["regeneration_supported"] is True
    assert operation["current_prompt"] == "Barrio urbano nocturno, cámara lenta"
    assert comment in operation["suggested_prompt"]
    assert "Cumplir literalmente" in operation["suggested_prompt"]
    assert operation["force_content_validation"] is True


def test_multi_scene_background_request_stays_in_scene_editor():
    proposal = build_proposal(
        comment="Cambiar el fondo, sin personas",
        segments=[segment(0, 2, "Letra")],
        base_revision=1,
        background_context={
            "scene_plan": {"scenes": [{"index": 0}, {"index": 1}]},
        },
    )
    operation = proposal["operations"][0]
    assert proposal["status"] == "needs_input"
    assert operation["regeneration_supported"] is False
    assert "multi_scene_background_requires_scene_editor" in operation["warnings"]


def test_lyrics_preview_context_returns_full_revision_bound_lyric():
    segments = [
        segment(0, 2, "Primera línea", _id="a"),
        segment(2, 4, "Segunda línea", _id="b"),
    ]
    proposal = build_proposal(
        comment='0:03 debe decir "Segunda línea corregida"',
        segments=segments,
        base_revision=7,
    )
    context = lyrics_preview_context(
        segments,
        revision=7,
        base_revision=proposal["base_revision"],
        base_segments_content_hash=proposal["segments_content_hash"],
    )
    assert context["matches_base"] is True
    assert [row["text"] for row in context["segments"]] == [
        "Primera línea", "Segunda línea",
    ]


def test_lyrics_preview_context_marks_changed_editor_snapshot_stale():
    original = [segment(0, 2, "Texto anterior")]
    proposal = build_proposal(
        comment='0:01 debe decir "Texto correcto"',
        segments=original,
        base_revision=2,
    )
    context = lyrics_preview_context(
        [segment(0, 2, "Editado por otra persona")],
        revision=3,
        base_revision=proposal["base_revision"],
        base_segments_content_hash=proposal["segments_content_hash"],
    )
    assert context["matches_base"] is False
    assert context["revision"] == 3
