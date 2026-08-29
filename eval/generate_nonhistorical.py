#!/usr/bin/env python3
"""Generate a local, explicitly non-historical baseline for no-raw cases."""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from eval.canonical import read_json, segments_to_lines, write_json


def generate(golden: Path, backend: Path, output: Path) -> dict:
    # Contract: this baseline is local/no-paid. Prevent pipeline.py's optional
    # Whisper API route even if the shell inherited a key.
    os.environ["OPENAI_API_KEY"] = ""
    sys.path.insert(0, str(backend))
    scripts = backend / "scripts"
    sys.path.insert(0, str(scripts))
    try:
        from pipeline_runner import transcribe_local
        runner_name = "local_pipeline_runner_legacy_no_paid_provider"
    except (ImportError, TypeError):
        import whisper
        whisper_model = whisper.load_model("large-v3-turbo")

        def transcribe_local(audio_path, artist="", song_title="", language="es", verbose=False):
            result = whisper_model.transcribe(
                audio_path, language=language, word_timestamps=False,
                verbose=False, condition_on_previous_text=True,
                beam_size=1, best_of=1,
            )
            return {"segments": result.get("segments") or [], "source": "local_whisper_large_v3_turbo", "meta": {}}

        runner_name = "local_harness_whisper_large_v3_turbo_no_paid_provider"

    manifest = read_json(golden / "manifest.json")
    rows = []
    for item in manifest["cases"]:
        case = golden / item["path"]
        meta = read_json(case / "meta.json")
        if meta.get("has_raw"):
            continue
        audio = case / meta["audio"]["filename"]
        language = (meta.get("language") or {}).get("value") or "es"
        print(f"local nonhistorical baseline {item['song_id']}: {meta.get('artist')} — {meta.get('title')}", flush=True)
        result = transcribe_local(
            str(audio), artist=str(meta.get("artist") or ""),
            song_title=str(meta.get("title") or ""), language=language, verbose=False,
        )
        payload = {
            "schema_version": 1, "song_id": item["song_id"],
            "historical": False, "generated_after_baseline_freeze": True,
            "runner": runner_name,
            "source": result.get("source"), "meta": result.get("meta"),
            "lines": segments_to_lines(result.get("segments") or []),
        }
        write_json(output / item["song_id"] / "hypothesis.json", payload)
        rows.append({"song_id": item["song_id"], "source": result.get("source"), "lines": len(payload["lines"])})
    report = {"schema_version": 1, "songs": len(rows), "historical": False, "cases": rows}
    write_json(output / "report.json", report)
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--golden", type=Path, default=Path("eval/golden"))
    parser.add_argument("--backend", type=Path, required=True)
    parser.add_argument("--output", type=Path, default=Path("eval/hypotheses/local_baseline_8"))
    args = parser.parse_args()
    print(json.dumps(generate(args.golden.resolve(), args.backend.resolve(), args.output.resolve()), indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
