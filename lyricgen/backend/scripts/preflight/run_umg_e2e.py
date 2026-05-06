"""End-to-end UMG master verification.

Submits one MP3 with delivery_profile=umg + the requested ProRes / fps /
resolution, polls until the job finishes, downloads the resulting .mov,
then runs umg_master_conformance against it. Prints a clean GO / NO-GO
verdict at the end.

Run from backend/:

    PREFLIGHT_USERNAME=admin PREFLIGHT_PASSWORD=... \
    python3 -m scripts.preflight.run_umg_e2e \
      --mp3 "/path/to/song.mp3" \
      --frame-size HD --fps 23.976 --prores 3

Cost: one full Veo Fast generation ($0.80) + ProRes encoding (no extra
billable). Total ~$0.80 per run.
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from pathlib import Path

from ._base import execute, Status
from ._clients import GenLyClient
from .check_umg_master import UmgMasterCheck


PRORES_PIX_FMT = {3: "yuv422p10le", 4: "yuv444p10le", 5: "yuv444p10le"}
FRAME_SIZE_DIMS = {
    "HD":      (1920, 1080),
    "DCI-2K":  (2048, 1080),
    "UHD-4K":  (3840, 2160),
    "DCI-4K":  (4096, 2160),
}


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--api-url", default=os.environ.get("PRODUCTION_API_URL")
                   or "https://genly-ai.up.railway.app")
    p.add_argument("--mp3", required=True, help="Path to a real MP3 to submit")
    p.add_argument("--frame-size", default="HD",
                   choices=list(FRAME_SIZE_DIMS.keys()))
    p.add_argument("--fps", default="23.976")
    p.add_argument("--prores", type=int, default=3, choices=[3, 4, 5],
                   help="3=ProRes 422 HQ, 4=ProRes 4444, 5=ProRes 4444 XQ")
    p.add_argument("--timeout", type=int, default=1500,
                   help="Max seconds to wait for the job to finish")
    p.add_argument("--out-dir", default=None,
                   help="Where to save the downloaded master (default: reports/umg_e2e/)")
    args = p.parse_args()

    user = os.environ.get("PREFLIGHT_USERNAME")
    pw = os.environ.get("PREFLIGHT_PASSWORD")
    if not (user and pw):
        print("PREFLIGHT_USERNAME / PREFLIGHT_PASSWORD must be set", file=sys.stderr)
        return 1
    if not Path(args.mp3).exists():
        print(f"MP3 not found: {args.mp3}", file=sys.stderr)
        return 1

    out_dir = Path(args.out_dir) if args.out_dir else Path(__file__).resolve().parent / "reports" / "umg_e2e"
    out_dir.mkdir(parents=True, exist_ok=True)

    client = GenLyClient(args.api_url, user, pw)
    print(f"[umg-e2e] login {args.api_url}")
    client.login()

    print(f"[umg-e2e] uploading {args.mp3} (umg / {args.frame_size} / "
          f"{args.fps} / ProRes profile {args.prores})")
    job_id = client.upload(
        args.mp3,
        artist="preflight-umg",
        delivery_profile="umg",
        umg_frame_size=args.frame_size,
        umg_fps=args.fps,
        umg_prores_profile=str(args.prores),
    )
    print(f"[umg-e2e] job_id = {job_id}")

    t0 = time.time()
    print(f"[umg-e2e] polling until done (timeout {args.timeout}s)...")
    try:
        final = client.wait_until_done(job_id, timeout_secs=args.timeout)
    except TimeoutError as e:
        print(f"[umg-e2e] HUNG: {e}")
        return 1

    elapsed = int(time.time() - t0)
    print(f"[umg-e2e] terminal status={final.get('status')} "
          f"step={final.get('current_step')} prog={final.get('progress')}% "
          f"({elapsed}s)")

    if final.get("status") != "done":
        print(f"[umg-e2e] job did not reach done — error: {final.get('error')}")
        return 1

    # Note: status response leaves umg_master_url as null even on success
    # because the column is only filled for delivery_profile=youtube. The
    # download endpoint resolves the file from job.s3_keys directly, so we
    # try downloading regardless and surface the failure cleanly.
    master_path = out_dir / f"{job_id}_umg_master.mov"
    print(f"[umg-e2e] downloading master → {master_path}")
    try:
        client.download(job_id, "umg_master", str(master_path))
    except Exception as e:
        print(f"[umg-e2e] download failed: {type(e).__name__}: {e}")
        return 1
    size_mb = master_path.stat().st_size / 1024 / 1024
    print(f"[umg-e2e] downloaded {size_mb:.1f} MB")

    width, height = FRAME_SIZE_DIMS[args.frame_size]
    expected = {
        "frame_size": args.frame_size,
        "width": width,
        "height": height,
        "fps": float(args.fps),
        "prores_profile": args.prores,
        "pix_fmt": PRORES_PIX_FMT[args.prores],
    }

    check = UmgMasterCheck(str(master_path), expected)
    result = execute(check)

    print()
    print(f"=== umg_master_conformance: {result.status.value} ===")
    print(result.summary)
    if result.status != Status.PASS:
        violations = result.details.get("violations", [])
        for v in violations:
            print(f"  - {v}")
        print()
        print("Full details:")
        import json
        print(json.dumps(result.details, indent=2, default=str))
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
