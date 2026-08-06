from types import SimpleNamespace

from auth import editor_v2_enabled


def _clear_editor_environment(monkeypatch):
    for key in (
        "ENVIRONMENT", "RAILWAY_ENVIRONMENT_NAME", "VERCEL_ENV", "ENV",
        "EDITOR_V2_GLOBALLY_ENABLED", "EDITOR_V2_TENANTS",
    ):
        monkeypatch.delenv(key, raising=False)


def test_editor_v2_production_rollout_is_tenant_scoped_even_for_admin(monkeypatch):
    _clear_editor_environment(monkeypatch)
    monkeypatch.setenv("ENVIRONMENT", "production")
    user = SimpleNamespace(role="admin", tenant_id="not-in-canary", billing_group=None)
    assert editor_v2_enabled(user) is False

    monkeypatch.setenv("EDITOR_V2_TENANTS", "pilot-team")
    user.tenant_id = "pilot-team"
    assert editor_v2_enabled(user) is True


def test_editor_v2_is_enabled_for_the_staging_environment(monkeypatch):
    _clear_editor_environment(monkeypatch)
    monkeypatch.setenv("RAILWAY_ENVIRONMENT_NAME", "staging")
    assert editor_v2_enabled(SimpleNamespace(tenant_id="any-team", billing_group=None)) is True
