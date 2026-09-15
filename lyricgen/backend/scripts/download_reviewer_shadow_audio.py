"""Download only signed GET sources from the frozen sample; never separate anew."""
import argparse
import concurrent.futures
import hashlib
import json
import os
from pathlib import Path

import requests


def sha_file(path):
    h = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--assets", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    payload = json.loads(args.assets.read_text())
    args.output.mkdir(mode=0o700, parents=True, exist_ok=True)
    def download(job):
        result = {"job_id": job["job_id"], "audio_sha256": job["audio_sha256"]}
        for view in ("mix", "stem"):
            path = args.output / f"{job['job_id']}-{view}.wav"
            try:
                if not path.exists():
                    url = job.get(view + "_url")
                    if not url or not url.startswith("https://"):
                        raise ValueError("signed_GET_source_unavailable")
                    with requests.get(url, stream=True, timeout=(20, 90)) as response:
                        response.raise_for_status()
                        with path.open("xb") as handle:
                            os.chmod(path, 0o600)
                            count = 0
                            for chunk in response.iter_content(1024 * 1024):
                                count += len(chunk)
                                if count > 250_000_000:
                                    raise ValueError("audio_download_budget_exceeded")
                                handle.write(chunk)
                sha = sha_file(path)
                if view == "mix" and sha != job["audio_sha256"]:
                    raise ValueError("source_audio_hash_mismatch")
                result[view] = {"path": str(path.resolve()), "sha256": sha, "status": "ok"}
            except Exception as exc:
                # No signed URLs or provider exception text in reports.
                result[view] = {"status": "tool_error", "error_type": type(exc).__name__,
                                "http_status": getattr(getattr(exc, "response", None), "status_code", None)}
        print(json.dumps({"job_id": job["job_id"], "mix": result['mix']['status'], "stem": result['stem']['status']}), flush=True)
        return result
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        rows = list(pool.map(download, payload["jobs"]))
    with (args.output / "audio-manifest.json").open("x") as handle:
        os.chmod(handle.name, 0o600)
        json.dump({"jobs": rows, "source_assets_file": str(args.assets)}, handle, indent=2)


if __name__ == "__main__":
    main()
