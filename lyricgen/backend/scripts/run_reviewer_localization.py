"""Replay frozen canary without provider calls or current-document writes."""
import argparse
from collections import Counter
import json
from pathlib import Path
import platform
import subprocess

from reviewer_phrase_alignment import align_phrase, extend_context, phrase_occurrences
from reviewer_shadow import source_binding, validate_snapshot
from reviewer_shadow_audio import file_sha, private_write


def main(root, output, known_truncated=()):
    snapshot = json.loads((root / "snapshot.json").read_text())
    songs = {j["job_id"]: j for j in snapshot["jobs"]}
    assets = {j["job_id"]: j for j in json.loads((root / "assets-private.json").read_text())["jobs"]}
    previous = json.loads((root / "canary/report.json").read_text())
    report = {"schema": "reviewer-localization-v1", "python": platform.python_version(),
        "commit": subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip(),
        "provider_calls": 0, "automatic_apply_allowed": False, "human_checked": False,
        "songs": [], "arms": ["existing_context_ctc", "bounded_extension_if_truncated"]}
    for old in previous["songs"]:
        job = songs[old["job_id"]]
        validate_snapshot(job)
        mix = root / "audio" / (job["job_id"] + "-mix.wav")
        assert file_sha(mix) == job["audio_sha256"]
        hypotheses = [h for h in assets[job["job_id"]]["machine_evidence"]["hypotheses_by_family"]
                      if h.get("view") == "full_audio_without_reference"]
        entry = {"job_id": job["job_id"], "source": source_binding(job), "windows": [],
                 "clock_policy": "align_original_mix_only_no_unverified_stem_transfer"}
        for old_window in old["windows"]:
            w = old_window["window"]
            segment = job["segments"][w["line_index"]]
            matches = [{"family": h["family"], "events_sha256": h["events_sha256"],
                        "occurrences": phrase_occurrences(segment["text"],
                            "\n".join(e.get("text", "") for e in h["events"]))}
                       for h in hypotheses]
            identified = any(m["occurrences"] for m in matches)
            row = {"line_index": w["line_index"], "current": segment,
                "phrase_identified": identified, "phrase_evidence": matches,
                "human_protected": bool(segment.get("locked") or segment.get("operator_locked")),
                "occurrence_localized": False, "candidate_generated": False,
                "selector_decision": "abstain", "arms": []}
            if identified:
                first = align_phrase(mix, segment["text"], w)
                row["arms"].append(first)
                if first.get("boundary_near_clip_edge") or (job["job_id"], w["line_index"]) in known_truncated:
                    expanded = extend_context(w, duration=float(job["duration_seconds"]), truncated=True)
                    if expanded["expanded"]:
                        row["arms"].append(align_phrase(mix, segment["text"], expanded))
                latest = row["arms"][-1]
                row["candidate_generated"] = bool(latest["words"])
                # Lexical repeats need ordered anchors; CTC forcing is not occurrence proof.
                row["occurrence_localized"] = bool(latest["words"] and
                    all(len(m["occurrences"]) <= 1 for m in matches))
                row["occurrence_status"] = "provisional_alignment_not_independent_recognition"
                if latest["words"]:
                    row["candidate_end"] = latest["words"][-1]["global_end"]
                row["selector_reason"] = ("human_protection" if row["human_protected"] else
                    "alignment_tool_error" if latest["status"] == "tool_error" else
                    "repeated_occurrence_requires_anchors" if not row["occurrence_localized"] else
                    "phonetic_endpoint_not_certified_by_forced_alignment")
            else:
                row["selector_reason"] = "phrase_not_found_in_full_audio_hypothesis"
            entry["windows"].append(row)
            print(json.dumps({k: row[k] for k in ["line_index", "phrase_identified",
                "occurrence_localized", "candidate_generated", "selector_reason"]}), flush=True)
        entry["external_discrepancy_coverage"] = [{**d,
            "listened_line_indices": sorted(set(d["line_indices"]) &
                {w["window"]["line_index"] for w in old["windows"]}),
            "whole_discrepancy_audio_verified": False}
            for d in old.get("external_discrepancies", [])]
        report["songs"].append(entry)
    report["reasons"] = dict(Counter(w["selector_reason"] for s in report["songs"] for w in s["windows"]))
    private_write(output, report)


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--root", type=Path, required=True)
    p.add_argument("--output", type=Path, required=True)
    p.add_argument("--known-truncated", action="append", default=[],
                   help="job_id:zero_based_line; explicit truncation observation")
    args = p.parse_args()
    main(args.root, args.output, {(v.split(":")[0], int(v.split(":")[1]))
                                for v in args.known_truncated})
