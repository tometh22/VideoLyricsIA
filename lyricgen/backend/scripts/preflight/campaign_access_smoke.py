"""Authenticated, read-only staging smoke for the campaign-access panel.

Uses only the dedicated administrator's own permission panel. Never creates
users, grants campaign access, changes workspaces or renders jobs. Authentication
creates a login session; the finally block closes only that same session.
Grant/revoke writes are covered by isolated PostgreSQL and browser tests.
"""
import os
import time

import requests

API = "https://api-staging-9b82.up.railway.app"
ORIGIN = "https://staging.genly.pro"


def main():
    expected = os.environ["EXPECTED_RELEASE"]
    session = requests.Session()
    session.headers["Origin"] = ORIGIN
    for _ in range(60):
        try:
            response = session.get(API + "/health/ready", timeout=20)
            data = response.json()
            if (
                response.ok and data.get("env") == "staging"
                and data.get("release") == expected
                and data.get("fleet_release_match") is True
                and data.get("quality_gates", {}).get("fleet_runtime_token_match") is True
                and "fleet_runtime_token_mismatch" not in data.get("degraded_reasons", [])
            ):
                break
        except (requests.RequestException, ValueError):
            pass
        time.sleep(10)
    else:
        session.close()
        raise RuntimeError("The expected staging API release and fleet runtime are not ready")

    current_session_id = None

    def call(method, path, token=None, **kwargs):
        # Fail closed against business writes, including future edits to this smoke.
        allowed_auth = path == "/auth/login" or (
            current_session_id is not None and path == f"/auth/sessions/{current_session_id}/revoke"
        )
        if method != "GET" and not (method == "POST" and allowed_auth):
            raise RuntimeError("This smoke permits only reads and its own login/logout")
        headers = {"Authorization": "Bearer " + token} if token else {}
        r = session.request(method, API + path, headers=headers, timeout=45, **kwargs)
        if r.status_code != 200:
            raise RuntimeError(f"{method} {path}: expected 200, got {r.status_code}")
        assert r.headers.get("Access-Control-Allow-Origin") == ORIGIN, "Missing staging CORS response"
        return r.json()

    try:
        admin = call("POST", "/auth/login", json={
            "username": os.environ["PREFLIGHT_USERNAME"], "password": os.environ["PREFLIGHT_PASSWORD"],
        })["token"]
        sessions = call("GET", "/auth/sessions", admin)["sessions"]
        current = [s for s in sessions if s.get("current") is True]
        assert len(current) == 1, "Cannot identify this smoke's login session"
        assert type(current[0].get("id")) is int and current[0]["id"] > 0, "Invalid current session ID"
        current_session_id = current[0]["id"]
        me = call("GET", "/auth/me", admin)
        assert me["role"] == "admin"
        base = f"/admin/users/{me['id']}/campaign-access"
        listed = call("GET", base, admin, params={"limit": 1})
        assert listed["user_active"] and listed["campaigns_enabled"]
        assert listed["total"] > 0 and len(listed["items"]) == 1
        campaign = listed["items"][0]
        assert campaign["inherited"] == "admin" and campaign["has_access"]
        searched = call("GET", base, admin, params={"search": campaign["name"], "limit": 100})
        assert campaign["id"] in {c["id"] for c in searched["items"]}
        grants = call("GET", base, admin, params={"granted_only": True, "limit": 1})
        assert all(c["granted"] for c in grants["items"])
        print("PASS: authenticated read-only campaign-access listing, search, inherited permissions, grant filter and staging CORS")
    finally:
        try:
            if current_session_id is None:
                print("Session revocation NOT verified: current session was not identified; no revocation attempted")
            else:
                try:
                    closed = call("POST", f"/auth/sessions/{current_session_id}/revoke", admin)
                    assert closed.get("was_current") is True
                except Exception:
                    print("Session revocation NOT verified: closing this smoke's own session failed")
                    raise
                print("Closed only this smoke's own login session; no business data changed")
        finally:
            session.close()


if __name__ == "__main__":
    main()
