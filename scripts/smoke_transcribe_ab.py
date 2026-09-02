#!/usr/bin/env python3
"""Smoke test A/B — testea el flow transcribe contra staging via API directa.

Hace upload de N audios y mide:
  - tiempo total (upload + transcribe)
  - success / failure por archivo
  - segment count + presencia de reference_lyrics
  - status flow del job (queued → transcribing → done)

Compara baseline (path sync legacy) vs nuevo (path async, PR #271). El path se
detecta autodescriptivamente: si POST /transcribe-uploaded devuelve segments
inline → sync legacy. Si devuelve {status: "transcribing_queued"} → async.

USO:
  python3 scripts/smoke_transcribe_ab.py \\
    --api https://api-staging-9b82.up.railway.app \\
    --user TUUSER --password TUPASS \\
    --dir ~/Downloads \\
    --max 20 \\
    --label baseline   # o "post-pr271"

Outputs:
  - logs por archivo en stdout
  - tabla resumen al final
  - JSON con todos los detalles en /tmp/smoke_transcribe_<label>.json
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import requests


AUDIO_EXTS = {".mp3", ".wav", ".m4a", ".aac", ".flac", ".ogg"}


def login(api: str, user: str, password: str) -> str:
    r = requests.post(
        f"{api}/auth/login",
        json={"username": user, "password": password},
        timeout=15,
    )
    r.raise_for_status()
    data = r.json()
    # API devuelve "token" (no "access_token"); fallback por compat.
    return data.get("token") or data["access_token"]


def list_audios(dir_path: Path, max_n: int) -> list[Path]:
    files = sorted(
        p for p in dir_path.iterdir()
        if p.is_file() and p.suffix.lower() in AUDIO_EXTS
    )
    return files[:max_n]


def parse_artist_song(name: str) -> tuple[str, str]:
    """Best-effort parse of 'Artist - Song.ext' or 'Song - Artist.ext'."""
    stem = Path(name).stem
    # Drop "_spotdown.org" suffix.
    stem = stem.replace("_spotdown.org", "").strip()
    if " - " in stem:
        a, b = stem.split(" - ", 1)
        return a.strip(), b.strip()
    if "_" in stem:
        a, b = stem.split("_", 1)
        return a.strip(), b.strip()
    return "", stem.strip()


def request_upload_url(api: str, token: str, filename: str, size: int,
                       artist: str = "", title: str = "") -> dict:
    r = requests.post(
        f"{api}/upload-url",
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        json={
            "filename": filename,
            "size_bytes": size,           # backend field name (no `size`)
            "content_type": "audio/mpeg" if filename.lower().endswith(".mp3") else "audio/wav",
            "artist": artist,
            "title": title,
        },
        timeout=30,
    )
    r.raise_for_status()
    return r.json()


def upload_to_r2(presigned_url: str, audio_path: Path, content_type: str) -> tuple[bool, str]:
    """PUT a R2 con el content-type EXACTO que el backend firmó. Si no
    coincide, R2 devuelve 403 SignatureDoesNotMatch."""
    with audio_path.open("rb") as fh:
        r = requests.put(
            presigned_url,
            data=fh,
            headers={"Content-Type": content_type},
            timeout=600,  # 10 min for big files
        )
    if r.ok:
        return True, ""
    return False, f"HTTP {r.status_code}: {r.text[:200]}"


def post_transcribe(api: str, token: str, job_id: str, language: str,
                    artist: str, title: str) -> dict:
    r = requests.post(
        f"{api}/transcribe-uploaded",
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        json={
            "job_id": job_id,
            "language": language,
            "artist": artist,
            "title": title,
        },
        # 5 min — sync path can take 30s+ on bad lrclib lookup; async returns immediate.
        timeout=300,
    )
    r.raise_for_status()
    return r.json()


def poll_status(api: str, token: str, job_id: str,
                timeout_s: int = 300) -> dict | None:
    """Polls /transcription-status until status is terminal. Returns final payload
    or None on timeout. Backoff 1.5s → 5s. Tolerates transient 429/5xx by
    sleeping and retrying — only gives up on persistent failure."""
    delay = 1.5
    consecutive_errors = 0
    start = time.monotonic()
    while time.monotonic() - start < timeout_s:
        try:
            r = requests.get(
                f"{api}/transcription-status/{job_id}",
                headers={"Authorization": f"Bearer {token}"},
                timeout=15,
            )
            if r.ok:
                data = r.json()
                consecutive_errors = 0
                status = data.get("status")
                if status == "transcribed":
                    return data
                if status == "transcription_failed":
                    return data
            else:
                # 429, 5xx, etc — back off but don't abort. Backend may be
                # rate-limited or rolling deploy.
                consecutive_errors += 1
                if consecutive_errors > 20:
                    return None  # genuine sustained failure
        except Exception:
            consecutive_errors += 1
            if consecutive_errors > 20:
                return None
        time.sleep(delay)
        delay = min(delay * 1.2, 5.0)
    return None


def test_one(api: str, token: str, audio: Path, label: str) -> dict:
    """Test a single audio file end-to-end. Returns a result dict."""
    artist, song = parse_artist_song(audio.name)
    started = time.monotonic()
    result = {
        "file": audio.name,
        "size_mb": round(audio.stat().st_size / (1024 * 1024), 1),
        "artist": artist,
        "song": song,
        "label": label,
        "phases": {},
        "ok": False,
        "error": None,
    }

    try:
        t0 = time.monotonic()
        upload_info = request_upload_url(
            api, token, audio.name, audio.stat().st_size,
            artist=artist, title=song,
        )
        result["phases"]["upload_url"] = round(time.monotonic() - t0, 2)
        job_id = upload_info["job_id"]
        result["job_id"] = job_id
        # Multipart upload not implemented in this smoke — bail if needed.
        if upload_info.get("use_multipart"):
            raise RuntimeError(
                f"File too large for single-PUT smoke (use_multipart=true)"
            )

        t0 = time.monotonic()
        # Single-PUT path. Multipart not implemented here.
        url = upload_info.get("upload_url") or upload_info.get("url")
        if not url:
            raise RuntimeError(f"No upload URL in response: {upload_info}")
        ct = "audio/mpeg" if audio.suffix.lower() == ".mp3" else "audio/wav"
        ok, err = upload_to_r2(url, audio, ct)
        if not ok:
            raise RuntimeError(f"R2 upload failed: {err}")
        result["phases"]["r2_upload"] = round(time.monotonic() - t0, 2)

        t0 = time.monotonic()
        initial = post_transcribe(api, token, job_id, "es", artist, song)
        result["phases"]["post_transcribe"] = round(time.monotonic() - t0, 2)
        result["initial_status"] = initial.get("status")

        # Branch — sync legacy returns segments inline; async returns 202 + job_id.
        if initial.get("segments") is not None:
            result["path"] = "sync_legacy"
            data = initial
        else:
            result["path"] = "async_queued"
            t0 = time.monotonic()
            data = poll_status(api, token, job_id)
            result["phases"]["poll_status"] = round(time.monotonic() - t0, 2)
            if not data:
                raise RuntimeError("Polling timeout")
            if data.get("status") == "transcription_failed":
                raise RuntimeError(f"Worker failed: {data.get('error')}")

        result["segments_n"] = len(data.get("segments") or [])
        result["reference_lyrics_chars"] = len(data.get("reference_lyrics") or "")
        result["coverage_warning"] = bool(data.get("coverage_warning"))
        result["recovery_source"] = data.get("recovery_source")
        result["ok"] = True
    except Exception as e:
        result["error"] = str(e)[:300]
    result["total_s"] = round(time.monotonic() - started, 2)
    return result


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--api", required=True)
    ap.add_argument("--user", required=True)
    ap.add_argument("--password", required=True)
    ap.add_argument("--dir", required=True, type=Path)
    ap.add_argument("--max", type=int, default=20)
    ap.add_argument("--label", default="baseline")
    ap.add_argument("--concurrency", type=int, default=3,
                    help="Concurrent uploads (default 3 — match plan v1 target)")
    args = ap.parse_args()

    dir_path = args.dir.expanduser().resolve()
    audios = list_audios(dir_path, args.max)
    if not audios:
        print(f"No audios found in {dir_path}", file=sys.stderr)
        sys.exit(1)

    print(f"\n=== SMOKE A/B — label={args.label} ===")
    print(f"API: {args.api}")
    print(f"Files: {len(audios)} from {dir_path}")
    print(f"Concurrency: {args.concurrency}\n")

    token = login(args.api, args.user, args.password)
    print(f"✓ Auth OK\n")

    results = []
    with ThreadPoolExecutor(max_workers=args.concurrency) as ex:
        futures = {
            ex.submit(test_one, args.api, token, audio, args.label): audio.name
            for audio in audios
        }
        for fut in as_completed(futures):
            r = fut.result()
            status = "✓" if r["ok"] else "✗"
            line = f"  {status} {r['file'][:45]:<45} {r['total_s']:>6}s "
            if r["ok"]:
                line += f"path={r['path']} segs={r['segments_n']} ref={r['reference_lyrics_chars']}ch"
                if r.get("coverage_warning"):
                    line += " ⚠cov"
            else:
                line += f"ERR={r['error']}"
            print(line)
            results.append(r)

    # Summary
    ok = [r for r in results if r["ok"]]
    bad = [r for r in results if not r["ok"]]
    paths = {}
    for r in ok:
        paths[r.get("path", "?")] = paths.get(r.get("path", "?"), 0) + 1

    print(f"\n=== SUMMARY ({args.label}) ===")
    print(f"  Success: {len(ok)}/{len(results)}")
    print(f"  Paths: {paths}")
    if ok:
        times = sorted(r["total_s"] for r in ok)
        p50 = times[len(times) // 2]
        p95 = times[int(len(times) * 0.95)] if len(times) > 1 else times[0]
        print(f"  Latency p50={p50}s p95={p95}s max={max(times)}s")
    if bad:
        print(f"  Errors:")
        for r in bad:
            print(f"    - {r['file']}: {r['error']}")

    out_path = Path(f"/tmp/smoke_transcribe_{args.label}.json")
    out_path.write_text(json.dumps(results, indent=2))
    print(f"\n  Full results → {out_path}")


if __name__ == "__main__":
    main()
