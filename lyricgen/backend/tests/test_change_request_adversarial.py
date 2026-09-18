"""Pure content-safety regressions; no DB, network or paid generation required."""
from copy import deepcopy
import random
import unicodedata

import pytest

from change_request_parser import MAX_COMMENT_LENGTH, parse_change_request
from change_request_proposals import (
    _replace_text, apply_operations, build_proposal, proposal_content_hash,
)


def segment(start, end, text, **extra):
    return {"start": start, "end": end, "text": text, **extra}


def proposal(comment, segments=None):
    return build_proposal(comment=comment,
                          segments=segments or [segment(9, 11, "viejo")], base_revision=3)


@pytest.mark.parametrize("comment", [
    "No quitar los puntos finales",
    '0:10 no cambiar "sol" por "luna"',
    '0:10 cambiar "a" por "b" y "c" por "d"',
    '0:10 repetir "cantando" antes de "este guaguancó"',
    "0:10 la misma frase del punto anterior",
    '0:10 quizás cambiar "sol" por "luna"',
    '0:10 si es posible cambiar "sol" por "luna"',
    '0:10 cambiar "sol" por "luna" solo acá',
    '0:10 debe decir "sin cerrar',
    "0:10 texto\u202eoculto",
    "0:99 Hola", "-0:10 Hola", "0:10.500 Hola", "0:105 Hola",
    "0:10-0:15 Hola", "1:99:20 Hola", "No cambiar el fondo",
    "0:10 no modificar el fondo", "0:10 NO BORRAR ESTA LINEA",
    "0:10 si suena mal, reemplazar esta frase",
    "0:10 debe decir nuevo solo en el estribillo", "0:10 debe decir hola excepto en el último coro",
])
def test_unsafe_or_unsupported_instruction_has_no_lyric_or_visual_action(comment):
    result = proposal(comment)
    assert result["status"] == "needs_input"
    assert result["applicable_count"] == result["visual_action_count"] == 0
    assert result["unresolved_count"] >= 1
    assert all(not row["applicable"] for row in result["operations"])


@pytest.mark.parametrize("literal", [
    "¿Por qué?", "¡Con las palma' oiga!", "Déjelo, no más pastar.",
    "¿Sí? ¡Año nuevo!", "Canción 🕊️", "Cafe\u0301", "Hola,", "…otra vez…",
])
def test_literal_punctuation_and_unicode_are_not_cleaned_away(literal):
    parsed = parse_change_request(f'0:10 debe decir "{literal}"')
    assert len(parsed["instructions"]) == 1
    row = parsed["instructions"][0]
    assert row["requested_text"] == literal
    built = proposal(f'0:10 debe decir "{literal}"')
    assert built["operations"][0]["proposed_segments"][0]["text"] == literal


def test_local_period_removal_does_not_expand_and_keeps_other_segments_exact():
    segments = [segment(9, 11, "Hola."), segment(19, 21, "Chau.")]
    built = proposal("0:10 quitar el punto final", segments)
    assert built["applicable_count"] == 1
    result, _ = apply_operations(segments, built, [built["operations"][0]["id"]])
    assert [row["text"] for row in result] == ["Hola", "Chau."]


def test_time_list_is_one_literal_at_each_location_not_a_conjunction():
    rows = parse_change_request('0:10 y 0:20 / 0:30: "Hola mundo"')["instructions"]
    assert [(row["timecode_seconds"], row["requested_text"]) for row in rows] == [
        (10, "Hola mundo"), (20, "Hola mundo"), (30, "Hola mundo")]


@pytest.mark.parametrize("comment,expected", [
    ('1:33 debería ser "¡Con las palma\' oiga!"', "¡Con las palma' oiga!"),
    ('- 1:58 la frase es "Como la madre que NOS dio el ser y la vida,"',
     "Como la madre que NOS dio el ser y la vida,"),
    ('2:02 la frase debe ser "Quiero yo a Vicuña, también a Andacollo"',
     "Quiero yo a Vicuña, también a Andacollo"),
])
def test_additional_explicit_phrase_grammar_preserves_complete_literal(comment, expected):
    rows = parse_change_request(comment)["instructions"]
    assert len(rows) == 1 and rows[0]["kind"] == "replace_text"
    assert rows[0]["requested_text"] == expected


def test_scope_does_not_leak_between_independent_instructions():
    rows = parse_change_request('0:10 cambiar "rojo" por "azul" en todas las apariciones\n'
                                '0:20 cambiar "uno" por "dos"')["instructions"]
    assert [row["scope"] for row in rows] == ["all_matching", "single"]


@pytest.mark.parametrize("comment", ['0:10 cambiar "sol" por "todas las veces"',
                                     '0:10 cambiar "en todo el tema" por "sol"'])
def test_quoted_literals_cannot_set_instruction_scope(comment):
    row = parse_change_request(comment)["instructions"][0]
    assert row["kind"] == "replace_text" and row["scope"] == "single"


@pytest.mark.parametrize("directive", ["No juntar frases completas en una sola pantalla",
                                      "- No juntar frases completas en una sola pantalla",
                                      "Si es posible, frases completas en una sola pantalla",
                                      'Cambiar "frases completas en una sola pantalla" por "otra frase"'])
def test_negated_or_quoted_layout_never_enables_global_merges(directive):
    parsed = parse_change_request('0:09 "hola mundo"\n\n' + directive)
    assert parsed["instructions"][0]["kind"] == "replace_text"
    assert not any(row["kind"] == "merge_phrase" for row in parsed["instructions"])


@pytest.mark.parametrize("prohibition", ["No toquen lo demás", "No regenerar el fondo", "Si no es mucho lío"])
def test_prohibition_continuation_never_becomes_lyric(prohibition):
    parsed = parse_change_request('0:10 "Hola"\n' + prohibition)
    assert [row["kind"] for row in parsed["instructions"]] == ["replace_text", "manual_review"]
    assert parsed["instructions"][0]["requested_text"] == "Hola"


@pytest.mark.parametrize("comment", [
    "0:10 evitar cortar la frase",
    "0:10 Hola\nPor favor conservar el resto",
    "0:10 Hola\nOjo con la segunda línea",
    "0:10 debe decir hola si corresponde",
    "0:10 debe decir hola (corregir solo aquí)",
    "0:10 Hola", "0:10 debe decir Hola",
    "0:10 Primera parte\nsegunda parte completa",
    '0:10 debe decir "Hola" y conservar el resto',
    '0:10 "Hola"\nOjo con la segunda línea',
])
def test_unquoted_or_mixed_prose_is_review_only_with_complete_source(comment):
    """No guessed distinction between unknown commands and unquoted lyrics."""
    parsed = parse_change_request(comment)
    assert parsed["has_actionable_text"] is False
    assert all(row["requested_text"] is None for row in parsed["instructions"])
    covered = set()
    for row in parsed["instructions"]:
        assert row["source_excerpt"] == comment[row["source_start"]:row["source_end"]]
        covered.update(range(row["source_start"], row["source_end"]))
    assert {i for i, char in enumerate(comment) if not char.isspace()} <= covered
    built = proposal(comment)
    assert built["applicable_count"] == 0
    assert built["unresolved_count"] >= 1


@pytest.mark.parametrize("literal", [
    "evitar cortar la frase", "Hola si corresponde", "Hola (corregir solo aquí)",
    "No modificar el fondo", "Por favor conservar el resto", "Ojo con la segunda línea",
    "Nunca termina tarde", "mantener más tiempo", "frase completa", "sin personas",
])
@pytest.mark.parametrize("prefix", ["0:10 ", "0:10 debe decir "])
def test_explicit_quoted_lyrics_are_content_not_editorial_commands(literal, prefix):
    parsed = parse_change_request(prefix + '"' + literal + '"')
    assert len(parsed["instructions"]) == 1
    assert parsed["instructions"][0]["kind"] == "replace_text"
    assert parsed["instructions"][0]["requested_text"] == literal


@pytest.mark.parametrize("constraint", ["No cambiar los colores", "No cambiar la iluminación", "Mantener la misma paleta"])
def test_visual_preservation_constraint_is_not_omitted_or_treated_as_approved(constraint):
    built = proposal("Cambiar fondo\n" + constraint)
    visual = next(row for row in built["operations"] if row["kind"] == "background_review")
    assert constraint in visual["suggested_prompt"]
    assert visual["regeneration_supported"] is False
    assert "unresolved_visual_constraint" in visual["warnings"]


def test_satisfied_text_does_not_hide_remaining_manual_work():
    built = proposal('0:10 debe decir "Hola"\nRevisar lo demás', [segment(9, 11, "Hola")])
    assert built["satisfied_count"] == 1
    assert built["unresolved_count"] == 1
    assert built["status"] == "partial"


def test_visual_words_inside_explicit_lyric_pair_do_not_generate_background():
    rows = parse_change_request('0:10 cambiar "armas" por "almas"')["instructions"]
    assert len(rows) == 1 and rows[0]["kind"] == "replace_text"
    assert rows[0]["current_text"] == "armas" and rows[0]["requested_text"] == "almas"


def test_mixed_visual_and_lyric_prose_abstains_instead_of_polluting_prompt():
    built = proposal('Cambiar fondo y cambiar la letra a "algo nuevo"')
    assert built["visual_action_count"] == built["applicable_count"] == 0
    assert built["operations"][0]["reason"] == "mixed_visual_and_lyric_instruction"


@pytest.mark.parametrize("current,expected", [("soledad", "sol"), ("sí", "si"), ("año", "ano"), ("SOL", "sol"), ("sol sol", "sol"),
                                          ("digo si\u0301", "si"), ("del an\u0303o", "an"), ("Cafe\u0301", "Cafe"),
                                          ("👩‍💻", "👩"), ("✈️", "✈"), ("👍🏽", "👍")])
def test_approximate_or_non_unique_token_is_not_authorized(current, expected):
    assert _replace_text(current, expected, "luna") is None


def test_exact_token_and_canonical_unicode_are_supported():
    assert _replace_text("Sale el sol.", "sol", "mar") == "Sale el mar."
    assert _replace_text("cafe\u0301", "café", "té") == "té"


def test_global_scope_keeps_significant_diacritics_distinct():
    built = proposal('Cambiar "si" por "no" en todas las apariciones',
                     [segment(9, 11, "si"), segment(19, 21, "sí")])
    assert built["applicable_count"] == 1
    assert built["operations"][0]["current_segments"][0]["text"] == "si"


def test_ambiguous_boundary_requires_operator_target_choice():
    built = proposal('0:10 debe decir "nuevo"',
                     [segment(8, 10, "primero"), segment(10, 12, "segundo")])
    assert built["applicable_count"] == 0
    assert built["status"] == "needs_input"


def test_structural_merge_does_not_bridge_silence():
    built = proposal('0:10 "hola mundo"\nfrases completas en una sola pantalla',
                     [segment(9, 11, "hola"), segment(100, 102, "mundo")])
    assert built["applicable_count"] == 0


def test_layout_does_not_retype_explicit_text_changes():
    rows = parse_change_request('0:10 "hola mundo"\n0:20 cambiar "uno" por "dos"\n'
                                'frases completas en una sola pantalla')["instructions"]
    assert [row["kind"] for row in rows] == ["merge_phrase", "replace_text", "layout_context"]
    assert rows[1]["current_text"] == "uno"


def test_competing_patches_are_not_presented_as_jointly_applicable():
    built = proposal('0:10 cambiar "sol" por "luna"\n0:10 cambiar "rojo" por "azul"',
                     [segment(9, 11, "sol rojo")])
    assert built["applicable_count"] == 0
    assert built["unresolved_count"] == 2
    assert {row["reason"] for row in built["operations"]} == {"conflicting_operations"}


def test_already_satisfied_is_revision_bound_evidence_not_target_missing():
    built = proposal('0:10 debe decir "Hola"', [segment(9, 11, "Hola")])
    assert built["satisfied_count"] == 1
    assert built["unresolved_count"] == built["applicable_count"] == 0
    assert built["operations"][0]["status"] == "already_satisfied"
    assert built["operations"][0]["verified_segments"][0]["text"] == "Hola"


def test_text_patch_invalidates_word_metadata_not_independent_style_or_timing():
    segments = [segment(9, 11, "antes", words=[{"text": "antes"}], tokens=[1],
                        word_timestamps=[1], pos=[0.5, 0.8], locked=True)]
    built = proposal('0:10 debe decir "después"', segments)
    result, _ = apply_operations(segments, built, [built["operations"][0]["id"]])
    assert result == [segment(9.0, 11.0, "después", pos=[0.5, 0.8], locked=True)]


def test_prompt_prioritizes_all_visual_constraints_and_excludes_lyric_edits():
    built = build_proposal(comment='0:10 debe decir "NO ES UN PROMPT"\n'
                                    'Cambiar el fondo. No incluir armas.\nQUITAR BANDERAS CHILENAS',
                           segments=[segment(9, 11, "antes")], base_revision=1,
                           background_context={"background_hint": "x" * 3990})
    visual = [row for row in built["operations"] if row["kind"] == "background_review"]
    assert len(visual) == 1
    prompt = visual[0]["suggested_prompt"]
    assert "No incluir armas" in prompt and "QUITAR BANDERAS CHILENAS" in prompt
    assert "NO ES UN PROMPT" not in prompt
    assert len(prompt) <= 4000
    assert len(visual[0]["source_spans"]) == 2


def test_constraints_too_large_fail_closed_without_truncating_them():
    comment = "Cambiar fondo, no incluir armas. " + "sin personas " * 350
    built = build_proposal(comment=comment, segments=[], base_revision=0)
    visual = built["operations"][0]
    assert visual["regeneration_supported"] is False
    assert comment.strip() in visual["suggested_prompt"]
    assert "background_constraints_exceed_prompt_limit" in visual["warnings"]


def test_source_coverage_includes_prefix_unknown_paragraph_and_visual_tail():
    comment = 'Cambiar "a" por "b"\n0:10 Hola\n\nmundo\nQUITAR BANDERAS CHILENAS'
    parsed = parse_change_request(comment)
    assert parsed["coverage_complete"] is True
    covered = set()
    for row in parsed["instructions"]:
        assert comment[row["source_start"]:row["source_end"]] == row["source_excerpt"]
        covered.update(range(row["source_start"], row["source_end"]))
    assert {i for i, char in enumerate(comment) if not char.isspace()} <= covered
    assert any(row["kind"] == "manual_review" and row["source_excerpt"] == "mundo" for row in parsed["instructions"])
    assert any(row["kind"] == "background_review" for row in parsed["instructions"])


def test_content_hash_changes_for_preview_edits_not_lifecycle_or_key_order():
    built = proposal('0:10 debe decir "nuevo"')
    original_hash = proposal_content_hash(built)
    lifecycle = deepcopy(built)
    lifecycle["status"] = "applied"
    lifecycle["updated_at"] = "tomorrow"
    lifecycle["operations"][0]["status"] = "applied"
    assert proposal_content_hash(lifecycle) == original_hash
    lifecycle["operations"][0]["proposed_segments"][0]["text"] = "otro"
    assert proposal_content_hash(lifecycle) != original_hash
    assert proposal_content_hash(dict(reversed(list(built.items())))) == original_hash


@pytest.mark.parametrize("field", ["request_sha256", "base_revision", "segments_content_hash", "audio_revision", "audio_sha256", "parser_version"])
def test_content_hash_covers_base_identities(field):
    built = proposal('0:10 debe decir "nuevo"')
    changed = {**built, field: "changed"}
    assert proposal_content_hash(changed) != proposal_content_hash(built)


def test_seeded_thousand_variants_keep_literals_sources_and_scope():
    rng = random.Random(20260918)
    alphabet = "abcdeñáíóúü!?¡¿.,’🕊"
    for _ in range(1000):
        literal = "".join(rng.choice(alphabet) for _ in range(rng.randint(1, 45)))
        # No synthetic unmatched wrapping apostrophe: it is a deliberate abstention.
        literal = "X" + unicodedata.normalize(rng.choice(["NFC", "NFD"]), literal)
        comment = f'0:10 debe decir "{literal}"'
        first = parse_change_request(comment)
        second = parse_change_request(comment + '\n0:20 cambiar "uno" por "dos" en todas las apariciones')
        assert first["instructions"][0] == second["instructions"][0]
        assert first["instructions"][0]["requested_text"] == literal
        assert parse_change_request(comment) == first


def test_parser_input_and_instruction_bounds_are_explicit():
    with pytest.raises(ValueError, match="comment_too_long"):
        parse_change_request("x" * (MAX_COMMENT_LENGTH + 1))
    with pytest.raises(ValueError, match="too_many_instructions"):
        parse_change_request("0:10 Hola\n" * 401)
    parsed = parse_change_request("x" * MAX_COMMENT_LENGTH)
    assert parsed["instructions"][0]["kind"] == "manual_review"
    assert len(parsed["instructions"][0]["source_excerpt"]) == MAX_COMMENT_LENGTH


@pytest.mark.parametrize("invalid_anchor", ["a\u200b", "x" * 501, " "])
@pytest.mark.parametrize("template", ['0:10 cambiar "{}" por "nuevo"',
                                     '0:10 dice "{}" debería ser "nuevo"'])
def test_invalid_old_literal_never_degrades_to_full_segment_replacement(invalid_anchor, template):
    comment = template.format(invalid_anchor)
    parsed = parse_change_request(comment)
    assert not parsed["has_actionable_text"]
    assert all(row["requested_text"] is None for row in parsed["instructions"])
    built = proposal(comment, [segment(9, 11, "No es el objetivo del cliente")])
    assert built["applicable_count"] == 0
    assert built["status"] == "needs_input"


def test_parenthetical_review_scope_does_not_expand_a_local_replacement():
    comment = '0:10 cambiar "sol" por "luna" (revisar todas las apariciones)'
    parsed = parse_change_request(comment)
    replacements = [row for row in parsed["instructions"] if row["kind"] == "replace_text"]
    assert len(replacements) == 1
    assert replacements[0]["scope"] == "single"
    segments = [segment(9, 11, "sol"), segment(19, 21, "sol")]
    built = proposal(comment, segments)
    applicable = [row for row in built["operations"] if row["applicable"]]
    assert len(applicable) == 1
    result, _ = apply_operations(segments, built, [applicable[0]["id"]])
    assert [row["text"] for row in result] == ["luna", "sol"]
    assert built["unresolved_count"] == 1
