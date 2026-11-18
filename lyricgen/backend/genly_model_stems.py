"""Optional serial, offline Demucs separation for songs missing a cached stem.

Only the already cached, SHA-pinned htdemucs checkpoint is accepted. A bounded
CPU subprocess streams 30-second blocks; no provider or download path exists.
Unverified clocks return a fallback status, never permission to transfer times.
"""
from __future__ import annotations

import argparse
import gc
import importlib.metadata
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile

from reviewer_shadow_audio import file_sha, private_write, probe
from shadow_reference_import import digest

MODEL_FILE = "955717e8-8726e21a.th"
MODEL_SHA256 = "8726e21a993978c7ba086d3872e7608d7d5bfca646ca4aca459ffda844faa8b4"
CONFIG = {"model": "htdemucs-955717e8", "demucs_version": "4.0.1", "device": "cpu",
          "threads": 2, "segment_seconds": 6., "block_seconds": 30., "block_overlap_seconds": 2.,
          "shifts": 0, "split_overlap": .25, "sample_rate": 44100, "channels": 2,
          "policy": "local-stem-stream-v1"}


def _validate_cached(receipt, audio, audio_sha256, output):
    if (receipt.get("original_sha256") != audio_sha256 or receipt.get("model_sha256") != MODEL_SHA256
            or receipt.get("configuration") != CONFIG or receipt.get("stem_path") != str(output.resolve())
            or not output.is_file() or receipt.get("stem_sha256") != file_sha(output)):
        return None
    if receipt.get("status") not in {"ready", "unverified"}:
        return None
    if receipt["status"] == "ready" and not (receipt.get("clock") or {}).get("verified"):
        return None
    return {**receipt, "source_path": str(audio.resolve()), "cache_hit": True}


def _run_child(command, *, timeout_s):
    process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                               text=True, start_new_session=True,
                               env={**os.environ, "OMP_NUM_THREADS": "2", "OPENBLAS_NUM_THREADS": "2",
                                    "HF_HUB_OFFLINE": "1", "TRANSFORMERS_OFFLINE": "1"})
    try:
        stdout, stderr = process.communicate(timeout=timeout_s)
        return process.returncode, stdout, stderr
    finally:
        # A lost worker lease must not leave a separator running alone.
        if process.poll() is None:
            os.killpg(process.pid, signal.SIGKILL)
            process.communicate()


def _prepare_local_stem(audio: Path, expected_sha: str, cache_dir: Path, *, python_executable=None,
                       timeout_s=600, max_duration_s=900, checkpoint: Path | None = None, runner=None) -> dict:
    audio, cache_dir = Path(audio), Path(cache_dir)
    result = {"status": "unavailable", "original_sha256": expected_sha, "hosted_provider_calls": 0,
              "model_downloaded": False, "fallback": "original_mix", "cache_hit": False}
    if not 1 <= timeout_s <= 600 or not 1 <= max_duration_s <= 900:
        raise ValueError("invalid_local_stem_budget")
    if not audio.is_file() or file_sha(audio) != expected_sha:
        raise ValueError("original_audio_hash_mismatch")
    checkpoint = Path(checkpoint) if checkpoint else Path.home()/".cache/torch/hub/checkpoints"/MODEL_FILE
    if not checkpoint.is_file():
        return {**result, "reason": "cached_demucs_checkpoint_missing"}
    if file_sha(checkpoint) != MODEL_SHA256:
        return {**result, "reason": "cached_demucs_checkpoint_mismatch"}
    duration = probe(audio)
    if not 0 < duration <= max_duration_s:
        return {**result, "reason": "local_stem_duration_budget"}
    folder = cache_dir / digest([expected_sha, MODEL_SHA256, CONFIG])
    folder.mkdir(parents=True, exist_ok=True, mode=0o700)
    folder.chmod(0o700)
    output, receipt_path = folder/"vocals.wav", folder/"receipt.json"
    if receipt_path.is_file():
        try:
            cached = _validate_cached(json.loads(receipt_path.read_text()), audio, expected_sha, output)
            if cached:
                return cached
        except (OSError, ValueError, TypeError):
            pass
        return {**result, "reason": "cached_stem_receipt_invalid"}
    # A prior interrupted run cannot be accepted just because it wrote bytes.
    if output.exists():
        return {**result, "reason": "incomplete_stem_cache"}
    command = [str(python_executable or sys.executable), str(Path(__file__).resolve()), "--child",
               "--audio", str(audio.resolve()), "--audio-sha256", expected_sha,
               "--checkpoint", str(checkpoint.resolve()), "--output", str(output.resolve()),
               "--max-duration", str(max_duration_s)]
    try:
        code, _, _ = (runner or _run_child)(command, timeout_s=timeout_s)
    except subprocess.TimeoutExpired:
        return {**result, "reason": "local_stem_timeout"}
    except OSError:
        return {**result, "reason": "local_stem_executor_unavailable"}
    if code != 0 or not receipt_path.is_file():
        return {**result, "reason": "local_stem_inference_failed"}
    try:
        receipt = _validate_cached(json.loads(receipt_path.read_text()), audio, expected_sha, output)
    except (OSError, ValueError, TypeError):
        receipt = None
    return {**receipt, "cache_hit": False} if receipt else {**result, "reason": "local_stem_receipt_invalid"}


def prepare_local_stem(audio: Path, expected_sha: str, cache_dir: Path, **kwargs) -> dict:
    """Serialize same-source separation even if two workers request it together."""
    import fcntl
    root = Path(cache_dir)
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    lock_path = root / (digest([expected_sha, MODEL_SHA256, CONFIG]) + ".lock")
    descriptor = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return {"status": "unavailable", "reason": "local_stem_executor_busy", "original_sha256": expected_sha,
                    "fallback": "original_mix", "hosted_provider_calls": 0, "model_downloaded": False}
        return _prepare_local_stem(Path(audio), expected_sha, root, **kwargs)
    finally:
        os.close(descriptor)


def manifest_entry(audio, expected_sha, existing_manifest):
    """Reuse only a hash-verified stem for this exact original recording."""
    if file_sha(audio) != expected_sha:
        raise ValueError("original_audio_hash_mismatch")
    if existing_manifest is None:
        return None
    rows = json.loads(Path(existing_manifest).read_text())
    if not isinstance(rows, list):
        raise ValueError("invalid_existing_stem_manifest")
    matches = [r for r in rows if isinstance(r, dict) and r.get("original_sha256") == expected_sha]
    if not matches:
        return None
    identities = {(r.get("stem_path"), r.get("stem_sha256")) for r in matches}
    if len(identities) != 1:
        raise ValueError("ambiguous_cached_stem")
    entry = matches[0]
    stem = Path(str(entry.get("stem_path") or ""))
    if not stem.is_file() or not entry.get("stem_sha256") or file_sha(stem) != entry["stem_sha256"]:
        raise ValueError("cached_stem_identity_mismatch")
    return {**entry, "source_path": str(Path(audio).resolve()), "stem_path": str(stem.resolve()),
            "runtime_clock_verification_required": True}


def write_manifest(path, rows):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with tempfile.NamedTemporaryFile(mode="w", dir=path.parent, prefix=".manifest-", delete=False) as stream:
        temporary = Path(stream.name)
        try:
            os.chmod(temporary, 0o600)
            json.dump(rows, stream, ensure_ascii=False)
            stream.flush()
            os.fsync(stream.fileno())
        except Exception:
            temporary.unlink(missing_ok=True)
            raise
    temporary.replace(path)


def _child(audio, audio_sha256, checkpoint, output, max_duration):
    # Verify the exact known checkpoint before deserializing its model class.
    # Demucs 4 stores that class in a trusted upstream pickle, not just tensors.
    if file_sha(checkpoint) != MODEL_SHA256 or file_sha(audio) != audio_sha256:
        raise ValueError("local_stem_input_identity_mismatch")
    if importlib.metadata.version("demucs") != CONFIG["demucs_version"]:
        raise ValueError("unsupported_demucs_version")
    import numpy as np
    import soundfile as sf
    import torch
    from demucs.apply import apply_model
    from demucs.states import load_model
    torch.set_num_threads(2)
    package = torch.load(checkpoint, map_location="cpu", weights_only=False)
    model = load_model(package).cpu().eval()
    del package
    if model.samplerate != 44100 or model.audio_channels != 2 or "vocals" not in model.sources:
        raise ValueError("unexpected_demucs_configuration")
    rate, block, overlap = 44100, 30*44100, 2*44100
    source_index = model.sources.index("vocals")
    with tempfile.TemporaryDirectory(prefix="decoded-", dir=output.parent) as temporary:
        decoded = Path(temporary)/"input.wav"
        subprocess.run(["ffmpeg", "-v", "error", "-nostdin", "-i", str(audio), "-ac", "2", "-ar", str(rate),
                        "-c:a", "pcm_f32le", "-threads", "1", "-n", str(decoded)], check=True, capture_output=True, timeout=90)
        with sf.SoundFile(decoded) as source:
            if source.frames > max_duration*rate or source.frames == 0:
                raise ValueError("local_stem_duration_budget")
            count, total, squared = 0, 0., 0.
            for chunk in source.blocks(blocksize=rate, dtype="float32", always_2d=True):
                mono = chunk.mean(axis=1, dtype=np.float64)
                count += len(mono)
                total += float(mono.sum())
                squared += float(mono @ mono)
            mean = total/count
            deviation = max(1e-8, float(np.sqrt(max(0., squared/count - mean*mean))))
            total_frames = source.frames
            temporary_output = output.with_suffix(".part.wav")
            try:
                with sf.SoundFile(temporary_output, "w", samplerate=rate, channels=2, subtype="FLOAT") as destination:
                    position, pending = 0, None
                    while position < total_frames:
                        source.seek(position)
                        chunk = source.read(min(block, total_frames-position), dtype="float32", always_2d=True)
                        wave = torch.from_numpy(((chunk-mean)/deviation).T.copy())
                        with torch.inference_mode():
                            estimates = apply_model(model, wave[None], shifts=0, split=True, overlap=.25,
                                                    progress=False, device="cpu", num_workers=0, segment=6.)
                        vocals = (estimates[0, source_index].T.numpy()*deviation+mean).copy()
                        del estimates, wave
                        consumed = 0
                        if pending is not None:
                            length = min(len(pending), len(vocals))
                            weight = np.linspace(0, 1, length, dtype=np.float32)[:, None]
                            destination.write(pending[:length]*(1-weight)+vocals[:length]*weight)
                            consumed = length
                        final = position+len(chunk) >= total_frames
                        if final:
                            destination.write(vocals[consumed:])
                            break
                        destination.write(vocals[consumed:-overlap])
                        pending = vocals[-overlap:].copy()
                        position += block-overlap
                os.chmod(temporary_output, 0o600)
                if sf.info(temporary_output).frames != total_frames:
                    raise ValueError("local_stem_frame_count_mismatch")
                temporary_output.replace(output)
            finally:
                temporary_output.unlink(missing_ok=True)
    del model
    gc.collect()
    from reviewer_shadow_audio import pcm
    from vocal_continuity import verify_stem_clock
    clock = verify_stem_clock(pcm(audio, rate=16000), pcm(output, rate=16000), 16000)
    receipt = {"schema": "genly-local-stem-v1", "status": "ready" if clock.get("verified") else "unverified",
               "original_sha256": audio_sha256, "source_path": str(audio.resolve()),
               "stem_path": str(output.resolve()), "stem_sha256": file_sha(output), "clock": clock,
               "model_sha256": MODEL_SHA256, "configuration": CONFIG, "hosted_provider_calls": 0,
               "model_downloaded": False, "generated_locally": True, "fallback": None if clock.get("verified") else "original_mix"}
    private_write(output.parent/"receipt.json", receipt)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--audio", type=Path, required=True)
    parser.add_argument("--audio-sha256", required=True)
    parser.add_argument("--cache-dir", type=Path)
    parser.add_argument("--timeout", type=int, default=600)
    parser.add_argument("--max-duration", type=int, default=900)
    parser.add_argument("--checkpoint", type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--child", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--manifest-output", type=Path)
    parser.add_argument("--existing-manifest", type=Path)
    args = parser.parse_args()
    if args.child:
        if not args.checkpoint or not args.output:
            parser.error("child requires checkpoint and output")
        _child(args.audio, args.audio_sha256, args.checkpoint, args.output, args.max_duration)
    else:
        if not args.cache_dir:
            parser.error("cache-dir is required")
        entry = manifest_entry(args.audio, args.audio_sha256, args.existing_manifest)
        if entry:
            result = {"status": "cached", "original_sha256": args.audio_sha256, "cache_hit": True,
                      "runtime_clock_verification_required": True, "hosted_provider_calls": 0, "model_downloaded": False}
        else:
            result = prepare_local_stem(args.audio, args.audio_sha256, args.cache_dir, timeout_s=args.timeout,
                                        max_duration_s=args.max_duration, checkpoint=args.checkpoint)
            if result.get("reason") in {"cached_stem_receipt_invalid", "cached_demucs_checkpoint_mismatch", "local_stem_receipt_invalid"}:
                raise ValueError(result["reason"])
            entry = result if result.get("status") == "ready" else None
        if args.manifest_output:
            write_manifest(args.manifest_output, [entry] if entry else [])
        print(json.dumps(result, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    def terminate(signum, _frame):
        raise SystemExit(128 + signum)
    signal.signal(signal.SIGTERM, terminate)
    raise SystemExit(main())
