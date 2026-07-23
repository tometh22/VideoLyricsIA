"""Regression coverage for production background-asset integrity."""

import pytest
from fastapi import HTTPException


def test_local_library_files_do_not_disable_ai_generation(tmp_path, monkeypatch):
    """A stray local MP4 must not make ``_ensure_background`` return None."""
    import pipeline
    import veo_breaker

    library = tmp_path / "library"
    library.mkdir()
    (library / "stray.mp4").write_bytes(b"\x00" * 512)
    monkeypatch.setattr(pipeline, "BACKGROUNDS_DIR", str(library))
    monkeypatch.setattr(
        pipeline,
        "_get_unique_prompt",
        lambda *_args, **_kwargs: {"prompt": "safe abstract gradient"},
    )
    monkeypatch.setattr(veo_breaker, "is_open", lambda: True)

    class _Gradient:
        def write_videofile(self, path, **_kwargs):
            with open(path, "wb") as output:
                output.write(b"fallback")

        def close(self):
            return None

    monkeypatch.setattr(
        pipeline,
        "_make_gradient_clip",
        lambda *_args, **_kwargs: _Gradient(),
    )

    result = pipeline._ensure_background(
        "oscuro",
        str(tmp_path),
        lyrics_text="línea de prueba",
        artist="smoke",
        job_id="integrity-test",
    )

    assert result == str(tmp_path / "bg_gradient_fallback.mp4")


def test_business_alerts_default_to_production_only(monkeypatch):
    import main

    monkeypatch.delenv("BUSINESS_ALERTS_ENABLED", raising=False)
    monkeypatch.setattr(main, "ENVIRONMENT", "staging")
    assert main._business_alerts_enabled() is False
    monkeypatch.setattr(main, "ENVIRONMENT", "production")
    assert main._business_alerts_enabled() is True


def test_business_alerts_allow_explicit_nonproduction_override(monkeypatch):
    import main

    monkeypatch.setattr(main, "ENVIRONMENT", "staging")
    monkeypatch.setenv("BUSINESS_ALERTS_ENABLED", "true")
    assert main._business_alerts_enabled() is True


def test_missing_legacy_asset_is_hidden_and_cannot_be_resolved(
    client, user_token, db, tmp_path, monkeypatch,
):
    import main
    from database import AssetUsage, BackgroundAsset

    monkeypatch.setattr(main, "_BACKGROUNDS_LIB", str(tmp_path))
    me = client.get(
        "/auth/me",
        headers={"Authorization": f"Bearer {user_token}"},
    ).json()
    asset = BackgroundAsset(
        name="Missing legacy file",
        filename="missing-local.mp4",
        file_type="mp4",
        is_active=True,
    )
    db.add(asset)
    db.commit()
    try:
        listed = client.get(
            "/backgrounds",
            headers={"Authorization": f"Bearer {user_token}"},
        )
        assert listed.status_code == 200
        assert asset.id not in {row["id"] for row in listed.json()}

        with pytest.raises(HTTPException) as exc_info:
            main._resolve_library_background(
                asset.id,
                "as_is",
                me,
                db,
                str(tmp_path / "job"),
                "missing-asset-job",
            )
        assert getattr(exc_info.value, "status_code", None) == 404
        assert db.query(AssetUsage).filter(
            AssetUsage.asset_id == asset.id
        ).count() == 0
    finally:
        db.query(AssetUsage).filter(AssetUsage.asset_id == asset.id).delete(
            synchronize_session=False
        )
        db.delete(asset)
        db.commit()


def test_tenant_admin_does_not_bypass_asset_scope():
    import main
    from types import SimpleNamespace

    other_tenant_asset = SimpleNamespace(owner_tenant_id="tenant-b")
    tenant_admin = {
        "role": "admin",
        "is_super_admin": False,
        "tenant_id": "tenant-a",
    }
    assert main._user_can_use_asset(other_tenant_asset, tenant_admin) is False


def test_r2_asset_is_unavailable_when_deployed_without_storage(monkeypatch):
    import main
    import storage
    from types import SimpleNamespace

    monkeypatch.setattr(main, "ENVIRONMENT", "staging")
    monkeypatch.setattr(storage, "is_enabled", lambda: False)
    asset = SimpleNamespace(filename="library/global/example.mp4")
    assert main._background_asset_is_available(asset) is False
