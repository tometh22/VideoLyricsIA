import pytest

from change_request_parser import parse_change_request
from change_request_proposals import (
    apply_operations,
    build_proposal,
    lyrics_preview_context,
)


def segment(start, end, text, **extra):
    return {"start": start, "end": end, "text": text, **extra}


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


def test_parser_extracts_umg_timestamp_lists_and_keeps_timing_note_manual():
    comment = """0:48_ Un caramelo Blanco de limón
1:11 Y Con el tu corazón
1:29 Un caramelo Blanco de limón
3:34 Porque la vida es un bar (revisar que termina antes de que cante)"""
    parsed = parse_change_request(comment)
    replacements = [
        row for row in parsed["instructions"] if row["kind"] == "replace_text"
    ]
    assert [(row["timecode_seconds"], row["requested_text"]) for row in replacements] == [
        (48.0, "Un caramelo Blanco de limón"),
        (71.0, "Y Con el tu corazón"),
        (89.0, "Un caramelo Blanco de limón"),
        (214.0, "Porque la vida es un bar"),
    ]
    assert all(row["requested_text"] in comment for row in replacements)
    timing = [row for row in parsed["instructions"] if row["kind"] == "timing_review"]
    assert len(timing) == 1
    assert timing[0]["timecode_seconds"] == 214.0


def test_umg_timestamp_list_builds_one_text_patch_per_matching_time():
    segments = [
        segment(47.0, 49.0, "Un caramelo blanco de lima"),
        segment(70.0, 72.0, "Y con tu corazón"),
        segment(88.0, 90.0, "Un caramelo blanco de lima"),
    ]
    proposal = build_proposal(
        comment="""0:48_ Un caramelo Blanco de limón
1:11 Y Con el tu corazón
1:29 Un caramelo Blanco de limón""",
        segments=segments, base_revision=3,
    )
    applicable = [row for row in proposal["operations"] if row["applicable"]]
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
