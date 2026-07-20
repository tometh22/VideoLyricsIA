"""Preview-cache regressions for background policy v5/v6.

v6 (2026-07-17, audit adversarial): el gradiente de policy-fallback ya NO
se cachea bajo la key real — cachearlo hacía que un render futuro con esa
key heredara un gradiente genérico como si fuera un fondo Veo validado,
salteando su propia generación/recovery. El preview igual termina
bg_preview_done; el render hace cache-miss y genera fresh."""

import pytest

import bg_preview
import jobs
import pipeline


def _base_params(**overrides):
    params = {
        "artist": "Artist",
        "song_title": "Song",
        "style": "auto",
        "background_hint": "",
        "bg_verbatim": False,
        "match_lyrics": True,
    }
    params.update(overrides)
    return params


def test_cache_key_isolated_by_policy_mode(monkeypatch):
    params = _base_params()
    keys = set()
    for mode in ("off", "shadow", "enforce"):
        monkeypatch.setenv("BACKGROUND_SMOKE_POLICY_MODE", mode)
        keys.add(bg_preview.compute_bg_cache_key(params))

    assert bg_preview.CACHE_VERSION == "v7"
    assert len(keys) == 3


def test_cache_key_tracks_policy_and_legacy_creative_mode(monkeypatch):
    monkeypatch.setenv("BACKGROUND_SMOKE_POLICY_MODE", "enforce")

    lyrics_key = bg_preview.compute_bg_cache_key(
        _base_params(match_lyrics=True)
    )
    auto_key = bg_preview.compute_bg_cache_key(
        _base_params(match_lyrics=False)
    )
    improved_key = bg_preview.compute_bg_cache_key(
        _base_params(background_hint="abstract blue smoke")
    )
    literal_key = bg_preview.compute_bg_cache_key(
        _base_params(
            background_hint="abstract blue smoke",
            bg_verbatim=True,
        )
    )

    assert len({lyrics_key, auto_key, improved_key, literal_key}) == 4


def test_policy_authorization_uses_only_raw_background_hint(monkeypatch):
    monkeypatch.setenv("BACKGROUND_SMOKE_POLICY_MODE", "enforce")
    captured = []
    real_fingerprint = bg_preview.cache_policy_fingerprint

    def capture(policy):
        captured.append(policy)
        return real_fingerprint(policy)

    monkeypatch.setattr(bg_preview, "cache_policy_fingerprint", capture)

    bg_preview.compute_bg_cache_key(
        _base_params(
            background_hint="",
            concept="lyrics describe smoke, fog and haze",
            genre="smoky rock",
        )
    )
    bg_preview.compute_bg_cache_key(
        _base_params(background_hint="slow violet smoke in clear studio light")
    )

    assert captured[0]["allow_atmospherics"] is False
    assert captured[0]["authorization_source"] == "default_deny"
    assert captured[1]["allow_atmospherics"] is True
    assert captured[1]["authorization_source"] == "operator_prompt"


def _wire_preview(monkeypatch, events, *, validation_passes):
    monkeypatch.setattr(bg_preview, "cache_check", lambda _key: False)
    monkeypatch.setattr(bg_preview, "compute_bg_cache_key", lambda _params: "cachekey")
    monkeypatch.setattr(jobs, "update_job", lambda *a, **kw: None)

    def fake_generate(_style, job_dir, **_kwargs):
        path = f"{job_dir}/background.mp4"
        with open(path, "wb") as fh:
            fh.write(b"preview")
        events.append("generated")
        return path

    def fake_validate(job_id, path, operator_prompt=None, **_kwargs):
        assert job_id == "previewjob01"
        assert path.endswith("background.mp4")
        assert operator_prompt == "an empty neon tunnel"
        events.append("validated")
        return validation_passes

    monkeypatch.setattr(pipeline, "_ensure_background", fake_generate)
    monkeypatch.setattr(pipeline, "_compute_allow_people", lambda *a, **kw: False)
    monkeypatch.setattr(
        pipeline, "_validate_background_asset_for_job", fake_validate,
    )

    def fake_fallback(job_dir, _style, **_kwargs):
        path = f"{job_dir}/safe-fallback.mp4"
        with open(path, "wb") as fh:
            fh.write(b"fallback")
        events.append("fallback")
        return path

    monkeypatch.setattr(
        pipeline, "_write_safe_gradient_background", fake_fallback,
    )


def test_preview_is_validated_before_becoming_a_cache_hit(monkeypatch):
    events = []
    _wire_preview(monkeypatch, events, validation_passes=True)

    def fake_cache_put(key, path):
        assert key == "cachekey"
        assert path.endswith("background.mp4")
        events.append("cached")
        return "bg_cache/cachekey.mp4"

    monkeypatch.setattr(bg_preview, "cache_put", fake_cache_put)
    result = bg_preview.run_bg_preview_job(
        "previewjob01", "cachekey", {"background_hint": "an empty neon tunnel"},
    )

    assert result["status"] == "bg_preview_done"
    assert events == ["generated", "validated", "cached"]


def test_rejected_preview_retries_then_falls_back_without_caching(monkeypatch):
    """v6: agotados los 2 intentos de safety, el gradiente se usa para
    cerrar el preview pero NUNCA se sube bajo la key real (poisoning del
    cache content-addressed: el render heredaría un gradiente genérico
    creyendo que reusa un Veo bueno)."""
    events = []
    _wire_preview(monkeypatch, events, validation_passes=False)

    def forbidden_cache_put(_key, _path):
        raise AssertionError("el fallback no debe cachearse bajo la key real")

    monkeypatch.setattr(bg_preview, "cache_put", forbidden_cache_put)
    result = bg_preview.run_bg_preview_job(
        "previewjob01", "cachekey", {"background_hint": "an empty neon tunnel"},
    )

    assert result["status"] == "bg_preview_done"
    assert events == [
        "generated", "validated",
        "generated", "validated",
        "fallback",
    ]


def test_provider_errors_retry_then_fall_back_without_caching(monkeypatch):
    """v6: mismo contrato para errores de proveedor — fallback local para
    cerrar el preview, sin envenenar la key real."""
    events = []
    _wire_preview(monkeypatch, events, validation_passes=True)

    def failed_generation(*_args, **_kwargs):
        events.append("generation_error")
        raise RuntimeError("imagen unavailable")

    monkeypatch.setattr(pipeline, "_ensure_background", failed_generation)

    def forbidden_cache_put(_key, _path):
        raise AssertionError("el fallback no debe cachearse bajo la key real")

    monkeypatch.setattr(bg_preview, "cache_put", forbidden_cache_put)
    result = bg_preview.run_bg_preview_job(
        "previewjob01", "cachekey", {"background_hint": "empty room"},
    )

    assert result["status"] == "bg_preview_done"
    assert events == [
        "generation_error", "generation_error", "fallback",
    ]


def test_preview_reraises_rq_death_penalty(monkeypatch):
    events = []
    _wire_preview(monkeypatch, events, validation_passes=True)

    def timed_out(*_args, **_kwargs):
        raise pipeline.RQJobTimeoutException("preview deadline")

    monkeypatch.setattr(pipeline, "_ensure_background", timed_out)
    with pytest.raises(pipeline.RQJobTimeoutException):
        bg_preview.run_bg_preview_job(
            "previewjob01", "cachekey", {"background_hint": "empty room"},
        )


def test_preview_policy_or_cache_mismatch_fails_before_generation(monkeypatch):
    updates = []
    monkeypatch.setattr(jobs, "update_job", lambda *a, **kw: updates.append(kw))
    monkeypatch.setattr(bg_preview, "compute_bg_cache_key", lambda _params: "expected")
    monkeypatch.setattr(
        pipeline,
        "_ensure_background",
        lambda *a, **kw: (_ for _ in ()).throw(
            AssertionError("mismatch must fail before generation")
        ),
    )

    result = bg_preview.run_bg_preview_job(
        "previewjob01",
        "stale",
        {"background_hint": ""},
        "background-v5:shadow:deny",
    )

    assert result["status"] == "bg_preview_failed"
    assert result["error"] == "background_policy_mismatch"
    assert updates[-1]["status"] == "bg_preview_failed"
