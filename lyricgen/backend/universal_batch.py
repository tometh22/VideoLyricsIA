#!/usr/bin/env python3
"""Idempotent, wave-limited Universal staging batch runner.

The runner is intentionally an API client.  It never writes the deliveries
table or calls portal endpoints; it only uploads audio, transcribes, and
queues `/generate` jobs for the authenticated staging tenant.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
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
        self.request("POST", "/transcribe-uploaded", json={
            "job_id": job_id,
            "language": "es",
            "artist": entry.artist,
            "title": entry.title,
            "live": bool(entry.version),
        })
        return self.wait_for_transcription(entry, job_id, poll_seconds)

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
    if len(videos) < count // 2 or len(photos) < count - count // 2:
        raise BatchError(f"background library lacks 15 video + 15 photo assets ({len(videos)} + {len(photos)})")
    # Tags are retained in the manifest through the selected asset id; the
    # server remains authoritative for actual file access and tenant scope.
    return videos[:count // 2] + photos[:count - count // 2]


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


def run(args: argparse.Namespace) -> int:
    entries = build_manifest(args.folder)
    manifest_path = Path(args.manifest)
    if manifest_path.exists() and args.resume:
        entries = _merge_entries(entries, load_manifest(manifest_path))
    write_manifest(manifest_path, entries, expected_count=args.expected_count)
    print(f"manifest: {manifest_path} ({len(entries)} WAV; expected {args.expected_count})")
    if len(entries) != args.expected_count and not args.allow_count_mismatch:
        print("REFUSING TO CREATE JOBS: WAV count does not match expected_count", file=sys.stderr)
        return 2
    api = Api(args.api_base, args.token)
    assets = select_backgrounds(api, len(entries))
    assign_profiles(entries, assets)
    write_manifest(manifest_path, entries, expected_count=args.expected_count)

    def process_wave(wave: list[AudioManifestEntry]) -> None:
        for entry in wave:
            if entry.job_id and entry.status in {"queued", "processing"}:
                # A runner restart must observe the existing job, never call
                # /upload-url a second time for the same checksum.
                api.wait_for_render(entry, entry.job_id, args.poll_seconds)
                write_manifest(manifest_path, entries, expected_count=args.expected_count)
                continue
            if entry.job_id and entry.status in {"transcribing", "transcribing_queued"}:
                segments = api.wait_for_transcription(entry, entry.job_id, args.poll_seconds)
                api.generate(entry, entry.job_id, segments, args.poll_seconds)
                write_manifest(manifest_path, entries, expected_count=args.expected_count)
                continue
            if entry.job_id and entry.status == "uploading":
                existing = api.request("GET", f"/batch/jobs/{entry.job_id}")
                if existing.get("status") in {"transcribed_pending", "transcribed"}:
                    segments = existing.get("segments_json") or []
                    api.generate(entry, entry.job_id, segments, args.poll_seconds)
                    write_manifest(manifest_path, entries, expected_count=args.expected_count)
                    continue
            if entry.status in {"pending_review", "done"} and entry.job_id:
                continue
            try:
                entry.status = "uploading"
                entry.job_id = api.upload_audio(entry)
                segments = api.transcribe(entry, entry.job_id, args.poll_seconds)
                api.generate(entry, entry.job_id, segments, args.poll_seconds)
            except Exception as exc:
                entry.status = "error"
                entry.error = str(exc)[:1000]
                write_manifest(manifest_path, entries, expected_count=args.expected_count)
                raise
            write_manifest(manifest_path, entries, expected_count=args.expected_count)

    canary_size = min(args.canary_size, len(entries))
    if canary_size:
        process_wave(entries[:canary_size])
        canary_statuses = [entry.status for entry in entries[:canary_size]]
        if any(status != "pending_review" for status in canary_statuses):
            print(f"CANARY FAILED: statuses={canary_statuses}; refusing remaining waves", file=sys.stderr)
            write_manifest(manifest_path, entries, expected_count=args.expected_count)
            return 3
        print(f"canary complete: {canary_size}/{len(entries)}")
    for offset in range(canary_size, len(entries), args.wave_size):
        wave = entries[offset: offset + args.wave_size]
        process_wave(wave)
        print(f"wave complete: {min(offset + args.wave_size, len(entries))}/{len(entries)}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--folder", default="/Users/tomi/Downloads/Audio_Wavs 2")
    parser.add_argument("--manifest", default=".context/universal-batch-manifest.json")
    parser.add_argument("--api-base", default=os.environ.get("STAGING_API_BASE", "https://staging.genly.pro"))
    parser.add_argument("--token", default=os.environ.get("STAGING_ADMIN_TOKEN", ""))
    parser.add_argument("--expected-count", type=int, default=DEFAULT_EXPECTED_COUNT)
    parser.add_argument("--allow-count-mismatch", action="store_true")
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--wave-size", type=int, default=5)
    parser.add_argument("--canary-size", type=int, default=3)
    parser.add_argument("--poll-seconds", type=float, default=5.0)
    args = parser.parse_args()
    if not args.token:
        parser.error("--token or STAGING_ADMIN_TOKEN is required")
    return run(args)


if __name__ == "__main__":
    raise SystemExit(main())
