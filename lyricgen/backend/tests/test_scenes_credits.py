"""Unit tests for the Escenas credits model helpers in auth.py:
scenes_credit_cost, _scenes_globally_enabled, has_scenes_access. Pure (env +
plain user dicts), no DB/network."""

import auth


def test_credit_cost_default(monkeypatch):
    monkeypatch.delenv("SCENES_CREDIT_COST", raising=False)
    assert auth.scenes_credit_cost() == 3


def test_credit_cost_env_override(monkeypatch):
    monkeypatch.setenv("SCENES_CREDIT_COST", "4")
    assert auth.scenes_credit_cost() == 4


def test_credit_cost_invalid_falls_back(monkeypatch):
    monkeypatch.setenv("SCENES_CREDIT_COST", "abc")
    assert auth.scenes_credit_cost() == 3


def test_credit_cost_floor_is_one(monkeypatch):
    # 0 / negatives clamp to 1 (1 = Escenas doesn't cost extra, e.g. a perk).
    monkeypatch.setenv("SCENES_CREDIT_COST", "0")
    assert auth.scenes_credit_cost() == 1


def test_globally_enabled_default_on(monkeypatch):
    monkeypatch.delenv("SCENES_GLOBALLY_ENABLED", raising=False)
    assert auth._scenes_globally_enabled() is True


def test_globally_enabled_off(monkeypatch):
    monkeypatch.setenv("SCENES_GLOBALLY_ENABLED", "0")
    assert auth._scenes_globally_enabled() is False


def test_access_admin_always(monkeypatch):
    # Admin passes even with the global kill-switch off.
    monkeypatch.setenv("SCENES_GLOBALLY_ENABLED", "0")
    assert auth.has_scenes_access({"role": "admin"}) is True


def test_access_public_when_globally_enabled(monkeypatch):
    monkeypatch.setenv("SCENES_GLOBALLY_ENABLED", "1")
    user = {"role": "user", "tenant_id": "acme", "billing_group": None}
    assert auth.has_scenes_access(user) is True


def test_access_blocked_when_global_off_and_not_allowlisted(monkeypatch):
    monkeypatch.setenv("SCENES_GLOBALLY_ENABLED", "0")
    monkeypatch.setattr(auth, "SCENES_ENABLED_TENANTS", {"universal_music"})
    user = {"role": "user", "tenant_id": "acme", "billing_group": None}
    assert auth.has_scenes_access(user) is False


def test_access_allowlist_fallback_when_global_off(monkeypatch):
    # Rollback mode (global off) → old allowlist still works for B2B.
    monkeypatch.setenv("SCENES_GLOBALLY_ENABLED", "0")
    monkeypatch.setattr(auth, "SCENES_ENABLED_TENANTS", {"universal_music"})
    user = {"role": "user", "tenant_id": "x", "billing_group": "universal_music"}
    assert auth.has_scenes_access(user) is True


def test_access_none_user():
    assert auth.has_scenes_access(None) is False
