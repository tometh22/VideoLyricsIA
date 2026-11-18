#!/usr/bin/env python3
"""Offline preview, never writes a job, calls providers or authorizes rollout.

python scripts/preview_display_segmentation.py --input input.json \
    --audio song.wav --output .context/preview.json
The JSON may be a segment list or an editor/status object with `segments`.
"""
import argparse
import hashlib
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from display_segmentation import apply, plan
from evidence_attestation import write_json_exclusive


def preview(segments, source):
    proposal = plan(segments, source=source)
    try:
        output = apply(segments, proposal, source=source)
        status = "preview_only"
        reason = None
    except (ValueError, TypeError, KeyError) as exc:
        output, status, reason = segments, "contract_rejected", str(exc)
    return {"status": status, "failure_code": reason, "database_writes": False,
            "human_approval": False, "proposal": proposal, "preview_segments": output}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, required=True)
    parser.add_argument("--audio", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    data = json.loads(args.input.read_text())
    segments = data.get("segments") if isinstance(data, dict) else data
    h = hashlib.sha256()
    with args.audio.open("rb") as audio:
        for chunk in iter(lambda: audio.read(1024 * 1024), b""):
            h.update(chunk)
    source = {"job_id": "offline-preview", "audio_sha256": h.hexdigest(),
              "audio_revision": 1, "segments_revision": 0}
    result = preview(segments, source)
    write_json_exclusive(args.output, result)
    print(json.dumps({"status": result["status"], "before": len(segments),
                      "after": len(result["preview_segments"]),
                      "candidates": len(result["proposal"]["changes"]),
                      "review": len(result["proposal"]["review"]),
                      "output": str(args.output)}))


if __name__ == "__main__":
    main()
