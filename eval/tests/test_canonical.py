from eval.canonical import RAW_QUALITY_MAP, derive_edits, infer_kind, segments_to_lines
import json

import pytest

from eval.extract import _rewind, finalize_extraction


def test_raw_quality_contract():
    assert set(RAW_QUALITY_MAP.values()) == {"exact", "reconstructed", "estimated", "none"}


def test_kind_is_conservative_and_explicitly_derived():
    assert infer_kind("(oh oh)") == ("adlib", True)
    assert infer_kind("texto hablado") == ("main", True)


def test_derived_edit_sequence():
    raw = [{"start": 1, "end": 2, "text": "ola"}]
    approved = [{"start": 1.2, "end": 2.3, "text": "hola"}]
    edits = derive_edits(raw, approved)
    assert {edit["op"] for edit in edits} == {"text_edit", "start_edit", "end_edit"}
    assert all(edit["derived"] for edit in edits)


def test_legacy_rewind_measures_raw_text_retention():
    final = [{"start": 2, "end": 3, "text": "hola"}]
    audits = [{"detail": {"changed": [{
        "id": "idx_0", "prev_start": 1, "prev_end": 2,
        "prev_text": "ola", "new_text": "hola", "text_changed": True,
    }]}}]
    raw, stats = _rewind(final, audits)
    assert raw == [{"start": 1.0, "end": 2.0, "text": "ola"}]
    assert stats["raw_text_changes"] == 1
    assert stats["total_text_changes"] == 1


def test_finalization_requires_all_five_portal_checks(tmp_path):
    output = tmp_path / "golden"
    partial = tmp_path / "golden.partial"
    partial.mkdir()
    report = {
        "songs": 65,
        "raw_quality_counts": {"exact": 23, "reconstructed": 18, "estimated": 16, "none": 8},
        "job_origin_counts": {"staging": 62, "production": 3},
        "portal_verification_sample": [{"song_id": f"song-{i}", "verified": False} for i in range(5)],
    }
    (partial / "extraction_report.json").write_text(json.dumps(report))
    verification = tmp_path / "verification.json"
    verification.write_text(json.dumps({"cases": [{"song_id": "song-0", "verified": True}]}))
    with pytest.raises(RuntimeError, match="incomplete"):
        finalize_extraction(output, verification)
    verification.write_text(json.dumps({
        "cases": [{"song_id": f"song-{i}", "verified": True} for i in range(5)],
    }))
    completed = finalize_extraction(output, verification)
    assert completed["status"] == "complete"
    assert output.is_dir() and not partial.exists()
