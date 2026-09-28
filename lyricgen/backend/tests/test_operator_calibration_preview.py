import hashlib
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import storage
from scripts.build_operator_calibration_preview import build
from scripts.sign_operator_calibration_audio import sign_urls


def _task():
    return {
        "schema": "operator-calibration-triage-v1",
        "task_id": "opaque-task",
        "job_id": "job00000001",
        "line_index": 0,
        "task_type": "timing_review",
        "sample_role": "changed",
        "clip_start_s": 1.0,
        "clip_end_s": 4.0,
        "repeated_phrase": False,
        "split": "test",
    }


def test_audio_signing_fails_if_snapshot_changed(monkeypatch):
    db = MagicMock()
    db.query.return_value.filter.return_value.all.return_value = [SimpleNamespace(
        job_id="job00000001", input_r2_key="private/audio.wav",
        input_audio_sha256="b" * 64, audio_revision=0,
    )]
    receipt = {
        "task_id": "opaque-task", "job_id": "job00000001",
        "audio_sha256": "a" * 64, "audio_revision": 0,
        "operator_revision_is_gold": False,
    }
    try:
        sign_urls(db, [_task()], [receipt], expiry_seconds=3600)
    except ValueError as exc:
        assert str(exc) == "source_audio_changed_or_unavailable"
    else:
        raise AssertionError("changed audio was signed")


def test_audio_signing_emits_only_bound_get_urls(monkeypatch):
    db = MagicMock()
    db.query.return_value.filter.return_value.all.return_value = [SimpleNamespace(
        job_id="job00000001", input_r2_key="private/audio.wav",
        input_audio_sha256="a" * 64, audio_revision=0,
    )]
    monkeypatch.setattr(storage, "generate_signed_url", lambda key, **_kwargs: (
        "https://example.test/" + key
    ))
    receipt = {
        "task_id": "opaque-task", "job_id": "job00000001",
        "audio_sha256": "a" * 64, "audio_revision": 0,
        "operator_revision_is_gold": False,
    }
    signed = sign_urls(db, [_task()], [receipt], expiry_seconds=3600)
    assert signed["audio_urls"] == {
        "job00000001": "https://example.test/private/audio.wav",
    }
    assert "original_start_s" not in signed
    assert "gold" not in signed


def test_blind_preview_omits_provenance_and_escapes_embedded_json(tmp_path: Path):
    task = _task()
    task["task_id"] = "opaque<script>alert(1)</script>"
    queue = [task]
    queue_sha = hashlib.sha256(json.dumps(
        queue, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    ).encode()).hexdigest()
    private = tmp_path / ".context"
    private.mkdir()
    queue_path = private / "queue.jsonl"
    queue_path.write_text(json.dumps(task) + "\n")
    urls_path = private / "audio.json"
    urls_path.write_text(json.dumps({
        "schema": "operator-calibration-audio-urls-v1",
        "queue_sha256": queue_sha,
        "audio_urls": {"job00000001": "https://example.test/audio?signature=opaque"},
    }))
    output = private / "reviewer" / "index.html"
    assert build(queue_path, urls_path, output) == 1
    html = output.read_text()
    assert "opaque\\u003cscript>" in html
    assert "opaque<script>" not in html
    assert "edited_start_s" not in html
    assert "original_text_hmac" not in html
    assert output.stat().st_mode & 0o777 == 0o600
