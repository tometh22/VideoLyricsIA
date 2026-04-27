"""Concurrent-upload stress test.

Purpose: simulate one UMG user uploading N MP3s in parallel and see what
breaks BEFORE production sees it. Reports per-request latency, success rate,
queue depth, and the bottlenecks the architecture map predicts.

Usage:
    # 1. Start the backend + a worker locally:
    #    cd lyricgen/backend
    #    REDIS_URL=redis://localhost:6379 ./venv/bin/uvicorn main:app --port 8000
    #    REDIS_URL=redis://localhost:6379 ./venv/bin/python worker.py  # in another shell
    #    (start redis if not running: brew services start redis)
    #
    # 2. Run the stress test:
    #    ./venv/bin/python scripts/stress_upload.py --concurrent 20 --base-url http://localhost:8000
    #
    # The script auto-creates a stress test user with a high daily cap so the
    # cap doesn't interfere with the test itself.
"""

import argparse
import asyncio
import os
import statistics
import subprocess
import sys
import tempfile
import time
import uuid

import httpx


def _make_test_mp3(out_path: str, duration: float = 5.0) -> None:
    """Generate a tiny silent MP3 fixture (so we don't need real audio)."""
    cmd = [
        "ffmpeg", "-v", "error", "-y",
        "-f", "lavfi", "-i", "anullsrc=r=44100:cl=stereo",
        "-t", str(duration),
        "-c:a", "libmp3lame", "-b:a", "96k",
        out_path,
    ]
    subprocess.run(cmd, check=True, capture_output=True)


async def _admin_login(client: httpx.AsyncClient, base_url: str) -> str:
    """Login as the default admin (created by ensure_default_admin)."""
    r = await client.post(f"{base_url}/auth/login", json={
        "username": "admin",
        "password": os.environ.get("ADMIN_PASSWORD", "genly2026"),
    })
    r.raise_for_status()
    return r.json()["token"]


async def _create_stress_user(client: httpx.AsyncClient, base_url: str,
                              admin_token: str) -> tuple[str, int]:
    """Create a fresh stress-test user with a high daily cap. Returns
    (user_token, user_id)."""
    suffix = uuid.uuid4().hex[:6]
    username = f"stress_{suffix}"
    password = "stresstest1234"

    create = await client.post(
        f"{base_url}/admin/users",
        headers={"Authorization": f"Bearer {admin_token}"},
        json={"username": username, "password": password, "plan": "unlimited"},
    )
    create.raise_for_status()
    user_id = create.json()["id"]

    # Authorize for AI usage and raise the daily cap so the cap doesn't gate
    # the test.
    await client.post(
        f"{base_url}/admin/users/{user_id}/authorize-ai",
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    await client.patch(
        f"{base_url}/admin/users/{user_id}",
        headers={"Authorization": f"Bearer {admin_token}"},
        json={"max_videos_per_day": 1000},
    )

    # Login as the new user.
    login = await client.post(f"{base_url}/auth/login", json={
        "username": username, "password": password,
    })
    login.raise_for_status()
    return login.json()["token"], user_id


async def _upload_one(client: httpx.AsyncClient, base_url: str, token: str,
                      mp3_path: str, idx: int) -> dict:
    """Single upload — returns timing + outcome dict."""
    started = time.perf_counter()
    try:
        with open(mp3_path, "rb") as f:
            r = await client.post(
                f"{base_url}/upload",
                headers={"Authorization": f"Bearer {token}"},
                files={"file": (f"stress_{idx}.mp3", f, "audio/mpeg")},
                data={
                    "artist": f"StressTest{idx}",
                    "style": "oscuro",
                    "delivery_profile": "youtube",
                },
                timeout=60.0,
            )
        elapsed = time.perf_counter() - started
        if r.status_code == 200:
            return {
                "idx": idx,
                "status": "ok",
                "code": 200,
                "elapsed_s": round(elapsed, 3),
                "job_id": r.json().get("job_id"),
            }
        else:
            return {
                "idx": idx,
                "status": "http_error",
                "code": r.status_code,
                "elapsed_s": round(elapsed, 3),
                "body": r.text[:200],
            }
    except (httpx.TimeoutException, httpx.RequestError) as e:
        return {
            "idx": idx,
            "status": "exception",
            "code": None,
            "elapsed_s": round(time.perf_counter() - started, 3),
            "body": str(e)[:200],
        }


async def _poll_queue_depth(client: httpx.AsyncClient, base_url: str,
                            admin_token: str) -> dict:
    """Hit /admin/queue-depth or /health for queue depth. Falls back to /health."""
    try:
        r = await client.get(
            f"{base_url}/health",
            headers={"Authorization": f"Bearer {admin_token}"},
            timeout=5.0,
        )
        if r.status_code == 200:
            return r.json()
    except httpx.RequestError:
        pass
    return {}


async def main(args):
    base_url = args.base_url.rstrip("/")
    n = args.concurrent

    # Build N MP3 fixtures.
    print(f"[stress] generating {n} MP3 fixtures...")
    tmpdir = tempfile.mkdtemp(prefix="stress-")
    mp3_paths = []
    for i in range(n):
        path = os.path.join(tmpdir, f"stress_{i}.mp3")
        _make_test_mp3(path, duration=args.duration)
        mp3_paths.append(path)
    print(f"[stress] fixtures in {tmpdir}")

    async with httpx.AsyncClient() as client:
        # Authenticate.
        print("[stress] logging in as admin...")
        admin_token = await _admin_login(client, base_url)

        print("[stress] creating stress-test user...")
        user_token, user_id = await _create_stress_user(client, base_url, admin_token)
        print(f"[stress] stress user id={user_id}")

        # Snapshot queue depth before.
        before = await _poll_queue_depth(client, base_url, admin_token)
        print(f"[stress] queue depth before: {before}")

        # Fire all N uploads concurrently.
        print(f"[stress] firing {n} concurrent uploads...")
        t0 = time.perf_counter()
        results = await asyncio.gather(*[
            _upload_one(client, base_url, user_token, mp3_paths[i], i)
            for i in range(n)
        ])
        elapsed = time.perf_counter() - t0

        # Snapshot queue depth right after.
        after = await _poll_queue_depth(client, base_url, admin_token)

    # Report.
    print("\n" + "=" * 70)
    print(f"STRESS TEST REPORT — {n} concurrent uploads")
    print("=" * 70)
    print(f"Total wall-clock: {elapsed:.2f}s")

    by_status = {}
    for r in results:
        by_status.setdefault(r["status"], []).append(r)

    for status, items in by_status.items():
        print(f"\n  [{status}]: {len(items)} requests")
        if items:
            elapseds = [r["elapsed_s"] for r in items]
            print(f"    elapsed: min={min(elapseds):.2f}s "
                  f"median={statistics.median(elapseds):.2f}s "
                  f"max={max(elapseds):.2f}s")
        if status != "ok":
            for r in items[:5]:
                code_str = r.get("code", "n/a")
                body = (r.get("body") or "")[:120]
                print(f"      idx={r['idx']} code={code_str} body={body!r}")
            if len(items) > 5:
                print(f"      ... ({len(items) - 5} more)")

    print(f"\nQueue depth before: {before}")
    print(f"Queue depth after:  {after}")

    success = len(by_status.get("ok", []))
    print(f"\nSuccess rate: {success}/{n} ({100 * success / n:.0f}%)")

    if success < n:
        print("\n⚠️  Failures detected — see breakdown above.")
        sys.exit(1)
    print("\n✅ All uploads accepted. Now watch worker logs to verify queue drains cleanly.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://localhost:8000",
                        help="Backend base URL")
    parser.add_argument("--concurrent", type=int, default=20,
                        help="Number of simultaneous uploads")
    parser.add_argument("--duration", type=float, default=5.0,
                        help="Duration in seconds of each test MP3 fixture")
    args = parser.parse_args()
    asyncio.run(main(args))
