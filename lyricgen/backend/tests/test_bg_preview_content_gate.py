"""Preview-cache regressions for the authoritative background safety gate."""

import bg_preview
import jobs
import pipeline


def _wire_preview(monkeypatch, events, *, validation_passes):
    monkeypatch.setattr(bg_preview, "cache_check", lambda _key: False)
    monkeypatch.setattr(jobs, "update_job", lambda *a, **kw: None)

    def fake_generate(_style, job_dir, **_kwargs):
        path = f"{job_dir}/background.mp4"
        with open(path, "wb") as fh:
            fh.write(b"preview")
        events.append("generated")
        return path

    def fake_validate(job_id, path, operator_prompt=None):
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


def test_rejected_preview_never_enters_cache(monkeypatch):
    events = []
    _wire_preview(monkeypatch, events, validation_passes=False)

    def forbidden_cache_put(*_args, **_kwargs):
        raise AssertionError("a rejected preview must never be cached")

    monkeypatch.setattr(bg_preview, "cache_put", forbidden_cache_put)
    result = bg_preview.run_bg_preview_job(
        "previewjob01", "cachekey", {"background_hint": "an empty neon tunnel"},
    )

    assert result["status"] == "bg_preview_failed"
    assert events == ["generated", "validated"]


def test_preview_cache_namespace_marks_content_validated_entries(monkeypatch):
    assert bg_preview.CACHE_VERSION == "v3-content-validated"
    validated_key = bg_preview.compute_bg_cache_key({})
    monkeypatch.setattr(bg_preview, "CACHE_VERSION", "v2-no-implicit-people")
    assert bg_preview.compute_bg_cache_key({}) != validated_key
