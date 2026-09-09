"""Bounded read-only staging mix download; never emits or stores signed URLs."""
from __future__ import annotations

import argparse
import base64
from concurrent.futures import ThreadPoolExecutor, as_completed
import hashlib
import json
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import threading
import time
from urllib.parse import urlsplit
import uuid

from reviewer_campaign import atomic_json, owner_lock
from reviewer_shadow_audio import file_sha
from shadow_reference_import import digest

MAX_FILE_BYTES = 250 * 1024 * 1024
MAX_TOTAL_BYTES = 20 * 1024 * 1024 * 1024


def fetch_assets(ids, identity):
    if not 0 < len(ids) <= 30 or any(len(i) != 12 or not i.isalnum() for i in ids):
        raise ValueError("invalid_bounded_job_ids")
    source = Path(__file__).with_name("export_reviewer_shadow_assets.py").read_bytes()
    code = "import base64;exec(compile(base64.b64decode(%r),'readonly_assets','exec'))" % base64.b64encode(source).decode()
    result = subprocess.run(["railway", "ssh", "-e", "staging", "-s", "api", "-i", str(identity),
                             "--", shlex.join(["python", "-c", code, "--jobs", ",".join(ids)])],
                            capture_output=True, timeout=90)
    if result.returncode:
        raise RuntimeError("staging_assets_transport_failed_exit_" + str(result.returncode))
    payload = json.loads(result.stdout)
    if payload.get("read_only") is not True:
        raise ValueError("readonly_receipt_missing")
    records = payload.get("jobs", [])
    if len({r["job_id"] for r in records}) != len(records) or any(r["job_id"] not in ids for r in records):
        raise ValueError("assets_roster_mismatch")
    return {r["job_id"]: r for r in records}


class ByteBudget:
    def __init__(self, used=0, limit=MAX_TOTAL_BYTES):
        self.used, self.limit, self.lock = used, limit, threading.Lock()

    def add(self, size):
        with self.lock:
            if self.used + size > self.limit:
                raise ValueError("campaign_download_byte_limit")
            self.used += size


def download_one(song, asset, audio_root, budget, *, get=None):
    import requests
    started = time.monotonic()
    jid = song["job_id"]
    target = Path(audio_root) / (jid + "-mix.wav")
    record = {"job_id": jid, "audio_sha256": song["audio_sha256"],
              "audio_revision": song["audio_revision"], "path": str(target),
              "status": "blocked", "bytes_downloaded": 0}
    if not asset:
        return {**record, "reason": "current_asset_not_exported"}
    if any(asset.get(k) != song.get(k) for k in ("job_id", "audio_sha256", "audio_revision")):
        return {**record, "reason": "current_audio_identity_changed"}
    if target.exists() and file_sha(target) == song["audio_sha256"]:
        return {**record, "status": "cached_verified", "size_bytes": target.stat().st_size}
    if not isinstance(asset.get("mix_url"), str) or urlsplit(asset["mix_url"]).scheme != "https":
        return {**record, "reason": "invalid_private_mix_url"}
    if shutil.disk_usage(audio_root).free < MAX_FILE_BYTES + 1024 * 1024 * 1024:
        return {**record, "reason": "insufficient_disk_space"}
    temporary = target.with_name(target.name + ".download-" + uuid.uuid4().hex)
    try:
        digestor = hashlib.sha256()
        with (get or requests.get)(asset["mix_url"], stream=True, timeout=(15, 90), allow_redirects=False) as response:
            if response.status_code != 200:
                return {**record, "reason": "audio_http_status", "http_status": response.status_code}
            declared = response.headers.get("Content-Length")
            if declared and int(declared) > MAX_FILE_BYTES:
                return {**record, "reason": "audio_file_byte_limit"}
            with temporary.open("xb") as handle:
                os.chmod(temporary, 0o600)
                for chunk in response.iter_content(chunk_size=1024 * 1024):
                    if not chunk:
                        continue
                    if record["bytes_downloaded"] + len(chunk) > MAX_FILE_BYTES:
                        raise ValueError("audio_file_byte_limit")
                    budget.add(len(chunk))
                    record["bytes_downloaded"] += len(chunk)
                    digestor.update(chunk)
                    handle.write(chunk)
                handle.flush()
                os.fsync(handle.fileno())
        if digestor.hexdigest() != song["audio_sha256"]:
            raise ValueError("downloaded_audio_sha_mismatch")
        if target.exists():
            # Never overwrite a pre-existing file, even when its bytes are wrong.
            quarantine = target.with_name(target.name + ".preserved-" + uuid.uuid4().hex)
            target.rename(quarantine)
            record["preserved_previous_path"] = str(quarantine)
        temporary.rename(target)
        return {**record, "status": "downloaded_verified", "size_bytes": target.stat().st_size,
                "latency_seconds": round(time.monotonic() - started, 3)}
    except Exception as exc:
        # Exception strings from HTTP libraries can contain the private signed URL.
        safe = str(exc) if isinstance(exc, ValueError) and str(exc) in {
            "downloaded_audio_sha_mismatch", "audio_file_byte_limit", "campaign_download_byte_limit"} else type(exc).__name__
        return {**record, "reason": safe, "latency_seconds": round(time.monotonic() - started, 3)}
    finally:
        if temporary.exists():
            temporary.unlink()  # Only this invocation's uncommitted private download.


def run(snapshot, audio_root, report_path, identity):
    jobs = snapshot["jobs"]
    if len(jobs) != 300 or len({j["job_id"] for j in jobs}) != 300 or digest(jobs) != snapshot["snapshot_sha256"]:
        raise ValueError("exact_300_snapshot_required")
    audio_root.mkdir(mode=0o700, parents=True, exist_ok=True)
    initial_bytes = sum(p.stat().st_size for p in audio_root.glob("*-mix.wav"))
    budget = ByteBudget(initial_bytes)
    report = {"schema": "campaign-audio-downloads-v1", "snapshot_sha256": snapshot["snapshot_sha256"],
              "max_concurrency": 2, "max_file_bytes": MAX_FILE_BYTES, "max_total_bytes": MAX_TOTAL_BYTES,
              "initial_cached_mix_bytes": initial_bytes, "songs": [], "inference_calls": 0}
    with owner_lock(audio_root / ".campaign-download-owner"):
        for start in range(0, len(jobs), 30):
            batch = jobs[start:start + 30]
            assets = None
            transport_error = None
            for _ in range(2):
                try:
                    assets = fetch_assets([j["job_id"] for j in batch], identity)
                    break
                except Exception as exc:
                    transport_error = type(exc).__name__
            if assets is None:
                report["songs"].extend({"job_id": j["job_id"], "status": "blocked", "reason": "assets_transport_" + transport_error} for j in batch)
            else:
                with ThreadPoolExecutor(max_workers=2) as pool:
                    futures = [pool.submit(download_one, j, assets.get(j["job_id"]), audio_root, budget) for j in batch]
                    for future in as_completed(futures):
                        report["songs"].append(future.result())
                        report["accounted_bytes"] = budget.used
                        atomic_json(report_path, report)
            atomic_json(report_path, report)
            counts = {s: sum(j["status"] == s for j in report["songs"]) for s in ("cached_verified", "downloaded_verified", "blocked")}
            print(json.dumps({"resolved": len(report["songs"]), "counts": counts}), flush=True)
    return report


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--snapshot", type=Path, required=True)
    parser.add_argument("--audio-root", type=Path, required=True)
    parser.add_argument("--report", type=Path, required=True)
    parser.add_argument("--identity", type=Path, required=True)
    args = parser.parse_args()
    run(json.loads(args.snapshot.read_text()), args.audio_root.resolve(), args.report.resolve(), args.identity)
