"""Tests de endpoints de Escenas (multi-escena) vía TestClient.

Cubren la garantía de gating/entitlement que la auditoría marcó:
- /auth/me devuelve features.scenes (audit A6) → el refresh del front desbloquea
  sin re-login.
- features.scenes = admin OR SCENES_ENABLED_TENANTS.
"""


def test_auth_me_returns_features_scenes_admin(client, admin_token):
    r = client.get("/auth/me", headers={"Authorization": f"Bearer {admin_token}"})
    assert r.status_code == 200
    body = r.json()
    assert "features" in body, "/auth/me debe incluir features (audit A6)"
    # Admin siempre elegible (has_scenes_access).
    assert body["features"]["scenes"] is True
    # No rompimos los otros flags.
    assert "prores_export" in body["features"]
    assert "telemetry" in body["features"]


def test_auth_me_features_scenes_false_for_plain_user(client, user_token):
    r = client.get("/auth/me", headers={"Authorization": f"Bearer {user_token}"})
    assert r.status_code == 200
    # Default (SCENES_ENABLED_TENANTS vacío) → un user común NO es elegible.
    assert r.json()["features"]["scenes"] is False
