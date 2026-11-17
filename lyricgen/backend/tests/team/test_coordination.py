import uuid
from datetime import timedelta

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from auth import get_current_user
from database import (
    BatchCampaign,
    BatchCampaignItem,
    EditorDocument,
    Job,
    TeamActivity,
    TeamAssignment,
    TeamCoordinator,
    User,
    get_db,
)
import team_coordination as team


@pytest.fixture
def env(db, monkeypatch):
    tenant = "team-" + uuid.uuid4().hex[:8]
    monkeypatch.setenv("BATCH_CAMPAIGN_ENABLED", "1")
    monkeypatch.setenv("BATCH_CAMPAIGN_SCOPES", tenant)
    users = []
    for i in range(4):
        u = User(
            username=f"{tenant}-{i}",
            hashed_password="unused",
            tenant_id=tenant if i < 3 else "other",
            role="admin" if i == 0 else "user",
            is_active=True,
        )
        db.add(u)
        db.flush()
        users.append(u)
    campaign = BatchCampaign(
        id=uuid.uuid4().hex[:12],
        tenant_id=tenant,
        created_by=users[0].id,
        name="Campaña",
        status="active",
    )
    db.add(campaign)
    db.flush()
    items = []
    for i, status in enumerate(
        ["transcribed", "pending_review", "done", "lyrics_approved", "discarded"]
    ):
        item = BatchCampaignItem(
            id=str(uuid.uuid4()),
            campaign_id=campaign.id,
            tenant_id=tenant,
            ordinal=i,
            filename=f"audio-{i}.wav",
            title=f"Canción {i}",
            artist="Artista",
            size_bytes=100,
            sha256=f"{i:064d}",
            upload_state="uploaded",
        )
        db.add(item)
        db.flush()
        db.add(
            Job(
                job_id=uuid.uuid4().hex[:12],
                user_id=users[0].id,
                tenant_id=tenant,
                campaign_id=campaign.id,
                campaign_item_id=item.id,
                artist="Artista",
                filename=item.filename,
                status=status,
            )
        )
        items.append(item)
    db.commit()
    actor = {"id": users[0].id, "tenant_id": tenant, "role": "admin"}
    app = FastAPI()
    app.include_router(team.router)
    app.dependency_overrides[get_db] = lambda: db
    app.dependency_overrides[get_current_user] = lambda: actor
    item_ids = [item.id for item in items]
    user_ids = [user.id for user in users]
    campaign_id = campaign.id
    try:
        with TestClient(app) as client:
            yield client, db, actor, users, campaign, items
    finally:
        # These HTTP calls commit. Remove only this fixture's data so later
        # campaign fixtures and aggregate tests see a clean shared CI database.
        db.rollback()
        job_ids = db.query(Job.job_id).filter(Job.campaign_id == campaign_id)
        db.query(TeamActivity).filter_by(tenant_id=tenant).delete(
            synchronize_session=False
        )
        db.query(TeamCoordinator).filter_by(tenant_id=tenant).delete(
            synchronize_session=False
        )
        db.query(TeamAssignment).filter(TeamAssignment.item_id.in_(item_ids)).delete(
            synchronize_session=False
        )
        db.query(EditorDocument).filter(EditorDocument.job_id.in_(job_ids)).delete(
            synchronize_session=False
        )
        db.query(Job).filter_by(campaign_id=campaign_id).delete(
            synchronize_session=False
        )
        db.query(BatchCampaignItem).filter(BatchCampaignItem.id.in_(item_ids)).delete(
            synchronize_session=False
        )
        db.query(BatchCampaign).filter_by(id=campaign_id).delete(
            synchronize_session=False
        )
        db.query(User).filter(User.id.in_(user_ids)).delete(synchronize_session=False)
        db.commit()


def payload(items, assignee, version=0):
    return {
        "items": [{"id": item.id, "version": version} for item in items],
        "assignee_id": assignee,
        "priority": "high",
        "due_date": "2026-11-20",
    }


def test_assign_persists_and_preserves_real_approval(env):
    c, db, actor, users, campaign, items = env
    r = c.post("/team/assignments", json=payload(items, users[1].id))
    assert r.status_code == 200, r.text
    assert set(r.json()["skipped"]) == {items[2].id, items[4].id}
    assert db.get(TeamAssignment, items[0].id).assignee_id == users[1].id
    assert (
        db.query(TeamActivity)
        .filter_by(tenant_id=actor["tenant_id"], action="assigned")
        .count()
        == 3
    )
    summary = c.get("/team/dashboard").json()
    assert summary["counts"] == {
        "pending": 1,
        "review": 1,
        "done": 1,
        "processing": 1,
        "discarded": 1,
    }
    db.expire_all()
    assert c.get("/team/tasks").json()["items"][0]["version"] == 1


def test_operator_scope_and_permissions(env):
    c, db, actor, users, campaign, items = env
    c.post("/team/assignments", json=payload([items[0]], users[1].id))
    actor.update(id=users[1].id, role="user")
    assert c.get("/team/tasks").json()["total"] == 1
    assert c.get("/team/dashboard").json()["counts"] == {"pending": 1}
    assert c.get("/team/tasks?tenant_id=other").status_code == 403
    assert (
        c.post("/team/assignments", json=payload([items[1]], users[1].id)).status_code
        == 403
    )
    assert (
        c.put(f"/team/coordinators/{users[1].id}", json={"enabled": True}).status_code
        == 403
    )
    actor["id"] = users[2].id
    assert (
        c.patch(
            f"/team/tasks/{items[0].id}", json={"version": 1, "action": "start"}
        ).status_code
        == 403
    )
    assert c.get("/team/tasks").json()["total"] == 0


def test_grant_revoke_and_cross_tenant_assignee(env):
    c, db, actor, users, campaign, items = env
    assert (
        c.post("/team/assignments", json=payload([items[0]], users[3].id)).status_code
        == 422
    )
    assert (
        c.put(f"/team/coordinators/{users[1].id}", json={"enabled": True}).status_code
        == 200
    )
    actor.update(id=users[1].id, role="user")
    assert c.get("/team/access").json()["can_manage"] is True
    assert (
        c.post("/team/assignments", json=payload([items[0]], users[2].id)).status_code
        == 200
    )
    actor.update(id=users[0].id, role="admin")
    c.put(f"/team/coordinators/{users[1].id}", json={"enabled": False})
    actor.update(id=users[1].id, role="user")
    assert c.get("/team/access").json()["can_manage"] is False
    assert (
        c.post("/team/assignments", json=payload([items[1]], users[2].id)).status_code
        == 403
    )


def test_conflict_rolls_back_entire_bulk_assignment(env):
    c, db, actor, users, campaign, items = env
    ordered = sorted([items[0], items[1]], key=lambda i: i.id)
    c.post("/team/assignments", json=payload([ordered[1]], users[1].id))
    r = c.post("/team/assignments", json=payload(ordered, users[2].id))
    assert r.status_code == 409
    assert db.get(TeamAssignment, ordered[0].id) is None
    assert db.get(TeamAssignment, ordered[1].id).assignee_id == users[1].id


def test_work_notes_block_and_actual_job_completion(env):
    c, db, actor, users, campaign, items = env
    c.post("/team/assignments", json=payload([items[0]], users[1].id))
    actor.update(id=users[1].id, role="user")
    path = f"/team/tasks/{items[0].id}"
    assert (
        c.patch(
            path, json={"version": 1, "action": "block", "blocked_reason": "  "}
        ).status_code
        == 422
    )
    assert (
        c.patch(
            path, json={"version": 1, "action": "start", "note": "Revisar estribillo"}
        ).status_code
        == 200
    )
    assert c.get("/team/tasks").json()["items"][0]["status"] == "working"
    assert c.patch(path, json={"version": 1, "action": "note"}).status_code == 409
    assert (
        c.patch(
            path,
            json={"version": 2, "action": "block", "blocked_reason": "Falta letra"},
        ).status_code
        == 200
    )
    assert c.get("/team/tasks").json()["items"][0]["status"] == "blocked"
    job = db.query(Job).filter_by(campaign_item_id=items[0].id).one()
    assert job.status == "transcribed"
    job.status = "done"
    db.commit()
    assert c.get("/team/tasks").json()["items"][0]["status"] == "done"
    assert c.patch(path, json={"version": 3, "action": "unblock"}).status_code == 409


def test_restore_history_is_not_treated_as_discard(env):
    c, db, actor, users, campaign, items = env
    items[0].discard_record = {"reason": "old", "restored_at": "2026-11-17"}
    db.commit()
    assert c.post("/team/assignments", json=payload([items[0]], users[1].id)).json()[
        "assigned"
    ] == [items[0].id]
    assert c.get("/team/tasks?state=pending").json()["total"] == 1


def test_filters_pagination_and_closed_campaigns(env):
    c, db, actor, users, campaign, items = env
    assert c.get("/team/tasks?limit=2&offset=2").json()["items"][0]["id"] == items[2].id
    assert c.get("/team/tasks?search=Canci%C3%B3n%201").json()["total"] == 1
    assert c.get("/team/tasks?state=done").json()["total"] == 1
    assert c.get("/team/tasks?campaign_id=unknown").json()["total"] == 0
    assert c.get("/team/tasks?limit=1001").status_code == 422
    campaign.status = "completed"
    db.commit()
    assert c.post("/team/assignments", json=payload([items[0]], users[1].id)).json()[
        "skipped"
    ] == [items[0].id]


def test_live_editor_activity_counts_as_work(env):
    c, db, actor, users, campaign, items = env
    job = db.query(Job).filter_by(campaign_item_id=items[0].id).one()
    db.add(
        EditorDocument(
            job_id=job.job_id,
            tenant_id=actor["tenant_id"],
            current_segments=[],
            original_segments=[],
            lock_user_id=users[1].id,
            lock_expires_at=team._now() + timedelta(minutes=5),
        )
    )
    db.commit()
    assert c.get("/team/tasks?state=working").json()["total"] == 1


def test_inactive_assignee_and_cross_team_item_are_rejected(env):
    c, db, actor, users, campaign, items = env
    users[1].is_active = False
    db.commit()
    assert (
        c.post("/team/assignments", json=payload([items[0]], users[1].id)).status_code
        == 422
    )
    items[0].tenant_id = "other"
    db.commit()
    assert (
        c.post("/team/assignments", json=payload([items[0]], users[2].id)).status_code
        == 404
    )
    assert (
        c.patch(
            f"/team/tasks/{items[0].id}", json={"version": 1, "action": "start"}
        ).status_code
        == 404
    )


def test_reassignment_revokes_old_assignee_work_permission(env):
    c, db, actor, users, campaign, items = env
    c.post("/team/assignments", json=payload([items[0]], users[1].id))
    assert (
        c.post(
            "/team/assignments", json=payload([items[0]], users[2].id, version=1)
        ).status_code
        == 200
    )
    actor.update(id=users[1].id, role="user")
    assert (
        c.patch(
            f"/team/tasks/{items[0].id}", json={"version": 2, "action": "start"}
        ).status_code
        == 403
    )
    assert c.get("/team/tasks").json()["total"] == 0


def test_simultaneous_first_assignment_has_one_winner(env):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier
    from fastapi import HTTPException
    from database import SessionLocal

    c, db, actor, users, campaign, items = env
    if db.bind.dialect.name != "postgresql":
        pytest.skip("Row-level locking requires PostgreSQL")
    barrier = Barrier(2)
    requests = [team.AssignRequest(**payload([items[0]], u.id)) for u in users[1:3]]

    def execute(body):
        with SessionLocal() as session:
            barrier.wait(timeout=10)
            try:
                team.assign(body, current_user=actor, db=session)
                return 200
            except HTTPException as error:
                return error.status_code

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(execute, requests))
    assert sorted(results) == [200, 409]
    db.expire_all()
    assert db.get(TeamAssignment, items[0].id).version == 1
