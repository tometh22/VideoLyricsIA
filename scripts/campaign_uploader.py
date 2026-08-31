#!/usr/bin/env python3
"""Resumable folder uploader for Genly batch campaigns.

The browser issues a short pairing code. This process exchanges it for a
campaign-only token, registers the complete manifest, then uploads directly
to R2. Re-running the command is safe: SHA-256 deduplicates items and R2's
multipart listing skips parts that already landed.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "lyricgen" / "backend"))
from batch_manifest import parse_audio_filename  # noqa: E402

ALLOWED = {".wav", ".mp3"}
MAX_BYTES = int(os.environ.get("BATCH_UPLOADER_MAX_BYTES", str(500 * 1024 * 1024)))
MAX_DURATION = float(os.environ.get("BATCH_UPLOADER_MAX_DURATION", "3600"))


def json_request(url: str, *, method="GET", body=None, token=None, attempts=5):
    payload = json.dumps(body).encode() if body is not None else None
    headers = {"Content-Type": "application/json"}
    if token:
        headers["X-Batch-Upload-Token"] = token
    last = None
    for attempt in range(attempts):
        try:
            request = urllib.request.Request(url, data=payload, method=method, headers=headers)
            with urllib.request.urlopen(request, timeout=60) as response:
                return json.loads(response.read() or b"{}")
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace")
            if exc.code < 500 and exc.code != 429:
                raise RuntimeError(f"{method} {url}: HTTP {exc.code} {detail}") from exc
            last = exc
        except (OSError, TimeoutError) as exc:
            last = exc
        if attempt + 1 < attempts:
            time.sleep(min(16, 2 ** attempt))
    raise RuntimeError(f"{method} {url} failed after {attempts} attempts: {last}")


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def duration(path: Path):
    executable = shutil.which("ffprobe")
    if not executable:
        return None
    result = subprocess.run(
        [executable, "-v", "error", "-show_entries", "format=duration",
         "-of", "default=noprint_wrappers=1:nokey=1", str(path)],
        capture_output=True, text=True, check=False,
    )
    try:
        return round(float(result.stdout.strip()), 3)
    except (TypeError, ValueError):
        return None


def inspect_file(path: Path) -> dict:
    error = None
    try:
        metadata = parse_audio_filename(path.name)
    except ValueError:
        metadata = {"filename": path.name, "title": None, "artist": None, "technical_code": None}
        error = "missing_metadata"
    size = path.stat().st_size
    seconds = duration(path)
    if size <= 0 or size > MAX_BYTES:
        error = "invalid_size"
    elif seconds is not None and (seconds <= 0 or seconds > MAX_DURATION):
        error = "invalid_duration"
    digest = sha256(path)
    return {
        "client_id": digest,
        "path": path,
        "filename": metadata["filename"],
        "title": metadata.get("title"),
        "artist": metadata.get("artist"),
        "technical_code": metadata.get("technical_code"),
        "size_bytes": size,
        "duration_seconds": seconds,
        "sha256": digest,
        "metadata_error": error,
    }


def put(url: str, data: bytes, content_type: str, attempts=6) -> str | None:
    last = None
    for attempt in range(attempts):
        try:
            request = urllib.request.Request(
                url, data=data, method="PUT", headers={"Content-Type": content_type},
            )
            with urllib.request.urlopen(request, timeout=300) as response:
                return response.headers.get("ETag", "").strip('"') or None
        except (urllib.error.HTTPError, OSError, TimeoutError) as exc:
            last = exc
            if isinstance(exc, urllib.error.HTTPError) and exc.code in {400, 401, 403, 404}:
                break
            time.sleep(min(20, 2 ** attempt))
    raise RuntimeError(f"R2 PUT failed: {last}")


def upload_one(base: str, token: str, entry: dict, item_id: str) -> tuple[str, str]:
    ticket = json_request(
        f"{base}/batch/uploads/{item_id}/ticket", method="POST", body={}, token=token,
    )
    if ticket.get("complete"):
        return entry["filename"], "already uploaded"
    # Must exactly match the value used to sign the R2 URL. Platform-specific
    # mimetypes (for example audio/x-wav on macOS) would invalidate SigV4.
    content_type = ticket.get("content_type") or (
        "audio/wav" if entry["filename"].lower().endswith(".wav") else "audio/mpeg"
    )
    path = entry["path"]
    parts = []
    if ticket.get("use_multipart"):
        completed = {
            int(part["part_number"]): str(part["etag"]).strip('"')
            for part in ticket.get("uploaded_parts", [])
        }
        part_size = int(ticket["part_size"])
        with path.open("rb") as stream:
            for part in ticket["parts"]:
                number = int(part["part_number"])
                size = min(part_size, entry["size_bytes"] - (number - 1) * part_size)
                if number in completed:
                    stream.seek(size, 1)
                    parts.append({"part_number": number, "etag": completed[number]})
                    continue
                data = stream.read(size)
                etag = put(part["url"], data, content_type)
                if not etag:
                    raise RuntimeError(f"Part {number} did not expose ETag")
                parts.append({"part_number": number, "etag": etag})
    else:
        put(ticket["upload_url"], path.read_bytes(), content_type)
    json_request(
        f"{base}/batch/uploads/{item_id}/complete",
        method="POST", body={"parts": parts}, token=token,
    )
    return entry["filename"], "uploaded"


def main() -> int:
    parser = argparse.ArgumentParser(description="Upload a WAV/MP3 folder to a Genly campaign")
    parser.add_argument("--api", required=True, help="API base URL")
    parser.add_argument("--campaign", required=True, help="Campaign id")
    parser.add_argument("--code", required=True, help="Temporary pairing code from the panel")
    parser.add_argument("--folder", required=True, type=Path)
    parser.add_argument("--concurrency", type=int, default=4)
    args = parser.parse_args()
    base = args.api.rstrip("/")
    folder = args.folder.expanduser().resolve()
    paths = sorted(path for path in folder.iterdir() if path.is_file() and path.suffix.lower() in ALLOWED)
    if not paths:
        print("No WAV/MP3 files found.", file=sys.stderr)
        return 2
    if len(paths) > 1000:
        print("Campaigns accept at most 1,000 files.", file=sys.stderr)
        return 2

    print(f"Inspecting {len(paths)} audio files…")
    entries = []
    with ThreadPoolExecutor(max_workers=max(1, min(args.concurrency, 8))) as pool:
        futures = {pool.submit(inspect_file, path): path for path in paths}
        for future in as_completed(futures):
            try:
                entries.append(future.result())
            except Exception as exc:
                print(f"ERROR inspecting {futures[future].name}: {exc}", file=sys.stderr)
    entries.sort(key=lambda item: item["filename"].casefold())
    exchange = json_request(
        f"{base}/batch/upload-sessions/exchange", method="POST",
        body={"campaign_id": args.campaign, "code": args.code},
    )
    token = exchange["upload_token"]

    item_ids = {}
    for start in range(0, len(entries), 100):
        chunk = entries[start:start + 100]
        body = {"items": [{key: value for key, value in entry.items() if key != "path"} for entry in chunk]}
        registered = json_request(
            f"{base}/batch/campaigns/{args.campaign}/manifest",
            method="POST", body=body, token=token,
        )
        for result in registered.get("items", []):
            item_ids[result["client_id"]] = result
        print(f"Manifest: {min(start + 100, len(entries))}/{len(entries)}")

    unique = {}
    for entry in entries:
        result = item_ids.get(entry["client_id"])
        if result and result.get("duplicate_reason") == "technical_code":
            print(
                f"DUPLICATE CODE {entry['filename']} → {result['item_id']} "
                "(se conserva el audio registrado primero)",
            )
            continue
        if result and result["item_id"] not in unique:
            unique[result["item_id"]] = entry
        elif result:
            print(f"DUPLICATE {entry['filename']} → {result['item_id']}")
    failures = []
    with ThreadPoolExecutor(max_workers=max(1, min(args.concurrency, 8))) as pool:
        futures = {
            pool.submit(upload_one, base, token, entry, item_id): (item_id, entry)
            for item_id, entry in unique.items()
        }
        for future in as_completed(futures):
            _, entry = futures[future]
            try:
                filename, state = future.result()
                print(f"OK {filename}: {state}")
            except Exception as exc:
                failures.append(entry["filename"])
                print(f"ERROR {entry['filename']}: {exc}", file=sys.stderr)
    print(f"Finished: {len(unique) - len(failures)} ok, {len(failures)} failed.")
    if failures:
        print("Run the same command with a new pairing code to resume only missing bytes.")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
