"""Fail-closed rollout and internal provenance regression tests."""

from __future__ import annotations

import sys
from types import SimpleNamespace

import pipeline
import queue_jobs
from background_policy import (
    runtime_rollout_fingerprint,
)


def test_pipeline_rejects_api_worker_policy_mismatch_before_work(monkeypatch):
    updates = []
    monkeypatch.setenv("BACKGROUND_SMOKE_POLICY_MODE", "shadow")
    monkeypatch.setattr(pipeline, "update_job", lambda *a, **kw: updates.append(kw))

    pipeline.run_pipeline(
        "lockstepjob",
        "missing.mp3",
        "Artist",
        "auto",
        background_policy_fingerprint="background-v5:enforce",
    )

    assert updates[-1]["status"] == "error"
    assert "configuration changed" in updates[-1]["error"]


def test_pipeline_enforce_rejects_legacy_job_without_lockstep_token(monkeypatch):
    updates = []
    monkeypatch.setenv("BACKGROUND_SMOKE_POLICY_MODE", "enforce")
    monkeypatch.setattr(pipeline, "update_job", lambda *a, **kw: updates.append(kw))

    pipeline.run_pipeline("legacyjob", "missing.mp3", "Artist", "auto")

    assert updates[-1]["status"] == "error"


def test_enqueue_pipeline_attaches_current_internal_policy_token(monkeypatch):
    captured = {}

    class FakeQueue:
        connection = object()

        def enqueue(self, *args, **kwargs):
            captured.update(kwargs)
            return SimpleNamespace(id="queued")

    monkeypatch.setenv("BACKGROUND_SMOKE_POLICY_MODE", "shadow")
    monkeypatch.setattr(queue_jobs, "_pick_queue", lambda *a, **kw: FakeQueue())
    monkeypatch.setattr(queue_jobs, "_evict_stale_rq_job", lambda *a, **kw: None)
    monkeypatch.setitem(
        sys.modules,
        "rq",
        SimpleNamespace(Retry=lambda **kwargs: SimpleNamespace(**kwargs)),
    )

    result = queue_jobs.enqueue_pipeline(
        "queuedjob",
        "audio.mp3",
        "Artist",
        "auto",
        background_hint="",
    )

    assert result == "queued"
    assert "background_policy_fingerprint" not in captured["kwargs"]
    assert captured["meta"]["background_policy_fingerprint"] == (
        runtime_rollout_fingerprint()
    )


def test_enqueue_edit_attaches_current_internal_policy_token(monkeypatch):
    captured = {}

    class FakeQueue:
        connection = object()

        def enqueue(self, *args, **kwargs):
            captured["args"] = args
            captured["kwargs"] = kwargs
            return SimpleNamespace(id="queued-edit")

    monkeypatch.setenv("BACKGROUND_SMOKE_POLICY_MODE", "shadow")
    monkeypatch.setattr(queue_jobs, "_pick_queue", lambda *a, **kw: FakeQueue())
    monkeypatch.setattr(queue_jobs, "_evict_stale_rq_job", lambda *a, **kw: None)
    monkeypatch.setitem(
        sys.modules,
        "rq",
        SimpleNamespace(Retry=lambda **kwargs: SimpleNamespace(**kwargs)),
    )

    result = queue_jobs.enqueue_edit(
        "queuededit",
        "typography",
        {"font": "Inter"},
    )

    assert result == "queued-edit"
    assert captured["kwargs"]["args"] == ("queuededit", "typography", {"font": "Inter"})
    assert captured["kwargs"]["meta"]["background_policy_fingerprint"] == runtime_rollout_fingerprint()


def test_edit_rejects_api_worker_policy_mismatch_before_db_work(monkeypatch):
    updates = []
    monkeypatch.setenv("BACKGROUND_SMOKE_POLICY_MODE", "shadow")
    monkeypatch.setattr(pipeline, "update_job", lambda *a, **kw: updates.append(kw))

    pipeline.run_edit_pipeline(
        "lockstepedit",
        "typography",
        {},
        background_policy_fingerprint="background-v5:enforce",
    )

    assert updates[-1]["status"] == "error"
    assert "configuration changed" in updates[-1]["error"]


def test_edit_policy_uses_persisted_raw_prompt_on_reuse_paths():
    for edit_type in ("typography", "lyrics", "metadata"):
        assert pipeline._operator_prompt_for_edit(
            edit_type,
            fresh_background_hint=None,
            persisted_operator_prompt="operator requested smoke",
        ) == "operator requested smoke"

    assert pipeline._operator_prompt_for_edit(
        "scene",
        fresh_background_hint=None,
        persisted_operator_prompt="original human subject",
    ) == "original human subject"


def test_background_source_scope_distinguishes_ai_from_human_assets():
    assert pipeline._background_source_is_ai(
        "/tmp/bg_custom.mp4",
        "inputs/genly/job/bg_custom.mp4",
        animate_image=False,
        variation_source_path=None,
    ) is False
    assert pipeline._background_source_is_ai(
        "/tmp/bg_library.mp4",
        "library/curated.mp4",
        animate_image=False,
        variation_source_path=None,
    ) is False
    assert pipeline._background_source_is_ai(
        "/opt/genly/backgrounds/arbitrary-legacy-name.mp4",
        None,
        animate_image=False,
        variation_source_path=None,
        library_asset_id=42,
    ) is False
    assert pipeline._background_source_is_ai(
        "/tmp/bg_cached.mp4",
        "backgrounds/job/bg_cached.mp4",
        animate_image=False,
        variation_source_path=None,
    ) is True
    assert pipeline._background_source_is_ai(
        "/tmp/bg_custom.jpg",
        "inputs/genly/job/bg_custom.jpg",
        animate_image=True,
        variation_source_path=None,
    ) is True


def test_legacy_provenance_never_trusts_generic_ai_cache_as_human():
    assert pipeline._legacy_background_source_is_ai(
        asset_usage_mode="as_is", cached_key="backgrounds/job/bg_cached.mp4"
    ) is True
    assert pipeline._legacy_background_source_is_ai(
        asset_usage_mode="variation", cached_key="library/source.mp4"
    ) is True
    assert pipeline._legacy_background_source_is_ai(
        asset_usage_mode=None, cached_key="inputs/job/bg_custom.mp4"
    ) is False
    assert pipeline._legacy_background_source_is_ai(
        asset_usage_mode=None, cached_key="backgrounds/job/bg_cached.mp4"
    ) is True
    assert pipeline._legacy_background_source_is_ai(
        asset_usage_mode=None, cached_key=None
    ) is True
    assert pipeline._legacy_background_source_is_ai(
        asset_usage_mode="as_is", cached_key="backgrounds/job/bg_cached.mp4"
    ) is True
    assert pipeline._legacy_background_source_is_ai(
        asset_usage_mode="unexpected", cached_key="library/source.mp4"
    ) is True
