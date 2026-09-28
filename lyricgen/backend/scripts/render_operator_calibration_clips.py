#!/usr/bin/env python3
"""Render private, short MP3 clips for the blind calibration reviewer.

The browser receives only local clips. Signed source URLs stay in the input
manifest and are never written into the preview page or clip metadata.
"""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
from urllib.parse import urlparse


def load_manifest(queue_path: Path, urls_path: Path) -> tuple[list[dict], dict, str]:
    tasks = [json.loads(line) for line in queue_path.read_text().splitlines() if line.strip()]
    audio = json.loads(urls_path.read_text())
    queue_sha = hashlib.sha256(json.dumps(
        tasks, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    ).encode()).hexdigest()
    if not tasks or audio.get("schema") != "operator-calibration-audio-urls-v1" or audio.get("queue_sha256") != queue_sha:
        raise ValueError("queue_or_audio_manifest_invalid")
    urls = audio.get("audio_urls") or {}
    for task in tasks:
        if task.get("schema") != "operator-calibration-triage-v1" or not re.fullmatch(r"[0-9a-f]{64}", task.get("task_id", "")):
            raise ValueError("task_invalid")
        if not 0 <= task["clip_start_s"] < task["clip_end_s"]:
            raise ValueError("clip_bounds_invalid")
        if urlparse(urls.get(task["job_id"], "")).scheme != "https":
            raise ValueError("audio_url_missing_or_not_https")
    return tasks, urls, queue_sha


def render_one(task: dict, url: str, output_dir: Path) -> str:
    if ".context" not in output_dir.resolve().parts:
        raise ValueError("clips_must_remain_in_context")
    output_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(output_dir, 0o700)
    dest = output_dir / f"{task['task_id']}.mp3"
    if dest.is_file() and dest.stat().st_size > 0:
        return "cached"
    temp = output_dir / f".{task['task_id']}.{os.getpid()}.mp3"
    try:
        result = subprocess.run([
            "ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error",
            "-ss", str(task["clip_start_s"]), "-i", url,
            "-t", str(task["clip_end_s"] - task["clip_start_s"]),
            "-vn", "-map_metadata", "-1", "-map_chapters", "-1",
            "-ac", "2", "-ar", "44100", "-c:a", "libmp3lame",
            "-q:a", "4", "-y", str(temp),
        ], capture_output=True, timeout=120)
        if result.returncode != 0 or not temp.is_file() or temp.stat().st_size == 0:
            raise RuntimeError(f"clip_render_failed:{task['task_id']}")
        os.chmod(temp, 0o600)
        os.replace(temp, dest)
        return "rendered"
    finally:
        temp.unlink(missing_ok=True)


def render(queue_path: Path, urls_path: Path, output_dir: Path, workers: int = 2) -> dict:
    tasks, urls, queue_sha = load_manifest(queue_path, urls_path)

    counts = {"rendered": 0, "cached": 0}
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = [pool.submit(render_one, task, urls[task["job_id"]], output_dir) for task in tasks]
        for future in as_completed(futures):
            counts[future.result()] += 1
    return {"tasks": len(tasks), **counts, "queue_sha256": queue_sha}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--queue", type=Path, required=True)
    parser.add_argument("--audio-urls", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--workers", type=int, choices=(1, 2), default=2)
    args = parser.parse_args()
    print(json.dumps(render(args.queue, args.audio_urls, args.output_dir, args.workers)))
