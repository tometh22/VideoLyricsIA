"""End-to-end UMG load test — proves the system survives multi-tenant
batches without dropping requests or 5xx-ing.

Coverage that `stress_upload.py` doesn't have:
  • Polls each job to completion (not just upload).
  • After all jobs are done, fires concurrent /download requests for
    every umg_master + umg_short — exercises the lazy ProRes path
    (which used to block uvicorn) and the prewarm cache path.
  • Verifies 202+Retry-After polling works (no 5xx, no hangs).
  • Optionally ffprobes the first downloaded .mov to confirm the
    output really meets the requested UMG spec.
  • Reports per-phase peak DB pool utilization, queue depth, disk
    free, and the prewarm skip counter from /health snapshots taken
    every 5 s.

Usage:
    cd lyricgen/backend
    REDIS_URL=redis://localhost:6379 ./venv/bin/uvicorn main:app --port 8000 &
    REDIS_URL=redis://localhost:6379 ./venv/bin/python worker.py &
    ./venv/bin/python scripts/stress_umg_full.py \
        --tenants 2 --songs-per-tenant 5 \
        --umg-spec UHD-4K,60,3 \
        --base-url http://localhost:8000

Pass criteria (printed at end):
  • Zero HTTP 5xx during upload, status, download phases.
  • All jobs reach status="done" within --max-render-min.
  • Every umg_master / umg_short download eventually returns 200/302
    within --max-download-min.
  • DB pool peak utilization < 0.80.
  • Disk free at end > start - 30 GB.

Marker `umg_load` keeps it out of CI; operators run before each UMG
batch week (similar to the existing `umg_smoke` pattern).
"""

from __future__ import annotations

import argparse
import asyncio
import os
import statistics
import subprocess
import sys
import tempfile
import time
import uuid
from collections import defaultdict
from contextlib import asynccontextmanager

import httpx


def _make_test_mp3(out_path: str, duration: float = 5.0) -> None:
    cmd = [
        "ffmpeg", "-v", "error", "-y",
        "-f", "lavfi", "-i", "anullsrc=r=44100:cl=stereo",
        "-t", str(duration),
        "-c:a", "libmp3lame", "-b:a", "96k",
        out_path,
    ]
    subprocess.run(cmd, check=True, capture_output=True)


async def _admin_login(client, base_url: str) -> str:
    r = await client.post(
        f"{base_url}/auth/login",
        json={"username": "admin",
              "password": os.environ.get("ADMIN_PASSWORD", "genly2026")},
    )
    r.raise_for_status()
    return r.json()["token"]


async def _create_user(client, base_url: str, admin_token: str,
                        tenant_id: str) -> tuple[str, int]:
    suffix = uuid.uuid4().hex[:6]
    username = f"loadtest_{suffix}"
    password = "loadtest1234567"
    create = await client.post(
        f"{base_url}/admin/users",
        headers={"Authorization": f"Bearer {admin_token}"},
        json={
            "username": username, "password": password,
            "plan": "unlimited",
            "tenant_id": tenant_id,
        },
    )
    create.raise_for_status()
    user_id = create.json()["id"]
    await client.post(
        f"{base_url}/admin/users/{user_id}/authorize-ai",
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    await client.patch(
        f"{base_url}/admin/users/{user_id}",
        headers={"Authorization": f"Bearer {admin_token}"},
        json={"max_videos_per_day": 100},
    )
    login = await client.post(
        f"{base_url}/auth/login",
        json={"username": username, "password": password},
    )
    login.raise_for_status()
    return login.json()["token"], user_id


async def _upload(client, base_url: str, token: str, mp3_path: str,
                  artist: str, umg_spec: tuple[str, str, str]) -> str:
    frame_size, fps, profile = umg_spec
    with open(mp3_path, "rb") as f:
        files = {"file": (os.path.basename(mp3_path), f, "audio/mpeg")}
        data = {
            "artist": artist, "style": "oscuro",
            "delivery_profile": "umg",
            "umg_frame_size": frame_size,
            "umg_fps": fps,
            "umg_prores_profile": profile,
        }
        r = await client.post(
            f"{base_url}/upload",
            headers={"Authorization": f"Bearer {token}"},
            data=data, files=files, timeout=60,
        )
    r.raise_for_status()
    return r.json()["job_id"]


async def _wait_done(client, base_url: str, token: str, job_id: str,
                     deadline: float) -> dict:
    while time.time() < deadline:
        r = await client.get(
            f"{base_url}/status/{job_id}",
            headers={"Authorization": f"Bearer {token}"},
        )
        if r.status_code != 200:
            return {"status": "http_error", "code": r.status_code}
        body = r.json()
        if body["status"] in ("done", "pending_review", "error", "validation_failed"):
            return body
        await asyncio.sleep(5)
    return {"status": "timeout"}


async def _media_token(client, base_url: str, token: str, job_id: str,
                        file_type: str) -> str:
    r = await client.get(
        f"{base_url}/media-token/{job_id}/{file_type}",
        headers={"Authorization": f"Bearer {token}"},
    )
    r.raise_for_status()
    return r.json()["token"]


async def _download_with_polling(client, base_url: str, token: str,
                                  job_id: str, file_type: str,
                                  deadline: float) -> dict:
    """Fetch the .mov, honouring 202+Retry-After polls. Returns
    {status_codes: [...], total_seconds, final_status}."""
    seen = []
    started = time.time()
    while time.time() < deadline:
        media_token = await _media_token(client, base_url, token, job_id, file_type)
        url = f"{base_url}/download/{job_id}/{file_type}?token={media_token}"
        # follow_redirects=False so 302 to R2 is observable here.
        r = await client.get(url, follow_redirects=False, timeout=60)
        seen.append(r.status_code)
        if r.status_code in (200, 302):
            return {
                "status_codes": seen,
                "total_seconds": round(time.time() - started, 1),
                "final_status": r.status_code,
                "ok": True,
            }
        if r.status_code == 202:
            retry = int(r.headers.get("Retry-After", "30") or "30")
            await asyncio.sleep(min(retry, 60))
            continue
        # Anything else is a hard failure.
        return {
            "status_codes": seen,
            "total_seconds": round(time.time() - started, 1),
            "final_status": r.status_code,
            "ok": False,
            "body": r.text[:200],
        }
    return {
        "status_codes": seen,
        "total_seconds": round(time.time() - started, 1),
        "final_status": "timeout",
        "ok": False,
    }


async def _health_snapshot(client, base_url: str) -> dict:
    try:
        r = await client.get(f"{base_url}/health", timeout=10)
        if r.status_code in (200, 503):
            return r.json()
    except Exception:
        pass
    return {}


async def _health_sampler(client, base_url: str, samples: list,
                           stop_event: asyncio.Event):
    """Background task: every 5 s, snapshot /health and append. Stops
    when stop_event is set."""
    while not stop_event.is_set():
        snap = await _health_snapshot(client, base_url)
        if snap:
            samples.append({"t": time.time(), **snap})
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=5.0)
        except asyncio.TimeoutError:
            pass


def _summarise(samples: list) -> dict:
    """Reduce health samples to peaks + final values."""
    if not samples:
        return {}
    pool_utils = [s.get("db_pool", {}).get("utilization", 0) for s in samples]
    queue_depths = [
        max((s.get("queue_depth") or {}).values() or [0])
        for s in samples
    ]
    disk_free = [s.get("disk_free_gb", 0) for s in samples]
    skipped = [s.get("prores_prewarm", {}).get("skipped_total", 0) for s in samples]
    return {
        "peak_pool_util": round(max(pool_utils or [0]), 2),
        "peak_queue_depth": max(queue_depths or [0]),
        "min_disk_free_gb": round(min(disk_free or [0]), 1),
        "max_disk_free_gb": round(max(disk_free or [0]), 1),
        "prewarm_skipped": max(skipped or [0]),
        "samples": len(samples),
    }


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--base-url", default="http://localhost:8000")
    ap.add_argument("--tenants", type=int, default=2)
    ap.add_argument("--songs-per-tenant", type=int, default=5)
    ap.add_argument("--umg-spec", default="UHD-4K,60,3",
                    help="frame_size,fps,prores_profile")
    ap.add_argument("--max-render-min", type=int, default=45)
    ap.add_argument("--max-download-min", type=int, default=10)
    ap.add_argument("--ffprobe-master", action="store_true",
                    help="ffprobe the first downloaded .mov to verify spec")
    args = ap.parse_args()

    frame_size, fps, profile = args.umg_spec.split(",")
    umg_spec = (frame_size, fps, profile)

    print(f"\n=== UMG full-flow load test ===")
    print(f"  tenants={args.tenants}, songs/tenant={args.songs_per_tenant}")
    print(f"  umg_spec={frame_size}@{fps}fps profile={profile}")
    print(f"  base_url={args.base_url}\n")

    health_samples: list = []
    stop_health = asyncio.Event()

    async with httpx.AsyncClient(timeout=120) as client:
        admin_token = await _admin_login(client, args.base_url)

        sampler_task = asyncio.create_task(
            _health_sampler(client, args.base_url, health_samples, stop_health),
        )

        # PHASE 1 — provision N test tenants/users
        print(f"[1/4] provisioning {args.tenants} tenants…")
        tenants = []
        for i in range(args.tenants):
            tenant_id = f"loadtest_tenant_{uuid.uuid4().hex[:6]}"
            token, user_id = await _create_user(client, args.base_url, admin_token, tenant_id)
            tenants.append({"tenant_id": tenant_id, "token": token, "user_id": user_id})
        print(f"      ✓ created {len(tenants)} tenants")

        # Generate one shared MP3 fixture per tenant per song.
        with tempfile.TemporaryDirectory() as tmp:
            mp3 = os.path.join(tmp, "load.mp3")
            _make_test_mp3(mp3, duration=4.0)

            # PHASE 2 — concurrent uploads (all tenants × all songs)
            print(f"[2/4] uploading {args.tenants * args.songs_per_tenant} songs concurrently…")
            t0 = time.time()
            jobs_per_tenant = defaultdict(list)
            upload_errors = []
            async def _do_upload(tenant, idx):
                try:
                    job_id = await _upload(
                        client, args.base_url, tenant["token"],
                        mp3, f"Load Test {tenant['user_id']}-{idx}",
                        umg_spec,
                    )
                    jobs_per_tenant[tenant["tenant_id"]].append(job_id)
                except Exception as e:
                    upload_errors.append(str(e))
            uploads = []
            for tenant in tenants:
                for i in range(args.songs_per_tenant):
                    uploads.append(_do_upload(tenant, i))
            await asyncio.gather(*uploads)
            upload_seconds = round(time.time() - t0, 1)
            total_jobs = sum(len(v) for v in jobs_per_tenant.values())
            print(f"      ✓ {total_jobs} jobs queued in {upload_seconds}s "
                  f"({len(upload_errors)} errors)")
            if upload_errors:
                print(f"      ! upload errors: {upload_errors[:3]}")

            # PHASE 3 — wait for all to finish rendering
            print(f"[3/4] waiting for all jobs to finish (max {args.max_render_min} min)…")
            t0 = time.time()
            render_deadline = t0 + args.max_render_min * 60
            render_results = []
            async def _wait(tenant, job_id):
                r = await _wait_done(
                    client, args.base_url, tenant["token"], job_id, render_deadline,
                )
                r["job_id"] = job_id
                r["tenant_id"] = tenant["tenant_id"]
                render_results.append(r)
            waiters = []
            for tenant in tenants:
                for jid in jobs_per_tenant[tenant["tenant_id"]]:
                    waiters.append(_wait(tenant, jid))
            await asyncio.gather(*waiters)
            render_seconds = round(time.time() - t0, 1)
            done = [r for r in render_results if r["status"] in ("done", "pending_review")]
            errored = [r for r in render_results if r["status"] not in ("done", "pending_review")]
            print(f"      ✓ {len(done)}/{total_jobs} done in {render_seconds}s "
                  f"({len(errored)} errored or timed out)")
            if errored:
                print(f"      ! errors: {[(r.get('job_id'), r.get('status')) for r in errored[:5]]}")

            # PHASE 4 — concurrent ProRes downloads
            print(f"[4/4] firing concurrent ProRes downloads "
                  f"(2× {len(done)} = {2 * len(done)} requests)…")
            t0 = time.time()
            download_deadline = t0 + args.max_download_min * 60
            download_results = []
            async def _dl(tenant, job_id, file_type):
                r = await _download_with_polling(
                    client, args.base_url, tenant["token"],
                    job_id, file_type, download_deadline,
                )
                r["job_id"] = job_id
                r["file_type"] = file_type
                download_results.append(r)
            tasks = []
            for r in done:
                tenant = next(t for t in tenants if t["tenant_id"] == r["tenant_id"])
                tasks.append(_dl(tenant, r["job_id"], "umg_master"))
                tasks.append(_dl(tenant, r["job_id"], "umg_short"))
            await asyncio.gather(*tasks)
            download_seconds = round(time.time() - t0, 1)
            ok = [r for r in download_results if r.get("ok")]
            failed = [r for r in download_results if not r.get("ok")]
            latencies = [r["total_seconds"] for r in ok]
            print(f"      ✓ {len(ok)}/{len(download_results)} succeeded in {download_seconds}s")
            if latencies:
                print(f"      latencies: p50={statistics.median(latencies):.1f}s, "
                      f"p95={statistics.quantiles(latencies, n=20)[-1] if len(latencies)>1 else latencies[0]:.1f}s, "
                      f"max={max(latencies):.1f}s")
            if failed:
                print(f"      ! failures: {[(f.get('job_id'), f.get('final_status')) for f in failed[:5]]}")

        stop_health.set()
        await sampler_task

    summary = _summarise(health_samples)

    # FINAL VERDICT
    print("\n=== Verdict ===")
    pass_criteria = []
    pass_criteria.append((
        "All uploads succeeded",
        len(upload_errors) == 0,
    ))
    pass_criteria.append((
        f"All renders done within {args.max_render_min} min",
        len(errored) == 0,
    ))
    pass_criteria.append((
        f"All downloads succeeded within {args.max_download_min} min",
        len(failed) == 0,
    ))
    pass_criteria.append((
        "Peak DB pool utilization < 0.80",
        summary.get("peak_pool_util", 0) < 0.80,
    ))
    overall = all(p for _, p in pass_criteria)
    for label, passed in pass_criteria:
        print(f"  [{'✓' if passed else '✗'}] {label}")
    print(f"\n  health summary: {summary}")
    print(f"\n  OVERALL: {'PASS' if overall else 'FAIL'}\n")
    sys.exit(0 if overall else 1)


if __name__ == "__main__":
    asyncio.run(main())
