#!/usr/bin/env python3
"""Checkpoint local mdx_extra stem generation for historical replay.

This command never uploads audio.  It deliberately invokes the same Demucs
model name used by the deployed separator and records each completed result in
the ignored cache manifest so a long Mac run can be resumed safely.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import soundfile as sf

from eval.canonical import read_json, write_json
from eval.raw_cohort import RAW_TRUSTED


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def run(golden: Path, cache: Path, device: str, limit: int | None) -> dict:
    manifest_path = cache / "manifest.json"
    manifest = read_json(manifest_path)
    case_records = {row["song_id"]: row for row in manifest["cases"]}
    golden_manifest = read_json(golden / "manifest.json")
    pending = [
        item for item in golden_manifest["cases"]
        if case_records.get(item["song_id"], {}).get("status") != "downloaded"
        and read_json(golden / item["path"] / "meta.json").get("raw_quality")
        in RAW_TRUSTED
    ]
    pending.sort(key=lambda item: float(
        read_json(golden / item["path"] / "meta.json").get("duration_s") or float("inf")
    ))
    if limit is not None:
        pending = pending[:limit]
    for number, item in enumerate(pending, 1):
        song_id = item["song_id"]
        meta = read_json(golden / item["path"] / "meta.json")
        source = golden / item["path"] / meta["audio"]["filename"]
        print(f"local mdx_extra {number}/{len(pending)} {song_id}", flush=True)
        with tempfile.TemporaryDirectory(prefix=f"genly-mdx-{song_id}-") as temporary:
            temporary_path = Path(temporary)
            input_path = temporary_path / f"{song_id}.wav"
            input_path.symlink_to(source)
            output_path = temporary_path / "output"
            subprocess.run([
                sys.executable, "-m", "demucs.separate", "-n", "mdx_extra",
                "--two-stems", "vocals", "-d", device, "-o", str(output_path),
                str(input_path),
            ], check=True)
            generated = output_path / "mdx_extra" / song_id / "vocals.wav"
            if not generated.is_file():
                raise RuntimeError(f"Demucs did not create {generated}")
            target_dir = cache / song_id
            target_dir.mkdir(parents=True, exist_ok=True)
            target = target_dir / "vocals.wav"
            shutil.copy2(generated, target)
        info = sf.info(str(target))
        case_records[song_id] = {
            "song_id": song_id,
            "status": "downloaded",
            "origin": "local_demucs_exact_model_name",
            "model": "mdx_extra",
            "device": device,
            "sha256": _sha256(target),
            "duration_s": info.frames / info.samplerate,
        }
        manifest["cases"] = [case_records[row["song_id"]] for row in manifest["cases"]]
        manifest["downloaded"] = sum(row.get("status") == "downloaded" for row in manifest["cases"])
        manifest["cache_misses"] = len(manifest["cases"]) - manifest["downloaded"]
        manifest["read_only_remote"] = True
        manifest["local_generation"] = True
        write_json(manifest_path, manifest)
    return {
        "completed": len(pending),
        "available": sum(row.get("status") == "downloaded" for row in manifest["cases"]),
        "remaining": sum(row.get("status") != "downloaded" for row in manifest["cases"]),
        "data_egress": False,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--golden", type=Path, default=Path("eval/golden"))
    parser.add_argument("--cache", type=Path, default=Path("eval/cache/full_stems"))
    parser.add_argument("--device", default="mps")
    parser.add_argument("--limit", type=int)
    args = parser.parse_args()
    print(json.dumps(run(args.golden.resolve(), args.cache.resolve(), args.device, args.limit), indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
