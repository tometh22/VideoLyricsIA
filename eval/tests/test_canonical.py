from eval.canonical import RAW_QUALITY_MAP, derive_edits, infer_kind, segments_to_lines
import json

import pytest

from eval.extract import _choose_portal_sample, _observed_edits, _rewind, finalize_extraction
from eval.verify_portal import verify_portal


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


def test_audit_history_omits_unchanged_fields_and_wins_deduplication():
    before = [{"start": 1, "end": 2, "text": "ola"}]
    after = [{"start": 1, "end": 2.5, "text": "hola"}]
    versions = [
        {"segments": before, "revision": 1, "created_at": "a", "created_by": "u"},
        {"segments": after, "revision": 2, "created_at": "b", "created_by": "u"},
    ]
    audits = [{"created_at": "b", "user_id": "u", "detail": {"changed": [{
        "id": "idx_0", "prev_start": 1, "new_start": 1,
        "prev_end": 2, "new_end": 2.5,
        "prev_text": "ola", "new_text": "hola",
    }]}}]
    edits = _observed_edits(versions, audits)
    assert len(edits) == 2
    assert {edit["op"] for edit in edits} == {"text_edit", "end_edit"}
    assert all(edit["source"] == "legacy_audit_diff" for edit in edits)


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


def test_portal_sample_is_adversarial_and_deterministic():
    cases = [
        {"song_id": f"estimated-{i}", "raw_quality": "estimated", "legacy_truncated": i == 0}
        for i in range(4)
    ] + [
        {"song_id": "truncated-extra", "raw_quality": "reconstructed", "legacy_truncated": True},
        {"song_id": "exact-1", "raw_quality": "exact", "legacy_truncated": False},
        {"song_id": "exact-2", "raw_quality": "exact", "legacy_truncated": False},
    ]
    first = _choose_portal_sample(cases)
    second = _choose_portal_sample(cases)
    assert first == second
    assert sum(case["raw_quality"] == "estimated" for case in first) >= 2
    assert any(case["legacy_truncated"] for case in first)


def test_portal_verifier_checks_live_identity_and_snapshot(tmp_path):
    golden = tmp_path / "golden.partial"
    sample = []
    songs = []
    for index in range(5):
        song_id = f"song-{index}"
        sample.append({"song_id": song_id, "raw_quality": "estimated", "legacy_truncated": index == 0})
        case = golden / song_id
        case.mkdir(parents=True)
        approved = [{"start": 1, "end": 2, "text": f"line {index}"}]
        from eval.canonical import canonical_sha256
        (case / "approved.json").write_text(json.dumps(approved))
        (case / "meta.json").write_text(json.dumps({
            "artist": f"Artist {index}", "title": f"Title {index}",
            "approved_at": "2026-08-29T10:00:00+00:00", "approved_by": "UMG",
            "approved_sha256": canonical_sha256(approved),
        }))
        (case / "versions.json").write_text("[]")
        songs.append({
            "artist": f"Artist {index}", "song": f"Title {index}",
            "versions": [{
                "job_id": song_id, "delivery_id": index, "label": "Renderizado",
                "approved_at": "2026-08-29T10:00:00+00:00", "approved_by_label": "UMG",
                "files": [{"type": "video", "available": True}],
            }],
        })
    golden.mkdir(exist_ok=True)
    (golden / "extraction_report.json").write_text(json.dumps({"portal_verification_sample": sample}))
    payload = tmp_path / "portal.json"
    payload.write_text(json.dumps({"songs": songs}))
    output = tmp_path / "verification.json"
    result = verify_portal(golden, payload, output)
    assert len(result["cases"]) == 5
    assert all(case["verified"] for case in result["cases"])
