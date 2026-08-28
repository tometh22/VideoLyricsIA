"""Editor drafts, versions, locks and workspace collaboration."""

import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from threading import Barrier

import pytest

from auth import create_user, start_login_session
from database import (
    AuditLog, EditorDocument, EditorVersion, Job, JobOutboxEvent, ProductEvent, SessionLocal,
    engine,
)
from editor import (
    acquire_lock,
    approve_document,
    ensure_document,
    expire_stale_quality_proposals,
    get_or_create_document,
    persist_quality_proposal_if_current,
    persist_quality_observation_if_current,
    save_document,
)
from transcription_quality import segments_hash
from quality_v6_contracts import PROPOSAL_WINDOW_SCHEMA, REVIEW_PROPOSAL_SCHEMA
from tests.conftest import auth


def _users_and_job(tenant="editor_team"):
    db = SessionLocal()
    try:
        first = create_user(db, f"editor_a_{uuid.uuid4().hex[:6]}", "testpass12345", None, tenant_id=tenant)
        second = create_user(db, f"editor_b_{uuid.uuid4().hex[:6]}", "testpass12345", None, tenant_id=tenant)
        job_id = f"ed_{uuid.uuid4().hex[:9]}"
        db.add(Job(
            job_id=job_id, user_id=first.id, tenant_id=tenant,
            artist="Test Artist", song_title="Test Song", filename="test.wav",
            style="oscuro", status="transcribed_pending", current_step="editing",
            delivery_profile="youtube",
        ))
        db.commit()
        ensure_document(db, job_id, tenant, [
            {"start": 0, "end": 1, "text": "one"},
            {"start": 1, "end": 2, "text": "two"},
        ])
        db.commit()
        return first, second, job_id
    finally:
        db.close()


def _token_for(user):
    """Create a browser-compatible token backed by a live login session."""
    db = SessionLocal()
    try:
        return start_login_session(db, user)
    finally:
        db.close()


def _proposal(proposal_id: str, windows: list[dict]) -> dict:
    return {
        "kind": "review_proposal", "schema": REVIEW_PROPOSAL_SCHEMA,
        "id": proposal_id, "policy_version": "lyrics-quality-v6",
        "review_only": True,
        "windows": [{
            "kind": "review_proposal_window",
            "schema": PROPOSAL_WINDOW_SCHEMA,
            "reasons": ["acoustic_cardinality_disagreement"],
            **window,
        } for window in windows],
    }


def test_editor_document_is_shared_by_tenant_and_conflicts_are_explicit(client):
    first, second, job_id = _users_and_job()
    token_a = _token_for(first)
    token_b = _token_for(second)

    loaded = client.get(f"/editor/{job_id}", headers=auth(token_b))
    assert loaded.status_code == 200
    assert loaded.json()["revision"] == 0
    assert loaded.json()["segments"][0]["text"] == "one"

    saved = client.patch(
        f"/editor/{job_id}",
        headers=auth(token_a),
        json={
            "base_revision": 0,
            "segments": [
                {"start": 0, "end": 1, "text": "ONE"},
                {"start": 1, "end": 2, "text": "two"},
            ],
            "checkpoint": "manual",
        },
    )
    assert saved.status_code == 200, saved.text
    assert saved.json()["revision"] == 1

    conflict = client.patch(
        f"/editor/{job_id}",
        headers=auth(token_b),
        json={
            "base_revision": 0,
            "segments": [{"start": 0, "end": 1, "text": "stale"}],
        },
    )
    assert conflict.status_code == 409
    detail = conflict.json()["detail"]
    assert detail["detail"] == "editor_revision_conflict"
    assert detail["server_revision"] == 1
    assert detail["server_segments"][0]["text"] == "ONE"


def test_opening_explicit_editor_revives_soft_superseded_job(client):
    """A duplicate wizard response must not hide the draft being edited."""
    first, _second, job_id = _users_and_job("editor_soft_supersede")
    db = SessionLocal()
    try:
        job = db.query(Job).filter(Job.job_id == job_id).one()
        job.archived_at = datetime.now(timezone.utc)
        db.commit()
    finally:
        db.close()

    response = client.get(f"/editor/{job_id}", headers=auth(_token_for(first)))
    assert response.status_code == 200, response.text

    db = SessionLocal()
    try:
        revived = db.query(Job).filter(Job.job_id == job_id).one()
        assert revived.archived_at is None
        assert revived.last_user_activity_at is not None
    finally:
        db.close()


def test_machine_transcription_checkpoint_is_never_pruned(db):
    first, _second, job_id = _users_and_job("editor_machine_history")
    job = db.query(Job).filter(Job.job_id == job_id).one()
    document = db.query(EditorDocument).filter(EditorDocument.job_id == job_id).one()
    machine = db.query(EditorVersion).filter(
        EditorVersion.job_id == job_id,
        EditorVersion.revision == 0,
    ).one()
    machine.reason = "transcription"
    machine.provenance = {"schema": "machine-transcription-lineage-v1"}
    db.flush()
    for index in range(55):
        document, _version, _applied = save_document(
            db, job, document, first.id, document.revision,
            [{"start": 0, "end": 1, "text": f"edit-{index}"}],
            "manual",
        )
    db.flush()
    preserved = db.query(EditorVersion).filter(
        EditorVersion.id == machine.id,
    ).one_or_none()
    assert preserved is not None
    assert preserved.provenance["schema"] == "machine-transcription-lineage-v1"


def test_editor_activity_heartbeat_uses_server_snapshot_and_identity(client):
    first, _second, job_id = _users_and_job("editor_activity")
    token = _token_for(first)
    assert client.post(f"/editor/{job_id}/lock", headers=auth(token)).status_code == 200
    response = client.post(
        f"/editor/{job_id}/activity/heartbeat", headers=auth(token),
        json={"session_id": "session-123456789", "activity_seq": 1},
    )
    assert response.status_code == 200, response.text
    assert response.json()["revision"] == 0
    assert len(response.json()["snapshot_sha256"]) == 64
    db = SessionLocal()
    try:
        event = db.query(ProductEvent).filter(
            ProductEvent.id == response.json()["event_id"],
        ).one()
        assert event.user_id == first.id
        assert event.created_at is not None
        assert event.properties["snapshot_sha256"] == response.json()["snapshot_sha256"]
        assert event.properties["activity_seq"] == 1
    finally:
        db.close()
    skipped = client.post(
        f"/editor/{job_id}/activity/heartbeat", headers=auth(token),
        json={"session_id": "session-123456789", "activity_seq": 3},
    )
    assert skipped.status_code == 409


def test_editor_versions_restore_and_lock_are_tenant_scoped(client):
    first, second, job_id = _users_and_job("editor_locks")
    token_a = _token_for(first)
    token_b = _token_for(second)

    lock_a = client.post(f"/editor/{job_id}/lock", headers=auth(token_a))
    assert lock_a.status_code == 200
    assert lock_a.json()["acquired"] is True

    lock_b = client.post(f"/editor/{job_id}/lock", headers=auth(token_b))
    assert lock_b.status_code == 200
    assert lock_b.json()["acquired"] is False
    assert lock_b.json()["user"]["id"] == first.id

    heartbeat = client.post(f"/editor/{job_id}/lock/heartbeat", headers=auth(token_a))
    assert heartbeat.status_code == 200
    assert heartbeat.json()["acquired"] is True

    saved = client.patch(
        f"/editor/{job_id}", headers=auth(token_a),
        json={"base_revision": 0, "segments": [{"start": 0, "end": 1, "text": "changed"}]},
    )
    assert saved.status_code == 200
    version_id = saved.json()["version_id"]

    versions = client.get(f"/editor/{job_id}/versions", headers=auth(token_b))
    assert versions.status_code == 200
    assert any(v["id"] == version_id for v in versions.json()["versions"])

    restored = client.post(
        f"/editor/{job_id}/restore", headers=auth(token_b),
        json={"version_id": versions.json()["versions"][-1]["id"], "base_revision": 1},
    )
    assert restored.status_code == 200
    assert restored.json()["revision"] == 2

    db = SessionLocal()
    try:
        document = db.query(EditorDocument).filter(EditorDocument.job_id == job_id).one()
        document.lock_expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
        db.commit()
    finally:
        db.close()
    takeover = client.post(f"/editor/{job_id}/lock", headers=auth(token_b))
    assert takeover.status_code == 200
    assert takeover.json()["acquired"] is True
    assert client.delete(f"/editor/{job_id}/lock", headers=auth(token_a)).status_code == 409
    assert client.delete(f"/editor/{job_id}/lock", headers=auth(token_b)).status_code == 200


def test_editor_rejects_other_tenant_and_analytics_is_bounded(client):
    first, _, job_id = _users_and_job("editor_private")
    other = create_user(
        SessionLocal(), f"other_{uuid.uuid4().hex[:6]}", "testpass12345", None,
        tenant_id="another_team",
    )
    try:
        response = client.get(f"/editor/{job_id}", headers=auth(_token_for(other)))
        assert response.status_code == 404
    finally:
        db = SessionLocal()
        db.query(EditorDocument).filter(EditorDocument.job_id == job_id).delete()
        db.query(EditorVersion).filter(EditorVersion.job_id == job_id).delete()
        db.close()

    events = client.post(
        "/analytics/events", headers=auth(_token_for(first)),
        json={"events": [
            {"name": "editor_opened", "properties": {"line_count": 2}},
            {"name": "not_allowed", "properties": {"lyrics": "secret"}},
        ]},
    )
    assert events.status_code == 200
    assert events.json()["accepted"] == 1


def test_platform_admin_can_use_editor_for_cross_tenant_review(client):
    _, _, job_id = _users_and_job("editor_client_workspace")
    db = SessionLocal()
    try:
        admin = create_user(
            db, f"editor_admin_{uuid.uuid4().hex[:6]}", "testpass12345", None,
            role="admin", tenant_id="editor_platform_admin",
        )
        admin_id = admin.id
    finally:
        db.close()

    token = _token_for(admin)
    loaded = client.get(f"/editor/{job_id}", headers=auth(token))
    assert loaded.status_code == 200, loaded.text
    assert loaded.json()["segments"][0]["text"] == "one"

    saved = client.patch(
        f"/editor/{job_id}", headers=auth(token),
        json={
            "base_revision": 0,
            "segments": [{"start": 0, "end": 1, "text": "admin correction"}],
            "checkpoint": "manual",
        },
    )
    assert saved.status_code == 200, saved.text

    verify = SessionLocal()
    try:
        job = verify.query(Job).filter(Job.job_id == job_id).one()
        document = verify.query(EditorDocument).filter(EditorDocument.job_id == job_id).one()
        assert job.tenant_id == "editor_client_workspace"
        assert document.tenant_id == "editor_client_workspace"
        assert document.current_segments[0]["text"] == "admin correction"
        actions = {
            row.action for row in verify.query(AuditLog).filter(AuditLog.user_id == admin_id).all()
        }
        assert "admin.cross_tenant_access" in actions
    finally:
        verify.close()


def test_quality_proposal_is_audio_revision_scoped_and_applies_idempotently(
    client, monkeypatch,
):
    monkeypatch.setenv("QUALITY_V6_PROPOSALS_ENABLED", "1")
    first, _second, job_id = _users_and_job("editor_quality_proposal")
    token = _token_for(first)
    proposal_id = f"proposal-{uuid.uuid4().hex}"
    db = SessionLocal()
    try:
        job = db.query(Job).filter(Job.job_id == job_id).one()
        document = db.query(EditorDocument).filter(
            EditorDocument.job_id == job_id,
        ).one()
        job.input_audio_sha256 = "a" * 64
        job.audio_revision = 4
        snapshot_hash = segments_hash(document.current_segments)
        stored = persist_quality_proposal_if_current(
            db,
            job_id=job_id,
            expected_revision=0,
            expected_segments_hash=snapshot_hash,
            expected_audio_revision=4,
            expected_audio_sha256="a" * 64,
            proposal={
                "kind": "review_proposal",
                "schema": REVIEW_PROPOSAL_SCHEMA,
                "id": proposal_id,
                "policy_version": "lyrics-quality-v6",
                "review_only": True,
                "windows": [{
                    "kind": "review_proposal_window",
                    "schema": PROPOSAL_WINDOW_SCHEMA,
                    "id": "tail",
                    "start": 0.0,
                    "end": 1.0,
                    "reasons": ["acoustic_cardinality_disagreement"],
                    "current_segments": [
                        {"start": 0, "end": 1, "text": "one"},
                    ],
                    "proposed_segments": [
                        {"start": 0, "end": 1, "text": "ONE + vocalization"},
                    ],
                }],
            },
        )
        assert stored is True
        db.commit()
    finally:
        db.close()

    loaded = client.get(f"/editor/{job_id}", headers=auth(token))
    assert loaded.status_code == 200, loaded.text
    assert loaded.json()["quality_proposal"]["id"] == proposal_id
    assert loaded.json()["quality_proposal"]["review_only"] is True

    body = {
        "base_revision": 0,
        "window_ids": ["tail"],
        "idempotency_key": "quality-proposal-request-0001",
    }
    applied = client.post(
        f"/editor/{job_id}/quality-proposals/{proposal_id}/apply",
        headers=auth(token), json=body,
    )
    assert applied.status_code == 200, applied.text
    assert applied.json()["applied"] is True
    assert applied.json()["revision"] == 1

    duplicate = client.post(
        f"/editor/{job_id}/quality-proposals/{proposal_id}/apply",
        headers=auth(token), json=body,
    )
    assert duplicate.status_code == 200, duplicate.text
    assert duplicate.json()["applied"] is False
    assert duplicate.json()["idempotent"] is True

    verify = SessionLocal()
    try:
        document = verify.query(EditorDocument).filter(
            EditorDocument.job_id == job_id,
        ).one()
        assert document.revision == 1
        assert [row["text"] for row in document.current_segments] == [
            "ONE + vocalization", "two",
        ]
        versions = verify.query(EditorVersion).filter(
            EditorVersion.job_id == job_id,
            EditorVersion.reason == "quality_proposal",
        ).all()
        assert len(versions) == 1
    finally:
        verify.close()


def test_quality_observation_records_hash_only_verdict_without_mutation(
    client, monkeypatch,
):
    monkeypatch.setenv("QUALITY_CONSENSUS_OBSERVATIONS_ENABLED", "1")
    monkeypatch.setenv(
        "QUALITY_CONTENT_FINGERPRINT_HMAC_KEY",
        "test-observation-key-0123456789-ABCDEFGHIJKLMNOPQRSTUVWXYZ",
    )
    first, _second, job_id = _users_and_job("editor_quality_observation")
    token = _token_for(first)
    proposal_id = f"observation-{uuid.uuid4().hex}"
    db = SessionLocal()
    try:
        job = db.query(Job).filter(Job.job_id == job_id).one()
        document = db.query(EditorDocument).filter(
            EditorDocument.job_id == job_id,
        ).one()
        job.input_audio_sha256 = "b" * 64
        job.audio_revision = 2
        stored = persist_quality_observation_if_current(
            db, job_id=job_id, expected_revision=0,
            expected_segments_hash=segments_hash(document.current_segments),
            expected_audio_revision=2, expected_audio_sha256="b" * 64,
            proposal={
                **_proposal(proposal_id, [{
                    "id": "window-a", "start": 0.0, "end": 1.0,
                    "current_segments": [{"start": 0, "end": 1, "text": "one"}],
                    "proposed_segments": [{"start": 0, "end": 1, "text": "won"}],
                    "source_families": ["whisper", "gemini_audio"],
                }]),
                "observation_only": True,
            },
        )
        assert stored is True
        db.commit()
    finally:
        db.close()

    response = client.post(
        f"/editor/{job_id}/quality-proposals/{proposal_id}/observe",
        headers=auth(token),
        json={
            "base_revision": 0, "window_id": "window-a",
            "verdict": "correct",
            "idempotency_key": "quality-observation-request-0001",
        },
    )
    assert response.status_code == 200, response.text
    assert response.json()["recorded"] is True

    verify = SessionLocal()
    try:
        document = verify.query(EditorDocument).filter(
            EditorDocument.job_id == job_id,
        ).one()
        assert document.revision == 0
        assert [row["text"] for row in document.current_segments] == ["one", "two"]
        assert document.quality_proposal["status"] == "observed"
        assert "won" not in str(document.quality_proposal)
        event = verify.query(ProductEvent).filter(
            ProductEvent.job_id == job_id,
            ProductEvent.name == "quality_consensus_observation",
        ).one()
        assert event.properties["verdict"] == "correct"
        assert len(event.properties["candidate_sha256"]) == 64
        assert "won" not in str(event.properties)
    finally:
        verify.close()

    # Even if the separately signed production-proposal switch is enabled,
    # an observation remains technically non-applicable.
    monkeypatch.setenv("QUALITY_V6_PROPOSALS_ENABLED", "1")
    apply_response = client.post(
        f"/editor/{job_id}/quality-proposals/{proposal_id}/apply",
        headers=auth(token),
        json={
            "base_revision": 0, "window_ids": ["window-a"],
            "idempotency_key": "quality-observation-apply-0001",
        },
    )
    assert apply_response.status_code == 409
    assert apply_response.json()["detail"] == "quality_observation_cannot_be_applied"


def test_quality_observation_kill_switch_erases_pending_raw_candidate(
    client, monkeypatch,
):
    monkeypatch.setenv("QUALITY_CONSENSUS_OBSERVATIONS_ENABLED", "1")
    first, _second, job_id = _users_and_job("editor_observation_kill_switch")
    token = _token_for(first)
    db = SessionLocal()
    try:
        job = db.query(Job).filter(Job.job_id == job_id).one()
        document = db.query(EditorDocument).filter(
            EditorDocument.job_id == job_id,
        ).one()
        job.input_audio_sha256 = "c" * 64
        assert persist_quality_observation_if_current(
            db, job_id=job_id, expected_revision=0,
            expected_segments_hash=segments_hash(document.current_segments),
            expected_audio_revision=0, expected_audio_sha256="c" * 64,
            proposal={
                **_proposal("observation-kill", [{
                    "id": "window-kill", "start": 0.0, "end": 1.0,
                    "current_segments": [{"start": 0, "end": 1, "text": "one"}],
                    "proposed_segments": [{"start": 0, "end": 1, "text": "candidate"}],
                    "source_families": ["whisper", "gemini_audio"],
                }]),
                "observation_only": True,
            },
        )
        db.commit()
    finally:
        db.close()

    monkeypatch.setenv("QUALITY_CONSENSUS_OBSERVATIONS_ENABLED", "0")
    loaded = client.get(f"/editor/{job_id}", headers=auth(token))
    assert loaded.status_code == 200
    assert loaded.json()["quality_proposal"] is None


def test_quality_proposal_rejects_stale_audio_identity(db, monkeypatch):
    monkeypatch.setenv("QUALITY_V6_PROPOSALS_ENABLED", "1")
    _first, _second, job_id = _users_and_job("editor_quality_stale_audio")
    job = db.query(Job).filter(Job.job_id == job_id).one()
    document = db.query(EditorDocument).filter(
        EditorDocument.job_id == job_id,
    ).one()
    job.input_audio_sha256 = "b" * 64
    job.audio_revision = 9
    db.flush()
    stored = persist_quality_proposal_if_current(
        db,
        job_id=job_id,
        expected_revision=0,
        expected_segments_hash=segments_hash(document.current_segments),
        expected_audio_revision=8,
        expected_audio_sha256="b" * 64,
        proposal={
            "kind": "review_proposal",
            "schema": REVIEW_PROPOSAL_SCHEMA,
            "policy_version": "lyrics-quality-v6",
            "review_only": True,
            "windows": [{
                "kind": "review_proposal_window",
                "schema": PROPOSAL_WINDOW_SCHEMA,
                "id": "stale", "start": 0, "end": 1,
                "reasons": ["acoustic_cardinality_disagreement"],
                "current_segments": [],
                "proposed_segments": [{"start": 0, "end": 1, "text": "x"}],
            }],
        },
    )
    assert stored is False
    assert document.quality_proposal is None


def test_quality_proposal_cannot_apply_after_audio_revision_changes(
    client, monkeypatch,
):
    monkeypatch.setenv("QUALITY_V6_PROPOSALS_ENABLED", "1")
    first, _second, job_id = _users_and_job("editor_quality_apply_audio_race")
    proposal_id = f"proposal-{uuid.uuid4().hex}"
    db = SessionLocal()
    try:
        job = db.query(Job).filter(Job.job_id == job_id).one()
        document = db.query(EditorDocument).filter(
            EditorDocument.job_id == job_id,
        ).one()
        job.input_audio_sha256 = "c" * 64
        job.audio_revision = 1
        assert persist_quality_proposal_if_current(
            db, job_id=job_id, expected_revision=0,
            expected_segments_hash=segments_hash(document.current_segments),
            expected_audio_revision=1, expected_audio_sha256="c" * 64,
            proposal={
                "kind": "review_proposal", "schema": REVIEW_PROPOSAL_SCHEMA,
                "id": proposal_id, "policy_version": "lyrics-quality-v6",
                "review_only": True,
                "windows": [{
                    "kind": "review_proposal_window",
                    "schema": PROPOSAL_WINDOW_SCHEMA,
                    "id": "tail", "start": 0, "end": 1,
                    "reasons": ["acoustic_cardinality_disagreement"],
                    "current_segments": [{"start": 0, "end": 1, "text": "one"}],
                    "proposed_segments": [{"start": 0, "end": 1, "text": "ONE"}],
                }],
            },
        )
        db.commit()
        job.audio_revision = 2
        db.commit()
    finally:
        db.close()

    response = client.post(
        f"/editor/{job_id}/quality-proposals/{proposal_id}/apply",
        headers=auth(_token_for(first)),
        json={
            "base_revision": 0, "window_ids": ["tail"],
            "idempotency_key": "quality-audio-race-request-01",
        },
    )
    assert response.status_code == 409
    assert response.json()["detail"] == "quality_proposal_stale"


def test_quality_proposal_dismiss_reason_cannot_store_free_text(client):
    first, _second, job_id = _users_and_job("editor_quality_private_reason")
    response = client.post(
        f"/editor/{job_id}/quality-proposals/unknown/dismiss",
        headers=auth(_token_for(first)),
        json={
            "base_revision": 0,
            "idempotency_key": "quality-dismiss-request-0001",
            "reason": "private lyric text must not enter audit log",
        },
    )
    assert response.status_code == 422


def test_expired_quality_proposal_payload_is_erased(db):
    _first, _second, job_id = _users_and_job("editor_quality_expiry")
    document = db.query(EditorDocument).filter(
        EditorDocument.job_id == job_id,
    ).one()
    document.quality_proposal = {
        "id": "expired", "status": "pending", "windows": [{"text": "private"}],
        "expires_at": "2000-01-01T00:00:00+00:00",
    }
    db.flush()
    # The collector is deliberately global.  Other tests (and production
    # tenants) may have left additional expired proposals for the same sweep,
    # so the contract is that our target is among the erased payloads rather
    # than that the database contained exactly one expired row.
    assert expire_stale_quality_proposals(db) >= 1
    db.commit()
    db.expire_all()
    document = db.query(EditorDocument).filter(
        EditorDocument.job_id == job_id,
    ).one()
    assert document.quality_proposal is None


def test_editor_get_persists_expired_proposal_cleanup(client, monkeypatch):
    monkeypatch.setenv("QUALITY_V6_PROPOSALS_ENABLED", "1")
    first, _second, job_id = _users_and_job("editor_quality_get_expiry")
    db = SessionLocal()
    try:
        document = db.query(EditorDocument).filter(
            EditorDocument.job_id == job_id,
        ).one()
        document.quality_proposal = {
            "id": "expired-get", "status": "pending",
            "windows": [{"private": "must be erased"}],
            "expires_at": "2000-01-01T00:00:00+00:00",
            "base_revision": 0,
        }
        db.commit()
    finally:
        db.close()
    response = client.get(f"/editor/{job_id}", headers=auth(_token_for(first)))
    assert response.status_code == 200
    assert response.json()["quality_proposal"] is None
    verify = SessionLocal()
    try:
        document = verify.query(EditorDocument).filter(
            EditorDocument.job_id == job_id,
        ).one()
        assert document.quality_proposal is None
    finally:
        verify.close()


def test_quality_proposal_rejects_non_exact_current_segment_content(monkeypatch):
    monkeypatch.setenv("QUALITY_V6_PROPOSALS_ENABLED", "1")
    _first, _second, job_id = _users_and_job("editor_quality_exact_content")
    db = SessionLocal()
    try:
        job = db.query(Job).filter(Job.job_id == job_id).one()
        document = db.query(EditorDocument).filter(
            EditorDocument.job_id == job_id,
        ).one()
        enriched = [dict(item) for item in document.current_segments]
        enriched[0]["locked"] = True
        document.current_segments = enriched
        job.segments_json = enriched
        job.input_audio_sha256 = "d" * 64
        job.audio_revision = 1
        db.flush()
        stored = persist_quality_proposal_if_current(
            db, job_id=job_id, expected_revision=0,
            expected_segments_hash=segments_hash(enriched),
            expected_audio_revision=1, expected_audio_sha256="d" * 64,
            proposal=_proposal("exact-mismatch", [{
                "id": "first", "start": 0, "end": 1,
                # Omitting `locked` is a content mismatch even though text and
                # timing look identical.
                "current_segments": [{"start": 0, "end": 1, "text": "one"}],
                "proposed_segments": [{"start": 0, "end": 1, "text": "ONE"}],
            }]),
        )
        assert stored is False
        assert document.quality_proposal is None
    finally:
        db.rollback()
        db.close()


@pytest.mark.parametrize("malformation", ["overlap", "duplicate_start", "out_of_order"])
def test_quality_proposal_rejects_malformed_proposed_timeline(
    monkeypatch, malformation,
):
    monkeypatch.setenv("QUALITY_V6_PROPOSALS_ENABLED", "1")
    _first, _second, job_id = _users_and_job(f"editor_quality_{malformation}")
    db = SessionLocal()
    try:
        job = db.query(Job).filter(Job.job_id == job_id).one()
        document = db.query(EditorDocument).filter(
            EditorDocument.job_id == job_id,
        ).one()
        job.input_audio_sha256 = "e" * 64
        job.audio_revision = 1
        proposed = (
            [
                {"start": 0, "end": 0.75, "text": "A"},
                {"start": 0.5, "end": 1, "text": "B"},
            ] if malformation == "overlap" else ([
                {"start": 0, "end": 0.4, "text": "A"},
                {"start": 0, "end": 1, "text": "B"},
            ] if malformation == "duplicate_start" else [
                {"start": 0.6, "end": 1, "text": "B"},
                {"start": 0, "end": 0.5, "text": "A"},
            ])
        )
        assert persist_quality_proposal_if_current(
            db, job_id=job_id, expected_revision=0,
            expected_segments_hash=segments_hash(document.current_segments),
            expected_audio_revision=1, expected_audio_sha256="e" * 64,
            proposal=_proposal(f"malformed-{malformation}", [{
                "id": "first", "start": 0, "end": 1,
                "current_segments": [{"start": 0, "end": 1, "text": "one"}],
                "proposed_segments": proposed,
            }]),
        ) is False
    finally:
        db.rollback()
        db.close()


def test_quality_proposal_rejects_overlapping_windows_even_across_silence(monkeypatch):
    monkeypatch.setenv("QUALITY_V6_PROPOSALS_ENABLED", "1")
    _first, _second, job_id = _users_and_job("editor_quality_window_overlap")
    db = SessionLocal()
    try:
        job = db.query(Job).filter(Job.job_id == job_id).one()
        document = db.query(EditorDocument).filter(
            EditorDocument.job_id == job_id,
        ).one()
        segments = [
            {"start": 0, "end": 0.5, "text": "one"},
            {"start": 1.5, "end": 2, "text": "two"},
        ]
        document.current_segments = segments
        job.segments_json = segments
        job.input_audio_sha256 = "1" * 64
        job.audio_revision = 1
        db.flush()
        assert persist_quality_proposal_if_current(
            db, job_id=job_id, expected_revision=0,
            expected_segments_hash=segments_hash(segments),
            expected_audio_revision=1, expected_audio_sha256="1" * 64,
            proposal=_proposal("window-overlap", [
                {
                    "id": "a", "start": 0, "end": 0.75,
                    "current_segments": [segments[0]],
                    "proposed_segments": [{"start": 0, "end": 0.5, "text": "A"}],
                },
                {
                    "id": "b", "start": 0.6, "end": 2,
                    "current_segments": [segments[1]],
                    "proposed_segments": [{"start": 1.5, "end": 2, "text": "B"}],
                },
            ]),
        ) is False
    finally:
        db.rollback()
        db.close()


def test_quality_proposal_apply_flag_off_revokes_pending_payload(
    client, monkeypatch,
):
    monkeypatch.setenv("QUALITY_V6_PROPOSALS_ENABLED", "1")
    first, _second, job_id = _users_and_job("editor_quality_flag_off")
    proposal_id = f"proposal-{uuid.uuid4().hex}"
    db = SessionLocal()
    try:
        job = db.query(Job).filter(Job.job_id == job_id).one()
        document = db.query(EditorDocument).filter(
            EditorDocument.job_id == job_id,
        ).one()
        job.input_audio_sha256 = "f" * 64
        job.audio_revision = 1
        assert persist_quality_proposal_if_current(
            db, job_id=job_id, expected_revision=0,
            expected_segments_hash=segments_hash(document.current_segments),
            expected_audio_revision=1, expected_audio_sha256="f" * 64,
            proposal=_proposal(proposal_id, [{
                "id": "first", "start": 0, "end": 1,
                "current_segments": [{"start": 0, "end": 1, "text": "one"}],
                "proposed_segments": [{"start": 0, "end": 1, "text": "ONE"}],
            }]),
        )
        db.commit()
    finally:
        db.close()
    monkeypatch.setenv("QUALITY_V6_PROPOSALS_ENABLED", "0")
    response = client.post(
        f"/editor/{job_id}/quality-proposals/{proposal_id}/apply",
        headers=auth(_token_for(first)),
        json={
            "base_revision": 0, "window_ids": ["first"],
            "idempotency_key": "quality-disabled-request-001",
        },
    )
    assert response.status_code == 409
    assert response.json()["detail"] == "quality_v6_proposals_disabled"
    verify = SessionLocal()
    try:
        document = verify.query(EditorDocument).filter(
            EditorDocument.job_id == job_id,
        ).one()
        assert document.quality_proposal is None
        assert document.revision == 0
    finally:
        verify.close()


def test_quality_proposal_apply_revalidates_stored_window_hash_and_timeline(
    client, monkeypatch,
):
    monkeypatch.setenv("QUALITY_V6_PROPOSALS_ENABLED", "1")
    first, _second, job_id = _users_and_job("editor_quality_apply_revalidate")
    proposal_id = f"proposal-{uuid.uuid4().hex}"
    db = SessionLocal()
    try:
        job = db.query(Job).filter(Job.job_id == job_id).one()
        document = db.query(EditorDocument).filter(
            EditorDocument.job_id == job_id,
        ).one()
        job.input_audio_sha256 = "3" * 64
        job.audio_revision = 1
        assert persist_quality_proposal_if_current(
            db, job_id=job_id, expected_revision=0,
            expected_segments_hash=segments_hash(document.current_segments),
            expected_audio_revision=1, expected_audio_sha256="3" * 64,
            proposal=_proposal(proposal_id, [{
                "id": "first", "start": 0, "end": 1,
                "current_segments": [{"start": 0, "end": 1, "text": "one"}],
                "proposed_segments": [{"start": 0, "end": 1, "text": "ONE"}],
            }]),
        )
        tampered = dict(document.quality_proposal)
        tampered["windows"] = [dict(tampered["windows"][0])]
        tampered["windows"][0]["proposed_segments"] = [
            {"start": 0, "end": 0.8, "text": "A"},
            {"start": 0.5, "end": 1, "text": "B"},
        ]
        document.quality_proposal = tampered
        db.commit()
    finally:
        db.close()
    response = client.post(
        f"/editor/{job_id}/quality-proposals/{proposal_id}/apply",
        headers=auth(_token_for(first)),
        json={
            "base_revision": 0, "window_ids": ["first"],
            "idempotency_key": "quality-malformed-apply-001",
        },
    )
    assert response.status_code == 422
    verify = SessionLocal()
    try:
        document = verify.query(EditorDocument).filter(
            EditorDocument.job_id == job_id,
        ).one()
        assert document.revision == 0
        assert [row["text"] for row in document.current_segments] == ["one", "two"]
    finally:
        verify.close()


def test_editor_save_and_restore_commit_quality_outbox_with_revision(
    client, monkeypatch,
):
    import main

    first, _second, job_id = _users_and_job("editor_quality_durable_outbox")
    token = _token_for(first)
    monkeypatch.setattr(main, "_dispatch_editor_quality_outbox", lambda _event_id: None)
    db = SessionLocal()
    try:
        job = db.query(Job).filter(Job.job_id == job_id).one()
        job.input_audio_sha256 = "2" * 64
        job.audio_revision = 1
        original = db.query(EditorVersion).filter(
            EditorVersion.job_id == job_id,
            EditorVersion.revision == 0,
        ).one()
        original_id = original.id
        db.commit()
    finally:
        db.close()

    saved = client.patch(
        f"/editor/{job_id}", headers=auth(token),
        json={
            "base_revision": 0,
            "segments": [
                {"start": 0, "end": 1, "text": "ONE"},
                {"start": 1, "end": 2, "text": "two"},
            ],
            "checkpoint": "manual",
        },
    )
    assert saved.status_code == 200, saved.text
    restored = client.post(
        f"/editor/{job_id}/restore", headers=auth(token),
        json={"version_id": original_id, "base_revision": 1},
    )
    assert restored.status_code == 200, restored.text

    verify = SessionLocal()
    try:
        events = verify.query(JobOutboxEvent).filter(
            JobOutboxEvent.job_id == job_id,
            JobOutboxEvent.event_type == "quality.enqueue",
        ).order_by(JobOutboxEvent.created_at.asc()).all()
        assert len(events) == 2
        assert [event.payload["expected_revision"] for event in events] == [1, 2]
        assert [event.payload["reason"] for event in events] == [
            "editor_save", "editor_version_restored",
        ]
        assert all("segments" not in event.payload for event in events)
    finally:
        verify.close()


def test_editor_preserves_metadata_draft_retry_and_version_details(client):
    first, _, job_id = _users_and_job("editor_metadata")
    token = _token_for(first)
    metadata_segments = [{
        "_id": "stable-row", "start": 0, "end": 1.25, "text": "hola",
        "words": [{"word": "hola", "start": 0, "end": 1}],
        "locked": True, "review": True,
        "pos": {"x": 0.25, "y": 0.7}, "scale": 1.2, "rot": -2,
        "future_field": {"kept": True},
    }]
    draft = client.patch(
        f"/editor/{job_id}", headers=auth(token),
        json={"base_revision": 0, "segments": metadata_segments, "checkpoint": "draft"},
    )
    assert draft.status_code == 200, draft.text
    assert draft.json()["revision"] == 1
    assert draft.json()["version_id"] is None

    # Lost-response retry: stale base but byte-equivalent content is a 200
    # no-op, never a false conflict and never another revision.
    retry = client.patch(
        f"/editor/{job_id}", headers=auth(token),
        json={"base_revision": 0, "segments": metadata_segments, "checkpoint": "draft"},
    )
    assert retry.status_code == 200
    assert retry.json()["revision"] == 1
    assert retry.json()["applied"] is False

    # If the delayed 5-second checkpoint raced the draft request, its stale
    # but identical payload still creates the durable version at revision 1.
    raced_checkpoint = client.patch(
        f"/editor/{job_id}", headers=auth(token),
        json={"base_revision": 0, "segments": metadata_segments, "checkpoint": "autosave"},
    )
    assert raced_checkpoint.status_code == 200
    assert raced_checkpoint.json()["applied"] is False
    assert raced_checkpoint.json()["version_id"]

    checkpoint = client.patch(
        f"/editor/{job_id}", headers=auth(token),
        json={"base_revision": 1, "segments": metadata_segments, "checkpoint": "manual"},
    )
    assert checkpoint.status_code == 200
    version_id = checkpoint.json()["version_id"]

    summaries = client.get(f"/editor/{job_id}/versions", headers=auth(token)).json()["versions"]
    assert all("segments" not in version for version in summaries)
    detail = client.get(f"/editor/{job_id}/versions/{version_id}", headers=auth(token))
    assert detail.status_code == 200
    assert detail.json()["segments"][0] == metadata_segments[0]


def test_stale_renderer_metadata_retry_is_not_a_collaboration_conflict(client):
    first, _, job_id = _users_and_job("editor_renderer_retry")
    token = _token_for(first)
    base = [{
        "_id": "row-1", "start": 0, "end": 1, "text": "hola",
        "words": [{"word": "hola", "start": 0, "end": 1}], "review": False,
    }]
    saved = client.patch(
        f"/editor/{job_id}", headers=auth(token),
        json={"base_revision": 0, "segments": base, "checkpoint": "draft"},
    )
    assert saved.status_code == 200
    assert saved.json()["revision"] == 1

    # A background/typography render refreshed IDs and word-review metadata
    # after the operator's request was sent. The stale retry is semantically
    # the same lyric snapshot and must not open the collaboration resolver.
    renderer_snapshot = [{
        "_id": "new-row-id", "segment_id": "row-1", "start": 0, "end": 1,
        "text": "hola", "words": [{"word": "hola", "start": 0.1, "end": 0.9}],
        "review": True,
    }]
    retry = client.patch(
        f"/editor/{job_id}", headers=auth(token),
        json={
            "base_revision": 0,
            "segments": renderer_snapshot,
            "checkpoint": "autosave",
        },
    )
    assert retry.status_code == 200, retry.text
    assert retry.json()["revision"] == 1
    assert retry.json()["applied"] is False


def test_conflict_resolution_preserves_both_and_rechecks_third_edit(client):
    first, second, job_id = _users_and_job("editor_conflict_resolution")
    token_a = _token_for(first)
    token_b = _token_for(second)
    team = [{"start": 0, "end": 1, "text": "team"}]
    local = [{"start": 0, "end": 1, "text": "local"}]
    third = [{"start": 0, "end": 1, "text": "third"}]
    assert client.patch(
        f"/editor/{job_id}", headers=auth(token_b),
        json={"base_revision": 0, "segments": team, "checkpoint": "manual"},
    ).status_code == 200

    resolved = client.post(
        f"/editor/{job_id}/conflicts/resolve", headers=auth(token_a),
        json={"strategy": "save_local_as_new", "server_revision": 1, "segments": local},
    )
    assert resolved.status_code == 200, resolved.text
    assert resolved.json()["revision"] == 2
    assert resolved.json()["segments"] == local

    assert client.patch(
        f"/editor/{job_id}", headers=auth(token_b),
        json={"base_revision": 2, "segments": third, "checkpoint": "manual"},
    ).status_code == 200
    stale_dialog = client.post(
        f"/editor/{job_id}/conflicts/resolve", headers=auth(token_a),
        json={"strategy": "save_local_as_new", "server_revision": 2, "segments": local},
    )
    assert stale_dialog.status_code == 409
    assert stale_dialog.json()["detail"]["server_revision"] == 3

    db = SessionLocal()
    try:
        snapshots = db.query(EditorVersion).filter(EditorVersion.job_id == job_id).all()
        texts = {version.segments[0]["text"] for version in snapshots}
        assert {"team", "local", "third"}.issubset(texts)
    finally:
        db.close()


def test_historical_revision_is_used_for_lazy_document(client):
    db = SessionLocal()
    try:
        user = create_user(db, f"historical_{uuid.uuid4().hex[:6]}", "testpass12345", None, tenant_id="editor_history")
        job_id = f"ed_{uuid.uuid4().hex[:9]}"
        segments = [{"start": 2, "end": 3, "text": "historic", "_id": 99}]
        db.add(Job(
            job_id=job_id, user_id=user.id, tenant_id="editor_history",
            artist="History", filename="history.wav", style="oscuro",
            status="transcribed_pending", current_step="editing",
            delivery_profile="youtube", segments_json=segments, segments_revision=7,
        ))
        db.commit()
    finally:
        db.close()
    response = client.get(f"/editor/{job_id}", headers=auth(_token_for(user)))
    assert response.status_code == 200
    assert response.json()["revision"] == 7
    assert response.json()["segments"] == segments


def test_equal_revision_legacy_timing_anomaly_is_migrated_once():
    first, _, job_id = _users_and_job("editor_timing_repair")
    malformed = [
        {"_id": 9, "start": 45.1106, "end": 45.8752, "text": "uoo no no te hice daño,"},
        {"_id": 10, "start": 45.9252, "end": 46.5273, "text": "te alejaste de miSsi"},
        {"_id": 11, "start": 45.1606, "end": 46.9606, "text": "Las palabras se fueron al viento y no se."},
        {"_id": 22, "start": 114.766, "end": 115.967, "text": "¡Gracias!"},
        {"_id": 23, "start": 45.1106, "end": 45.8752, "text": "uoo no no te hice daño,"},
        {"_id": 24, "start": 45.9252, "end": 46.5273, "text": "te alejaste de mi"},
    ]
    db = SessionLocal()
    try:
        job = db.query(Job).filter(Job.job_id == job_id).one()
        document = db.query(EditorDocument).filter(EditorDocument.job_id == job_id).one()
        job.segments_json = malformed
        document.current_segments = malformed
        db.commit()
        repaired = get_or_create_document(db, job_id, job.tenant_id, malformed)
        db.commit()
        assert [row["_id"] for row in repaired.current_segments] == [9, 10, 11, 22]
        assert repaired.current_segments[2]["start"] == 46.5773
        assert job.segments_revision == repaired.revision == 2
    finally:
        db.close()


def test_lazy_reconciliation_preserves_equal_revision_divergence():
    first, _, job_id = _users_and_job("editor_reconcile")
    db = SessionLocal()
    try:
        job = db.query(Job).filter(Job.job_id == job_id).one()
        job.segments_json = [{"start": 0, "end": 1, "text": "deployed job"}]
        job.segments_revision = 0
        db.commit()

        document = get_or_create_document(db, job_id, job.tenant_id, job.segments_json)
        db.commit()
        assert document.revision == 2
        assert document.current_segments[0]["text"] == "deployed job"
        assert job.segments_revision == 2
        versions = db.query(EditorVersion).filter(EditorVersion.job_id == job_id).order_by(EditorVersion.revision).all()
        assert versions[-2].segments[0]["text"] == "one"
        assert versions[-1].segments[0]["text"] == "deployed job"
        assert versions[-1].reason == "migration"
    finally:
        db.close()


def test_lazy_reconciliation_skips_an_orphan_conflicting_revision():
    first, _, job_id = _users_and_job("editor_reconcile_orphan")
    db = SessionLocal()
    try:
        job = db.query(Job).filter(Job.job_id == job_id).one()
        document = db.query(EditorDocument).filter(EditorDocument.job_id == job_id).one()
        document.current_segments = [{"start": 0, "end": 1, "text": "document current"}]
        document.revision = 2
        job.segments_json = [{"start": 0, "end": 1, "text": "legacy old"}]
        job.segments_revision = 1
        db.add(EditorVersion(
            id=str(uuid.uuid4()), job_id=job_id, tenant_id=job.tenant_id,
            revision=2, segments=[{"start": 0, "end": 1, "text": "orphan conflict"}],
            created_by=first.id, reason="migration",
        ))
        db.commit()

        reconciled = get_or_create_document(db, job_id, job.tenant_id, job.segments_json)
        db.commit()
        assert reconciled.revision == 3
        assert reconciled.current_segments[0]["text"] == "document current"
        assert job.segments_revision == 3
        assert db.query(EditorVersion).filter(
            EditorVersion.job_id == job_id, EditorVersion.revision == 3,
        ).one().segments[0]["text"] == "document current"
    finally:
        db.close()


def test_analytics_rejects_cross_tenant_nested_and_oversized_batches(client):
    first, _, job_id = _users_and_job("editor_analytics")
    other_first, _, other_job_id = _users_and_job("editor_analytics_other")
    token = _token_for(first)
    response = client.post(
        "/analytics/events", headers=auth(token),
        json={"events": [
            {"name": "editor_opened", "job_id": job_id, "properties": {"line_count": 2}},
            {"name": "editor_opened", "job_id": other_job_id, "properties": {"line_count": 2}},
            {"name": "editor_opened", "properties": {"line_count": 2, "lyrics": {"text": "secret"}}},
            {"name": "unknown", "properties": {}},
        ]},
    )
    assert response.status_code == 200
    assert response.json() == {"accepted": 1, "rejected": 3}
    too_many = client.post(
        "/analytics/events", headers=auth(token),
        json={"events": [{"name": "editor_opened", "properties": {}} for _ in range(51)]},
    )
    assert too_many.status_code == 422
    assert other_first.id != first.id


def test_transaction_rollback_never_leaves_partial_editor_state():
    first, _, job_id = _users_and_job("editor_rollback")
    db = SessionLocal()
    try:
        job = db.query(Job).filter(Job.job_id == job_id).one()
        document = db.query(EditorDocument).filter(EditorDocument.job_id == job_id).one()
        save_document(
            db, job, document, first.id, 0,
            [{"start": 0, "end": 1, "text": "must rollback"}], "manual",
        )
        db.rollback()
    finally:
        db.close()
    verify = SessionLocal()
    try:
        job = verify.query(Job).filter(Job.job_id == job_id).one()
        document = verify.query(EditorDocument).filter(EditorDocument.job_id == job_id).one()
        assert document.revision == 0
        assert job.segments_revision == 0
        assert document.current_segments[0]["text"] == "one"
        assert job.segments_json[0]["text"] == "one"
        assert verify.query(EditorVersion).filter(EditorVersion.job_id == job_id).count() == 1
    finally:
        verify.close()


def test_approval_requires_the_current_exact_snapshot():
    first, _, job_id = _users_and_job("editor_approval")
    db = SessionLocal()
    try:
        job = db.query(Job).filter(Job.job_id == job_id).one()
        document = db.query(EditorDocument).filter(EditorDocument.job_id == job_id).one()
        document, version, _ = save_document(
            db, job, document, first.id, 0,
            [{"start": 0, "end": 1, "text": "approved exact"}], "manual",
        )
        db.commit()
        approved_document, approved_version = approve_document(
            db, job, first.id, editor_revision=1, editor_version_id=version.id,
        )
        db.commit()
        assert approved_document.revision == 1
        assert approved_version.is_approved is True
        assert approved_version.segments[0]["text"] == "approved exact"

        document = db.query(EditorDocument).filter(EditorDocument.job_id == job_id).one()
        save_document(
            db, job, document, first.id, 1,
            [{"start": 0, "end": 1, "text": "remote intermediate"}], "manual",
        )
        db.commit()
        with pytest.raises(RuntimeError, match="editor_revision_conflict"):
            approve_document(db, job, first.id, editor_version_id=version.id)
        db.rollback()
    finally:
        db.close()


def test_approval_allows_renderer_metadata_revision_when_lyrics_are_unchanged():
    first, _, job_id = _users_and_job("editor_approval_metadata")
    db = SessionLocal()
    try:
        job = db.query(Job).filter(Job.job_id == job_id).one()
        document = db.query(EditorDocument).filter(EditorDocument.job_id == job_id).one()
        document, version, _ = save_document(
            db, job, document, first.id, 0,
            [
                {"start": 0, "end": 1, "text": "one", "words": [{"start": 0}]},
                {"start": 1, "end": 2, "text": "two"},
            ], "manual",
        )
        db.commit()

        document = db.query(EditorDocument).filter(EditorDocument.job_id == job_id).one()
        save_document(
            db, job, document, first.id, 1,
            [
                {"start": 0, "end": 1, "text": "one", "words": [{"start": 0.2}], "review": True},
                {"start": 1, "end": 2, "text": "two"},
            ], "migration",
        )
        db.commit()

        approved_document, approved_version = approve_document(
            db, job, first.id, editor_revision=1, editor_version_id=version.id,
        )
        db.commit()
        assert approved_document.revision == 2
        assert approved_version.is_approved is True
        assert approved_version.segments[0]["text"] == "one"
    finally:
        db.close()


def test_retention_keeps_fifty_drafts_and_all_approved_snapshots():
    first, _, job_id = _users_and_job("editor_retention")
    db = SessionLocal()
    try:
        job = db.query(Job).filter(Job.job_id == job_id).one()
        document = db.query(EditorDocument).filter(EditorDocument.job_id == job_id).one()
        document, approved, _ = save_document(
            db, job, document, first.id, 0,
            [{"start": 0, "end": 1, "text": "approved"}], "approve",
        )
        approved_id = approved.id
        for index in range(55):
            document, _, _ = save_document(
                db, job, document, first.id, document.revision,
                [{"start": 0, "end": 1, "text": f"draft-{index}"}], "manual",
            )
        db.commit()
        drafts = db.query(EditorVersion).filter(
            EditorVersion.job_id == job_id,
            EditorVersion.is_approved.is_(False),
        ).all()
        assert len(drafts) == 50
        assert db.query(EditorVersion).filter(EditorVersion.id == approved_id).one().is_approved is True
    finally:
        db.close()


@pytest.mark.skipif(engine.dialect.name != "postgresql", reason="requires PostgreSQL row locks")
def test_postgres_two_simultaneous_patches_exactly_one_wins():
    first, _, job_id = _users_and_job("editor_pg_patch")
    barrier = Barrier(2)

    def writer(text):
        db = SessionLocal()
        try:
            job = db.query(Job).filter(Job.job_id == job_id).one()
            document = db.query(EditorDocument).filter(EditorDocument.job_id == job_id).one()
            barrier.wait(timeout=5)
            try:
                save_document(db, job, document, first.id, 0, [{"start": 0, "end": 1, "text": text}], "manual")
                db.commit()
                return "won"
            except RuntimeError:
                db.rollback()
                return "conflict"
        finally:
            db.close()

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(writer, ["alpha", "beta"]))
    assert sorted(outcomes) == ["conflict", "won"]


@pytest.mark.skipif(engine.dialect.name != "postgresql", reason="requires PostgreSQL row locks")
def test_postgres_two_simultaneous_locks_exactly_one_gets_lease():
    first, second, job_id = _users_and_job("editor_pg_lock")
    barrier = Barrier(2)

    def locker(user_id):
        db = SessionLocal()
        try:
            document = db.query(EditorDocument).filter(EditorDocument.job_id == job_id).one()
            barrier.wait(timeout=5)
            result = acquire_lock(db, document, user_id)
            db.commit()
            return result["acquired"]
        finally:
            db.close()

    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(locker, [first.id, second.id]))
    assert sorted(outcomes) == [False, True]
