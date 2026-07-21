"""Gating de la feature Art Track (has_art_track_access).

A diferencia de Escenas (pública por default), Art Track se lanza APAGADO:
default OFF → solo admin la ve, hasta abrirla por env sin deploy
(ART_TRACK_GLOBALLY_ENABLED=1 global, o ART_TRACK_ENABLED_TENANTS=... por
tenant). Esto es lo que mantiene la feature invisible para UMG en prod hasta
que se decida prenderla.
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
    monkeypatch.setattr(auth, "ART_TRACK_ENABLED_TENANTS", set())
    monkeypatch.setenv("ART_TRACK_GLOBALLY_ENABLED", "0")
    assert auth.has_art_track_access(_stub_user(role="admin")) is True


def test_default_off_denies_umg(monkeypatch):
    # Default: sin global, sin allowlist → UMG NO ve la feature.
    monkeypatch.setattr(auth, "ART_TRACK_ENABLED_TENANTS", set())
    monkeypatch.setenv("ART_TRACK_GLOBALLY_ENABLED", "0")
    assert auth.has_art_track_access(_stub_user(tenant_id="universal_music")) is False


def test_default_off_denies_everyone_non_admin(monkeypatch):
    monkeypatch.setattr(auth, "ART_TRACK_ENABLED_TENANTS", set())
    monkeypatch.setenv("ART_TRACK_GLOBALLY_ENABLED", "0")
    assert auth.has_art_track_access(_stub_user(tenant_id="acme")) is False


def test_access_via_tenant_allowlist(monkeypatch):
    monkeypatch.setenv("ART_TRACK_GLOBALLY_ENABLED", "0")
    monkeypatch.setattr(auth, "ART_TRACK_ENABLED_TENANTS", {"universal_music"})
    assert auth.has_art_track_access(_stub_user(tenant_id="universal_music")) is True
    # otro tenant sigue sin acceso
    assert auth.has_art_track_access(_stub_user(tenant_id="acme")) is False


def test_access_via_billing_group(monkeypatch):
    monkeypatch.setenv("ART_TRACK_GLOBALLY_ENABLED", "0")
    monkeypatch.setattr(auth, "ART_TRACK_ENABLED_TENANTS", {"umg"})
    assert auth.has_art_track_access(
        _stub_user(tenant_id="chile", billing_group="umg")) is True


def test_global_flag_opens_for_all(monkeypatch):
    monkeypatch.setattr(auth, "ART_TRACK_ENABLED_TENANTS", set())
    monkeypatch.setenv("ART_TRACK_GLOBALLY_ENABLED", "1")
    assert auth.has_art_track_access(_stub_user(tenant_id="anyone")) is True


def test_none_user_denied():
    assert auth.has_art_track_access(None) is False


def test_dict_user_default_off_denies_umg(monkeypatch):
    # /generate y /retry pasan un DICT (no un objeto). El gate real corre con
    # dict → cubrir esa rama explícitamente.
    monkeypatch.setattr(auth, "ART_TRACK_ENABLED_TENANTS", set())
    monkeypatch.setenv("ART_TRACK_GLOBALLY_ENABLED", "0")
    umg = {"role": "user", "tenant_id": "universal_music", "billing_group": None}
    assert auth.has_art_track_access(umg) is False
    assert auth.has_art_track_access({"role": "admin"}) is True


def test_dict_user_access_via_tenant(monkeypatch):
    monkeypatch.setenv("ART_TRACK_GLOBALLY_ENABLED", "0")
    monkeypatch.setattr(auth, "ART_TRACK_ENABLED_TENANTS", {"universal_music"})
    assert auth.has_art_track_access(
        {"role": "user", "tenant_id": "universal_music"}) is True
    assert auth.has_art_track_access(
        {"role": "user", "tenant_id": "acme"}) is False


def test_globally_enabled_helper_default_off(monkeypatch):
    monkeypatch.delenv("ART_TRACK_GLOBALLY_ENABLED", raising=False)
    assert auth._art_track_globally_enabled() is False
