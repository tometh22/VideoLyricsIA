"""Evaluate cached audio-only patches through the supervised bridge offline."""
import argparse
from copy import deepcopy
import json
from pathlib import Path

from reviewer_assist import prepare
from reviewer_shadow import review_window
from reviewer_shadow_audio import private_write
from shadow_reference_import import digest


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--root", type=Path, required=True)
    p.add_argument("--output", type=Path, required=True)
    a = p.parse_args()
    import subprocess
    commit = subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip()
    job = next(j for j in json.loads((a.root / "snapshot.json").read_text())["jobs"]
               if j["job_id"] == "e926daf14d7a")
    offline = deepcopy(job)
    offline["segments"] = deepcopy(job["original_segments"])
    offline["segments_revision"] = job["original_revision"]
    offline["segments_sha256"] = digest(offline["segments"])
    cases = json.loads((a.root / "text-edit-replay-v1/report.json").read_text())["cases"]
    rows = []
    for case in cases:
        decision = review_window(offline, case["window"], commit=commit,
            evidence=[{**e, "kind": "minimal_text_patch_request"} for e in case["requests"]])
        prepared = prepare(offline, [decision])
        rows.append({"line_index": case["line_index"], "decision": decision,
            "supervised_bridge": prepared,
            "matches_later_text": decision["content"].get("text") == case["later_comparator"]["text"],
            "live_line_protected": bool(case["later_comparator"].get("locked") or
                                          case["later_comparator"].get("operator_locked"))})
    private_write(a.output, {"commit": commit, "cases": rows, "clean_gold": False,
        "publication_attempted": False, "automatic_apply_allowed": False})
