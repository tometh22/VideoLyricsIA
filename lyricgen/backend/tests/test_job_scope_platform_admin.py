"""A platform admin (role=admin) who is NOT in SUPER_ADMIN_USERS must reach the
same job-scoped endpoints the editor and campaign approval already grant.

Incidente 14-sep-2026 (Illapu "Vuelvo para vivir", campaña Chile): la
revisora editó 77 veces por PATCH /editor (rol admin, otro tenant), la
aprobación de campaña la dejaba pasar, pero /language-resolution devolvía
404 porque exigía super-admin → el gate de discrepancia era irresoluble y
"Aprobar" quedaba bloqueado con un cartel de "no coincide con la referencia".
"""

import uuid

from tests.conftest import auth


def _regular_user_job(client):
    from database import Job, SessionLocal, User

    username = f"scope_{uuid.uuid4().hex[:6]}"
    res = client.post("/auth/register", json={
        "username": username, "password": "testpass12345",
        "email": f"{username}@test.com",
    })
    assert res.status_code == 200, res.text
    s = SessionLocal()
    try:
        u = s.query(User).filter(User.username == username).first()
        job_id = uuid.uuid4().hex[:12]
        s.add(Job(
            job_id=job_id, user_id=u.id, tenant_id=u.tenant_id,
            artist="Illapu", song_title="Vuelvo para vivir", style="auto",
            filename="song.wav", status="transcribed_pending",
            current_step="editing", progress=0, delivery_profile="youtube",
            segments_json=[{"start": 0.0, "end": 2.0, "text": "vuelvo a casa"}],
            segments_revision=0,
        ))
        s.commit()
        return job_id, u.tenant_id
    finally:
        s.close()


def _not_super_admin(monkeypatch):
    # role=admin sigue siendo admin de plataforma; NO está en el allowlist.
    monkeypatch.setenv("SUPER_ADMIN_USERS", "alguien@otro.test")
    monkeypatch.setenv("ENVIRONMENT", "staging")


def test_language_resolution_open_to_platform_admin_cross_tenant(client, admin_token, monkeypatch):
    _not_super_admin(monkeypatch)
    job_id, tenant = _regular_user_job(client)
    me = client.get("/auth/me", headers=auth(admin_token)).json()
    assert me["role"] == "admin" and me["tenant_id"] != tenant
    res = client.post(f"/jobs/{job_id}/language-resolution", headers=auth(admin_token),
                      json={"base_revision": 0})
    assert res.status_code == 200, res.text
    assert res.json()["ok"] is True


def test_quality_acknowledge_open_to_platform_admin_cross_tenant(client, admin_token, monkeypatch):
    _not_super_admin(monkeypatch)
    job_id, _tenant = _regular_user_job(client)
    res = client.post(
        f"/jobs/{job_id}/transcription-quality/acknowledge", headers=auth(admin_token),
        json={"base_revision": 0, "window_ids": ["qw_x"], "decision": "confirmed",
              "idempotency_key": uuid.uuid4().hex},
    )
    assert res.status_code != 404, res.text


def test_save_segments_open_to_platform_admin_cross_tenant(client, admin_token, monkeypatch):
    _not_super_admin(monkeypatch)
    job_id, _tenant = _regular_user_job(client)
    res = client.post(f"/jobs/{job_id}/save-segments", headers=auth(admin_token),
                      json={"segments": [{"start": 0.0, "end": 2.0, "text": "vuelvo a casa"}]})
    assert res.status_code == 200, res.text


def test_regular_user_still_isolated_by_tenant(client, user_token):
    job_id, _tenant = _regular_user_job(client)
    res = client.post(f"/jobs/{job_id}/language-resolution", headers=auth(user_token),
                      json={"base_revision": 0})
    assert res.status_code == 404
