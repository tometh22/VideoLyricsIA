"""Tests for admin endpoints."""

from tests.conftest import auth


def test_admin_stats(client, admin_token):
    res = client.get("/admin/stats", headers=auth(admin_token))
    assert res.status_code == 200
    data = res.json()
    assert "users" in data
    assert "jobs" in data
    assert "revenue" in data
    assert "plans" in data


def test_admin_users(client, admin_token):
    res = client.get("/admin/users", headers=auth(admin_token))
    assert res.status_code == 200
    data = res.json()
    assert "total" in data
    assert "users" in data
    assert isinstance(data["users"], list)


def test_admin_create_user(client, admin_token):
    res = client.post("/admin/users", headers=auth(admin_token), json={
        "username": "admin_created",
        "password": "password123",
        "email": "admincreated@test.com",
        "plan_id": "250",
    })
    assert res.status_code == 200
    assert res.json()["plan"] == "250"


def test_admin_update_user(client, admin_token):
    # Create user first
    create_res = client.post("/admin/users", headers=auth(admin_token), json={
        "username": "to_update",
        "password": "password123",
    })
    user_id = create_res.json()["id"]

    # Update plan
    res = client.patch(f"/admin/users/{user_id}", headers=auth(admin_token), json={
        "plan_id": "500",
    })
    assert res.status_code == 200
    assert res.json()["plan"] == "500"


def test_admin_disable_user(client, admin_token):
    create_res = client.post("/admin/users", headers=auth(admin_token), json={
        "username": "to_disable",
        "password": "password123",
    })
    user_id = create_res.json()["id"]

    res = client.patch(f"/admin/users/{user_id}", headers=auth(admin_token), json={
        "is_active": False,
    })
    assert res.status_code == 200
    assert res.json()["is_active"] is False


def test_admin_denied_for_regular_user(client, user_token):
    res = client.get("/admin/stats", headers=auth(user_token))
    assert res.status_code == 403


def test_admin_jobs(client, admin_token):
    res = client.get("/admin/jobs", headers=auth(admin_token))
    assert res.status_code == 200
    assert "total" in res.json()


def test_admin_invoices(client, admin_token):
    res = client.get("/admin/invoices", headers=auth(admin_token))
    assert res.status_code == 200


def test_admin_audit_log(client, admin_token):
    res = client.get("/admin/audit", headers=auth(admin_token))
    assert res.status_code == 200
    assert isinstance(res.json(), list)


def test_admin_search_users(client, admin_token):
    client.post("/admin/users", headers=auth(admin_token), json={
        "username": "searchable_user",
        "password": "password123",
        "email": "searchable@test.com",
    })
    res = client.get("/admin/users?search=searchable", headers=auth(admin_token))
    assert res.status_code == 200
    assert res.json()["total"] >= 1
