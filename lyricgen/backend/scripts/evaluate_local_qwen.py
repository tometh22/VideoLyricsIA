#!/usr/bin/env python3
"""Replay frozen development crops with blind Qwen; cache before scoring."""
import argparse
import json
import os
from pathlib import Path
import resource
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from genly_model_runtime import extract_native_clip
from local_lora_probe import private_json
from local_qwen_mlx import LocalQwenMLX
from reviewer_shadow_audio import file_sha
from local_asr_training import generated_metrics, edit_distance
from reviewer_shadow import tokens


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--comparison-report", type=Path, required=True)
    parser.add_argument("--checkpoint", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--sample-set", choices=["replay", "validation"], default="replay")
    parser.add_argument("--max-samples", type=int, default=1)
    args = parser.parse_args()
    if not 1 <= args.max_samples <= 100:
        raise ValueError("invalid_sample_budget")
    ids = json.loads(args.comparison_report.read_text())[args.sample_set + "_sample_ids"][:args.max_samples]
    mapping = {row["sample_id"]: row for row in (json.loads(line) for line in args.manifest.read_text().splitlines() if line.strip())}
    rows = [mapping[sample_id] for sample_id in ids]
    if not rows:
        raise ValueError("empty_comparison_subset")
    if (args.output / "predictions.json").exists():
        raise ValueError("output_exists")
    args.output.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(args.output, 0o700)
    verified = set()
    for row in rows:
        if row["split"] == "train":
            raise ValueError("comparison_contains_current_training_sample")
        if row["audio_path"] not in verified:
            if file_sha(Path(row["audio_path"])) != row["audio_sha256"]:
                raise ValueError("source_audio_changed")
            verified.add(row["audio_path"])
    started = time.monotonic()
    listener = LocalQwenMLX(args.checkpoint)
    predictions = []
    for row in rows:
        clip = args.output / (row["sample_id"] + ".wav")
        clip_sha = extract_native_clip(row["audio_path"], {"start": row["start_s"], "end": row["end_s"]}, clip)
        begin = time.monotonic()
        hypothesis = listener.listen(clip)
        predictions.append({"sample_id": row["sample_id"], "song_id": row["song_id"],
                            "audio_sha256": row["audio_sha256"], "clip_sha256": clip_sha,
                            "start_s": row["start_s"], "end_s": row["end_s"],
                            "hypothesis": hypothesis, "reference_conditioned": False,
                            "latency_seconds": round(time.monotonic() - begin, 3)})
        private_json(args.output / "predictions.json", {"model_id": listener.model_id, "rows": predictions,
                                                        "evaluation_claim": "development_only"})
        print(json.dumps({"completed": len(predictions), "total": len(rows),
                          "latency_seconds": predictions[-1]["latency_seconds"],
                          "max_rss_gib": round(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024**3, 3)}), flush=True)
    # Reference text is used only after all prediction bytes have been saved.
    scored = [{**prediction, "reference": mapping[prediction["sample_id"]]["text"],
               "wer": edit_distance(tokens(mapping[prediction["sample_id"]]["text"]), tokens(prediction["hypothesis"])) / max(1, len(tokens(mapping[prediction["sample_id"]]["text"])))} for prediction in predictions]
    report = {"model_id": listener.model_id, "samples": len(scored),
              "macro_sample_wer": sum(row["wer"] for row in scored) / len(scored),
              "metrics": generated_metrics(rows, [prediction["hypothesis"] for prediction in predictions]),
              "elapsed_seconds": round(time.monotonic() - started, 3),
              "max_rss_gib": round(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024**3, 3),
              "rows": scored, "fresh_holdout": False, "quality_validated": False,
              "hosted_provider_calls": 0, "audio_uploaded": False}
    private_json(args.output / "evaluation.json", report)
    print(json.dumps({k: v for k, v in report.items() if k != "rows"}))
    return 0


if __name__ == "__main__":
    main()
