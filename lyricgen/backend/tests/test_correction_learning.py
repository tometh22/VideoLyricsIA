"""Correction-learning taxonomy, privacy and lifecycle contracts."""
from datetime import datetime, timedelta, timezone
import json
from types import SimpleNamespace
import uuid

import pytest

from correction_learning import (
    TRUST_DAYS,
    classify_corrections,
    create_observation,
    derive_server_active_edit_ms,
    hmac_identifier,
    machine_snapshot_provenance,
    mature_observations,
    mine_patterns,
    now_utc,
    public_observation_summary,
    privacy_safe_features,
    StaleCorrectionSnapshot,
)
from database import CorrectionObservation, EditorVersion, Job, ProductEvent
from editor import approve_document, ensure_document, save_document
from evidence_attestation import lyric_snapshot_hash


def _job(db, *, tenant="quality-tenant"):
    from database import User
    user = User(
        username=f"quality_{uuid.uuid4().hex[:8]}",
        email=f"quality_{uuid.uuid4().hex[:8]}@example.test",
        hashed_password="unused", role="user", tenant_id=tenant,
    )
    db.add(user)
    db.flush()
    job = Job(
        job_id=uuid.uuid4().hex[:12], user_id=user.id, tenant_id=tenant,
        artist="Fixture Artist", song_title="Fixture Song", filename="fixture.wav",
        style="oscuro", status="transcribed_pending", current_step="editing",
        input_r2_key="private/source/fixture.wav", timing_source="forced_align",
        transcription_quality={
            "pipeline_release": "release-1",
            "pipeline_config_fingerprint": "config-1",
            "policy_version": "lyrics-quality-v5", "decision": "review_required",
            "metrics": {"is_live": True, "language": "es"},
        },
    )
    db.add(job)
    db.flush()
    return user, job


def test_segmental_dp_classifies_split_vocalization_and_hashes_text():
    original = [{"start": 60.85, "end": 63.77, "text": "Real"}]
    approved = [
        {"start": 60.85, "end": 61.8, "text": "Real"},
        {"start": 61.8, "end": 63.77, "text": "uoh uoh"},
    ]
    result = classify_corrections(original, approved, secret="test-secret")
    assert result["categories"]["split"] == 1
    assert result["categories"]["missing_vocalization"] == 1
    serialized = json.dumps(result, ensure_ascii=False).casefold()
    assert "real" not in serialized
    assert "uoh" not in serialized
    assert len(result["metrics"]["lexical_hmacs"][0]) == 64


def test_pericos_full_outro_is_a_six_event_learning_fixture_without_plaintext():
    original = [
        {"start": 60.85, "end": 63.77, "text": "Real"},
        {"start": 63.77, "end": 67.04, "text": "Real"},
        {"start": 67.05, "end": 73.17, "text": "Real"},
        {"start": 73.18, "end": 75.65, "text": "Real"},
        {"start": 79.31, "end": 83.27, "text": "Real"},
    ]
    approved = [
        {"start": 60.85, "end": 63.77, "text": "Real, uoh uoh"},
        {"start": 63.77, "end": 67.04, "text": "Real, uoh uoh"},
        {"start": 67.05, "end": 73.17, "text": "Real, uoh uoh"},
        {"start": 73.18, "end": 75.65, "text": "Real, uoh uoh"},
        {"start": 75.65, "end": 75.75, "text": "¡no!"},
        {"start": 79.31, "end": 83.27, "text": "¡nooooooooo!"},
    ]
    result = classify_corrections(original, approved, secret="fixture-secret")
    assert len(approved) == 6
    assert result["categories"]["missing_event"] >= 1
    assert (
        result["categories"].get("missing_vocalization", 0)
        + result["categories"].get("vocalization_changed", 0)
    ) >= 1
    serialized = json.dumps(result, ensure_ascii=False).casefold()
    assert "uoh" not in serialized and "nooooo" not in serialized


def test_timing_repairs_include_order_range_and_overlap():
    original = [
        {"start": 45.9, "end": 47.0, "text": "a", "_id": "a"},
        {"start": 45.1, "end": 44.0, "text": "b", "_id": "b"},
    ]
    approved = [
        {"start": 45.1, "end": 45.8, "text": "b", "_id": "b"},
        {"start": 45.9, "end": 47.0, "text": "a", "_id": "a"},
    ]
    categories = classify_corrections(original, approved, secret="secret")["categories"]
    assert categories["temporal_order_repaired"] == 1
    assert categories["timing_inversion_repaired"] == 1
    assert categories["timing_overlap_repaired"] == 1
    assert categories["reordered"] == 2


def test_hmac_is_scoped_deterministic_and_not_plaintext(monkeypatch):
    monkeypatch.setenv("QUALITY_LEARNING_HMAC_KEY", "a-long-test-secret")
    first = hmac_identifier("operator", "operator@example.test")
    assert first == hmac_identifier("operator", "operator@example.test")
    assert first != hmac_identifier("session", "operator@example.test")
    assert "operator" not in first


def test_hmac_normalises_artist_variants_and_requires_generation(monkeypatch):
    monkeypatch.setenv("QUALITY_LEARNING_HMAC_KEY", "a-long-test-secret")
    monkeypatch.setenv("QUALITY_LEARNING_HMAC_KEY_ID", "generation-1")
    canonical = hmac_identifier("artist", "Los Pericos")
    assert canonical == hmac_identifier("artist", "  los   pericos ")
    assert canonical == hmac_identifier("artist", "LOS PERICOS")
    assert canonical == hmac_identifier("artist", "Lós-Péricos!")
    monkeypatch.delenv("QUALITY_LEARNING_HMAC_KEY_ID")
    with pytest.raises(RuntimeError, match="key_id"):
        hmac_identifier("artist", "Los Pericos")


def test_active_minutes_are_derived_from_server_heartbeats(db, monkeypatch):
    monkeypatch.setenv("QUALITY_LEARNING_HMAC_KEY", "test-hmac-secret")
    user, job = _job(db, tenant="heartbeat-tenant")
    document = ensure_document(
        db, job.job_id, job.tenant_id,
        [{"start": 0, "end": 1, "text": "machine"}],
        initial_reason="transcription",
        initial_provenance=machine_snapshot_provenance(job, job.transcription_quality),
    )
    document, _version, _ = save_document(
        db, job, document, user.id, document.revision,
        [{"start": 0, "end": 1, "text": "approved"}], "manual",
    )
    _document, approved = approve_document(
        db, job, user.id, editor_revision=document.revision,
    )
    snapshot = lyric_snapshot_hash(approved.segments)
    started = datetime(2026, 8, 1, tzinfo=timezone.utc)
    for index, offset in enumerate((0, 15, 30, 90)):
        at = started + timedelta(seconds=offset)
        db.add(ProductEvent(
            tenant_id=job.tenant_id, user_id=user.id, job_id=job.job_id,
            name="editor_activity_heartbeat", occurred_at=at, created_at=at,
            properties={
                "session_id": "server-session", "activity_seq": index + 1,
                "revision": approved.revision, "snapshot_sha256": snapshot,
                "pipeline_release": "release-1",
                "pipeline_config_fingerprint": "config-1",
            },
        ))
    db.flush()
    assert derive_server_active_edit_ms(
        db, job, approved, "server-session",
    ) == 30_000


def test_approval_queue_payload_to_worker_observation_is_privacy_safe(
    db, monkeypatch,
):
    import queue_jobs
    import rq
    monkeypatch.setenv("QUALITY_LEARNING_HMAC_KEY", "test-hmac-secret")
    monkeypatch.setenv("TRANSCRIPTION_QUALITY_QUEUE_ENABLED", "1")
    monkeypatch.setenv("QUALITY_LEARNING_CAPTURE_ENABLED", "1")
    user, job = _job(db, tenant="queue-integration-tenant")
    document = ensure_document(
        db, job.job_id, job.tenant_id,
        [{"start": 0, "end": 1, "text": "machine"}],
        initial_reason="transcription",
        initial_provenance=machine_snapshot_provenance(job, job.transcription_quality),
    )
    document, _version, _ = save_document(
        db, job, document, user.id, document.revision,
        [{"start": 0, "end": 1, "text": "approved"}], "manual",
    )
    _document, approved = approve_document(
        db, job, user.id, editor_revision=document.revision,
    )
    snapshot = lyric_snapshot_hash(approved.segments)
    started = datetime(2026, 8, 2, tzinfo=timezone.utc)
    for index, offset in enumerate((0, 15)):
        at = started + timedelta(seconds=offset)
        db.add(ProductEvent(
            tenant_id=job.tenant_id, user_id=user.id, job_id=job.job_id,
            name="editor_activity_heartbeat", occurred_at=at, created_at=at,
            properties={
                "session_id": "raw-session-must-not-enter-rq",
                "activity_seq": index + 1, "revision": approved.revision,
                "snapshot_sha256": snapshot, "pipeline_release": "release-1",
                "pipeline_config_fingerprint": "config-1",
            },
        ))
    db.commit()

    captured = {}
    class FakeQueue:
        def __init__(self, *args, **kwargs):
            pass
        def enqueue(self, function, *, args, kwargs, **options):
            captured.update({
                "function": function, "args": args, "kwargs": kwargs,
                "options": options,
            })
            return SimpleNamespace(id=options["job_id"])

    monkeypatch.setattr(queue_jobs, "_init_redis", lambda: None)
    monkeypatch.setattr(queue_jobs, "_redis", object())
    monkeypatch.setattr(queue_jobs, "_active_rq_job", lambda *args: None)
    monkeypatch.setattr(queue_jobs, "_evict_stale_rq_job", lambda *args: None)
    monkeypatch.setattr(rq, "Queue", FakeQueue)
    queue_jobs.enqueue_correction_learning(
        job.job_id, approved.id, active_edit_ms=14_400_000,
        session_id="raw-session-must-not-enter-rq",
    )
    serialized_payload = json.dumps(captured["kwargs"])
    assert "raw-session-must-not-enter-rq" not in serialized_payload
    assert captured["kwargs"]["active_edit_ms"] == 15_000
    assert captured["kwargs"]["active_edit_source"] == "server_product_events_v1"
    assert captured["kwargs"]["expected_learning_epoch"] == 0

    result = captured["function"](*captured["args"], **captured["kwargs"])
    assert result["mutated_segments"] is False
    db.expire_all()
    observation = db.query(CorrectionObservation).filter(
        CorrectionObservation.job_id == job.job_id,
    ).one()
    assert observation.active_edit_ms == 15_000
    assert observation.metrics["operator_time_source"] == "server_product_events_v1"
    # This integration test commits so the worker's independent Session can
    # observe the approval. Remove its fixture rows explicitly to preserve the
    # suite's empty-DB bootstrap contract for later HTTP tests.
    from database import EditorDocument
    db.query(CorrectionObservation).filter(
        CorrectionObservation.job_id == job.job_id,
    ).delete(synchronize_session=False)
    db.query(ProductEvent).filter(ProductEvent.job_id == job.job_id).delete(
        synchronize_session=False,
    )
    db.query(EditorVersion).filter(EditorVersion.job_id == job.job_id).delete(
        synchronize_session=False,
    )
    db.query(EditorDocument).filter(EditorDocument.job_id == job.job_id).delete(
        synchronize_session=False,
    )
    db.query(Job).filter(Job.job_id == job.job_id).delete(synchronize_session=False)
    from database import User
    db.query(User).filter(User.id == user.id).delete(synchronize_session=False)
    db.commit()


def test_machine_provenance_is_hash_only():
    class Row:
        input_r2_key = "tenant/raw/audio.wav"
        timing_source = "forced_align"
    provenance = machine_snapshot_provenance(Row(), {
        "pipeline_release": "release", "pipeline_config_fingerprint": "cfg",
        "policy_version": "v5", "decision": "review_required",
        "reasons": [{"code": "event_count"}],
    })
    assert provenance["schema"] == "machine-transcription-lineage-v1"
    assert len(provenance["audio_sha256"]) == 64
    assert "tenant/raw/audio.wav" not in json.dumps(provenance)
    assert len(provenance["quality_evidence_sha256"]) == 64


def test_acoustic_context_reads_structural_events_without_text():
    class Row:
        timing_source = "acoustic_dp_ctc_v1"
        transcription_quality = {
            "decision": "review_required",
            "metrics": {"language": "es"},
            "analysis_windows": [{
                "structure": {"best_partition": {"events": [{
                    "type_posterior": {
                        "sustained_vocalization": 0.7,
                        "crowd_or_overlap": 0.6,
                    },
                }]}},
            }],
        }
    features = privacy_safe_features(Row())
    assert features["sustained_vocal"] is True
    assert features["crowd_or_chorus"] is True
    assert "text" not in json.dumps(features)


def test_observation_uses_exact_machine_snapshot_and_invalidates_after_edit(db, monkeypatch):
    monkeypatch.setenv("QUALITY_LEARNING_HMAC_KEY", "test-hmac-secret")
    user, job = _job(db)
    original = [{"start": 0, "end": 1, "text": "Real"}]
    quality = dict(job.transcription_quality)
    document = ensure_document(
        db, job.job_id, job.tenant_id, original,
        initial_reason="transcription",
        initial_provenance=machine_snapshot_provenance(job, quality),
    )
    document, _version, _ = save_document(
        db, job, document, user.id, document.revision,
        [
            {"start": 0, "end": 1, "text": "Real"},
            {"start": 1, "end": 2, "text": "uoh uoh"},
        ],
        "manual",
    )
    _document, approved = approve_document(
        db, job, user.id, editor_revision=document.revision,
    )
    observation = create_observation(
        db, job.job_id, approved.id, active_edit_ms=120_000,
        active_edit_source="server_product_events_v1",
        session_hmac="f" * 64,
    )
    db.flush()
    assert observation.source_confidence == "exact"
    assert observation.label_tier == "observed"
    assert observation.categories["missing_event"] == 1
    assert observation.categories["missing_vocalization"] == 1
    assert observation.session_hmac == "f" * 64
    assert observation.operator_hmac != str(user.id)
    serialized = json.dumps({
        "categories": observation.categories,
        "features": observation.features,
        "metrics": observation.metrics,
    }, ensure_ascii=False).casefold()
    assert "real" not in serialized and "uoh" not in serialized

    observation.matures_at = now_utc() - timedelta(seconds=1)
    db.flush()
    assert mature_observations(db)["matured"] == 1
    assert observation.label_tier == "trusted"

    from correction_learning import invalidate_job_observations
    assert invalidate_job_observations(db, job.job_id, "later_editor_revision") == 1
    assert observation.label_tier == "invalidated"


def test_client_reported_edit_minutes_never_enter_learning_gates(db, monkeypatch):
    monkeypatch.setenv("QUALITY_LEARNING_HMAC_KEY", "test-hmac-secret")
    user, job = _job(db, tenant="untrusted-time-tenant")
    document = ensure_document(
        db, job.job_id, job.tenant_id,
        [{"start": 0, "end": 1, "text": "machine"}],
        initial_reason="transcription",
        initial_provenance=machine_snapshot_provenance(job, job.transcription_quality),
    )
    document, _version, _ = save_document(
        db, job, document, user.id, document.revision,
        [{"start": 0, "end": 1, "text": "approved"}], "manual",
    )
    _document, approved = approve_document(
        db, job, user.id, editor_revision=document.revision,
    )
    observation = create_observation(
        db, job.job_id, approved.id, active_edit_ms=14_400_000,
    )
    assert observation.active_edit_ms is None
    assert observation.metrics["operator_time_source"] == "untrusted_or_missing"


def test_legacy_snapshot_never_becomes_trusted(db, monkeypatch):
    monkeypatch.setenv("QUALITY_LEARNING_HMAC_KEY", "test-hmac-secret")
    user, job = _job(db, tenant="legacy-tenant")
    document = ensure_document(
        db, job.job_id, job.tenant_id,
        [{"start": 0, "end": 1, "text": "machine"}],
    )
    document, _version, _ = save_document(
        db, job, document, user.id, document.revision,
        [{"start": 0, "end": 1, "text": "approved"}], "manual",
    )
    _document, approved = approve_document(
        db, job, user.id, editor_revision=document.revision,
    )
    original_version = db.query(EditorVersion).filter(
        EditorVersion.job_id == job.job_id,
        EditorVersion.revision == 0,
    ).one()
    assert original_version.provenance is None
    observation = create_observation(db, job.job_id, approved.id)
    assert observation.source_confidence == "legacy_unverified"
    observation.matures_at = now_utc() - timedelta(days=TRUST_DAYS + 1)
    db.flush()
    mature_observations(db)
    assert observation.label_tier == "observed"


def test_backfill_dry_run_selects_only_current_unobserved_approval(db, monkeypatch):
    monkeypatch.setenv("QUALITY_LEARNING_HMAC_KEY", "test-hmac-secret")
    from scripts.quality_learning_backfill import collect_candidates
    user, job = _job(db, tenant="backfill-tenant")
    document = ensure_document(
        db, job.job_id, job.tenant_id,
        [{"start": 0, "end": 1, "text": "machine"}],
    )
    document, _version, _ = save_document(
        db, job, document, user.id, document.revision,
        [{"start": 0, "end": 1, "text": "approved"}], "manual",
    )
    _document, approved = approve_document(
        db, job, user.id, editor_revision=document.revision,
    )
    assert (job.job_id, approved.id, approved.revision) in collect_candidates(db, 500)
    create_observation(db, job.job_id, approved.id)
    assert (job.job_id, approved.id, approved.revision) not in collect_candidates(db, 500)


def test_independent_final_reviewer_promotes_exact_observation(db, monkeypatch):
    monkeypatch.setenv("QUALITY_LEARNING_HMAC_KEY", "test-hmac-secret")
    user, job = _job(db, tenant="independent-tenant")
    from database import User
    reviewer = User(
        username=f"reviewer-{uuid.uuid4().hex[:6]}", hashed_password="unused",
        role="admin", tenant_id=job.tenant_id,
    )
    db.add(reviewer)
    db.flush()
    document = ensure_document(
        db, job.job_id, job.tenant_id,
        [{"start": 0, "end": 1, "text": "machine"}],
        initial_reason="transcription",
        initial_provenance=machine_snapshot_provenance(job, job.transcription_quality),
    )
    document, _version, _ = save_document(
        db, job, document, user.id, document.revision,
        [{"start": 0, "end": 1, "text": "approved"}], "manual",
    )
    _document, approved = approve_document(
        db, job, user.id, editor_revision=document.revision,
    )
    observation = create_observation(db, job.job_id, approved.id)
    assert observation.label_tier == "observed"
    job.approved_by = reviewer.id
    db.flush()
    repeated = create_observation(db, job.job_id, approved.id)
    assert repeated.label_tier == "trusted"
    assert db.query(CorrectionObservation).filter(
        CorrectionObservation.job_id == job.job_id,
    ).count() == 1


def test_late_approval_analysis_is_discarded_after_new_editor_revision(db, monkeypatch):
    monkeypatch.setenv("QUALITY_LEARNING_HMAC_KEY", "test-hmac-secret")
    user, job = _job(db, tenant="occ-tenant")
    document = ensure_document(
        db, job.job_id, job.tenant_id,
        [{"start": 0, "end": 1, "text": "machine"}],
        initial_reason="transcription",
        initial_provenance=machine_snapshot_provenance(job, job.transcription_quality),
    )
    document, _version, _ = save_document(
        db, job, document, user.id, document.revision,
        [{"start": 0, "end": 1, "text": "approved"}], "manual",
    )
    _document, approved = approve_document(
        db, job, user.id, editor_revision=document.revision,
    )
    approved_revision = approved.revision
    approved_hash = lyric_snapshot_hash(approved.segments)
    document, _later, _ = save_document(
        db, job, document, user.id, document.revision,
        [{"start": 0, "end": 1, "text": "later edit"}], "manual",
    )
    with pytest.raises(StaleCorrectionSnapshot, match="revision"):
        create_observation(
            db, job.job_id, approved.id,
            expected_revision=approved_revision,
            expected_approved_hash=approved_hash,
        )
    assert db.query(CorrectionObservation).filter(
        CorrectionObservation.job_id == job.job_id,
    ).count() == 0


def test_change_request_epoch_discards_observation_not_created_yet(db, monkeypatch):
    monkeypatch.setenv("QUALITY_LEARNING_HMAC_KEY", "test-hmac-secret")
    user, job = _job(db, tenant="epoch-tenant")
    document = ensure_document(
        db, job.job_id, job.tenant_id,
        [{"start": 0, "end": 1, "text": "machine"}],
        initial_reason="transcription",
        initial_provenance=machine_snapshot_provenance(job, job.transcription_quality),
    )
    document, _version, _ = save_document(
        db, job, document, user.id, document.revision,
        [{"start": 0, "end": 1, "text": "approved"}], "manual",
    )
    _document, approved = approve_document(
        db, job, user.id, editor_revision=document.revision,
    )
    queued_epoch = int(job.quality_learning_epoch or 0)
    from correction_learning import invalidate_job_observations
    assert invalidate_job_observations(
        db, job.job_id, "client_lyrics_or_timing_change_request",
    ) == 0
    with pytest.raises(StaleCorrectionSnapshot, match="epoch"):
        create_observation(
            db, job.job_id, approved.id,
            expected_revision=approved.revision,
            expected_approved_hash=lyric_snapshot_hash(approved.segments),
            expected_learning_epoch=queued_epoch,
        )
    assert db.query(CorrectionObservation).filter(
        CorrectionObservation.job_id == job.job_id,
    ).count() == 0


def test_public_summary_never_returns_internal_identifiers():
    row = CorrectionObservation(
        id="private-id", identity_hash="a" * 64, job_id="private-job",
        tenant_id="private-tenant", original_revision=0, approved_revision=1,
        approved_version_id="private-version", original_hash="b" * 64,
        approved_hash="c" * 64, pipeline_release="r",
        pipeline_config_fingerprint="c", timing_source="t",
        pipeline_route="route-a",
        label_tier="trusted", source_confidence="exact",
        categories={"split": 1}, features={},
        metrics={"operator_time_source": "server_product_events_v1"},
        active_edit_ms=300_000,
    )
    payload = public_observation_summary([row])
    serialized = json.dumps(payload)
    for forbidden in ("private-id", "private-job", "private-tenant", "private-version"):
        assert forbidden not in serialized
    assert payload["operator_minutes"]["p50"] == 5.0
    assert payload["by_release"]["r"]["corrections"] == 1
    assert payload["by_route"]["route-a"]["observations"] == 1
    assert payload["by_timing_source"]["t"]["operator_minutes_p90"] == 5.0
    assert payload["by_category"]["split"]["observations"] == 1


def test_pattern_mining_requires_multi_tenant_support_and_stays_associational(db):
    from database import QualityFixProposal, QualityPattern, User
    now = now_utc()
    users = {}
    for tenant in ("mine-a", "mine-b", "mine-c", "control-a", "control-b", "control-c"):
        user = User(
            username=f"{tenant}-{uuid.uuid4().hex[:6]}", hashed_password="unused",
            role="user", tenant_id=tenant,
        )
        db.add(user)
        db.flush()
        users[tenant] = user
    for index in range(20):
        exposed = index < 10
        tenant_group = ("mine-a", "mine-b", "mine-c") if exposed else (
            "control-a", "control-b", "control-c"
        )
        tenant = tenant_group[index % 3]
        job_id = uuid.uuid4().hex[:12]
        version_id = str(uuid.uuid4())
        db.add(Job(
            job_id=job_id, user_id=users[tenant].id, tenant_id=tenant,
            artist="same-artist", song_title=f"song-{index}", filename="fixture.wav",
            style="oscuro", status="done", current_step="done",
        ))
        db.flush()
        db.add(EditorVersion(
            id=version_id, job_id=job_id, tenant_id=tenant, revision=1,
            segments=[], created_by=users[tenant].id, reason="approve",
            is_approved=True,
        ))
        db.flush()
        db.add(CorrectionObservation(
            id=str(uuid.uuid4()), identity_hash=uuid.uuid4().hex + uuid.uuid4().hex,
            job_id=job_id, tenant_id=tenant, original_revision=0,
            approved_revision=1, approved_version_id=version_id,
            original_hash="a" * 64, approved_hash="b" * 64,
            pipeline_release="release", pipeline_config_fingerprint="config",
            timing_source="forced_align", label_tier="trusted",
            source_confidence="exact",
            hmac_key_id="test-v1",
            artist_hmac=f"artist-{index % 3}",
            song_hmac=f"song-{index}",
            categories={"missing_event": 1} if exposed else {},
            features={"is_live": exposed, "language": "es"},
            metrics={"operator_time_source": "server_product_events_v1"},
            active_edit_ms=120_000 if exposed else 0,
            trusted_at=now, created_at=now, updated_at=now,
        ))
    db.flush()
    result = mine_patterns(db)
    assert result["qualified"] >= 1
    pattern = db.query(QualityPattern).filter(
        QualityPattern.category == "missing_event",
        QualityPattern.context_key == "is_live=true",
    ).one()
    assert pattern.support_jobs == 10 and pattern.support_tenants == 3
    assert pattern.support_artists == 3
    assert pattern.status == "correlated"
    assert pattern.evidence["association_only"] is True
    proposal = db.query(QualityFixProposal).filter(
        QualityFixProposal.pattern_id == pattern.id,
    ).one()
    assert len(proposal.candidate_config) == 1
    assert "same-artist" not in json.dumps(pattern.evidence)

    for row in db.query(CorrectionObservation).all():
        if (row.categories or {}).get("missing_event"):
            row.artist_hmac = "one-artist"
    db.flush()
    mine_patterns(db)
    db.refresh(pattern)
    db.refresh(proposal)
    assert pattern.status == "stale"
    assert proposal.status == "superseded"
