"""Read-only media provenance; no inferred offset from weak correlation."""
import argparse
import json
import subprocess
from pathlib import Path

from reviewer_shadow_audio import file_sha, private_write


def audit(path):
    meta = json.loads(subprocess.check_output(["ffprobe", "-v", "error",
        "-show_entries", "stream=codec_name,sample_rate,start_time,duration,duration_ts,time_base:format=format_name,start_time,duration",
        "-of", "json", str(path)]))
    decoded = subprocess.run(["ffmpeg", "-v", "error", "-nostdin", "-i", str(path),
        "-ac", "1", "-ar", "44100", "-f", "f32le", "pipe:1"],
        check=True, capture_output=True, timeout=90)
    return {"sha256": file_sha(path), "container": meta,
        "decoded_samples_44100": len(decoded.stdout) // 4,
        "decoded_duration": len(decoded.stdout) / (4 * 44100)}


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--root", type=Path, required=True)
    p.add_argument("--output", type=Path, required=True)
    a = p.parse_args()
    selected = json.loads((a.root / "canary-selection.json").read_text())["songs"]
    assets = {j["job_id"]: j for j in json.loads((a.root / "assets-private.json").read_text())["jobs"]}
    rows = []
    for s in selected:
        j = s["job_id"]
        asset = assets[j]
        rows.append({"job_id": j, "mix": audit(a.root / "audio" / f"{j}-mix.wav"),
            "stem": audit(a.root / "audio" / f"{j}-stem.wav"),
            "stem_cache_key": asset["stem_key"], "stem_model_identity": asset["stem_model_identity"],
            "transfer_verified": False, "offset_applied": None,
            "blocker": "provider_encoder_delay_and_padding_not_proven",
            "safe_path": "align_original_mix_on_its_native_decoded_clock"})
    private_write(a.output, {"schema": "reviewer-clock-audit-v1", "songs": rows})
