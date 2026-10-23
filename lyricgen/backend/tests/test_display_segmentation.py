"""Regression contract for automatic consecutive lyric cards, without audio IO."""
from copy import deepcopy
import math

import pytest

import display_segmentation as ds

SOURCE = {"job_id": "initial-job", "audio_sha256": "a" * 64,
          "audio_revision": 1, "segments_revision": 0}
TEXT = "Qué locura ver, te manejan sin razón, qué locura ver, se olvidaron del amor"


def line(text=TEXT, start=2.0, identity="parent"):
    words = []
    cursor = start
    for token in text.split():
        words.append({"word": token, "start": cursor, "end": cursor + 0.3, "score": 0.95})
        cursor += 0.5
    return {"_id": identity, "segment_id": identity, "text": text, "start": start,
            "end": cursor, "words": words, "provider_evidence": {"immutable": "parent"},
            "line_signals_v2": {"old_index": 0}, "scale": 1.1}


def proposal(rows):
    return ds.plan(rows, source=SOURCE)


def test_exact_tokens_outer_bounds_unique_ids_and_private_evidence_not_cloned():
    rows = [line()]
    before = deepcopy(rows)
    p = proposal(rows)
    output = ds.apply(rows, p, source=SOURCE)
    assert len(output) > 1
    assert " ".join(row["text"] for row in output).split() == TEXT.split()
    assert output[0]["start"] == rows[0]["start"]
    assert output[-1]["end"] == rows[0]["end"]
    assert len({r["_id"] for r in output}) == len(output)
    assert all(r["segment_id"] == r["_id"] for r in output)
    assert all(r["scale"] == 1.1 and "provider_evidence" not in r and "line_signals_v2" not in r for r in output)
    assert [w for r in output for w in r["words"]] == rows[0]["words"]
    assert rows == before
    assert ds.apply(rows, p, source=SOURCE) == output  # Same retry, same result.


@pytest.mark.parametrize("edit,reason", [
    (lambda r: r.update(locked=True), "protected_line"),
    (lambda r: r.update(operator_locked=True), "protected_line"),
    (lambda r: r.update(timing_locked=True), "protected_line"),
    (lambda r: r.update(provider_output_integrity=False), "source_evidence_invalid"),
    (lambda r: r.update(words_stale=True), "stale_word_evidence"),
    (lambda r: r.update(word_evidence_status="stale"), "stale_word_evidence"),
    (lambda r: r.pop("words"), "word_coverage_incomplete"),
    (lambda r: r["words"].pop(), "word_coverage_incomplete"),
    (lambda r: r["words"][0].update(word="Que"), "word_text_mismatch"),
    (lambda r: r["words"][0].update(start=-1), "word_clocks_invalid_or_overlapping"),
    (lambda r: r["words"][0].update(end=math.inf), "invalid_time"),
    (lambda r: r["words"][0].update(start=math.nan), "invalid_time"),
    (lambda r: r["words"][1].update(start=2.1), "word_clocks_invalid_or_overlapping"),
    (lambda r: r["words"][0].update(synthetic=True), "stale_or_synthetic_word_evidence"),
])
def test_unsafe_clock_or_operator_work_abstains(edit, reason):
    row = line()
    edit(row)
    # Nonfinite input is not JSON-serializable evidence, reject atomically.
    if reason == "invalid_time":
        with pytest.raises(ValueError):
            proposal([row])
        return
    p = proposal([row])
    assert not p["changes"]
    assert p["review"][0]["reason"] == reason
    assert ds.apply([row], p, source=SOURCE) == [row]


@pytest.mark.parametrize("field,value", [("text", TEXT + " más"), ("locked", True), ("end", 90)])
def test_apply_binds_all_source_fields_including_locks(field, value):
    rows = [line()]
    p = proposal(rows)
    rows[0][field] = value
    with pytest.raises(ValueError, match="stale_or_invalid"):
        ds.apply(rows, p, source=SOURCE)


@pytest.mark.parametrize("field,value", [("audio_sha256", "b" * 64), ("audio_revision", 2), ("segments_revision", 1)])
def test_apply_rejects_audio_and_editor_race(field, value):
    rows = [line()]
    with pytest.raises(ValueError, match="stale_or_invalid"):
        ds.apply(rows, proposal(rows), source={**SOURCE, field: value})


def test_forged_ranges_rejected_even_with_recomputed_proposal_hash():
    rows = [line()]
    p = proposal(rows)
    p["changes"][0]["ranges"] = [[0, 3], [3, 4]]
    p["proposal_hash"] = ds.digest({k: v for k, v in p.items() if k != "proposal_hash"})
    with pytest.raises(ValueError, match="proposal_invalid"):
        ds.apply(rows, p, source=SOURCE)


def test_repeated_parent_text_gets_occurrence_specific_ids_untouched_rows_exact():
    rows = [line(), {"_id": "middle", "start": 11, "end": 12, "text": "(Oh)"},
            line(start=13, identity="repeat")]
    output = ds.apply(rows, proposal(rows), source=SOURCE)
    assert len({r["_id"] for r in output}) == len(output)
    assert output.count(rows[1]) == 1
    assert ds.plan(output, source=SOURCE)["changes"] == []


def test_no_pause_no_invented_times_and_short_held_note_not_split():
    row = line()
    for i, w in enumerate(row["words"]):
        w.update(start=2 + i * 0.3, end=2 + (i + 1) * 0.3)
    p = proposal([row])
    assert not p["changes"]
    assert p["review"][0]["reason"] == "no_supported_readable_partition"
    assert not proposal([line("Oh mi amor", start=0)]) ["changes"]
    held = {"start": 0, "end": 20, "text": "Oh mi amor"}
    assert proposal([held])["review"] == []


def test_chorus_parentheses_accents_and_punctuation_are_preserved():
    row = line("Vení conmigo, (no me olvides) volvé a casa, te espero con amor")
    output = ds.apply([row], proposal([row]), source=SOURCE)
    assert len(output) > 1
    assert " ".join(r["text"] for r in output) == row["text"]
    assert all(ds._balanced(r["text"].split()) for r in output)


def test_no_sorting_hides_inversion_and_no_new_consecutive_layers():
    rows = [line(), {"start": 1, "end": 2, "text": "anterior"}]
    with pytest.raises(ValueError, match="invalid_source_timeline|overlapping_vocal_layers"):
        ds.apply(rows, proposal(rows), source=SOURCE)
    rows = [line(), {"start": 5, "end": 13, "text": "(coro)"}]
    with pytest.raises(ValueError, match="overlapping_vocal_layers"):
        ds.apply(rows, proposal(rows), source=SOURCE)


def test_modes_fail_closed_and_enforce_is_only_fresh_and_authorized(monkeypatch):
    rows = [line()]
    monkeypatch.setenv("DISPLAY_SEGMENTATION_MODE", "off")
    assert ds.run(rows, source=SOURCE, fresh=True, authorization={}) == (rows, None, None)
    monkeypatch.setenv("DISPLAY_SEGMENTATION_MODE", "shadow")
    output, report, _ = ds.run(rows, source=SOURCE, fresh=True, authorization={})
    assert output == rows and report["status"] == "shadow" and report["proposed_count"]
    monkeypatch.setenv("DISPLAY_SEGMENTATION_MODE", "enforce")
    for fresh, grant, status in [(True, {}, "authorization_required"), (False, {"authorized": True}, "ineligible_existing_work")]:
        assert ds.run(rows, source=SOURCE, fresh=fresh, authorization=grant)[1]["status"] == status
    output, report, _ = ds.run(rows, source=SOURCE, fresh=True, authorization={"authorized": True})
    assert output != rows and report["status"] == "applied"


def test_two_cards_are_checked_and_huge_sample_has_no_truncation():
    rows = [line(start=i * 10, identity=str(i)) for i in range(30)]
    p = proposal(rows)
    assert len(p["changes"]) == 30
    assert len(ds.apply(rows, p, source=SOURCE)) >= 60
    assert len(proposal(rows[:2])["changes"]) == 2


def test_existing_structural_warning_cannot_disappear_by_splitting_its_parent():
    row = line()
    row["live_structural_suggestion"] = {"reason": "chorus_count_uncertain"}
    p = proposal([row])
    assert not p["changes"]
    assert p["review"][0]["reason"] == "parent_quality_review_required"
