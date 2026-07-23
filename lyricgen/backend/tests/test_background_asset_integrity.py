"""Regression coverage for production background-asset integrity."""


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
