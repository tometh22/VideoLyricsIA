"""Materialize complete offline candidates from immutable cached evidence."""
import argparse
from copy import deepcopy
import json
from pathlib import Path
import subprocess
import time

from reviewer_candidate import build_candidate
from reviewer_phrase_alignment import align_phrase
from reviewer_shadow import review_window
from reviewer_shadow_audio import private_write
from shadow_reference_import import digest


def run(root, output):
    jobs = {j["job_id"]: j for j in json.loads((root / "snapshot.json").read_text())["jobs"]}
    assets = {j["job_id"]: j for j in json.loads((root / "assets-private.json").read_text())["jobs"]}
    refs = json.loads((root / "import-reconciled.json").read_text())["rows"]
    canary = json.loads((root / "canary/report.json").read_text())
    lexical = json.loads((root / "text-edit-replay-v1/report.json").read_text())["cases"]
    commit = subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip()
    output.mkdir(mode=0o700, parents=True, exist_ok=True)
    entries = []
    for job_id, mode in [("e926daf14d7a", "historical_development"),
                         ("b9f7e218a071", "current_snapshot"),
                         ("497451e63958", "current_snapshot"),
                         ("e926daf14d7a", "current_snapshot")]:
        started = time.monotonic()
        source = deepcopy(jobs[job_id])
        if mode == "historical_development":
            assert source["original_revision"] == 0
            assert digest(source["original_segments"]) == source["original_sha256"]
            source["segments"] = deepcopy(source["original_segments"])
            source["segments_revision"] = 0
            source["segments_sha256"] = source["original_sha256"]
            decisions = [review_window(source, c["window"], commit=commit,
                evidence=[{**e, "kind": "minimal_text_patch_request"} for e in c["requests"]])
                for c in lexical]
        else:
            old = next(s for s in canary["songs"] if s["job_id"] == job_id)
            decisions = [w["B_audio"] for w in old["windows"]]
        result = build_candidate(source, decisions,
            hypotheses=assets[job_id]["machine_evidence"]["hypotheses_by_family"],
            external_reference=next((r for r in refs if r.get("matched_job_id") == job_id), None))
        result["mode"] = mode
        result["implementation_commit"] = commit
        result["clean_gold"] = False
        result["realignments"] = []
        for change in result["changes"]:
            if change["field"] != "text":
                continue
            i = change["line_index"]
            decision = next(d for d in decisions if d["window"]["line_index"] == i)
            alignment = align_phrase(root / "audio" / f"{job_id}-mix.wav",
                result["segments"][i]["text"], decision["window"])
            result["realignments"].append({"line_index": i, **alignment})
            # Word clocks remain evidence, not certified display endpoints.
        result["build_latency_seconds"] = round(time.monotonic() - started, 3)
        result["provider_calls_this_build"] = 0
        result["observed_cost_usd"] = None
        result["cost_status"] = "cached_requests_usage_retained_no_invoice_attached"
        path = output / f"{job_id}-{mode}.json"
        private_write(path, result)
        entries.append({"job_id": job_id, "mode": mode, "path": str(path.resolve()),
            "baseline_sha256": result["baseline_sha256"], "candidate_sha256": result["candidate_sha256"],
            "line_count": len(result["segments"]), "changes": len(result["changes"]),
            "protected_lines": sum(bool(s.get("locked") or s.get("operator_locked")) for s in source["segments"]),
            "timeline_findings": len(result["residual_qc"]["timeline"]),
            "hypothesis_discrepancies": len(result["residual_qc"]["hypothesis_discrepancies"])})
        print(json.dumps(entries[-1]), flush=True)
    private_write(output / "manifest.json", {"implementation_commit": commit, "candidates": entries,
        "current_documents_modified": False, "human_accuracy_measured": False})


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--root", type=Path, required=True)
    p.add_argument("--output", type=Path, required=True)
    a = p.parse_args()
    run(a.root, a.output)
