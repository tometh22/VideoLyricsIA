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
import threading
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


class CampaignAuth:
    """Shared, renewable upload credential; secrets remain memory-only."""

    def __init__(
        self,
        base: str,
        campaign_id: str,
        *,
        pairing_code: str = "",
        username: str = "",
        password: str = "",
        force_expire_after_requests: int = 0,
    ):
        self.base = base.rstrip("/")
        self.campaign_id = campaign_id
        self.pairing_code = pairing_code
        self.username = username
        self.password = password
        self.force_after = max(0, int(force_expire_after_requests))
        self.token = ""
        self.expires_at = 0.0
        self.request_count = 0
        self.forced_once = False
        self.events: list[str] = []
        self.lock = threading.RLock()

    def _account_token(self) -> str:
        if not self.username or not self.password:
            raise RuntimeError(
                "upload token recovery requires STAGING_BATCH_USER and "
                "STAGING_BATCH_PASSWORD"
            )
        result = json_request(
            f"{self.base}/auth/login", method="POST",
            body={"username": self.username, "password": self.password},
        )
        token = str(result.get("token") or "")
        if not token:
            raise RuntimeError("campaign login returned no token")
        return token

    def _exchange_locked(self, *, renewal: bool) -> None:
        code = self.pairing_code if not renewal else ""
        if not code:
            account_token = self._account_token()
            created = json_request(
                f"{self.base}/batch/campaigns/{self.campaign_id}/upload-session",
                method="POST",
                headers={"Authorization": f"Bearer {account_token}"},
            )
            code = str(created.get("pairing_code") or "")
        result = json_request(
            f"{self.base}/batch/upload-sessions/exchange", method="POST",
            body={"campaign_id": self.campaign_id, "code": code},
        )
        token = str(result.get("upload_token") or "")
        if not token:
            raise RuntimeError("campaign token exchange returned no token")
        self.token = token
        self.expires_at = time.time() + int(result.get("expires_in") or 0)
        self.pairing_code = ""
        self.events.append("renewal" if renewal else "exchange")

    def authorization(self) -> tuple[str, bool]:
        with self.lock:
            if not self.token:
                self._exchange_locked(renewal=False)
            elif self.expires_at and self.expires_at - time.time() <= 900:
                self._exchange_locked(renewal=True)
                self.events.append("proactive_refresh")
            self.request_count += 1
            if (
                self.force_after
                and not self.forced_once
                and self.request_count >= self.force_after
            ):
                self.forced_once = True
                self.events.append("forced_expiry")
                self.token = "forced-expired-canary-token"
                return self.token, True
            return self.token, False

    def recover_401(self, failed_token: str) -> None:
        with self.lock:
            if failed_token != self.token:
                # Forced-expiry injection or another thread already renewed.
                self.events.append("recovered_401")
                return
            self._exchange_locked(renewal=True)
            self.events.append("recovered_401")


def json_request(
    url: str,
    *,
    method="GET",
    body=None,
    token=None,
    auth: CampaignAuth | None = None,
    headers=None,
    attempts=5,
):
    payload = json.dumps(body).encode() if body is not None else None
    request_headers = {"Content-Type": "application/json", **(headers or {})}
    last = None
    for attempt in range(attempts):
        active_token = token
        if auth is not None:
            active_token, _forced = auth.authorization()
        call_headers = dict(request_headers)
        if active_token:
            call_headers["X-Batch-Upload-Token"] = active_token
        try:
            request = urllib.request.Request(
                url, data=payload, method=method, headers=call_headers,
            )
            with urllib.request.urlopen(request, timeout=60) as response:
                return json.loads(response.read() or b"{}")
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace")
            if exc.code == 401 and auth is not None and attempt + 1 < attempts:
                auth.recover_401(active_token or "")
                last = exc
                continue
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


def upload_one(
    base: str,
    auth: CampaignAuth,
    entry: dict,
    item_id: str,
) -> tuple[str, str]:
    ticket = json_request(
        f"{base}/batch/uploads/{item_id}/ticket", method="POST", body={}, auth=auth,
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
        method="POST", body={"parts": parts}, auth=auth,
    )
    return entry["filename"], "uploaded"


def main() -> int:
    parser = argparse.ArgumentParser(description="Upload a WAV/MP3 folder to a Genly campaign")
    parser.add_argument("--api", required=True, help="API base URL")
    parser.add_argument("--campaign", required=True, help="Campaign id")
    parser.add_argument("--code", default="", help="Temporary pairing code from the panel")
    parser.add_argument("--folder", required=True, type=Path)
    parser.add_argument("--concurrency", type=int, default=4)
    parser.add_argument(
        "--username", default=(
            os.environ.get("STAGING_BATCH_USERNAME", "")
            or os.environ.get("STAGING_BATCH_USER", "")
        ), help=argparse.SUPPRESS,
    )
    parser.add_argument(
        "--password", default=os.environ.get("STAGING_BATCH_PASSWORD", ""),
        help=argparse.SUPPRESS,
    )
    parser.add_argument(
        "--force-token-expiry-after-requests", type=int, default=0,
        help="canary auth-resilience proof; requires campaign credentials",
    )
    args = parser.parse_args()
    base = args.api.rstrip("/")
    if not args.code and not (args.username and args.password):
        parser.error("--code or campaign credentials in the environment are required")
    if args.force_token_expiry_after_requests and not (args.username and args.password):
        parser.error("forced expiry proof requires campaign credentials")
    auth = CampaignAuth(
        base, args.campaign, pairing_code=args.code,
        username=args.username, password=args.password,
        force_expire_after_requests=args.force_token_expiry_after_requests,
    )
    auth_probe = json_request(f"{base}/batch/upload-sessions/me", auth=auth)
    if str(auth_probe.get("campaign_id") or "") != args.campaign:
        raise RuntimeError("campaign upload token is scoped to another campaign")
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
    item_ids = {}
    for start in range(0, len(entries), 100):
        chunk = entries[start:start + 100]
        body = {"items": [{key: value for key, value in entry.items() if key != "path"} for entry in chunk]}
        registered = json_request(
            f"{base}/batch/campaigns/{args.campaign}/manifest",
            method="POST", body=body, auth=auth,
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
            pool.submit(upload_one, base, auth, entry, item_id): (item_id, entry)
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
    if args.force_token_expiry_after_requests:
        required = {"forced_expiry", "recovered_401"}
        if not required.issubset(auth.events):
            print("Auth resilience proof did not observe 401 recovery.", file=sys.stderr)
            return 3
        print("Auth resilience: forced expiry -> 401 -> automatic recovery: confirmed")
    if failures:
        print("Run the same command with a new pairing code to resume only missing bytes.")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
