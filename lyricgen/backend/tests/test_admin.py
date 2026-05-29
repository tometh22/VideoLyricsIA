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


def test_admin_create_user_in_reserved_tenant(client, admin_token):
    """POST /admin/users must bypass the reserved-tenant guard.

    `auth.create_user` defaults `enforce_reserved=True` to protect the
    public /auth/register endpoint from a self-registered user landing
    in a system tenant (`default`, `admin`) or squatting on a B2B name
    (`umg`, `warner`). But the admin POST is the "admin seeding script"
    path that the helper's own docstring says should bypass — the
    admin is EXPLICITLY assigning the tenant.

    Regression: prior to this fix, admin couldn't create UMG operators
    because `tenant_id="umg"` got blocked by the same guard meant for
    the public funnel. UMG onboarding 2026-05-28 hit this on the live
    /admin/users path.
    """
    res = client.post("/admin/users", headers=auth(admin_token), json={
        "username": "umg_operator_test",
        "password": "password123",
        "email": "op@umg-test.com",
        "plan_id": "250",
        "tenant_id": "umg",
    })
    assert res.status_code == 200, res.text
    assert res.json()["tenant_id"] == "umg"


def test_admin_create_multiple_users_in_same_tenant(client, admin_token):
    """Admin must be able to place N users into a shared team tenant.

    The "tenant already exists" collision check in auth.create_user
    fires alongside the reserved-tenant guard (both inside the same
    `if enforce_reserved` block). It protects the public funnel from
    a user attaching themselves to a strangers tenant, but blocks the
    intended B2B model where the admin creates a team workspace
    (e.g. all UMG operators on tenant_id="universal_music"). This test
    pins that admin-driven team-workspace creation works.
    """
    # Create the first user — claims the tenant.
    res1 = client.post("/admin/users", headers=auth(admin_token), json={
        "username": "umusic_op_one",
        "password": "password123",
        "email": "one@umusic-test.com",
        "plan_id": "250",
        "tenant_id": "umusic_test_team",
    })
    assert res1.status_code == 200, res1.text
    assert res1.json()["tenant_id"] == "umusic_test_team"

    # Create the second user — must SUCCEED, joining the same tenant.
    res2 = client.post("/admin/users", headers=auth(admin_token), json={
        "username": "umusic_op_two",
        "password": "password123",
        "email": "two@umusic-test.com",
        "plan_id": "250",
        "tenant_id": "umusic_test_team",
    })
    assert res2.status_code == 200, res2.text
    assert res2.json()["tenant_id"] == "umusic_test_team"
    # Different users, same tenant — they will share visibility via
    # _job_scope in production. Confirm the IDs are distinct.
    assert res1.json()["id"] != res2.json()["id"]


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


# ---------------------------------------------------------------------------
# Cost dashboard endpoints
# ---------------------------------------------------------------------------


def test_admin_cost_dashboard_endpoint(client, admin_token):
    res = client.get("/admin/cost", headers=auth(admin_token))
    assert res.status_code == 200
    data = res.json()
    assert "since_days" in data
    assert "grand_total_cost" in data
    assert "grand_total_calls" in data
    assert "tenants" in data
    assert isinstance(data["tenants"], list)


def test_admin_cost_dashboard_custom_window(client, admin_token):
    res = client.get("/admin/cost?since_days=7", headers=auth(admin_token))
    assert res.status_code == 200
    assert res.json()["since_days"] == 7


def test_admin_cost_per_tenant_endpoint(client, admin_token):
    res = client.get("/admin/cost/default", headers=auth(admin_token))
    assert res.status_code == 200
    data = res.json()
    assert data["tenant_id"] == "default"
    assert "total_cost" in data
    assert "total_calls" in data
    assert "by_tool" in data


def test_admin_cost_denied_for_regular_user(client, user_token):
    res = client.get("/admin/cost", headers=auth(user_token))
    assert res.status_code == 403
