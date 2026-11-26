#!/usr/bin/env python3
"""Connect this Mac to staging's Genly model queue; serial local inference."""
import argparse
import json
from pathlib import Path
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from genly_model_worker import StagingClient, WorkerError, run_claim


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--api-url", default="https://api-staging-9b82.up.railway.app")
    parser.add_argument("--auth-file", type=Path, required=True)
    parser.add_argument("--selector", type=Path, required=True)
    parser.add_argument("--adapter", type=Path)
    parser.add_argument("--model", choices=["base", "target", "qwen"], default="base")
    parser.add_argument("--qwen-python", type=Path)
    parser.add_argument("--qwen-checkpoint", type=Path)
    parser.add_argument("--qwen-model-name", default="mlx-community/Qwen3-ASR-1.7B-4bit")
    parser.add_argument("--qwen-memory-limit-gib", type=float, default=2.75)
    parser.add_argument("--device", choices=["mps", "cpu"], default="mps")
    parser.add_argument("--stem-manifest", type=Path)
    parser.add_argument("--separate-vocals", action="store_true", help="Prepare missing stems locally using cached Demucs weights")
    parser.add_argument("--activity-vendor", type=Path, help="Pinned local singing-activity detector")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--worker-id", default="genly-local-mac")
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--poll-seconds", type=int, default=10)
    args = parser.parse_args()
    if not 5 <= args.poll_seconds <= 60:
        parser.error("poll seconds must be between 5 and 60")
    if not args.selector.is_file():
        parser.error("selector artifact is required before advertising worker availability")
    if not 1.5 <= args.qwen_memory_limit_gib <= 3:
        parser.error("Qwen memory limit must be between 1.5 and 3 GiB")
    if args.model == "qwen" and (args.adapter or not args.qwen_python or not args.qwen_python.is_file()
                                or not args.qwen_checkpoint or not args.qwen_checkpoint.is_dir()):
        parser.error("qwen requires its local interpreter/checkpoint and no Whisper adapter")
    client = StagingClient(args.api_url, args.auth_file)
    args.output.mkdir(parents=True, mode=0o700, exist_ok=True)
    args.output.chmod(0o700)
    failures = 0
    while True:
        claim = None
        try:
            client.refresh_if_due()
            response = client.request("POST", "/admin/genly-model/claim",
                                      payload={"worker_id": args.worker_id})
            claim = response.get("request")
            failures = 0
            if claim:
                print(json.dumps({"status": "processing", "job_id": claim["job_id"]}), flush=True)
                result = run_claim(client, claim, worker_id=args.worker_id, output_root=args.output,
                                   selector=args.selector.resolve(),
                                   adapter=args.adapter.resolve() if args.adapter else None,
                                   model=args.model, device=args.device,
                                   # Keep the venv entry point: resolving this symlink
                                   # would launch the base Python without MLX installed.
                                   qwen_python=args.qwen_python.absolute() if args.qwen_python else None,
                                   qwen_checkpoint=args.qwen_checkpoint.resolve() if args.qwen_checkpoint else None,
                                   qwen_model_name=args.qwen_model_name, qwen_memory_limit_gib=args.qwen_memory_limit_gib,
                                   separate_vocals=args.separate_vocals,
                                   activity_vendor=args.activity_vendor.resolve() if args.activity_vendor else None,
                                   stem_manifest=args.stem_manifest.resolve() if args.stem_manifest else None)
                print(json.dumps(result), flush=True)
            if args.once:
                return 0
        except Exception as exc:
            failures += 1
            reason = str(exc) if isinstance(exc, WorkerError) else type(exc).__name__
            print(json.dumps({"status": "failed", "reason": reason}), flush=True)
            if claim:
                try:
                    client.request("POST", "/admin/genly-model/fail", payload={
                        "worker_id": args.worker_id, "run_id": claim["run_id"],
                        "lease_token": claim["lease_token"], "reason": reason})
                except Exception:
                    pass  # Lease expiry is visible server-side if service is unreachable.
            if args.once or reason in {"api_status_401", "api_status_403"} or failures >= 3:
                return 2
        time.sleep(args.poll_seconds)


if __name__ == "__main__":
    raise SystemExit(main())
