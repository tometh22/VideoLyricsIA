"""Reviewer-pilot test copies: isolation, agent authorship and no approval.

Every assertion here reads back persisted rows. A caller verifying a saved
pilot copy must never have to trust a client-supplied flag.
"""

import uuid

import pytest

from auth import create_user
from database import AuditLog, EditorDocument, Job, SessionLocal
from editor import (
    PILOT_AGENT_ROLE,
    actor_for,
    approve_document,
    assert_pilot_actor,
    ensure_document,
    save_document,
    serialize_document,
)
from tests.conftest import auth

TENANT = "pilot_tenant"
BASELINE = [
    {"start": 0.0, "end": 1.0, "text": "una"},
    {"start": 1.0, "end": 2.0, "text": "dos"},
    {"start": 2.0, "end": 3.0, "text": "tres"},
]


def _source_job(db, *, tenant=TENANT, campaign_id=None):
    owner = create_user(
        db, f"pilot_owner_{uuid.uuid4().hex[:6]}", "testpass12345", None,
        tenant_id=tenant,
    )
    job_id = f"src{uuid.uuid4().hex[:9]}"
    db.add(Job(
        job_id=job_id, user_id=owner.id, tenant_id=tenant,
        artist="Artista", song_title="Cancion", filename="a.wav",
        style="oscuro", status="transcribed_pending", current_step="editing",
        delivery_profile="youtube", campaign_id=campaign_id,
        input_audio_sha256="a" * 64, machine_snapshot_required=True,
    ))
    db.commit()
    ensure_document(db, job_id, tenant, BASELINE)
    db.commit()
    return owner, job_id


def _agent(db, pilot_id, tenant=TENANT):
    agent = create_user(
        db, f"pilot-agent:{pilot_id}", "testpass12345", None, tenant_id=tenant,
    )
    agent.role = PILOT_AGENT_ROLE
    db.commit()
    return agent


def _copy_job(db, source_job_id, agent, pilot_id, tenant=TENANT):
    job_id = f"cpy{uuid.uuid4().hex[:9]}"
    source = db.query(Job).filter(Job.job_id == source_job_id).one()
    db.add(Job(
        job_id=job_id, user_id=agent.id, tenant_id=tenant,
        artist=source.artist, song_title=source.song_title,
        filename=source.filename, style=source.style,
        status="transcribed_pending", current_step="editing",
        delivery_profile="youtube", campaign_id=None, campaign_item_id=None,
        pilot_id=pilot_id, parent_job_id=source_job_id,
        input_audio_sha256=source.input_audio_sha256,
        machine_snapshot_required=False,
    ))
    db.commit()
    ensure_document(db, job_id, tenant, BASELINE)
    db.commit()
    return job_id


def test_agent_owns_its_copy_and_nobody_else_can_write_it():
    db = SessionLocal()
    try:
        pilot_id = f"p{uuid.uuid4().hex[:8]}"
        owner, source_job_id = _source_job(db)
        agent = _agent(db, pilot_id)
        copy_job_id = _copy_job(db, source_job_id, agent, pilot_id)
        job = db.query(Job).filter(Job.job_id == copy_job_id).one()
        document = db.query(EditorDocument).filter(
            EditorDocument.job_id == copy_job_id,
        ).one()

        assert_pilot_actor(db, job, agent.id)          # the owning agent may write

        with pytest.raises(ValueError, match="only_by_its_agent"):
            assert_pilot_actor(db, job, owner.id)      # a human may not

        source = db.query(Job).filter(Job.job_id == source_job_id).one()
        with pytest.raises(ValueError, match="only_edit_its_pilot_copy"):
            assert_pilot_actor(db, source, agent.id)   # nor the agent elsewhere
        assert document.revision == 0
    finally:
        db.close()


def test_saving_as_the_agent_persists_agent_authorship():
    db = SessionLocal()
    try:
        pilot_id = f"p{uuid.uuid4().hex[:8]}"
        _, source_job_id = _source_job(db)
        agent = _agent(db, pilot_id)
        copy_job_id = _copy_job(db, source_job_id, agent, pilot_id)
        job = db.query(Job).filter(Job.job_id == copy_job_id).one()
        document = db.query(EditorDocument).filter(
            EditorDocument.job_id == copy_job_id,
        ).one()

        before = serialize_document(db, document, job)
        assert before["actor_kind"] is None and before["agent_id"] is None
        assert before["pilot_id"] == pilot_id
        assert before["source_job_id"] == source_job_id
        assert before["campaign_id"] is None
        assert before["approved"] is False
        assert before["learning_eligible"] is False

        edited = [dict(row) for row in BASELINE]
        edited[1]["text"] = "DOS corregido"
        document, version, applied = save_document(
            db, job, document, agent.id, document.revision, edited, "manual",
        )
        db.commit()

        after = serialize_document(db, document, job)
        assert applied is True
        assert after["revision"] > before["revision"]
        assert after["actor_kind"] == "agent"
        assert after["agent_id"] == f"pilot-agent:{pilot_id}"
        assert [row["text"] for row in after["segments"]] == [
            "una", "DOS corregido", "tres",
        ]
        assert actor_for(db, version.created_by)["actor_kind"] == "agent"
    finally:
        db.close()


def test_editing_the_copy_never_touches_the_source():
    db = SessionLocal()
    try:
        pilot_id = f"p{uuid.uuid4().hex[:8]}"
        _, source_job_id = _source_job(db)
        agent = _agent(db, pilot_id)
        copy_job_id = _copy_job(db, source_job_id, agent, pilot_id)
        source_document = db.query(EditorDocument).filter(
            EditorDocument.job_id == source_job_id,
        ).one()
        source_before = (
            [dict(row) for row in source_document.current_segments],
            source_document.revision,
        )

        job = db.query(Job).filter(Job.job_id == copy_job_id).one()
        document = db.query(EditorDocument).filter(
            EditorDocument.job_id == copy_job_id,
        ).one()
        edited = [dict(row) for row in BASELINE]
        edited[0]["text"] = "cambio en la copia"
        save_document(db, job, document, agent.id, document.revision, edited, "manual")
        db.commit()

        db.expire_all()
        source_document = db.query(EditorDocument).filter(
            EditorDocument.job_id == source_job_id,
        ).one()
        assert (
            [dict(row) for row in source_document.current_segments],
            source_document.revision,
        ) == source_before
    finally:
        db.close()


def test_a_pilot_copy_can_never_be_approved():
    db = SessionLocal()
    try:
        pilot_id = f"p{uuid.uuid4().hex[:8]}"
        _, source_job_id = _source_job(db)
        agent = _agent(db, pilot_id)
        copy_job_id = _copy_job(db, source_job_id, agent, pilot_id)
        job = db.query(Job).filter(Job.job_id == copy_job_id).one()
        document = db.query(EditorDocument).filter(
            EditorDocument.job_id == copy_job_id,
        ).one()

        edited = [dict(row) for row in BASELINE]
        edited[2]["text"] = "tres corregido"
        with pytest.raises(ValueError, match="cannot_be_approved"):
            save_document(db, job, document, agent.id, document.revision, edited, "approve")
        db.rollback()
        with pytest.raises(ValueError, match="cannot_be_approved"):
            approve_document(db, job, agent.id)
        db.rollback()
        assert db.query(Job).filter(Job.job_id == copy_job_id).one().approved_at is None
    finally:
        db.close()


def test_copy_edits_stay_out_of_the_training_corpus():
    db = SessionLocal()
    try:
        pilot_id = f"p{uuid.uuid4().hex[:8]}"
        _, source_job_id = _source_job(db)
        agent = _agent(db, pilot_id)
        copy_job_id = _copy_job(db, source_job_id, agent, pilot_id)
        job = db.query(Job).filter(Job.job_id == copy_job_id).one()
        document = db.query(EditorDocument).filter(
            EditorDocument.job_id == copy_job_id,
        ).one()
        assert job.machine_snapshot_required is False

        edited = [dict(row) for row in BASELINE]
        edited[1]["text"] = "dos corregido"
        save_document(db, job, document, agent.id, document.revision, edited, "manual")
        db.commit()

        entries = db.query(AuditLog).filter(
            AuditLog.action == "lyrics.segments_diff",
        ).all()
        mine = [e for e in entries if (e.detail or {}).get("job_id") == copy_job_id]
        assert mine, "the bounded operational audit signal is still recorded"
        for entry in mine:
            # training_corpus.build_line_delta_audit emits these; the bounded
            # branch taken for machine_snapshot_required=False must not.
            assert "author_kind" not in entry.detail
            assert "human_certified" not in entry.detail
            assert "correction_summary" in entry.detail
    finally:
        db.close()


def test_endpoint_creates_an_isolated_copy_and_refuses_a_copy_of_a_copy(
    client, admin_token, monkeypatch,
):
    monkeypatch.setenv("ENVIRONMENT", "staging")
    db = SessionLocal()
    try:
        _, source_job_id = _source_job(db, tenant="pilot_api_tenant")
    finally:
        db.close()
    pilot_id = f"p{uuid.uuid4().hex[:8]}"

    created = client.post(
        "/pilot/test-copies",
        json={"source_job_id": source_job_id, "pilot_id": pilot_id},
        headers=auth(admin_token),
    )
    assert created.status_code == 200, created.text
    payload = created.json()
    assert payload["source_job_id"] == source_job_id
    assert payload["job_id"] != source_job_id
    assert payload["pilot_id"] == pilot_id
    assert payload["campaign_id"] is None
    assert payload["approved"] is False
    assert payload["learning_eligible"] is False
    assert payload["agent_id"] == f"pilot-agent:{pilot_id}"

    db = SessionLocal()
    try:
        copy_row = db.query(Job).filter(Job.job_id == payload["job_id"]).one()
        source_row = db.query(Job).filter(Job.job_id == source_job_id).one()
        assert copy_row.campaign_id is None and copy_row.campaign_item_id is None
        assert copy_row.machine_snapshot_required is False
        assert copy_row.parent_job_id == source_job_id
        assert copy_row.input_audio_sha256 == source_row.input_audio_sha256
        assert copy_row.status == "transcribed_pending"
        assert copy_row.video_url is None and copy_row.s3_keys is None
        # The source keeps its own enrolment and stays out of the pilot.
        assert source_row.pilot_id is None
        assert source_row.machine_snapshot_required is True
    finally:
        db.close()

    again = client.post(
        "/pilot/test-copies",
        json={"source_job_id": payload["job_id"], "pilot_id": pilot_id},
        headers=auth(admin_token),
    )
    assert again.status_code == 400


def test_endpoint_is_admin_only_and_refused_outside_test_environments(
    client, admin_token, user_token, monkeypatch,
):
    db = SessionLocal()
    try:
        _, source_job_id = _source_job(db, tenant="pilot_guard_tenant")
    finally:
        db.close()
    body = {"source_job_id": source_job_id, "pilot_id": "guardrail"}

    monkeypatch.setenv("ENVIRONMENT", "staging")
    assert client.post("/pilot/test-copies", json=body, headers=auth(user_token)).status_code == 403

    monkeypatch.setenv("ENVIRONMENT", "production")
    monkeypatch.delenv("RAILWAY_ENVIRONMENT_NAME", raising=False)
    assert client.post("/pilot/test-copies", json=body, headers=auth(admin_token)).status_code == 403
