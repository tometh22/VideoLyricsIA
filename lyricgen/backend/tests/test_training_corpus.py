from datetime import datetime, timezone
from types import SimpleNamespace
import uuid

from database import Job
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
    first = next(row for row in payload["changes"] if row["line_id"] == "line-0")
    assert first["before"]["start"] == 0.0
    assert first["after"]["start"] == 0.0037
    assert first["fields"]["start"] is False
    assert first["start_delta_ms"] == 0.0
    assert first["fields"]["end"] is True


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
