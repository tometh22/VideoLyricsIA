#!/usr/bin/env python3
"""Package and verify the missing mdx_extra stems for the 41-song replay.

The bundle is credential-free and deterministic.  RunPod receives only the
15 cache misses, writes a per-file SHA-256 manifest, and returns one archive.
Import refuses any result whose source identity or stem digest is wrong.
"""

from __future__ import annotations

import argparse
import gzip
import hashlib
import io
import json
import shutil
import tarfile
import tempfile
from pathlib import Path
from typing import Any

import soundfile as sf

from eval.canonical import read_json, write_json
from eval.raw_cohort import RAW_TRUSTED


MODEL = "mdx_extra"


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _tar_info(name: str, size: int, mode: int = 0o644) -> tarfile.TarInfo:
    info = tarfile.TarInfo(name)
    info.size, info.mode, info.mtime = size, mode, 0
    info.uid = info.gid = 0
    info.uname = info.gname = ""
    return info


def _runner() -> bytes:
    return b'''#!/usr/bin/env bash
set -euo pipefail
cd /workspace/genly-stems
if ! command -v ffmpeg >/dev/null 2>&1; then
  apt-get update -qq
  DEBIAN_FRONTEND=noninteractive apt-get install -y -qq ffmpeg
fi
VENV=/workspace/genly-stems-venv
if command -v uv >/dev/null 2>&1; then
  uv venv --system-site-packages --seed "$VENV"
else
  python -m venv --system-site-packages "$VENV"
fi
PYTHON_BIN="$VENV/bin/python"
"$PYTHON_BIN" -m pip install --upgrade pip
"$PYTHON_BIN" -m pip install "demucs==4.0.1" "soundfile>=0.12,<1"
# PyTorch 2.9 requires TorchCodec 0.8/0.9.  Use its CPU wheel because Demucs
# only needs it to encode the returned WAV; a latest CUDA wheel can require a
# newer libnvrtc than the pod image and fail after the expensive separation.
"$PYTHON_BIN" -m pip install --no-deps \
  --index-url https://download.pytorch.org/whl/cpu "torchcodec==0.9.1"
"$PYTHON_BIN" - <<'PY'
import hashlib, json, subprocess, sys, time
from pathlib import Path
import soundfile as sf
from eval.raw_cohort import RAW_TRUSTED

root = Path("dataset")
destination = Path("results")
bundle = json.loads(Path("RUNPOD_BUNDLE_MANIFEST.json").read_text())

def sha(path):
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()

rows = []
for number, case in enumerate(bundle["cases"], 1):
    case_id = case["song_id"]
    source = root / case_id / case["filename"]
    if sha(source) != case["source_audio_sha256"]:
        raise RuntimeError(f"{case_id}: source SHA-256 mismatch before separation")
    target_dir = destination / case_id
    target_dir.mkdir(parents=True, exist_ok=True)
    target = target_dir / "vocals.wav"
    if target.is_file():
        info = sf.info(str(target))
        duration = info.frames / info.samplerate
        if duration + 1.0 >= float(case["duration_s"]):
            rows.append({
                "song_id": case_id,
                "source_audio_sha256": case["source_audio_sha256"],
                "stem_sha256": sha(target),
                "duration_s": duration,
                "model": "mdx_extra",
                "device": "cuda",
                "elapsed_s": 0.0,
                "resumed": True,
            })
            print(f"[{number}/{len(bundle['cases'])}] {case_id} resumed", flush=True)
            continue
    output = Path("work") / case_id
    started = time.monotonic()
    subprocess.run([
        sys.executable, "-m", "demucs.separate", "-n", "mdx_extra",
        "--two-stems", "vocals", "-d", "cuda", "-o", str(output), str(source),
    ], check=True)
    stem = output / "mdx_extra" / source.stem / "vocals.wav"
    if not stem.is_file():
        raise RuntimeError(f"{case_id}: Demucs output missing: {stem}")
    target.write_bytes(stem.read_bytes())
    info = sf.info(str(target))
    rows.append({
        "song_id": case_id,
        "source_audio_sha256": case["source_audio_sha256"],
        "stem_sha256": sha(target),
        "duration_s": info.frames / info.samplerate,
        "model": "mdx_extra",
        "device": "cuda",
        "elapsed_s": round(time.monotonic() - started, 3),
    })
    print(f"[{number}/{len(bundle['cases'])}] {case_id}", flush=True)

Path("results/manifest.json").write_text(json.dumps({
    "schema_version": 1,
    "bundle_sha256": bundle["identity_sha256"],
    "model": "mdx_extra",
    "cases": rows,
}, indent=2, sort_keys=True) + "\\n")
PY
tar -czf /workspace/genly-stem-results.tar.gz results
sha256sum /workspace/genly-stem-results.tar.gz > /workspace/genly-stem-results.tar.gz.sha256
'''


def _pending(golden: Path, cache: Path) -> list[dict[str, Any]]:
    golden_manifest = read_json(golden / "manifest.json")
    cache_manifest = read_json(cache / "manifest.json")
    cached = {row["song_id"]: row for row in cache_manifest["cases"]}
    rows = []
    for item in golden_manifest["cases"]:
        if item["raw_quality"] not in RAW_TRUSTED:
            continue
        if cached.get(item["song_id"], {}).get("status") == "downloaded":
            continue
        case = golden / item["path"]
        meta = read_json(case / "meta.json")
        source = case / meta["audio"]["filename"]
        actual = _sha256_file(source)
        if actual != meta["audio"]["sha256"]:
            raise RuntimeError(f"{item['song_id']}: local source SHA-256 mismatch")
        rows.append({
            "song_id": item["song_id"],
            "path": source,
            "filename": source.name,
            "source_audio_sha256": actual,
            "duration_s": float(meta["duration_s"]),
        })
    return rows


def package(golden: Path, cache: Path, output: Path) -> dict[str, Any]:
    pending = _pending(golden, cache)
    if not pending:
        raise RuntimeError("all eligible stems are already present")
    cases = [{key: row[key] for key in ("song_id", "filename", "source_audio_sha256", "duration_s")} for row in pending]
    identity = _sha256_bytes(json.dumps(cases, sort_keys=True, separators=(",", ":")).encode())
    manifest = {
        "schema_version": 1,
        "purpose": "golden_set_missing_mdx_extra_stems",
        "contains_credentials": False,
        "model": MODEL,
        "identity_sha256": identity,
        "cases": cases,
    }
    payloads: list[tuple[str, bytes | Path, int]] = []
    for row in pending:
        payloads.append((f"dataset/{row['song_id']}/{row['filename']}", row["path"], 0o644))
    runner = _runner()
    payloads.extend([
        ("RUNPOD_BUNDLE_MANIFEST.json", (json.dumps(manifest, indent=2, sort_keys=True) + "\n").encode(), 0o644),
        ("run_job.sh", runner, 0o755),
    ])
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("wb") as raw:
        with gzip.GzipFile(filename="", mode="wb", fileobj=raw, mtime=0) as zipped:
            with tarfile.open(fileobj=zipped, mode="w") as archive:
                for name, content, mode in sorted(payloads, key=lambda row: row[0]):
                    if isinstance(content, Path):
                        with content.open("rb") as handle:
                            archive.addfile(_tar_info(name, content.stat().st_size, mode), handle)
                    else:
                        archive.addfile(_tar_info(name, len(content), mode), io.BytesIO(content))
    return {
        **manifest,
        "archive": str(output),
        "archive_bytes": output.stat().st_size,
        "archive_sha256": _sha256_file(output),
    }


def _safe_extract(archive: Path, destination: Path) -> None:
    with tarfile.open(archive, "r:gz") as handle:
        for member in handle.getmembers():
            if member.issym() or member.islnk():
                raise RuntimeError(f"archive links are forbidden: {member.name}")
            member_path = (destination / member.name).resolve()
            if destination.resolve() not in member_path.parents and member_path != destination.resolve():
                raise RuntimeError(f"unsafe archive member: {member.name}")
        handle.extractall(destination)


def import_results(golden: Path, cache: Path, archive: Path) -> dict[str, Any]:
    pending = _pending(golden, cache)
    expected = {row["song_id"]: row for row in pending}
    if not expected:
        raise RuntimeError("no missing stems to import")
    expected_cases = [
        {key: row[key] for key in (
            "song_id", "filename", "source_audio_sha256", "duration_s",
        )}
        for row in pending
    ]
    expected_identity = _sha256_bytes(json.dumps(
        expected_cases, sort_keys=True, separators=(",", ":"),
    ).encode())
    with tempfile.TemporaryDirectory(prefix="genly-runpod-stems-") as temporary:
        root = Path(temporary)
        _safe_extract(archive, root)
        results = root / "results"
        remote = read_json(results / "manifest.json")
        if remote.get("model") != MODEL:
            raise RuntimeError(f"unexpected separator model: {remote.get('model')!r}")
        if remote.get("bundle_sha256") != expected_identity:
            raise RuntimeError("result bundle identity does not match the pending cohort")
        remote_rows = {row["song_id"]: row for row in remote.get("cases") or []}
        if set(remote_rows) != set(expected):
            raise RuntimeError("result song IDs do not exactly match the pending cohort")
        verified = []
        for song_id, local in expected.items():
            row = remote_rows[song_id]
            if row.get("source_audio_sha256") != local["source_audio_sha256"]:
                raise RuntimeError(f"{song_id}: remote source identity mismatch")
            source = results / song_id / "vocals.wav"
            actual_stem_sha = _sha256_file(source)
            if actual_stem_sha != row.get("stem_sha256"):
                raise RuntimeError(f"{song_id}: returned stem SHA-256 mismatch")
            info = sf.info(str(source))
            duration = info.frames / info.samplerate
            if duration + 1.0 < local["duration_s"]:
                raise RuntimeError(f"{song_id}: returned stem is unexpectedly short")
            target = cache / song_id / "vocals.wav"
            target.parent.mkdir(parents=True, exist_ok=True)
            partial = target.with_suffix(".partial")
            shutil.copy2(source, partial)
            if _sha256_file(partial) != actual_stem_sha:
                partial.unlink(missing_ok=True)
                raise RuntimeError(f"{song_id}: local copy SHA-256 mismatch")
            partial.replace(target)
            verified.append({
                "song_id": song_id,
                "status": "downloaded",
                "origin": "runpod_demucs_exact_model_name",
                "model": MODEL,
                "device": "cuda",
                "source_audio_sha256": local["source_audio_sha256"],
                "sha256": actual_stem_sha,
                "duration_s": duration,
            })
    manifest_path = cache / "manifest.json"
    manifest = read_json(manifest_path)
    updates = {row["song_id"]: row for row in verified}
    manifest["cases"] = [updates.get(row["song_id"], row) for row in manifest["cases"]]
    manifest["downloaded"] = sum(row.get("status") == "downloaded" for row in manifest["cases"])
    manifest["cache_misses"] = len(manifest["cases"]) - manifest["downloaded"]
    manifest["runpod_generation"] = True
    manifest["sha256_verified_on_import"] = True
    write_json(manifest_path, manifest)
    return {"imported": len(verified), "available": manifest["downloaded"], "remaining": manifest["cache_misses"], "cases": verified}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("package", "import"))
    parser.add_argument("--golden", type=Path, default=Path("eval/golden"))
    parser.add_argument("--cache", type=Path, default=Path("eval/cache/full_stems"))
    parser.add_argument("--archive", type=Path, required=True)
    args = parser.parse_args()
    if args.action == "package":
        result = package(args.golden.resolve(), args.cache.resolve(), args.archive.resolve())
    else:
        result = import_results(args.golden.resolve(), args.cache.resolve(), args.archive.resolve())
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
