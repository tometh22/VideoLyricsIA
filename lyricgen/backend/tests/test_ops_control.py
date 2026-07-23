from types import SimpleNamespace

import pytest

import ops_control
import queue_jobs


@pytest.mark.parametrize("path", [
    "/upload-url", "/upload-multipart-init", "/generate",
    "/transcribe-uploaded", "/edit/job-1", "/retry/job-1",
    "/jobs/job-1/reanchor", "/jobs/job-1/scenes/2/regenerate",
    "/youtube/upload/job-1",
])
def test_submission_switch_covers_work_producers(path):
    assert ops_control.is_submission_path("POST", path) is True


@pytest.mark.parametrize("path", [
    "/status/job-1", "/jobs", "/auth/me", "/download/job-1/video",
    "/upload-multipart-part", "/upload-multipart-complete",
    "/upload-multipart-abort", "/jobs/job-1/save-segments",
])
def test_submission_switch_allows_reads_autosave_and_inflight_completion(path):
    method = "POST" if "multipart" in path or path.endswith("save-segments") else "GET"
    assert ops_control.is_submission_path(method, path) is False


def test_internal_enqueue_is_fail_closed_while_paused(monkeypatch):
    monkeypatch.setattr(
        ops_control,
        "get_submissions_state",
        lambda: {"paused": True, "reason": "cutover"},
    )
    with pytest.raises(queue_jobs.SubmissionsPausedError, match="cutover"):
        queue_jobs.enqueue_prores_prewarm("job-1", "umg_master")


def test_expired_dynamic_switch_falls_back_open(monkeypatch):
    class Redis:
        def get(self, _key):
            return '{"paused":true,"until":"2000-01-01T00:00:00Z"}'

    monkeypatch.setattr(ops_control, "_client", lambda: Redis())
    monkeypatch.setattr(ops_control, "_LAST_VALID_STATE", None)
    monkeypatch.delenv("SUBMISSIONS_PAUSED", raising=False)
    state = ops_control.get_submissions_state()
    assert state["paused"] is False
    assert state["source"] == "expired"


def test_control_plane_failure_is_fail_closed_in_production(monkeypatch):
    class Redis:
        def get(self, _key):
            raise TimeoutError("redis timeout")

    monkeypatch.setattr(ops_control, "_client", lambda: Redis())
    monkeypatch.setattr(ops_control, "_LAST_VALID_STATE", None)
    monkeypatch.setenv("ENVIRONMENT", "production")
    monkeypatch.delenv("SUBMISSIONS_PAUSED", raising=False)
    state = ops_control.get_submissions_state()
    assert state["paused"] is True
    assert state["source"] == "fail_closed"


def test_missing_key_cannot_erase_last_known_pause(monkeypatch):
    class Redis:
        def get(self, _key):
            return None

    monkeypatch.setattr(ops_control, "_client", lambda: Redis())
    monkeypatch.setattr(ops_control, "_LAST_VALID_STATE", {
        "paused": True, "reason": "cutover", "until": None,
        "retry_after": 120, "source": "redis",
    })
    state = ops_control.get_submissions_state()
    assert state["paused"] is True
    assert state["source"] == "cached_missing_key"
