"""Gating del add-on premium "Escenas" (has_scenes_access).

Modelo de créditos (2026-06): Escenas es PÚBLICO por default
(SCENES_GLOBALLY_ENABLED=1) — lo gobierna el costo en créditos, no una allowlist.
Estos tests del allowlist/rollback corren con SCENES_GLOBALLY_ENABLED=0 (el
esquema viejo: admin O SCENES_ENABLED_TENANTS). El comportamiento público (flag
ON) está cubierto en test_scenes_credits.py.
"""
import auth


def _stub_user(role="user", tenant_id="default", billing_group=None):
    class U:
        pass
    u = U()
    u.role = role
    u.tenant_id = tenant_id
    u.billing_group = billing_group
    return u


def test_admin_always_has_access(monkeypatch):
    monkeypatch.setattr(auth, "SCENES_ENABLED_TENANTS", set())
    assert auth.has_scenes_access(_stub_user(role="admin")) is True


def test_default_empty_denies_non_admin(monkeypatch):
    monkeypatch.setenv("SCENES_GLOBALLY_ENABLED", "0")  # rollback: gating por allowlist
    monkeypatch.setattr(auth, "SCENES_ENABLED_TENANTS", set())
    assert auth.has_scenes_access(_stub_user(tenant_id="umg")) is False


def test_access_via_tenant(monkeypatch):
    monkeypatch.setenv("SCENES_GLOBALLY_ENABLED", "0")
    monkeypatch.setattr(auth, "SCENES_ENABLED_TENANTS", {"umg"})
    assert auth.has_scenes_access(_stub_user(tenant_id="umg")) is True


def test_access_via_billing_group(monkeypatch):
    monkeypatch.setenv("SCENES_GLOBALLY_ENABLED", "0")
    monkeypatch.setattr(auth, "SCENES_ENABLED_TENANTS", {"universal_music"})
    u = _stub_user(tenant_id="universal_argentina", billing_group="universal_music")
    assert auth.has_scenes_access(u) is True


def test_access_dict_user(monkeypatch):
    monkeypatch.setenv("SCENES_GLOBALLY_ENABLED", "0")
    monkeypatch.setattr(auth, "SCENES_ENABLED_TENANTS", {"umg"})
    d = {"role": "user", "tenant_id": "umg", "billing_group": None}
    assert auth.has_scenes_access(d) is True


def test_none_user_denied():
    assert auth.has_scenes_access(None) is False
