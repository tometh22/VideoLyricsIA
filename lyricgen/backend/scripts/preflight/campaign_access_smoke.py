"""Staging-only authenticated grant/revoke smoke, with a disposable reviewer.

Uses the existing dedicated smoke administrator. Never renders or modifies a
campaign. The temporary account is revoked and soft-deleted even after failure.
"""
import os
import secrets
import time
import uuid

import requests

API = "https://api-staging-9b82.up.railway.app"
ORIGIN = "https://staging.genly.pro"


def main():
    expected = os.environ["EXPECTED_RELEASE"]
    session = requests.Session()
    session.headers["Origin"] = ORIGIN
    for _ in range(60):
        try:
            response = session.get(API + "/health/deploy", timeout=20)
            data = response.json()
            if response.ok and data.get("env") == "staging" and data.get("release") == expected:
                break
        except (requests.RequestException, ValueError):
            pass
        time.sleep(10)
    else:
        raise RuntimeError("The expected staging API release is not ready")

    def call(method, path, token=None, expected_status=200, **kwargs):
        headers = {"Authorization": "Bearer " + token} if token else {}
        r = session.request(method, API + path, headers=headers, timeout=45, **kwargs)
        if r.status_code != expected_status:
            raise RuntimeError(f"{method} {path}: expected {expected_status}, got {r.status_code}")
        assert r.headers.get("Access-Control-Allow-Origin") == ORIGIN, "Missing staging CORS response"
        return r.json()

    admin = call("POST", "/auth/login", json={
        "username": os.environ["PREFLIGHT_USERNAME"], "password": os.environ["PREFLIGHT_PASSWORD"],
    })["token"]
    assert call("GET", "/auth/me", admin)["role"] == "admin"
    workspace = "universal_music"
    options = call("GET", "/admin/user-assignment-options", admin)
    campaign = next(c for c in options["campaigns"] if c["tenant_id"] != workspace)
    username = "campaign_access_smoke_" + uuid.uuid4().hex[:12]
    password = secrets.token_urlsafe(30) + "Aa1!"
    user_id = None
    try:
        created = call("POST", "/admin/users", admin, json={
            "username": username, "password": password, "role": "user",
            "plan_id": "free", "tenant_id": workspace, "ai_authorized": False,
        })
        user_id = created["id"]
        reviewer = call("POST", "/auth/login", json={"username": username, "password": password})["token"]
        base = f"/admin/users/{user_id}/campaign-access"
        path = base + "/" + campaign["id"]
        listed = call("GET", base, admin, params={"search": campaign["name"]})
        assert listed["campaigns_enabled"] and any(c["id"] == campaign["id"] for c in listed["items"])
        call("GET", "/batch/campaigns/" + campaign["id"], reviewer, expected_status=404)
        granted = call("PUT", path, admin, json={"enabled": True, "expected_granted": False})
        assert granted["granted"]
        assert call("GET", base, admin, params={"granted_only": True})["items"][0]["granted"]
        assert campaign["id"] in {c["id"] for c in call("GET", "/batch/campaigns", reviewer)["items"]}
        call("GET", "/batch/campaigns/" + campaign["id"], reviewer)
        call("GET", "/batch/campaigns/" + campaign["id"] + "/review-queue", reviewer, params={"limit": 1})
        call("PUT", path, reviewer, expected_status=403, json={"enabled": False, "expected_granted": True})
        call("PUT", path, admin, json={"enabled": False, "expected_granted": True})
        call("GET", "/batch/campaigns/" + campaign["id"], reviewer, expected_status=404)
        print("PASS: authenticated admin grant, reviewer visibility/review queue, non-admin denial, and revocation; staging CORS verified")
    finally:
        if user_id is not None:
            # Re-read the persisted state: also handles a lost mutation response.
            base = f"/admin/users/{user_id}/campaign-access"
            try:
                grants = call("GET", base, admin, params={"granted_only": True})["items"]
                for grant in grants:
                    call("PUT", base + "/" + grant["id"], admin, json={"enabled": False, "expected_granted": True})
            finally:
                call("DELETE", f"/admin/users/{user_id}", admin)
                print("Disposable reviewer deactivated and anonymized")


if __name__ == "__main__":
    main()
