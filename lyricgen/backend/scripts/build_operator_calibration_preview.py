#!/usr/bin/env python3
"""Build a private, blind clip-review page from a triage queue and GET URLs.

The page contains audio links and tasks, never machine/operator lyric versions.
Local annotations remain drafts until independently adjudicated.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
from urllib.parse import urlparse


def _read_jsonl(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def build(queue_path: Path, urls_path: Path, output: Path) -> int:
    tasks = _read_jsonl(queue_path)
    audio = json.loads(urls_path.read_text())
    if not tasks or audio.get("schema") != "operator-calibration-audio-urls-v1":
        raise ValueError("queue_or_audio_urls_invalid")
    queue_sha = hashlib.sha256(json.dumps(
        tasks, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    ).encode()).hexdigest()
    if audio.get("queue_sha256") != queue_sha:
        raise ValueError("audio_urls_bound_to_different_queue")
    urls = audio.get("audio_urls") or {}
    for task in tasks:
        if task.get("schema") != "operator-calibration-triage-v1":
            raise ValueError("task_schema_invalid")
        url = urls.get(task.get("job_id"))
        if not isinstance(url, str) or urlparse(url).scheme != "https":
            raise ValueError("audio_url_missing_or_not_https")
    if ".context" not in output.resolve().parts:
        raise ValueError("preview_must_remain_in_context")
    output.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(output.parent, 0o700)
    template = Path(__file__).with_name("operator_calibration_preview.html").read_text()
    payload = {
        "queue_sha256": queue_sha,
        "expires_at": audio.get("expires_at"),
        # Only the clip and an opaque task ID reach the reviewer. In
        # particular, controls, change types, source models and split labels
        # stay sealed; showing them would bias the blind observation.
        "tasks": [{
            "task_id": task["task_id"],
            "clip_start_s": task["clip_start_s"],
            "clip_end_s": task["clip_end_s"],
            "audio_url": urls[task["job_id"]],
        } for task in tasks],
    }
    safe_json = json.dumps(payload, ensure_ascii=False).replace("<", "\\u003c")
    content = template.replace("__CALIBRATION_DATA__", safe_json)
    descriptor = os.open(output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        handle.write(content)
    return len(tasks)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--queue", type=Path, required=True)
    parser.add_argument("--audio-urls", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    count = build(args.queue, args.audio_urls, args.output)
    print(json.dumps({"preview_tasks": count, "gold_labels": 0, "automatic_apply_allowed": False}))
