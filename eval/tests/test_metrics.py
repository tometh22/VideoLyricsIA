from __future__ import annotations

import json

from eval.metrics import align_lines, normalize_text, score_edit_effort, score_song


def line(idx, start, end, text, kind="main"):
    return {"idx": idx, "start_s": start, "end_s": end, "text": text, "kind": kind}


def test_normalization_contract():
    assert normalize_text('¿Sí, pingüino! [A-B] — "2026"…') == "si pinguino a b 2026"
    assert normalize_text("  HOLA\t mundo\n") == "hola mundo"


def test_alignment_exact_omitted_and_invented():
    ref = [line(0, 0, 1, "hola"), line(1, 2, 3, "mundo")]
    hyp = [line(0, 0, 1, "hola"), line(1, 4, 5, "fantasma")]
    result = align_lines(ref, hyp)
    assert [(m["ref_idx"], m["hyp_idx"]) for m in result["matches"]] == [(0, 0)]
    assert result["omitted_ref_indices"] == [1]
    assert result["invented_hyp_indices"] == [1]


def test_segmentation_two_reference_lines_to_one_hypothesis():
    ref = [line(0, 0, 1, "uno"), line(1, 1, 2, "dos")]
    hyp = [line(0, 0, 2, "uno dos")]
    _, _, errors = score_song("song", ref, hyp)
    assert "segmentation" in {error["type"] for error in errors}


def test_distant_repeated_occurrence_is_not_a_timing_match():
    ref = [line(0, 0, 1, "mismo estribillo"), line(1, 60, 61, "mismo estribillo")]
    hyp = [line(0, 0, 1, "mismo estribillo")]
    result = align_lines(ref, hyp)
    assert [(m["ref_idx"], m["hyp_idx"]) for m in result["matches"]] == [(0, 0)]
    assert result["omitted_ref_indices"] == [1]


def test_song_perfect_and_minimal_changes():
    ref = [line(0, 0, 1, "Sí")]
    perfect, _, _ = score_song("song", ref, ref)
    assert perfect["song_perfect"] is True
    changed_word, _, _ = score_song("song", ref, [line(0, 0, 1, "no")])
    assert changed_word["song_perfect"] is False
    changed_timing, _, _ = score_song("song", ref, [line(0, 0.2, 1, "Sí")])
    assert changed_timing["song_perfect"] is False


def test_main_only_excludes_non_main_and_parenthetical_content():
    ref = [line(0, 0, 1, "hola (oh)"), line(1, 1, 2, "yeah", "adlib")]
    hyp = [line(0, 0, 1, "hola"), line(1, 1, 2, "distinto", "adlib")]
    metrics, _, _ = score_song("song", ref, hyp)
    assert metrics["wer_main"] == 0
    assert metrics["wer_full"] > 0


def test_edit_effort():
    edits = [
        {"op": "start_edit", "line_idx": 0, "before": 1.0, "after": 1.2},
        {"op": "text_edit", "line_idx": 0, "before": "ola", "after": "hola"},
    ]
    result = score_edit_effort(edits, 2)
    assert result["edit_count_total"] == 2
    assert result["lines_touched_pct"] == 0.5
    assert round(result["timing_shift_ms"]["p50"]) == 200


def test_determinism():
    ref = [line(0, 0, 1, "hola")]
    first = score_song("song", ref, ref)
    second = score_song("song", ref, ref)
    assert json.dumps(first, sort_keys=True) == json.dumps(second, sort_keys=True)
