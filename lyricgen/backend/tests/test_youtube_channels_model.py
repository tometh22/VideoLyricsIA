"""Model-level tests para la tabla de canales de YouTube POR TENANT.

Blindan las invariantes a nivel DB que hacen enforceable el aislamiento
cross-tenant + "un solo default por tenant" — el punto de mover el modelo
fuera del singleton `system_youtube_token` (donde el único canal conectado
se filtraba a todos los tenants). Ver youtube-per-tenant-channel-requirement.

Nota: el fixture `db` commitea a un sqlite de sesión compartido (el rollback
solo deshace lo no-commiteado), así que cada test usa tenant/channel ids
globalmente únicos para ser independiente.
"""
import pytest
from sqlalchemy.exc import IntegrityError

from database import YoutubeChannel


def _chan(tenant, channel_id, *, is_default=False, status="active"):
    return YoutubeChannel(
        tenant_id=tenant,
        channel_id=channel_id,
        channel_title="Canal",
        encrypted_token_json="enc",
        is_default=is_default,
        status=status,
    )


def test_two_tenants_can_hold_the_same_youtube_channel(db):
    """El unique es por (tenant, channel): Chile y Argentina son tenants
    distintos → el mismo channel_id NO colisiona entre ellos."""
    db.add(_chan("m_chile", "UCsame01", is_default=True))
    db.add(_chan("m_arg", "UCsame01", is_default=True))
    db.commit()  # distinto tenant → OK


def test_same_channel_twice_in_one_tenant_is_rejected(db):
    db.add(_chan("m_dup", "UCdup01"))
    db.commit()
    db.add(_chan("m_dup", "UCdup01"))
    with pytest.raises(IntegrityError):
        db.commit()
    db.rollback()


def test_many_non_default_channels_per_tenant_ok(db):
    db.add(_chan("m_multi", "UCa01"))
    db.add(_chan("m_multi", "UCb01"))
    db.add(_chan("m_multi", "UCc01"))
    db.commit()  # varios canales por tenant, ninguno default → OK


def test_only_one_default_per_tenant(db):
    db.add(_chan("m_onedef", "UCa02", is_default=True))
    db.commit()
    db.add(_chan("m_onedef", "UCb02", is_default=True))
    with pytest.raises(IntegrityError):  # unique parcial WHERE is_default
        db.commit()
    db.rollback()


def test_default_coexists_with_non_defaults(db):
    db.add(_chan("m_coexist", "UCa03", is_default=True))
    db.add(_chan("m_coexist", "UCb03", is_default=False))
    db.add(_chan("m_coexist", "UCc03", is_default=False))
    db.commit()  # 1 default + N no-default → OK


def test_defaults_applied_on_insert(db):
    ch = YoutubeChannel(
        tenant_id="m_def", channel_id="UCdef01", encrypted_token_json="enc",
    )
    db.add(ch)
    db.commit()
    assert ch.status == "active"
    assert not ch.is_default
