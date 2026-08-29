#!/usr/bin/env python3
"""Download existing R2 vocal stems read-only for historical replay."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
from pathlib import Path

import boto3

from eval.canonical import read_json, write_json


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _duration(path: Path) -> float:
    result = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "default=nw=1:nk=1", str(path)],
        capture_output=True, text=True, check=True,
    )
    return float(result.stdout.strip())


def download(golden: Path, output: Path) -> dict:
    required = ["R2_ACCESS_KEY_ID", "R2_SECRET_ACCESS_KEY", "R2_ENDPOINT_URL", "R2_BUCKET"]
    missing = [name for name in required if not os.environ.get(name)]
    if missing:
        raise RuntimeError(f"missing R2 environment: {', '.join(missing)}")
    client = boto3.client(
        "s3", endpoint_url=os.environ["R2_ENDPOINT_URL"],
        aws_access_key_id=os.environ["R2_ACCESS_KEY_ID"],
        aws_secret_access_key=os.environ["R2_SECRET_ACCESS_KEY"],
        region_name="auto",
    )
    bucket = os.environ["R2_BUCKET"]
    manifest = read_json(golden / "manifest.json")
    rows = []
    for item in manifest["cases"]:
        case = golden / item["path"]
        meta = read_json(case / "meta.json")
        if meta["raw_quality"] not in {"exact", "reconstructed"}:
            continue
        song_id = item["song_id"]
        prefix = f"stems/{meta['audio']['sha256']}_mdx_extra_"
        response = client.list_objects_v2(Bucket=bucket, Prefix=prefix, MaxKeys=20)
        candidates = sorted(
            (entry for entry in response.get("Contents") or [] if str(entry.get("Key") or "").endswith(".wav")),
            key=lambda entry: entry.get("LastModified"), reverse=True,
        )
        destination = output / song_id / "vocals.wav"
        if not candidates:
            rows.append({"song_id": song_id, "status": "cache_miss", "prefix": prefix})
            continue
        key = str(candidates[0]["Key"])
        destination.parent.mkdir(parents=True, exist_ok=True)
        partial = destination.with_suffix(".partial")
        if not destination.is_file():
            partial.unlink(missing_ok=True)
            client.download_file(bucket, key, str(partial))
            partial.replace(destination)
        duration = _duration(destination)
        if duration + 1.0 < float(meta["duration_s"]):
            destination.unlink(missing_ok=True)
            rows.append({"song_id": song_id, "status": "invalid_short_stem", "key": key, "duration_s": duration})
            continue
        rows.append({
            "song_id": song_id, "status": "downloaded", "key": key,
            "sha256": _sha256(destination), "duration_s": duration,
        })
        print(f"cached stem {song_id}: {duration:.1f}s", flush=True)
    report = {
        "schema_version": 1, "read_only": True, "songs": len(rows),
        "downloaded": sum(row["status"] == "downloaded" for row in rows),
        "cache_misses": sum(row["status"] == "cache_miss" for row in rows),
        "cases": rows,
    }
    write_json(output / "manifest.json", report)
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--golden", type=Path, default=Path("eval/golden"))
    parser.add_argument("--output", type=Path, default=Path("eval/cache/full_stems"))
    args = parser.parse_args()
    print(json.dumps(download(args.golden.resolve(), args.output.resolve()), indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
