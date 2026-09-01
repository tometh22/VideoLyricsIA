from copy import deepcopy
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
import uuid

from database import EditorVersion, Job
from editor import (
    approve_document,
    attach_machine_evidence,
    ensure_document,
    save_document,
)
from machine_evidence import (
    approval_training_provenance,
    build_machine_evidence,
    finalize_machine_evidence,
    snapshot_hash,
)
from training_corpus import build_line_delta_audit, materialize_training_pair


def _ref(value):
    return f"h:{value}"


def test_line_delta_is_complete_and_filters_sub_50ms_jitter():
    before = [
        {"_id": f"line-{index}", "start": index, "end": index + 0.5, "text": f"old {index}"}
        for index in range(25)
    ]
    after = [dict(row) for row in before]
    for index, row in enumerate(after):
        row["end"] += 0.2
        row["text"] = f"new {index}"
    # Text changed, but this tiny start drift must not become a timing label.
    after[0]["start"] += 0.0037
    after.pop(3)
    after.append({"_id": "inserted", "start": 30, "end": 31, "text": "new line"})

    payload = build_line_delta_audit(
        before, after, job_id="job", from_revision=1, to_revision=2,
        checkpoint="draft", text_ref=_ref,
    )

    assert payload["truncated"] is False
    assert len(payload["changes"]) == 26
    assert payload["summary"]["deletions"] == 1
    assert payload["summary"]["insertions"] == 1
    assert payload["summary"]["reorders"] == 0
    first = next(row for row in payload["changes"] if row["line_id"] == "line-0")
    assert first["before"]["start"] == 0.0
    assert first["after"]["start"] == 0.0037
    assert first["fields"]["start"] is False
    assert first["start_delta_ms"] == 0.0
    assert first["fields"]["end"] is True


def test_first_id_assignment_aligns_edit_around_new_insertion():
    before = [
        {"start": 0.0, "end": 1.0, "text": "ola"},
        {"start": 2.0, "end": 3.0, "text": "world"},
    ]
    after = [
        {"_id": "intro", "start": 0.0, "end": 0.3, "text": "intro"},
        {"_id": "greeting", "start": 0.3, "end": 1.0, "text": "hola"},
        {"_id": "world", "start": 2.0, "end": 3.0, "text": "world"},
    ]

    payload = build_line_delta_audit(
        before, after, job_id="job", from_revision=0, to_revision=1,
        checkpoint="draft", text_ref=_ref,
    )

    updates = [row for row in payload["changes"] if row["operation"] == "update"]
    inserts = [row for row in payload["changes"] if row["operation"] == "insert"]
    assert [(row["line_id"], row["from_index"], row["to_index"]) for row in updates] == [
        ("greeting", 0, 1),
    ]
    assert [row["line_id"] for row in inserts] == ["intro"]
    assert payload["summary"]["reorders"] == 0


def test_metadata_only_editor_change_is_not_training_noise():
    before = [{"_id": "a", "start": 1, "end": 2, "text": "hola", "pos": {"x": .2}}]
    after = [{"_id": "a", "start": 1.01, "end": 2.01, "text": "hola", "pos": {"x": .9}}]
    assert build_line_delta_audit(
        before, after, job_id="job", from_revision=1, to_revision=2,
        checkpoint="draft", text_ref=_ref,
    ) is None


def test_training_pair_materializes_machine_gold_families_and_edits():
    original = [{"_id": "a", "start": 0, "end": 1, "text": "ola"}]
    approved_segments = [{"_id": "a", "start": 0, "end": 1.2, "text": "hola"}]
    quality = {
        "decision": "review_required", "risk": .4,
        "policy_version": "lyrics-quality-v6",
    }
    evidence = finalize_machine_evidence(
        build_machine_evidence({
            "segments": original,
            "_primary_asr_family": "whisper-large-v3",
            "_asr_words": [{"word": "ola", "start": 0, "end": 1}],
        }),
        original_segments=original,
        quality=quality,
        audio_sha256="a" * 64,
        audio_revision=0,
    )
    job = SimpleNamespace(
        job_id="job123", tenant_id="tenant", artist="Artist", song_title="Song",
    )
    document = SimpleNamespace(original_segments=original, machine_evidence=evidence)
    initial = SimpleNamespace(
        id="v0", revision=0, segments=original, reason="transcription",
        is_approved=False, provenance=None, created_at=datetime.now(timezone.utc),
    )
    approval = approval_training_provenance(
        segments=approved_segments, quality=quality, revision=1,
    )
    approved = SimpleNamespace(
        id="v1", revision=1, segments=approved_segments, reason="approve",
        is_approved=True, provenance={"training_approval": approval},
        created_at=datetime.now(timezone.utc),
    )
    delta = build_line_delta_audit(
        original, approved_segments, job_id=job.job_id,
        from_revision=0, to_revision=1, checkpoint="autosave", text_ref=_ref,
    )
    audit = SimpleNamespace(id=1, detail=delta, created_at=datetime.now(timezone.utc))

    pair = materialize_training_pair(
        job=job, document=document, versions=[initial, approved], audits=[audit],
    )

    assert pair["complete"] is True
    assert pair["pre_human"]["segments"] == original
    assert pair["approved"]["segments"] == approved_segments
    assert pair["hypotheses_by_family"][0]["family"] == "whisper-large-v3"
    assert len(pair["intermediate_line_deltas"]) == 1

    missing_origin = materialize_training_pair(
        job=job, document=document, versions=[approved], audits=[],
    )
    assert missing_origin["complete"] is False
    assert "transcription_checkpoint_missing" in missing_origin["issues"]

    wrong_origin = SimpleNamespace(
        id="wrong-v0", revision=0,
        segments=[{"_id": "a", "start": 0, "end": 1, "text": "different"}],
        reason="transcription", is_approved=False, provenance=None,
        created_at=datetime.now(timezone.utc),
    )
    mismatched_origin = materialize_training_pair(
        job=job, document=document, versions=[wrong_origin, approved], audits=[audit],
    )
    assert mismatched_origin["complete"] is False
    assert "transcription_checkpoint_snapshot_mismatch" in mismatched_origin["issues"]

    missing_delta = materialize_training_pair(
        job=job, document=document, versions=[initial, approved], audits=[],
    )
    assert missing_delta["complete"] is False
    assert "editor_delta_missing:0->1" in missing_delta["issues"]

    corrupt_delta = deepcopy(delta)
    corrupt_delta["changes"][0]["after"]["end"] = 99.0
    corrupt_audit = SimpleNamespace(
        id=9, detail=corrupt_delta, created_at=datetime.now(timezone.utc),
    )
    invalid_chain = materialize_training_pair(
        job=job, document=document, versions=[initial, approved],
        audits=[corrupt_audit],
    )
    assert invalid_chain["complete"] is False
    assert "editor_delta_content_mismatch:0->1" in invalid_chain["issues"]

    after_approval_segments = [
        {"_id": "a", "start": 0, "end": 1.4, "text": "hola otra vez"},
    ]
    after_approval = SimpleNamespace(
        id="v2", revision=2, segments=after_approval_segments, reason="autosave",
        is_approved=False, provenance=None, created_at=datetime.now(timezone.utc),
    )
    post_delta = build_line_delta_audit(
        approved_segments, after_approval_segments, job_id=job.job_id,
        from_revision=1, to_revision=2, checkpoint="autosave", text_ref=_ref,
    )
    post_audit = SimpleNamespace(
        id=2, detail=post_delta, created_at=datetime.now(timezone.utc),
    )
    bounded = materialize_training_pair(
        job=job, document=document, versions=[initial, approved, after_approval],
        audits=[audit, post_audit],
    )
    assert bounded["complete"] is True
    assert [row["revision"] for row in bounded["intermediate_checkpoints"]] == [0, 1]
    assert [row["to_revision"] for row in bounded["intermediate_line_deltas"]] == [1]

    tampered = deepcopy(approval)
    tampered["song_quality_signal"]["traffic_light"] = "purple"
    tampered["evidence_sha256"] = snapshot_hash({
        key: value for key, value in tampered.items() if key != "evidence_sha256"
    })
    approved.provenance = {"training_approval": tampered}
    invalid_signal = materialize_training_pair(
        job=job, document=document, versions=[initial, approved], audits=[audit],
    )
    assert invalid_signal["complete"] is False
    assert "machine_quality_signal_invalid" in invalid_signal["issues"]

    corrupt_hash = deepcopy(approval)
    corrupt_hash["evidence_sha256"] = "0" * 64
    approved.provenance = {"training_approval": corrupt_hash}
    invalid_hash = materialize_training_pair(
        job=job, document=document, versions=[initial, approved], audits=[audit],
    )
    assert invalid_hash["complete"] is False
    assert "approval_evidence_hash_mismatch" in invalid_hash["issues"]


def test_five_new_jobs_are_exportable_end_to_end(db):
    from scripts.export_training_pairs import export_pairs

    jobs = []
    for index in range(5):
        job = Job(
            job_id=uuid.uuid4().hex[:12], user_id=1, tenant_id="training-e2e",
            artist=f"Artist {index}", song_title=f"Song {index}",
            filename=f"song-{index}.wav", style="oscuro",
            status="transcribed_pending", current_step="editing",
            delivery_profile="youtube", machine_snapshot_required=True,
            segments_json=[{
                "_id": "line-a", "start": 0.0, "end": 1.0,
                "text": "machine lyric",
            }],
            transcription_quality={
                "decision": "review_required", "risk": .31,
                "policy_version": "lyrics-quality-v6",
            },
        )
        db.add(job)
        db.flush()
        document = ensure_document(
            db, job.job_id, job.tenant_id, job.segments_json,
            initial_reason="transcription",
        )
        evidence = finalize_machine_evidence(
            build_machine_evidence({
                "segments": job.segments_json,
                "_primary_asr_family": "whisper-large-v3",
                "_asr_words": [{
                    "word": "machine lyric", "start": 0.0, "end": 1.0,
                }],
                "_independent_asr_family": "gemini-audio",
                "_independent_asr_words": [{
                    "word": "machine lyric", "start": 0.0, "end": 1.0,
                }],
            }),
            original_segments=document.original_segments,
            quality=job.transcription_quality,
            audio_sha256=(f"{index:x}" * 64)[:64],
            audio_revision=0,
        )
        attach_machine_evidence(db, document, evidence)
        document, _version, applied = save_document(
            db, job, document, 1, document.revision,
            [{
                "_id": "line-a", "start": 0.0, "end": 1.2,
                "text": "approved lyric",
            }],
            "draft",
        )
        assert applied is True
        approve_document(db, job, 1, editor_revision=document.revision)
        jobs.append(job)
    db.flush()

    exported = export_pairs(db, jobs)

    assert len(exported) == 5
    assert all(row["complete"] is True for row in exported)
    assert all(len(row["hypotheses_by_family"]) == 3 for row in exported)
    assert all(len(row["intermediate_line_deltas"]) == 1 for row in exported)
    assert all(row["approved"]["training_approval"] for row in exported)


def test_latest_export_filters_approval_before_limit(db):
    from scripts.export_training_pairs import _latest_eligible_jobs

    base = datetime(2026, 1, 1, tzinfo=timezone.utc)
    expected = []
    for index in range(60):
        job_id = uuid.uuid4().hex[:12]
        job = Job(
            job_id=job_id, user_id=1, tenant_id="latest-export-tests",
            artist="Artist", song_title=f"Song {index}", filename="song.wav",
            style="oscuro", status="transcribed_pending", current_step="editing",
            delivery_profile="youtube", segments_json=[],
            machine_snapshot_required=True,
            created_at=base + timedelta(minutes=index),
        )
        db.add(job)
        db.flush()
        if index < 5:
            db.add(EditorVersion(
                id=str(uuid.uuid4()), job_id=job_id, tenant_id=job.tenant_id,
                revision=0, segments=[], reason="approve", is_approved=True,
                created_by=1,
            ))
            expected.append(job_id)
    db.flush()

    selected = _latest_eligible_jobs(db, 5)

    assert [row.job_id for row in selected] == list(reversed(expected))


def test_require_complete_fails_when_requested_sample_count_is_short():
    from scripts.export_training_pairs import _required_export_failed

    assert _required_export_failed({"rows": 0, "incomplete_rows": 0}, 5) is True
    assert _required_export_failed({"rows": 4, "incomplete_rows": 0}, 5) is True
    assert _required_export_failed({"rows": 5, "incomplete_rows": 1}, 5) is True
    assert _required_export_failed({"rows": 5, "incomplete_rows": 0}, 5) is False


def test_private_export_writer_tightens_existing_file_before_payload(tmp_path):
    from scripts.export_training_pairs import _write_private

    target = tmp_path / "pairs.jsonl"
    target.write_text("old", encoding="utf-8")
    target.chmod(0o644)

    _write_private(target, b"private lyrics\n")

    assert target.read_bytes() == b"private lyrics\n"
    assert target.stat().st_mode & 0o777 == 0o600
