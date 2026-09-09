from copy import deepcopy
import pytest

from forced_align import wordstamps_to_segments
from line_evidence import annotate_provider_evidence
from timing_validation import diagnose


def word(text, start, end, **kwargs):
    return dict(word=text, start=start, end=end, **kwargs)


def broken_rows():
    return [dict(start=10.439, end=41.9, text="No sé a dónde voy", words=[
        word("No",10.46,10.64), word("sé",10.64,10.78), word("a",10.78,10.92),
        word("dónde",10.92,11.16), word("voy",41.84,41.9)]),
        dict(start=41.9,end=42.26,text="El invierno se quedó",words=[
            word("El",41.9,42.06),word("invierno",42.06,42.26),
            word("se",42.26,42.26),word("quedó",42.26,42.26)])]


def test_reproduced_gap_flag_without_timing_or_text_change():
    rows = broken_rows()
    before = deepcopy(rows)
    findings = diagnose(rows)
    assert findings[0]["internal_gaps"][0]["gap_seconds"] == pytest.approx(30.68)
    assert findings[0]["reasons"] == ["internal_word_gap_with_degenerate_support"]
    assert findings[1]["reasons"] == ["word_timing_support_unusable"]
    assert rows == before


@pytest.mark.parametrize("words", [
    [word("oh",0,1),word("oh",31,32)],  # valid long pause + repeated words
    [word("a",0,15),word("b",15,30)],  # long continuous vocalization
    [],  # absent evidence is not evidence of a structural defect
    [word("a",0,1),word("b",1,1),word("c",2,3)],  # one unusable token
])
def test_controls_not_flagged_by_duration_repetition_or_missing_words(words):
    assert diagnose([dict(start=0,end=32,text="control",words=words)]) == []


def test_invalid_words_never_manufacture_a_gap_across_missing_evidence():
    rows = [dict(start=0,end=40,text="a b c",words=[
        word("a",0,1),word("b",None,None),word("c",39,40)])]
    assert diagnose(rows) == []
    rows[0]["words"][2]["end"] = float("nan")
    assert diagnose(rows)[0]["reasons"] == ["word_timing_support_unusable"]


def test_reannotation_preserves_native_probability_and_timing_lineage(monkeypatch):
    monkeypatch.setenv("QUALITY_CONTENT_FINGERPRINT_HMAC_KEY", "test-privacy-hmac-key-0123456789abcdef")
    monkeypatch.setenv("QUALITY_CONTENT_FINGERPRINT_HMAC_KEY_ID", "test-v1")
    raw = [word("hola",1,1.4,probability=0.000001), word("mundo",1.4,2,probability=.9)]
    rows = wordstamps_to_segments(raw, ["hola mundo"])
    first = annotate_provider_evidence(rows, content_source="catalog_reference",
        timing_source="forced_align", provider="replicate/cureau", model="cureau/force-align-wordstamps",
        model_revision="44dedb84066ba1e00761f45c1003c5c19ed3b12ae9d42c1c1883ca4c016ffa85", view="alignment_audio")
    second = annotate_provider_evidence(first)
    third = annotate_provider_evidence(second)
    assert first == second == third
    assert first[0]["timing_provenance"]["source"] == "forced_align"
    for row in third:
        assert row["recognition_score"] is None
        assert row["alignment_score"] is None
        assert row["provider_evidence"]["mean_score"] is None
        assert row["provider_evidence"]["frozen_provider_output"]["mean_recognition_score"] is None
        assert row["words"][0]["probability"] == raw[0]["probability"]
        assert "score" not in row["words"][0]
        assert row["words"][0]["probability_provenance"]["score_equivalence"] is False


def test_new_explicit_timing_source_supersedes_old_identity():
    rows = annotate_provider_evidence(broken_rows(), timing_source="forced_align")
    updated = annotate_provider_evidence(rows, timing_source="ctc_alignment")
    assert updated[0]["timing_provenance"]["source"] == "ctc_alignment"
    assert updated[0]["timing_validation"]["status"] == "unvalidated"


def test_quality_diagnostic_and_editor_serialization_preserve_payload():
    from editor import normalize_segments
    from transcription_quality import evaluate
    rows = annotate_provider_evidence(broken_rows(), timing_source="forced_align")
    original = deepcopy(rows)
    serialized = normalize_segments(rows)
    assert serialized == rows == original
    quality = evaluate(rows, {})
    assert any(r["code"] == "word_timing_not_validated" for r in quality["reasons"])
    assert rows == original
