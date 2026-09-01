"""The editor must never lose the machine state it is correcting."""
from copy import deepcopy
import uuid

import pytest

from database import AuditLog, EditorVersion, Job
from editor import (
    approve_document,
    attach_machine_evidence,
    ensure_document,
    require_machine_snapshot,
    save_document,
)
from machine_evidence import (
    MachineSnapshotMissing,
    SCHEMA,
    build_machine_evidence,
    finalize_machine_evidence,
    quality_training_signal,
    snapshot_hash,
    validate_machine_evidence,
    validate_quality_training_signal,
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
    assert evidence["decisions"]["song_quality_signal"] == {
        "schema": "song-quality-signal-v1",
        "traffic_light": "yellow",
        "verdict": "review",
        "score": None,
        "score_source": "unavailable",
        "raw_score": None,
        "risk": None,
        "policy_version": "v-test",
    }
    assert evidence["pre_human"]["segment_count"] == 1


def test_empty_selected_state_is_explicit_even_with_raw_words():
    captured = build_machine_evidence({
        "segments": [],
        "_primary_asr_family": "whisper-large-v3",
        "_asr_words": [{"start": 0.1, "end": 0.4, "word": "raw"}],
    })
    selected = [
        item for item in captured["hypotheses_by_family"]
        if item["role"] == "selected"
    ]
    assert len(selected) == 1
    assert selected[0]["events"] == []
    assert selected[0]["events_sha256"] == snapshot_hash([])


def test_snapshot_hash_canonicalizes_negative_zero_for_jsonb_roundtrip():
    before_jsonb = {
        "quality": {
            "delivery_repair_shadow": {
                "t4_word_line_boundaries": {
                    "rows": [{"wrapper_padding_s": -0.0}],
                },
            },
        },
    }
    after_jsonb = deepcopy(before_jsonb)
    after_jsonb["quality"]["delivery_repair_shadow"][
        "t4_word_line_boundaries"
    ]["rows"][0]["wrapper_padding_s"] = 0.0

    assert snapshot_hash(before_jsonb) == snapshot_hash(after_jsonb)


def test_finalize_binds_selected_to_canonical_durable_segments():
    raw = [{"start": 0.123456, "end": 1.234567, "text": "line"}]
    canonical = [{"start": 0.1235, "end": 1.2346, "text": "line"}]
    captured = build_machine_evidence({"segments": raw})
    evidence = finalize_machine_evidence(
        captured,
        original_segments=canonical,
        quality={"decision": "review"},
        audio_sha256="a" * 64,
        audio_revision=0,
    )
    selected = next(
        item for item in evidence["hypotheses_by_family"]
        if item["role"] == "selected"
    )
    assert selected["events"] == canonical
    assert selected["events_sha256"] == snapshot_hash(canonical)
    validate_machine_evidence(evidence, canonical)


def test_song_signal_rejects_inconsistent_score_risk_and_light():
    valid = quality_training_signal({
        "decision": "review_required", "risk": 0.31,
        "policy_version": "lyrics-quality-v6",
    })
    validate_quality_training_signal(valid)

    for mutation in (
        {"score": None},
        {"risk": "not-a-number"},
        {"traffic_light": "green", "risk": 0.9},
    ):
        invalid = {**valid, **mutation}
        with pytest.raises(MachineSnapshotMissing, match="inconsistent"):
            validate_quality_training_signal(invalid)


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

    changed = deepcopy(evidence)
    changed["hypotheses_by_family"][0]["family"] = "different-family"
    changed["evidence_sha256"] = snapshot_hash({
        "hypotheses_by_family": changed["hypotheses_by_family"],
        "capture": changed["capture"],
        "pre_human": changed["pre_human"],
        "decisions": changed["decisions"],
    })
    with pytest.raises(RuntimeError, match="already_frozen"):
        attach_machine_evidence(db, document, changed)


def test_reference_selected_output_keeps_named_raw_recognition_family(db):
    _row, document = _job(db)
    selected = [{
        **SEGMENTS[0],
        "content_source": "catalog_reference",
        "provider_evidence": {
            "source": "catalog_reference",
            "correlated_family": "unknown",
        },
    }]
    captured = build_machine_evidence({
        "segments": selected,
        "_recognition_hypotheses": [{
            "family": "openai/whisper-1",
            "kind": "segments",
            "view": "mix",
            "events": [{
                "start": 0.1, "end": 1.2,
                "text": "quiero despegar crudo",
            }],
        }],
    })
    evidence = finalize_machine_evidence(
        captured,
        original_segments=document.original_segments,
        quality={"decision": "review", "policy_version": "v-test"},
        audio_sha256="a" * 64,
        audio_revision=1,
    )

    hypotheses = evidence["hypotheses_by_family"]
    primary = next(item for item in hypotheses if item["role"] == "primary")
    selected_hypothesis = next(
        item for item in hypotheses if item["role"] == "selected"
    )
    assert primary["family"] == "openai/whisper-1"
    assert primary["view"] == "mix"
    assert selected_hypothesis["family"] == "catalog_reference"
    assert evidence["capture"]["recognition_attempt_count"] == 1
    validate_machine_evidence(evidence, document.original_segments)


def test_pre_anchor_family_is_derived_from_its_own_segments():
    pre_anchor = [{
        **SEGMENTS[0],
        "content_source": "whisperx",
        "provider_evidence": {
            "source": "whisperx",
            "correlated_family": "replicate/whisperx:model-revision",
        },
    }]
    selected = [{
        **SEGMENTS[0],
        "content_source": "operator_reference",
        "provider_evidence": {"source": "operator_reference"},
    }]
    evidence = build_machine_evidence({
        "segments": selected,
        "_pre_anchor_provider_segments": pre_anchor,
    })
    hypothesis = next(
        item for item in evidence["hypotheses_by_family"]
        if item["role"] == "pre_anchor"
    )
    assert hypothesis["family"] == "replicate/whisperx:model-revision"


def test_v3_rejects_unknown_family():
    captured = build_machine_evidence({
        "segments": SEGMENTS,
        "_recognition_hypotheses": [{
            "family": "unknown",
            "kind": "segments",
            "events": SEGMENTS,
        }],
    })
    evidence = finalize_machine_evidence(
        captured,
        original_segments=SEGMENTS,
        quality={"decision": "review"},
        audio_sha256="a" * 64,
        audio_revision=1,
    )
    with pytest.raises(MachineSnapshotMissing, match="family_unknown"):
        validate_machine_evidence(evidence, SEGMENTS)


def test_v3_attempt_counter_must_equal_durable_primary_hypotheses():
    captured = build_machine_evidence({
        "segments": SEGMENTS,
        "_recognition_hypotheses": [{
            "family": "openai/whisper-1",
            "kind": "segments",
            "events": SEGMENTS,
        }],
    })
    evidence = finalize_machine_evidence(
        captured,
        original_segments=SEGMENTS,
        quality={"decision": "review"},
        audio_sha256="a" * 64,
        audio_revision=1,
    )
    evidence["capture"]["recognition_attempt_count"] = 2
    evidence["evidence_sha256"] = snapshot_hash({
        "hypotheses_by_family": evidence["hypotheses_by_family"],
        "capture": evidence["capture"],
        "pre_human": evidence["pre_human"],
        "decisions": evidence["decisions"],
    })
    with pytest.raises(MachineSnapshotMissing, match="hypothesis_missing"):
        validate_machine_evidence(evidence, SEGMENTS)


def test_snapshot_hash_mismatch_blocks_approval(db):
    row, document = _job(db, required=True)
    evidence = _evidence(document)
    evidence["pre_human"]["segments_sha256"] = "0" * 64
    document.machine_evidence = evidence
    db.flush()
    with pytest.raises(MachineSnapshotMissing, match="hash_mismatch"):
        require_machine_snapshot(row, document)


def test_scalar_pre_human_evidence_fails_closed_without_500(db):
    row, document = _job(db, required=True)
    evidence = _evidence(document)
    evidence["pre_human"] = "corrupt"
    document.machine_evidence = evidence
    db.flush()

    with pytest.raises(MachineSnapshotMissing, match="machine_pre_human_missing"):
        require_machine_snapshot(row, document)


def test_nested_scalar_types_in_quality_signal_fail_closed(db):
    row, document = _job(db, required=True)
    evidence = _evidence(document)
    evidence["decisions"]["song_quality_signal"]["traffic_light"] = {
        "not": "a scalar",
    }
    evidence["evidence_sha256"] = snapshot_hash({
        "hypotheses_by_family": evidence["hypotheses_by_family"],
        "capture": evidence["capture"],
        "pre_human": evidence["pre_human"],
        "decisions": evidence["decisions"],
    })
    document.machine_evidence = evidence
    db.flush()

    with pytest.raises(MachineSnapshotMissing, match="machine_quality_signal_invalid"):
        require_machine_snapshot(row, document)


@pytest.mark.parametrize(
    ("mutate", "expected"),
    [
        (lambda evidence: evidence.update(hypotheses_by_family=[{}]),
         "machine_hypothesis_invalid"),
        (lambda evidence: evidence["hypotheses_by_family"][0].update(event_count=99),
         "machine_hypothesis_invalid"),
        (lambda evidence: next(
            item for item in evidence["hypotheses_by_family"]
            if item["role"] == "selected"
        ).update(role="primary"),
         "machine_selected_hypothesis_missing"),
    ],
)
def test_malformed_or_missing_selected_hypotheses_fail_closed(
    db, mutate, expected,
):
    row, document = _job(db, required=True)
    evidence = _evidence(document)
    mutate(evidence)
    evidence["evidence_sha256"] = snapshot_hash({
        "hypotheses_by_family": evidence["hypotheses_by_family"],
        "capture": evidence["capture"],
        "pre_human": evidence["pre_human"],
        "decisions": evidence["decisions"],
    })
    document.machine_evidence = evidence
    db.flush()

    with pytest.raises(MachineSnapshotMissing, match=expected):
        require_machine_snapshot(row, document)


def test_approval_freezes_song_signal_on_exact_version(db):
    row, document = _job(db, required=True)
    row.transcription_quality = {
        "decision": "review_required", "risk": 0.23,
        "score": None, "policy_version": "lyrics-quality-v6",
    }
    attach_machine_evidence(db, document, _evidence(document))
    _document, version = approve_document(
        db, row, 1, editor_revision=document.revision,
    )
    approval = version.provenance["training_approval"]
    assert version.reason == "transcription"
    assert approval["schema"] == "training-approval-evidence-v1"
    assert approval["song_quality_signal"]["traffic_light"] == "yellow"
    assert approval["song_quality_signal"]["score"] == 77.0
    assert approval["song_quality_signal"]["score_source"] == "risk_derived"


def test_evidence_jobs_keep_every_durable_draft_checkpoint(db):
    row, document = _job(db, required=True)
    attach_machine_evidence(db, document, _evidence(document))
    for index in range(60):
        document, _version, applied = save_document(
            db, row, document, 1, document.revision,
            [{"start": 0.1, "end": 1.2 + index / 10, "text": f"line {index}"}],
            "draft",
        )
        assert applied is True
    versions = db.query(EditorVersion).filter(
        EditorVersion.job_id == row.job_id,
    ).all()
    assert len(versions) == 61  # transcription + every applied draft
    audits = [
        item for item in db.query(AuditLog).filter(
            AuditLog.action == "lyrics.segments_diff",
        ).all()
        if (item.detail or {}).get("job_id") == row.job_id
    ]
    assert len(audits) == 60
    assert all(item.detail["schema"] == "editor-line-delta-v2" for item in audits)
    assert all(item.detail["truncated"] is False for item in audits)
