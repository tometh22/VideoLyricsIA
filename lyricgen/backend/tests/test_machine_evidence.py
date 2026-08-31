"""The editor must never lose the machine state it is correcting."""
import uuid

import pytest

from database import EditorDocument, Job
from editor import (
    approve_document,
    attach_machine_evidence,
    ensure_document,
    require_machine_snapshot,
)
from machine_evidence import (
    MachineSnapshotMissing,
    SCHEMA,
    build_machine_evidence,
    finalize_machine_evidence,
)


SEGMENTS = [{"start": 0.1, "end": 1.2, "text": "quiero despegar"}]


def _job(db, *, required=False):
    job_id = uuid.uuid4().hex[:12]
    row = Job(
        job_id=job_id, user_id=1, tenant_id="machine-evidence-tests",
        artist="Artist", song_title="Song", filename="song.wav",
        style="oscuro", status="transcribed_pending", current_step="editing",
        delivery_profile="youtube", segments_json=SEGMENTS,
        machine_snapshot_required=required,
    )
    db.add(row)
    db.flush()
    document = ensure_document(
        db, job_id, row.tenant_id, SEGMENTS, initial_reason="transcription",
    )
    return row, document


def _evidence(document):
    captured = build_machine_evidence({
        "segments": SEGMENTS,
        "_primary_asr_family": "whisper-large-v3",
        "_asr_words": [
            {"start": 0.1, "end": 0.5, "word": "quiero", "score": 0.9},
            {"start": 0.6, "end": 1.2, "word": "despegar", "score": 0.8},
        ],
        "_independent_asr_family": "gemini-audio",
        "_independent_asr_words": [
            {"start": 0.1, "end": 1.2, "word": "quiero despegar"},
        ],
    })
    return finalize_machine_evidence(
        captured,
        original_segments=document.original_segments,
        quality={"decision": "review", "policy_version": "v-test"},
        audio_sha256="a" * 64,
        audio_revision=1,
    )


def test_capture_keeps_independent_family_and_machine_decision(db):
    _row, document = _job(db)
    evidence = _evidence(document)
    assert evidence["schema"] == SCHEMA
    assert {item["role"] for item in evidence["hypotheses_by_family"]} == {
        "primary", "independent", "selected",
    }
    assert evidence["decisions"]["route"] == "review"
    assert evidence["pre_human"]["segment_count"] == 1


def test_required_job_cannot_be_approved_without_machine_snapshot(db):
    row, document = _job(db, required=True)
    with pytest.raises(MachineSnapshotMissing, match="machine_snapshot_missing"):
        require_machine_snapshot(row, document)
    with pytest.raises(MachineSnapshotMissing):
        approve_document(db, row, 1, editor_revision=document.revision)


def test_machine_snapshot_is_hash_bound_and_immutable(db):
    row, document = _job(db, required=True)
    evidence = _evidence(document)
    assert attach_machine_evidence(db, document, evidence) is True
    require_machine_snapshot(row, document)
    assert attach_machine_evidence(db, document, evidence) is False

    changed = dict(evidence)
    changed["evidence_sha256"] = "b" * 64
    with pytest.raises(RuntimeError, match="already_frozen"):
        attach_machine_evidence(db, document, changed)


def test_snapshot_hash_mismatch_blocks_approval(db):
    row, document = _job(db, required=True)
    evidence = _evidence(document)
    evidence["pre_human"]["segments_sha256"] = "0" * 64
    document.machine_evidence = evidence
    db.flush()
    with pytest.raises(MachineSnapshotMissing, match="hash_mismatch"):
        require_machine_snapshot(row, document)

