#!/usr/bin/env python3
"""Idempotent, wave-limited Universal staging batch runner.

The runner is intentionally an API client.  It never writes the deliveries
table or calls portal endpoints; it only uploads audio, transcribes, and
queues `/generate` jobs for the authenticated staging tenant.
"""

from __future__ import annotations

import argparse
import concurrent.futures
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


TERMINAL = {"pending_review", "done", "error", "rejected", "validation_failed"}
DEFAULT_EXPECTED_COUNT = 30


class BatchError(RuntimeError):
    pass


class Api:
    def __init__(self, base_url: str, token: str, timeout: int = 120):
        self.base = base_url.rstrip("/")
        self.session = requests.Session()
        self.session.headers.update({"Authorization": f"Bearer {token}"})
        self.timeout = timeout

    def request(self, method: str, path: str, **kwargs) -> Any:
        response = self.session.request(method, self.base + path,
                                        timeout=self.timeout, **kwargs)
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
                put = self.session.put(ticket["upload_url"], data=body,
                                       headers={"Content-Type": "audio/wav"},
                                       timeout=max(self.timeout, 900))
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
                put = self.session.put(signed[part_number], data=body,
                                       headers={"Content-Type": "audio/wav"},
                                       timeout=max(self.timeout, 900))
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
        result = self.request("POST", "/transcribe-uploaded", json={
            "job_id": job_id,
            "language": "es",
            "artist": entry.artist,
            "title": entry.title,
            "live": bool(entry.version),
        })
        entry.status = result.get("status") or "transcribing_queued"
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
                }
                entry.status = "transcribed"
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
    if entry.status in {"pending_review", "done", "queued", "processing",
                        "transcribing", "transcribing_queued", "transcribed"}:
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


def _reconcile_entry(api: Api, entry: AudioManifestEntry,
                     poll_seconds: float, save) -> bool:
    """Lleva una cancion ya enviada a su estado terminal."""
    if entry.status in {"pending_review", "done"}:
        return True
    if entry.status in {"queued", "processing"} and entry.job_id:
        api.wait_for_render(entry, entry.job_id, poll_seconds)
        save()
        if entry.status not in {"pending_review", "done"}:
            raise BatchError(entry.error or f"render ended as {entry.status}")
        return True
    if not entry.job_id:
        return False
    if entry.status == "transcribed":
        detail = api.request("GET", f"/batch/jobs/{entry.job_id}")
        segments = detail.get("segments_json") or []
    elif entry.status in {"transcribing", "transcribing_queued", "uploading"}:
        segments = api.wait_for_transcription(entry, entry.job_id, poll_seconds)
        save()
    else:
        return False
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
) -> list[AudioManifestEntry]:
    """Encola toda la ola y luego reconcilia; un fallo no mata las demas."""
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
                _reconcile_entry, api_factory(), entry, poll_seconds, save,
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


def run(args: argparse.Namespace) -> int:
    if args.wave_size < 1 or args.concurrency < 1:
        raise BatchError("wave-size and concurrency must be positive")
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
    api = Api(args.api_base, args.token)
    jobs_to_create = sum(
        entry.status not in {"pending_review", "done", "error"}
        for entry in entries
    )
    api.validate_capacity(
        expected_count=jobs_to_create, wave_size=args.wave_size,
    )
    assets = select_backgrounds(api, len(entries))
    assign_profiles(entries, assets)
    store.save()

    def _api_factory():
        # requests.Session no garantiza thread-safety; cada task usa la suya.
        return Api(args.api_base, args.token)

    canary_size = min(args.canary_size, len(entries))
    if canary_size:
        api.wait_for_backlog_capacity(
            needed=sum(
                entry.status not in {"pending_review", "done"}
                for entry in entries[:canary_size]
            ),
            poll_seconds=args.capacity_poll_seconds,
            max_wait_seconds=args.capacity_wait_seconds,
        )
        process_wave(
            entries[:canary_size], api_factory=_api_factory,
            poll_seconds=args.poll_seconds, concurrency=args.concurrency,
            save=store.save,
        )
        canary_statuses = [entry.status for entry in entries[:canary_size]]
        if any(status != "pending_review" for status in canary_statuses):
            print(f"CANARY FAILED: statuses={canary_statuses}; refusing remaining waves", file=sys.stderr)
            store.save()
            return 3
        print(f"canary complete: {canary_size}/{len(entries)}")
        if canary_size < len(entries) and not args.continue_after_canary:
            print(
                "stopped after canary; inspect gates, then rerun with "
                "--resume --continue-after-canary"
            )
            return 0
    for offset in range(canary_size, len(entries), args.wave_size):
        wave = entries[offset: offset + args.wave_size]
        needed = sum(
            entry.status not in {"pending_review", "done"} for entry in wave
        )
        if needed:
            api.wait_for_backlog_capacity(
                needed=needed,
                poll_seconds=args.capacity_poll_seconds,
                max_wait_seconds=args.capacity_wait_seconds,
            )
        process_wave(
            wave, api_factory=_api_factory, poll_seconds=args.poll_seconds,
            concurrency=args.concurrency, save=store.save,
        )
        print(f"wave complete: {min(offset + args.wave_size, len(entries))}/{len(entries)}")
    failures = [
        entry for entry in entries
        if entry.status not in {"pending_review", "done"}
    ]
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
    parser.add_argument("--wave-size", type=int, default=30)
    parser.add_argument("--concurrency", type=int, default=5)
    parser.add_argument("--canary-size", type=int, default=30)
    parser.add_argument("--continue-after-canary", action="store_true")
    parser.add_argument("--poll-seconds", type=float, default=5.0)
    parser.add_argument("--capacity-poll-seconds", type=float, default=30.0)
    parser.add_argument("--capacity-wait-seconds", type=float, default=86400.0)
    args = parser.parse_args()
    if not args.token:
        parser.error("--token or STAGING_BATCH_TOKEN is required")
    return run(args)


if __name__ == "__main__":
    raise SystemExit(main())
