"""Drive integration feature gating — `has_drive_access` policy.

Canary mode: el botón "Guardar en Drive" arranca admin-only. Cuando
querramos abrir a un tenant (UMG, Warner) se setea
DRIVE_ENABLED_TENANTS=<lista> y los users de ese tenant ven el botón
sin redeploy de código. Mismo patrón que PRORES_TENANTS.
"""
import pytest

import auth


def _stub_user(role="user", tenant_id="default"):
    class U:
        pass
    u = U()
    u.role = role
    u.tenant_id = tenant_id
    return u


def test_admin_always_has_access(monkeypatch):
    """Canary: admin bypassea siempre el allow-list (incluso vacío)."""
    monkeypatch.setattr(auth, "DRIVE_ENABLED_TENANTS", set())
    assert auth.has_drive_access(_stub_user(role="admin", tenant_id="default")) is True


def test_regular_user_default_denied(monkeypatch):
    """Canary core: user normal sin tenant allow-listed = denegado."""
    monkeypatch.setattr(auth, "DRIVE_ENABLED_TENANTS", set())
    assert auth.has_drive_access(_stub_user(role="user", tenant_id="default")) is False


def test_user_in_allowed_tenant_gets_access(monkeypatch):
    """Cuando el operator opta un tenant in, todos sus users entran."""
    monkeypatch.setattr(auth, "DRIVE_ENABLED_TENANTS", {"umg"})
    assert auth.has_drive_access(_stub_user(role="user", tenant_id="umg")) is True


def test_user_in_other_tenant_still_denied(monkeypatch):
    monkeypatch.setattr(auth, "DRIVE_ENABLED_TENANTS", {"umg"})
    assert auth.has_drive_access(_stub_user(role="user", tenant_id="warner")) is False


def test_tenant_match_case_insensitive(monkeypatch):
    monkeypatch.setattr(auth, "DRIVE_ENABLED_TENANTS", {"umg"})
    assert auth.has_drive_access(_stub_user(role="user", tenant_id="UMG")) is True
    assert auth.has_drive_access(_stub_user(role="user", tenant_id="Umg")) is True


def test_dict_user_shape_supported(monkeypatch):
    """get_current_user devuelve dict; el helper acepta ambos shapes."""
    monkeypatch.setattr(auth, "DRIVE_ENABLED_TENANTS", {"umg"})
    assert auth.has_drive_access({"role": "user", "tenant_id": "umg"}) is True
    assert auth.has_drive_access({"role": "admin", "tenant_id": "default"}) is True
    assert auth.has_drive_access({"role": "user", "tenant_id": "default"}) is False


def test_none_user_denied():
    """Defensive: caller pasa None (no autenticado) → False, no crash."""
    assert auth.has_drive_access(None) is False


def test_user_without_tenant_id_denied(monkeypatch):
    monkeypatch.setattr(auth, "DRIVE_ENABLED_TENANTS", {"umg"})
    assert auth.has_drive_access(_stub_user(role="user", tenant_id=None)) is False
    assert auth.has_drive_access(_stub_user(role="user", tenant_id="")) is False


def test_drive_and_prores_are_independent(monkeypatch):
    """Tener prores_access NO implica drive_access — son flags distintos
    para que el operator pueda abrir UMG a ProRes pero retener Drive."""
    monkeypatch.setattr(auth, "PRORES_TENANTS", {"umg"})
    monkeypatch.setattr(auth, "DRIVE_ENABLED_TENANTS", set())
    user = _stub_user(role="user", tenant_id="umg")
    assert auth.has_prores_access(user) is True
    assert auth.has_drive_access(user) is False
