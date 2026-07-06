"""Enterprise controls: maker-checker approval, audit API, tenant settings."""

import uuid
from datetime import datetime, timezone

import pytest

from tests.conftest import auth
from tests.test_youtube_publish import (
    _register, _seed_done_job, _connect_channel, _patch_enqueue, METADATA,
)


def _make_approver(user_id):
    from database import SessionLocal, User

    s = SessionLocal()
    try:
        s.query(User).filter(User.id == user_id).update({"can_approve_public": True})
        s.commit()
    finally:
        s.close()


def _set_tenant_approval(tenant_id, value=True):
    from database import SessionLocal, TenantSettings

    s = SessionLocal()
    try:
        row = s.query(TenantSettings).filter(TenantSettings.tenant_id == tenant_id).first()
        if row:
            row.settings_json = {"require_public_approval": value}
        else:
            s.add(TenantSettings(tenant_id=tenant_id, settings_json={"require_public_approval": value}))
        s.commit()
    finally:
        s.close()


def _admin_token(client):
    res = client.post("/auth/login", json={"username": "admin", "password": "testadmin123"})
    return res.json()["token"]


# ─── Maker-checker flow ──────────────────────────────────────────────

def test_public_publish_without_toggle_flows_through(client, monkeypatch):
    calls = _patch_enqueue(monkeypatch)
    token, user_id, tenant_id = _register(client)
    _connect_channel(user_id, tenant_id)
    job_id = _seed_done_job(user_id, tenant_id)

    res = client.post(f"/youtube/publish/{job_id}", headers=auth(token),
                      json={"metadata": METADATA, "privacy": "public", "include_short": False})
    assert res.status_code == 200
    assert res.json()[0]["status"] == "queued"
    assert len(calls) == 1


def test_public_publish_with_toggle_parks_pending(client, monkeypatch):
    calls = _patch_enqueue(monkeypatch)
    token, user_id, tenant_id = _register(client)
    _connect_channel(user_id, tenant_id)
    _set_tenant_approval(tenant_id)
    job_id = _seed_done_job(user_id, tenant_id)

    res = client.post(f"/youtube/publish/{job_id}", headers=auth(token),
                      json={"metadata": METADATA, "privacy": "public", "include_short": False})
    assert res.status_code == 200, res.text
    assert res.json()[0]["status"] == "pending_approval"
    assert calls == []  # nothing enqueued until approved

    # Unlisted is unaffected by the toggle.
    job2 = _seed_done_job(user_id, tenant_id)
    res2 = client.post(f"/youtube/publish/{job2}", headers=auth(token),
                       json={"metadata": METADATA, "privacy": "unlisted", "include_short": False})
    assert res2.json()[0]["status"] == "queued"


def test_approver_bypasses_toggle(client, monkeypatch):
    _patch_enqueue(monkeypatch)
    token, user_id, tenant_id = _register(client)
    _connect_channel(user_id, tenant_id)
    _set_tenant_approval(tenant_id)
    _make_approver(user_id)
    job_id = _seed_done_job(user_id, tenant_id)

    res = client.post(f"/youtube/publish/{job_id}", headers=auth(token),
                      json={"metadata": METADATA, "privacy": "public", "include_short": False})
    assert res.json()[0]["status"] == "queued"


def test_approve_flow(client, monkeypatch):
    from database import SessionLocal, PublishJob, AuditLog

    calls = _patch_enqueue(monkeypatch)
    token_maker, maker_id, tenant_id = _register(client)
    _connect_channel(maker_id, tenant_id)
    _set_tenant_approval(tenant_id)
    job_id = _seed_done_job(maker_id, tenant_id)

    pk = client.post(f"/youtube/publish/{job_id}", headers=auth(token_maker),
                     json={"metadata": METADATA, "privacy": "public", "include_short": False}).json()[0]["id"]

    # The maker (non-approver) can't approve their own request.
    assert client.post(f"/youtube/publish-jobs/{pk}/approve", headers=auth(token_maker)).status_code == 403

    # An approver in the same tenant can. (Simulate a teammate.)
    from database import User
    s = SessionLocal()
    try:
        s.query(User).filter(User.id == maker_id).update({"can_approve_public": True})
        s.commit()
    finally:
        s.close()

    res = client.post(f"/youtube/publish-jobs/{pk}/approve", headers=auth(token_maker))
    assert res.status_code == 200, res.text
    assert res.json()["status"] == "queued"
    assert calls == [pk]

    s = SessionLocal()
    try:
        row = s.query(PublishJob).filter(PublishJob.id == pk).one()
        assert row.approved_by == maker_id
        audit = (
            s.query(AuditLog).filter(AuditLog.action == "publish.approve_public")
            .order_by(AuditLog.id.desc()).first()
        )
        assert audit.detail["publish_job_id"] == pk
    finally:
        s.close()


def test_deny_flow_notifies_and_allows_retry(client, monkeypatch):
    from database import SessionLocal, PublishJob

    _patch_enqueue(monkeypatch)
    denials = []
    import emails
    monkeypatch.setattr(emails, "send_publish_denied", lambda *a, **k: denials.append(a))

    token, user_id, tenant_id = _register(client)
    _connect_channel(user_id, tenant_id)
    _set_tenant_approval(tenant_id)
    job_id = _seed_done_job(user_id, tenant_id)

    pk = client.post(f"/youtube/publish/{job_id}", headers=auth(token),
                     json={"metadata": METADATA, "privacy": "public", "include_short": False}).json()[0]["id"]

    _make_approver(user_id)
    res = client.post(f"/youtube/publish-jobs/{pk}/deny", headers=auth(token),
                      json={"reason": "Falta el arte oficial"})
    assert res.status_code == 200
    assert res.json()["status"] == "denied"
    assert res.json()["denial_reason"] == "Falta el arte oficial"
    assert len(denials) == 1

    # Denied is terminal → a new request is allowed.
    res2 = client.post(f"/youtube/publish/{job_id}", headers=auth(token),
                       json={"metadata": METADATA, "privacy": "public", "include_short": False})
    assert res2.status_code == 200


def test_cross_tenant_approval_is_404(client, monkeypatch):
    _patch_enqueue(monkeypatch)
    token_a, user_a, tenant_a = _register(client)
    token_b, user_b, _ = _register(client)
    _connect_channel(user_a, tenant_a)
    _set_tenant_approval(tenant_a)
    _make_approver(user_b)
    job_id = _seed_done_job(user_a, tenant_a)

    pk = client.post(f"/youtube/publish/{job_id}", headers=auth(token_a),
                     json={"metadata": METADATA, "privacy": "public", "include_short": False}).json()[0]["id"]

    assert client.post(f"/youtube/publish-jobs/{pk}/approve", headers=auth(token_b)).status_code == 404


def test_pending_list_and_policy(client, monkeypatch):
    _patch_enqueue(monkeypatch)
    token, user_id, tenant_id = _register(client)
    _connect_channel(user_id, tenant_id)
    _set_tenant_approval(tenant_id)
    job_id = _seed_done_job(user_id, tenant_id)

    policy = client.get("/youtube/publish-policy", headers=auth(token)).json()
    assert policy == {"require_public_approval": True, "can_approve": False}

    client.post(f"/youtube/publish/{job_id}", headers=auth(token),
                json={"metadata": METADATA, "privacy": "public", "include_short": False})
    pending = client.get("/youtube/publish/pending/list", headers=auth(token)).json()
    assert len(pending) == 1 and pending[0]["status"] == "pending_approval"

    _make_approver(user_id)
    policy2 = client.get("/youtube/publish-policy", headers=auth(token)).json()
    assert policy2["can_approve"] is True


# ─── Tenant settings admin API ───────────────────────────────────────

def test_tenant_settings_requires_an_approver(client, monkeypatch):
    _patch_enqueue(monkeypatch)
    admin = _admin_token(client)
    _, user_id, tenant_id = _register(client)

    res = client.put(f"/admin/tenants/{tenant_id}/settings", headers=auth(admin),
                     json={"require_public_approval": True})
    assert res.status_code == 400  # no approvers yet → deadlock guard

    _make_approver(user_id)
    res = client.put(f"/admin/tenants/{tenant_id}/settings", headers=auth(admin),
                     json={"require_public_approval": True})
    assert res.status_code == 200

    got = client.get(f"/admin/tenants/{tenant_id}/settings", headers=auth(admin)).json()
    assert got == {"require_public_approval": True}


# ─── Audit API ───────────────────────────────────────────────────────

def test_audit_filters_and_pagination(client, monkeypatch):
    from database import SessionLocal, AuditLog

    admin = _admin_token(client)
    marker = f"testaudit.{uuid.uuid4().hex[:6]}"
    s = SessionLocal()
    try:
        for i in range(5):
            s.add(AuditLog(user_id=1, action=f"{marker}.event", detail={"i": i}))
        s.add(AuditLog(user_id=2, action="other.event", detail={}))
        s.commit()
    finally:
        s.close()

    res = client.get(f"/admin/audit?action={marker}&limit=2&offset=0", headers=auth(admin))
    assert res.status_code == 200
    data = res.json()
    assert data["total"] == 5
    assert len(data["entries"]) == 2
    assert all(e["action"].startswith(marker) for e in data["entries"])

    page2 = client.get(f"/admin/audit?action={marker}&limit=2&offset=4", headers=auth(admin)).json()
    assert len(page2["entries"]) == 1


def test_audit_csv_export_is_audited(client):
    from database import SessionLocal, AuditLog

    admin = _admin_token(client)
    res = client.get("/admin/audit/export.csv?action=job.", headers=auth(admin))
    assert res.status_code == 200
    assert res.headers["content-type"].startswith("text/csv")
    body = res.text
    assert body.splitlines()[0] == "id,created_at,user_id,action,ip_address,detail"

    s = SessionLocal()
    try:
        entry = (
            s.query(AuditLog).filter(AuditLog.action == "admin.audit_export")
            .order_by(AuditLog.id.desc()).first()
        )
        assert entry is not None and entry.detail["action"] == "job."
    finally:
        s.close()
