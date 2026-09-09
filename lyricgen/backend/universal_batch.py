#!/usr/bin/env python3
"""Idempotent, wave-limited Universal staging batch runner.

This legacy API client is retained only as a render-resume bridge for manifests
that already contain campaign job ids. Campaign upload/transcription belongs to
``scripts/campaign_uploader.py``; routing those files through the ordinary
``/upload-url`` endpoint would lose the campaign and approval guarantees.
"""

from __future__ import annotations

import argparse
import base64
import concurrent.futures
import hashlib
import json
import os
import sys
import threading
import time
from pathlib import Path
from typing import Any

import requests

from batch_manifest import AudioManifestEntry, build_manifest, load_manifest, write_manifest
from batch_profiles import normalize_render_profile


TRANSCRIPTION_TERMINAL = {"lyrics_review_pending", "lyrics_approved"}
RENDER_TERMINAL = {"pending_review", "done", "error", "rejected", "validation_failed"}
TERMINAL = TRANSCRIPTION_TERMINAL | RENDER_TERMINAL
DEFAULT_EXPECTED_COUNT = 30


def _resolved_language(result: dict[str, Any]) -> str | None:
    """Return only a worker-confirmed language for the batch manifest.

    Mixed or unavailable LID is an explicit abstention.  The runner must not
    turn either case into a Spanish fallback because that would re-introduce
    the cross-language omission this path is designed to prevent.
    """
    detected_languages = result.get("detected_languages")
    if result.get("mixed_language") or (
        isinstance(detected_languages, (list, tuple, set))
        and len({str(value).strip().lower() for value in detected_languages if value}) > 1
    ):
        return None
    direct = result.get("detected_language") or result.get("language")
    if isinstance(direct, str) and direct.strip().lower() not in {"", "unknown"}:
        return direct.strip().lower()
    quality = result.get("transcription_quality")
    metrics = quality.get("metrics") if isinstance(quality, dict) else None
    value = metrics.get("language") if isinstance(metrics, dict) else None
    if isinstance(value, str) and value.strip().lower() not in {"", "unknown"}:
        return value.strip().lower()
    return None


class BatchError(RuntimeError):
    pass


def _jwt_exp(token: str) -> float | None:
    """Read an exp hint without trusting it for authentication decisions."""
    try:
        payload = token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        value = json.loads(base64.urlsafe_b64decode(payload.encode("ascii")))
        return float(value["exp"])
    except (IndexError, KeyError, TypeError, ValueError, json.JSONDecodeError):
        return None


class AuthSession:
    """Thread-safe campaign authentication with proactive and 401 recovery.

    A valid JWT can be refreshed through ``/auth/refresh`` before expiry.  If
    the server rejects it (expired/revoked), campaign credentials are required
    to re-login. Secrets are accepted from environment variables only; they
    are never placed in the manifest or error messages.
    """

    def __init__(
        self,
        base_url: str,
        token: str,
        *,
        username: str = "",
        password: str = "",
        refresh_margin_seconds: int = 21600,
        force_expire_after_requests: int = 0,
        timeout: int = 120,
    ):
        self.base = base_url.rstrip("/")
        self._token = token
        self._username = username
        self._password = password
        self._refresh_margin = max(60, int(refresh_margin_seconds))
        self._force_after = max(0, int(force_expire_after_requests))
        self._request_count = 0
        self._forced_once = False
        self._lock = threading.RLock()
        self.timeout = timeout
        self.events: list[str] = []

    def _login_locked(self) -> None:
        if not self._username or not self._password:
            raise BatchError(
                "campaign authentication cannot recover: set "
                "STAGING_BATCH_USERNAME and STAGING_BATCH_PASSWORD"
            )
        response = requests.post(
            self.base + "/auth/login",
            json={"username": self._username, "password": self._password},
            timeout=min(self.timeout, 30),
        )
        if response.status_code >= 400:
            raise BatchError(
                f"campaign authentication login failed: HTTP {response.status_code}"
            )
        token = response.json().get("token")
        if not token:
            raise BatchError("campaign authentication login returned no token")
        self._token = str(token)
        self.events.append("login")

    def _refresh_locked(self) -> bool:
        if not self._token:
            return False
        response = requests.post(
            self.base + "/auth/refresh",
            headers={"Authorization": f"Bearer {self._token}"},
            timeout=min(self.timeout, 30),
        )
        if response.status_code >= 400:
            return False
        token = response.json().get("token")
        if not token:
            return False
        self._token = str(token)
        self.events.append("refresh")
        return True

    def _ensure_token_locked(self) -> None:
        if not self._token:
            self._login_locked()
            return
        expires_at = _jwt_exp(self._token)
        if expires_at is not None and expires_at - time.time() <= self._refresh_margin:
            if not self._refresh_locked():
                self._login_locked()

    def authorization(self) -> tuple[str, bool]:
        with self._lock:
            self._ensure_token_locked()
            self._request_count += 1
            if (
                self._force_after
                and not self._forced_once
                and self._request_count >= self._force_after
            ):
                self._forced_once = True
                self.events.append("forced_expiry")
                return "forced-expired-canary-token", True
            return self._token, False

    def recover_401(self, failed_token: str) -> None:
        with self._lock:
            # Another worker may already have replaced the rejected token.
            if self._token and failed_token != self._token:
                self.events.append("recovered_401")
                return
            if not self._refresh_locked():
                self._login_locked()
            self.events.append("recovered_401")


class Api:
    def __init__(
        self,
        base_url: str,
        token: str = "",
        timeout: int = 120,
        *,
        auth: AuthSession | None = None,
    ):
        self.base = base_url.rstrip("/")
        self.session = requests.Session()
        self.auth = auth or AuthSession(self.base, token, timeout=timeout)
        self.timeout = timeout

    def request(self, method: str, path: str, **kwargs) -> Any:
        token, _forced = self.auth.authorization()
        headers = dict(kwargs.pop("headers", {}) or {})
        headers["Authorization"] = f"Bearer {token}"
        response = self.session.request(
            method, self.base + path, timeout=self.timeout,
            headers=headers, **kwargs,
        )
        if response.status_code == 401:
            self.auth.recover_401(token)
            retry_token, _ = self.auth.authorization()
            headers["Authorization"] = f"Bearer {retry_token}"
            response = self.session.request(
                method, self.base + path, timeout=self.timeout,
                headers=headers, **kwargs,
            )
        if response.status_code >= 400:
            try:
                detail = response.json()
            except ValueError:
                detail = response.text[:500]
            raise BatchError(f"{method} {path} -> {response.status_code}: {detail}")
        return response.json() if response.content else {}

    def validate_capacity(self, *, expected_count: int, wave_size: int) -> None:
        """Fail-fast antes de subir bytes o crear el primer job."""
        me = self.request("GET", "/auth/me")
        usage = self.request("GET", "/usage")
        capacity = self.request("GET", "/batch/capacity")
        needed_window = min(expected_count, wave_size)

        if not capacity.get("bypass"):
            if not capacity.get("campaign_enabled"):
                raise BatchError(
                    "account is not enabled in BATCH_CAMPAIGN_SCOPES"
                )
            for key in ("user_backlog", "tenant_backlog"):
                remaining = capacity.get(key, {}).get("remaining")
                if remaining is None or int(remaining) < needed_window:
                    raise BatchError(
                        f"{key} has {remaining} slots; wave needs {needed_window}"
                    )

        daily_remaining = capacity.get("daily", {}).get("remaining")
        if daily_remaining is not None and int(daily_remaining) < expected_count:
            raise BatchError(
                f"daily cap has {daily_remaining} slots; batch needs {expected_count}"
            )
        plan = str(me.get("plan") or usage.get("plan") or "")
        allow_overage = bool(me.get("allow_overage"))
        available = usage.get("total_available")
        if (
            plan != "unlimited"
            and not allow_overage
            and available is not None
            and int(available) < expected_count
        ):
            raise BatchError(
                f"plan has {available} credits; batch needs {expected_count} "
                "and allow_overage is false"
            )

    def wait_for_backlog_capacity(
        self,
        *,
        needed: int,
        poll_seconds: float,
        max_wait_seconds: float,
    ) -> None:
        """Espera revisores en vez de chocar el 429 a mitad de una ola."""
        deadline = time.monotonic() + max_wait_seconds
        last_report = 0.0
        while True:
            capacity = self.request("GET", "/batch/capacity")
            if capacity.get("bypass"):
                return
            user_remaining = capacity.get("user_backlog", {}).get("remaining")
            tenant_remaining = capacity.get("tenant_backlog", {}).get("remaining")
            if (
                user_remaining is not None
                and tenant_remaining is not None
                and min(int(user_remaining), int(tenant_remaining)) >= needed
            ):
                return
            now = time.monotonic()
            if now >= deadline:
                raise BatchError(
                    "timed out waiting for review backlog capacity: "
                    f"user={user_remaining}, tenant={tenant_remaining}, needed={needed}"
                )
            if now - last_report >= 60 or last_report == 0:
                print(
                    "waiting for reviewers/backlog slots: "
                    f"user={user_remaining}, tenant={tenant_remaining}, needed={needed}"
                )
                last_report = now
            time.sleep(max(1.0, poll_seconds))

    def _put_presigned(self, url: str, body) -> requests.Response:
        """PUT bytes to a presigned R2/S3 URL.

        Deliberately NOT ``self.session``: the session carries the API's
        ``Authorization: Bearer`` header, and S3-compatible storage rejects a
        query-string-signed request that also carries an Authorization header
        with ``400 InvalidArgument`` ("only one auth mechanism allowed"). Every
        WAV above the 16 MB multipart threshold failed on part 1 because of
        this before the first staging canary ran.
        """
        # A presigned PUT is idempotent (single object or a numbered multipart
        # part), so transient transport failures are safe to retry. The first
        # staging canary lost 2/15 uploads to BrokenPipe under 5-way
        # concurrency; without this the song is marked error and needs a
        # manual --retry-errors pass.
        payload = body.read() if hasattr(body, "read") else body
        last_exc: Exception | None = None
        for attempt in range(4):
            try:
                return requests.put(
                    url, data=payload, headers={"Content-Type": "audio/wav"},
                    timeout=max(self.timeout, 900),
                )
            except (requests.ConnectionError, requests.Timeout) as exc:
                last_exc = exc
                if attempt < 3:
                    time.sleep(1.5 * (2 ** attempt))
        raise BatchError(f"presigned PUT failed after retries: {last_exc!r}")

    def upload_audio(self, entry: AudioManifestEntry) -> str:
        path = Path(entry.source_path)
        ticket = self.request("POST", "/upload-url", json={
            "filename": entry.filename,
            "content_type": "audio/wav",
            "size_bytes": entry.size_bytes,
            "artist": entry.artist,
            "title": entry.title,
        })
        job_id = ticket["job_id"]
        if not ticket.get("use_multipart"):
            with path.open("rb") as body:
                put = self._put_presigned(ticket["upload_url"], body)
            if put.status_code >= 300:
                raise BatchError(f"R2 PUT failed for {entry.filename}: {put.status_code}")
            return job_id

        part_size = int(ticket.get("part_size") or 8 * 1024 * 1024)
        expected_parts = (entry.size_bytes + part_size - 1) // part_size
        init = self.request("POST", "/upload-multipart-init", json={
            "job_id": job_id,
            "filename": entry.filename,
            "content_type": "audio/wav",
            "expected_parts": expected_parts,
        })
        signed = {int(p["part_number"]): p["url"] for p in init.get("presigned_parts", [])}
        parts = []
        with path.open("rb") as fh:
            for part_number in range(1, expected_parts + 1):
                if part_number not in signed:
                    signed[part_number] = self.request(
                        "POST", "/upload-multipart-part-url",
                        json={"job_id": job_id, "part_number": part_number},
                    )["url"]
                body = fh.read(part_size)
                put = self._put_presigned(signed[part_number], body)
                if put.status_code >= 300:
                    raise BatchError(f"R2 part {part_number} failed for {entry.filename}: {put.status_code}")
                etag = put.headers.get("ETag") or put.headers.get("Etag")
                if not etag:
                    raise BatchError(f"R2 part {part_number} has no ETag for {entry.filename}")
                parts.append({"part_number": part_number, "etag": etag.strip('"')})
        self.request("POST", "/upload-multipart-complete",
                     json={"job_id": job_id, "parts": parts})
        return job_id

    def transcribe(self, entry: AudioManifestEntry, job_id: str,
                   poll_seconds: float) -> list[dict]:
        self.start_transcription(entry, job_id)
        return self.wait_for_transcription(entry, job_id, poll_seconds)

    def start_transcription(self, entry: AudioManifestEntry, job_id: str) -> dict:
        payload = {
            "job_id": job_id,
            # Empty means provider auto-LID.  Never force a campaign-wide
            # language: the worker confirms LID from the isolated vocal stem
            # and abstains for mixed-language recordings.
            "language": "",
            "artist": entry.artist,
            "title": entry.title,
            "live": bool(entry.version),
        }
        canonical = json.dumps(
            payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True,
        )
        idem_key = "batch-transcribe-" + hashlib.sha256(
            canonical.encode("utf-8"),
        ).hexdigest()

        result = None
        last_error = None
        for attempt in range(3):
            try:
                result = self.request(
                    "POST", "/transcribe-uploaded", json=payload,
                    headers={"Idempotency-Key": idem_key},
                )
                break
            except (requests.Timeout, requests.ConnectionError) as exc:
                # The server may have committed its outbox before the HTTP
                # response was lost. Reconcile the durable job before retrying
                # the exact same hash; never create a second upload/job.
                last_error = exc
                try:
                    existing = self.request("GET", f"/batch/jobs/{job_id}")
                except (requests.Timeout, requests.ConnectionError, BatchError):
                    existing = {}
                server_status = str(existing.get("status") or "")
                if server_status in {
                    "transcribing_queued", "transcribing", "transcribed",
                    "transcribed_pending", "queued", "processing",
                    "pending_review", "done",
                }:
                    result = {
                        "job_id": job_id,
                        "status": server_status,
                        "deduplicated": True,
                        "resumed_after_ambiguous_response": True,
                    }
                    break
                if server_status and server_status != "awaiting_upload":
                    raise BatchError(
                        f"ambiguous transcription submit ended as {server_status}: "
                        f"{entry.filename}"
                    ) from exc
                if attempt < 2:
                    time.sleep(0.5 * (2 ** attempt))
        if result is None:
            raise BatchError(
                f"transcription submit response remained ambiguous for {entry.filename}"
            ) from last_error
        entry.status = result.get("status") or "transcribing_queued"
        if entry.status == "transcribed_pending":
            entry.status = "transcribed"
        return result

    def wait_for_transcription(self, entry: AudioManifestEntry, job_id: str,
                               poll_seconds: float) -> list[dict]:
        deadline = time.monotonic() + 3600
        while time.monotonic() < deadline:
            result = self.request("GET", f"/transcription-status/{job_id}")
            status = result.get("status")
            if status == "transcribed":
                entry.search_result = {
                    "reference_lyrics": result.get("reference_lyrics"),
                    "coverage_warning": result.get("coverage_warning"),
                    "timing_source": result.get("timing_source"),
                    "language_requested": "auto",
                    "language": _resolved_language(result),
                    "detected_language": result.get("detected_language"),
                    "detected_languages": result.get("detected_languages") or [],
                    "mixed_language": bool(result.get("mixed_language")),
                }
                # Stage 1 ends here. Background selection and /generate are
                # forbidden until a reviewer signs the exact lyrics/timings.
                entry.status = "lyrics_review_pending"
                return result.get("segments") or []
            if status == "transcription_failed":
                raise BatchError(result.get("error") or "transcription failed")
            time.sleep(poll_seconds)
        raise BatchError("transcription polling timed out")

    def generate(self, entry: AudioManifestEntry, job_id: str,
                 segments: list[dict], poll_seconds: float) -> dict:
        profile = entry.render_profile or {}
        fields = {
            "job_id": job_id,
            "artist": entry.artist,
            "song_title": entry.title,
            "segments_json": json.dumps(segments, ensure_ascii=False),
            "delivery_profile": "both",
            "umg_frame_size": "HD",
            "umg_fps": "24",
            "umg_prores_profile": "3",  # ProRes 422 HQ
            "background_id": str(entry.background_id or ""),
            "background_mode": "as_is",
            "render_profile": json.dumps(profile, ensure_ascii=False),
        }
        self.request("POST", "/generate", files={
            key: (None, value) for key, value in fields.items()
        })
        entry.status = "queued"
        return self.wait_for_render(entry, job_id, poll_seconds)

    def wait_for_render(self, entry: AudioManifestEntry, job_id: str,
                        poll_seconds: float) -> dict:
        """Resume polling an already-created render without duplicating it."""
        deadline = time.monotonic() + 7200
        while time.monotonic() < deadline:
            detail = self.request("GET", f"/batch/jobs/{job_id}")
            status = detail.get("status")
            if status in TERMINAL:
                files = detail.get("files") or {}
                entry.scoreboard = {
                    "status": status,
                    "coverage_warning": bool((entry.search_result or {}).get("coverage_warning")),
                    "timing_source": (entry.search_result or {}).get("timing_source"),
                    "mismatches": detail.get("validation_result", {}).get("mismatches", [])
                    if isinstance(detail.get("validation_result"), dict) else [],
                    "mp4_available": bool(files.get("video_url")),
                    "short_available": bool(files.get("short_url")),
                    "thumbnail_available": bool(files.get("thumbnail_url")),
                    "prores_lazy": not bool(detail.get("prores_ready")),
                    "review_required": status == "pending_review",
                }
                entry.status = status
                if status != "pending_review":
                    entry.error = detail.get("error") or f"unexpected terminal status: {status}"
                return detail
            time.sleep(poll_seconds)
        raise BatchError("render polling timed out")


def _asset_tags(asset: dict[str, Any]) -> str:
    tags = asset.get("tags") or ""
    return " ".join(tags) if isinstance(tags, list) else str(tags)


def select_backgrounds(api: Api, count: int = DEFAULT_EXPECTED_COUNT) -> list[dict[str, Any]]:
    assets = api.request("GET", "/backgrounds")
    umg_assets = [asset for asset in assets if "umg" in _asset_tags(asset).lower()]
    # The production library uses `umg` tags as its stable ownership marker.
    # Keep a guarded fallback for older staging rows that predate the tag.
    if len(umg_assets) >= count:
        assets = umg_assets
    videos, photos = [], []
    for asset in assets:
        kind = str(asset.get("file_type") or "").lower()
        filename = str(asset.get("filename") or "").lower()
        if kind in {"video", "mp4", "video_simple", "video_cinematic"} or filename.endswith(".mp4"):
            videos.append(asset)
        elif kind in {"image", "jpg", "jpeg", "png"} or filename.endswith((".jpg", ".jpeg", ".png")):
            photos.append(asset)
    if not videos or not photos:
        raise BatchError(
            "background library needs at least one video and one photo "
            f"asset ({len(videos)} + {len(photos)})"
        )
    # Tags are retained in the manifest through the selected asset id; the
    # server remains authoritative for actual file access and tenant scope.
    # Campaigns can contain more songs than library assets; cycle the curated
    # pool deterministically instead of refusing song 81 because there are
    # only 80 backgrounds. The exact mapping remains frozen in the manifest.
    video_count = count // 2
    photo_count = count - video_count
    return (
        [videos[i % len(videos)] for i in range(video_count)]
        + [photos[i % len(photos)] for i in range(photo_count)]
    )


def assign_profiles(entries: list[AudioManifestEntry], assets: list[dict[str, Any]]) -> None:
    styles = ("oscuro", "minimal", "calido", "neon")
    fonts = ("poppins-bold", "montserrat-bold", "roboto-bold", "anton")
    for index, entry in enumerate(entries):
        is_video = index < len(entries) // 2
        profile = normalize_render_profile({
            "font": fonts[index % len(fonts)],
            "font_scale": 1.0 if index % 2 else 1.3,
            "text_case": "upper" if index % 3 else "lower",
            "transition": "fade" if index % 2 else "cut",
            "background_type": "video" if is_video else "photo",
            "movement": "estatico" if is_video else "foto-estatica",
            "effect": "" if is_video else ("rain", "bokeh", "light", "fog")[index % 4],
            "style": styles[index % len(styles)],
            "background_id": int(assets[index]["id"]),
        })
        entry.render_profile = profile
        entry.background_id = profile["background_id"]


def _merge_entries(fresh: list[AudioManifestEntry], old: dict[str, Any]) -> list[AudioManifestEntry]:
    previous = {row.get("sha256"): row for row in old.get("entries", [])}
    merged = []
    for entry in fresh:
        row = previous.get(entry.sha256)
        if row:
            for key in ("status", "job_id", "search_result", "scoreboard", "render_profile", "background_id", "error"):
                if key in row:
                    setattr(entry, key, row[key])
        merged.append(entry)
    return merged


class ManifestStore:
    """Serializa snapshots atomicos desde los workers concurrentes."""

    def __init__(self, path: Path, entries: list[AudioManifestEntry], expected_count: int):
        self.path = path
        self.entries = entries
        self.expected_count = expected_count
        self._lock = threading.Lock()

    def save(self) -> None:
        with self._lock:
            write_manifest(
                self.path, self.entries, expected_count=self.expected_count,
            )


def _mark_error(entry: AudioManifestEntry, exc: Exception, save) -> None:
    entry.status = "error"
    entry.error = str(exc)[:1000]
    save()


def _submit_transcription(api: Api, entry: AudioManifestEntry, save) -> bool:
    """Deja una cancion encolada sin esperar el resultado.

    Separar submission de reconciliation permite llenar una ola de 30 y usar
    toda la flota. El runner viejo esperaba el render completo de la primera
    cancion antes de siquiera crear la segunda.
    """
    if entry.status in {
        "lyrics_review_pending", "lyrics_approved", "pending_review", "done",
        "queued", "processing", "transcribing", "transcribing_queued",
        "transcribed",
    }:
        return True
    if entry.status in TERMINAL:
        if entry.status not in {"pending_review", "done"}:
            entry.error = entry.error or f"terminal status: {entry.status}"
            entry.status = "error"
            save()
        return False

    if entry.job_id and entry.status == "uploading":
        existing = api.request("GET", f"/batch/jobs/{entry.job_id}")
        server_status = existing.get("status") or "awaiting_upload"
        if server_status in TERMINAL:
            entry.status = server_status
            save()
            return server_status in {"pending_review", "done"}
        if server_status in {"queued", "processing", "transcribing",
                             "transcribing_queued"}:
            entry.status = server_status
            save()
            return True
        if server_status in {"transcribed_pending", "transcribed"}:
            entry.status = "transcribed"
            save()
            return True
        if server_status != "awaiting_upload":
            raise BatchError(
                f"cannot resume {entry.filename}: server status={server_status!r}"
            )
    else:
        entry.status = "uploading"
        save()
        entry.job_id = api.upload_audio(entry)
        # Persist the id BEFORE the enqueue call. A crash between the two can
        # resume the existing awaiting_upload row instead of creating another.
        save()

    api.start_transcription(entry, entry.job_id)
    save()
    return True


def _reconcile_transcription_entry(
    api: Api,
    entry: AudioManifestEntry,
    poll_seconds: float,
    save,
) -> bool:
    """Stop at lyrics/timing review; never choose or generate a background."""
    if entry.status in {"lyrics_review_pending", "lyrics_approved"}:
        return True
    if not entry.job_id:
        return False
    if entry.status == "transcribed":
        detail = api.request("GET", f"/batch/jobs/{entry.job_id}")
        server_status = str(detail.get("status") or "")
        entry.status = (
            "lyrics_approved" if server_status == "lyrics_approved"
            else "lyrics_review_pending"
        )
        save()
        return True
    elif entry.status in {"transcribing", "transcribing_queued", "uploading"}:
        api.wait_for_transcription(entry, entry.job_id, poll_seconds)
        save()
        return entry.status == "lyrics_review_pending"
    return False


def _render_approved_entry(
    api: Api,
    entry: AudioManifestEntry,
    poll_seconds: float,
    save,
) -> bool:
    """Render only a server-authoritative, reviewer-approved revision."""
    if not entry.job_id:
        raise BatchError(f"missing job id for {entry.filename}")
    detail = api.request("GET", f"/batch/jobs/{entry.job_id}")
    server_status = str(detail.get("status") or "")
    if server_status in {"pending_review", "done"}:
        entry.status = server_status
        save()
        return True
    if server_status != "lyrics_approved":
        entry.status = "lyrics_review_pending"
        save()
        raise BatchError(
            f"{entry.filename} is not lyrics_approved (server={server_status!r})"
        )
    entry.status = "lyrics_approved"
    segments = detail.get("segments_json") or []
    api.generate(entry, entry.job_id, segments, poll_seconds)
    save()
    if entry.status not in {"pending_review", "done"}:
        raise BatchError(entry.error or f"render ended as {entry.status}")
    return True


def process_wave(
    wave: list[AudioManifestEntry],
    *,
    api_factory,
    poll_seconds: float,
    concurrency: int,
    save,
    stage: str = "transcription",
) -> list[AudioManifestEntry]:
    """Run one explicit stage; a failure in one song does not stop peers."""
    if stage == "render":
        with concurrent.futures.ThreadPoolExecutor(max_workers=concurrency) as pool:
            rendered = {
                pool.submit(
                    _render_approved_entry,
                    api_factory(),
                    entry,
                    poll_seconds,
                    save,
                ): entry
                for entry in wave
            }
            for future in concurrent.futures.as_completed(rendered):
                entry = rendered[future]
                try:
                    future.result()
                except Exception as exc:
                    _mark_error(entry, exc, save)
        return wave
    if stage != "transcription":
        raise BatchError(f"unsupported stage: {stage}")

    candidates: list[AudioManifestEntry] = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=concurrency) as pool:
        submitted = {
            pool.submit(_submit_transcription, api_factory(), entry, save): entry
            for entry in wave
        }
        for future in concurrent.futures.as_completed(submitted):
            entry = submitted[future]
            try:
                if future.result():
                    candidates.append(entry)
            except Exception as exc:
                _mark_error(entry, exc, save)

    with concurrent.futures.ThreadPoolExecutor(max_workers=concurrency) as pool:
        reconciled = {
            pool.submit(
                _reconcile_transcription_entry,
                api_factory(), entry, poll_seconds, save,
            ): entry
            for entry in candidates
        }
        for future in concurrent.futures.as_completed(reconciled):
            entry = reconciled[future]
            try:
                future.result()
            except Exception as exc:
                _mark_error(entry, exc, save)
    return wave


def _assert_wave_approved(api: Api, wave: list[AudioManifestEntry]) -> None:
    """Fail before background lookup unless every song is reviewer-approved."""
    unapproved: list[str] = []
    for entry in wave:
        if not entry.job_id:
            unapproved.append(f"{entry.filename}:missing_job")
            continue
        detail = api.request("GET", f"/batch/jobs/{entry.job_id}")
        server_status = str(detail.get("status") or "")
        if server_status in {"pending_review", "done"}:
            # A resumed render wave may contain songs that already completed.
            # They satisfy the pre-background gate without being rendered a
            # second time.
            entry.status = server_status
        elif server_status != "lyrics_approved":
            unapproved.append(f"{entry.filename}:{server_status or 'unknown'}")
        else:
            entry.status = "lyrics_approved"
    if unapproved:
        raise BatchError(
            "background/render stage blocked; lyrics/timings are not approved: "
            + ", ".join(unapproved)
        )


def _stage_counts(entries: list[AudioManifestEntry]) -> dict[str, int]:
    counts = {
        "not_uploaded": 0,
        "transcribing": 0,
        "lyrics_review_pending": 0,
        "lyrics_approved": 0,
        "rendering": 0,
        "final_review_pending": 0,
        "done": 0,
        "error": 0,
    }
    for entry in entries:
        status = str(entry.status or "pending")
        if status in {"pending", "uploading"}:
            key = "not_uploaded"
        elif status in {"transcribing", "transcribing_queued", "transcribed"}:
            key = "transcribing"
        elif status == "lyrics_review_pending":
            key = status
        elif status == "lyrics_approved":
            key = status
        elif status in {"queued", "processing"}:
            key = "rendering"
        elif status == "pending_review":
            key = "final_review_pending"
        elif status == "done":
            key = "done"
        else:
            key = "error"
        counts[key] += 1
    return counts


def run(args: argparse.Namespace) -> int:
    if args.wave_size < 1 or args.concurrency < 1:
        raise BatchError("wave-size and concurrency must be positive")
    stage = getattr(args, "stage", "")
    if stage != "render":
        raise BatchError(
            "legacy universal_batch transcription is disabled: use "
            "scripts/campaign_uploader.py so every job is campaign-bound"
        )
    entries = build_manifest(args.folder)
    manifest_path = Path(args.manifest)
    if manifest_path.exists() and args.resume:
        entries = _merge_entries(entries, load_manifest(manifest_path))
        if args.retry_errors:
            for entry in entries:
                if entry.status == "error":
                    # Crear un intento nuevo es mas seguro que adivinar si el
                    # fallo anterior fue upload, transcripcion o render. El
                    # backend supersede el draft anterior por filename.
                    entry.status = "pending"
                    entry.job_id = None
                    entry.error = None
                    entry.search_result = None
                    entry.scoreboard = None
    store = ManifestStore(manifest_path, entries, args.expected_count)
    store.save()
    print(f"manifest: {manifest_path} ({len(entries)} WAV; expected {args.expected_count})")
    if len(entries) != args.expected_count and not args.allow_count_mismatch:
        print("REFUSING TO CREATE JOBS: WAV count does not match expected_count", file=sys.stderr)
        return 2
    auth = AuthSession(
        args.api_base,
        args.token,
        username=getattr(args, "username", ""),
        password=getattr(args, "password", ""),
        refresh_margin_seconds=getattr(args, "refresh_margin_seconds", 21600),
        force_expire_after_requests=getattr(
            args, "force_token_expiry_after_requests", 0,
        ),
    )
    api = Api(args.api_base, auth=auth)
    # Render resume creates no jobs. Daily/backlog creation limits already
    # accounted for these campaign jobs during manifest upload.
    def _api_factory():
        # requests.Session no garantiza thread-safety; cada task usa la suya.
        return Api(args.api_base, auth=auth)

    canary_size = min(args.canary_size, len(entries))
    if canary_size:
        canary = entries[:canary_size]
        if stage == "render":
            # This check intentionally happens before /backgrounds. All ten
            # exact revisions must be approved as a unit before any visual
            # work starts.
            _assert_wave_approved(api, canary)
            assets = select_backgrounds(api, len(canary))
            assign_profiles(canary, assets)
            store.save()
        process_wave(
            canary, api_factory=_api_factory,
            poll_seconds=args.poll_seconds, concurrency=args.concurrency,
            save=store.save, stage=stage,
        )
        canary_statuses = [entry.status for entry in canary]
        expected_status = (
            {"lyrics_review_pending", "lyrics_approved"}
            if stage == "transcription"
            else {"pending_review", "done"}
        )
        if any(status not in expected_status for status in canary_statuses):
            print(f"CANARY FAILED: statuses={canary_statuses}; refusing remaining waves", file=sys.stderr)
            store.save()
            return 3
        print(
            f"canary {stage} complete: {canary_size}/{len(entries)}; "
            f"stage_counts={json.dumps(_stage_counts(entries), sort_keys=True)}"
        )
        if getattr(args, "force_token_expiry_after_requests", 0):
            required = {"forced_expiry", "recovered_401"}
            if not required.issubset(auth.events):
                raise BatchError(
                    "forced token expiry did not produce a confirmed 401 recovery"
                )
            print("auth resilience: forced expiry -> 401 -> automatic recovery: confirmed")
        if canary_size < len(entries) and not args.continue_after_canary:
            print(
                "stopped after canary; inspect gates, then rerun with "
                "--resume --continue-after-canary"
            )
            return 0
    for offset in range(canary_size, len(entries), args.wave_size):
        wave = entries[offset: offset + args.wave_size]
        if stage == "render":
            _assert_wave_approved(api, wave)
            assets = select_backgrounds(api, len(wave))
            assign_profiles(wave, assets)
            store.save()
        process_wave(
            wave, api_factory=_api_factory, poll_seconds=args.poll_seconds,
            concurrency=args.concurrency, save=store.save, stage=stage,
        )
        print(f"wave complete: {min(offset + args.wave_size, len(entries))}/{len(entries)}")
    accepted = (
        {"lyrics_review_pending", "lyrics_approved"}
        if stage == "transcription" else {"pending_review", "done"}
    )
    failures = [entry for entry in entries if entry.status not in accepted]
    if failures:
        print(
            f"batch complete with {len(failures)} failed song(s); "
            "rerun with --resume after resolving them",
            file=sys.stderr,
        )
        return 4
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--folder", default="/Users/tomi/Downloads/Audio_Wavs 2")
    parser.add_argument("--manifest", default=".context/universal-batch-manifest.json")
    parser.add_argument(
        "--api-base",
        default=os.environ.get(
            "STAGING_API_BASE",
            os.environ.get("STAGING_API_URL", "https://api-staging-9b82.up.railway.app"),
        ),
    )
    parser.add_argument(
        "--token",
        default=(
            os.environ.get("STAGING_BATCH_TOKEN", "")
            or os.environ.get("STAGING_ADMIN_TOKEN", "")
        ),
    )
    parser.add_argument("--expected-count", type=int, default=DEFAULT_EXPECTED_COUNT)
    parser.add_argument("--allow-count-mismatch", action="store_true")
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--retry-errors", action="store_true")
    parser.add_argument(
        "--stage", choices=("render",), required=True,
        help=(
            "resume rendering campaign-bound jobs after lyrics_approved; "
            "use scripts/campaign_uploader.py for upload/transcription"
        ),
    )
    parser.add_argument("--wave-size", type=int, default=30)
    parser.add_argument("--concurrency", type=int, default=5)
    parser.add_argument("--canary-size", type=int, default=10)
    parser.add_argument("--continue-after-canary", action="store_true")
    parser.add_argument("--poll-seconds", type=float, default=5.0)
    parser.add_argument("--capacity-poll-seconds", type=float, default=30.0)
    parser.add_argument("--capacity-wait-seconds", type=float, default=86400.0)
    parser.add_argument(
        "--username", default=(
            os.environ.get("STAGING_BATCH_USERNAME", "")
            or os.environ.get("STAGING_BATCH_USER", "")
        ),
        help=argparse.SUPPRESS,
    )
    parser.add_argument(
        "--password", default=os.environ.get("STAGING_BATCH_PASSWORD", ""),
        help=argparse.SUPPRESS,
    )
    parser.add_argument("--refresh-margin-seconds", type=int, default=21600)
    parser.add_argument(
        "--force-token-expiry-after-requests", type=int, default=0,
        help="canary-only auth resilience proof; requires campaign credentials",
    )
    args = parser.parse_args()
    if not args.token and not (args.username and args.password):
        parser.error(
            "STAGING_BATCH_TOKEN or both STAGING_BATCH_USERNAME/"
            "STAGING_BATCH_PASSWORD are required"
        )
    if args.force_token_expiry_after_requests and not (args.username and args.password):
        parser.error(
            "forced expiry proof requires STAGING_BATCH_USERNAME and "
            "STAGING_BATCH_PASSWORD for automatic recovery"
        )
    return run(args)


if __name__ == "__main__":
    raise SystemExit(main())
