"""Development-only paired replay; human targets never enter aligner inputs.

Historical corrections are operational comparators, NOT uncontaminated gold.
No training/evaluation split changes and no production document writes.
"""
import argparse
import json
from pathlib import Path

from reviewer_phrase_alignment import align_phrase
from reviewer_shadow_audio import file_sha, private_write
from shadow_reference_import import digest


def replay(root, output):
    jobs = json.loads((root / "snapshot.json").read_text())["jobs"]
    job = next(j for j in jobs if j["job_id"] == "e926daf14d7a")
    original = job["original_segments"]
    if job["original_revision"] != 0 or digest(original) != job["original_sha256"]:
        raise ValueError("unreliable_prehuman_snapshot")
    mix = root / "audio" / (job["job_id"] + "-mix.wav")
    assert file_sha(mix) == job["audio_sha256"]
    # Frozen prior canary controls; do not select on closeness to human endpoints.
    indices = [7, 36, 40]
    predictions = []
    for i in indices:
        baseline = original[i]
        start = max(0., float(baseline["start"]) - 2.)
        window = {"start": start,
            "end": min(float(job["duration_seconds"]), start + 24., float(baseline["end"]) + 8.)}
        result = align_phrase(mix, baseline["text"], window)
        predictions.append({"line_index": i, "prehuman": baseline, "alignment": result})
    # Human text/times are accessed ONLY after all predictions finish.
    for result in predictions:
        current = job["segments"][result["line_index"]]
        result["human_comparator"] = current
        result["end_delta_human_minus_baseline"] = current["end"] - result["prehuman"]["end"]
        result["start_delta_human_minus_baseline"] = current["start"] - result["prehuman"]["start"]
        words = result["alignment"]["words"]
        result["candidate_minus_human_end"] = words[-1]["global_end"] - current["end"] if words else None
    private_write(output, {"schema": "reviewer-prehuman-development-v1", "job_id": job["job_id"],
        "original_sha256": job["original_sha256"], "human_targets_hidden_during_inference": True,
        "clean_gold": False, "contamination": "historical_auto_trim_not_readjudicated",
        "purpose": "development_only_not_reserved_evaluation", "automatic_apply_allowed": False,
        "predictions": predictions})


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--root", type=Path, required=True)
    p.add_argument("--output", type=Path, required=True)
    a = p.parse_args()
    replay(a.root, a.output)
