"""ProRes/Drive gating por billing_group.

Regresión real (2026-06-03): al mover a los operadores de Universal del
tenant 'umusic' a 'universal_argentina', perdieron ProRes porque
PRORES_TENANTS sólo listaba el tenant viejo. Fix: gatear también por
billing_group, así toda la cuenta Universal (AR + CL + futuros países)
hereda el acceso con PRORES_TENANTS=umg,universal_music.
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


def test_prores_via_billing_group(monkeypatch):
    """Usuario en universal_argentina pero con billing_group universal_music
    obtiene ProRes cuando el GRUPO está en la lista (no su tenant)."""
    monkeypatch.setattr(auth, "PRORES_TENANTS", {"umg", "universal_music"})
    u = _stub_user(tenant_id="universal_argentina", billing_group="universal_music")
    assert auth.has_prores_access(u) is True


def test_prores_via_tenant_still_works(monkeypatch):
    """El match por tenant sigue funcionando (compat)."""
    monkeypatch.setattr(auth, "PRORES_TENANTS", {"umg"})
    assert auth.has_prores_access(_stub_user(tenant_id="umg")) is True


def test_prores_denied_without_match(monkeypatch):
    monkeypatch.setattr(auth, "PRORES_TENANTS", {"umg", "universal_music"})
    u = _stub_user(tenant_id="otro", billing_group="otra_cuenta")
    assert auth.has_prores_access(u) is False


def test_prores_dict_user_billing_group(monkeypatch):
    """get_current_user devuelve un dict — el match por grupo también
    funciona sobre el dict (camino real del feature flag prores_export)."""
    monkeypatch.setattr(auth, "PRORES_TENANTS", {"universal_music"})
    d = {"role": "user", "tenant_id": "universal_chile", "billing_group": "universal_music"}
    assert auth.has_prores_access(d) is True


def test_drive_via_billing_group(monkeypatch):
    monkeypatch.setattr(auth, "DRIVE_ENABLED_TENANTS", {"universal_music"})
    u = _stub_user(tenant_id="universal_argentina", billing_group="universal_music")
    assert auth.has_drive_access(u) is True
