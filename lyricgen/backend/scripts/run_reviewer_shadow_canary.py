"""Real, bounded three-song functional canary. No product/database imports.

Inputs: private frozen manifests. Output: exclusive, tenant-private artifact
directory with exact audio clips, request traces and paired A/B/C decisions.
An interrupted provider call is never purchased again automatically.
"""
import argparse
from collections import Counter
from copy import deepcopy
import json
import os
from pathlib import Path
import time

from reviewer_shadow import review_window, sequence_discrepancies, source_binding, validate_snapshot
from reviewer_shadow_audio import (BlindAudioTools, acoustic_endpoint_candidates, check_sync,
                                   extract_clip, file_sha, localized_witnesses, private_write)


def run(args):
    songs = {s["job_id"]: s for s in json.loads(args.snapshot.read_text())["jobs"]}
    sample = {s["job_id"]: s for s in json.loads(args.sample.read_text())["songs"]}
    refs = {r["matched_job_id"]: r for r in json.loads(args.references.read_text())["rows"] if r.get("matched_job_id")}
    selected = json.loads(args.canary.read_text())["songs"]
    if len(selected) != 3 or len({s["job_id"] for s in selected}) != 3:
        raise ValueError("canary_requires_three_unique_songs")
    args.output.mkdir(mode=0o700, parents=True, exist_ok=True)
    output = []
    for choice in selected:
        song = songs[choice["job_id"]]
        validate_snapshot(song)
        original = deepcopy(song)
        root = args.output / song["job_id"]
        root.mkdir(mode=0o700, exist_ok=True)
        final = root / "result.json"
        if final.exists():
            saved = json.loads(final.read_text())
            if saved["source"] != source_binding(song) or saved["commit"] != args.commit:
                raise ValueError("existing_canary_artifact_has_different_source_or_commit")
            output.append(saved)
            continue
        started = time.monotonic()
        tools = BlindAudioTools(root / "requests")
        entry = {"job_id": song["job_id"], "artist": song["artist"], "title": song["title"],
                 "case": choice["case"], "source": source_binding(song), "commit": args.commit,
                 "reference_availability": refs.get(song["job_id"], {}).get("availability", "no_association"),
                 "reference_provenance": refs.get(song["job_id"], {}).get("provenance"),
                 "windows": [], "tool_errors": [], "automatic_apply_allowed": False,
                 "human_checked": False, "human_precision": None, "human_minutes_saved": None}
        mix, stem = Path(choice["mix"]), Path(choice["stem"])
        if file_sha(mix) != song["audio_sha256"]:
            raise ValueError("source_audio_hash_mismatch")
        entry["media"] = {"mix_sha256": song["audio_sha256"], "stem_sha256": file_sha(stem),
                          "stem_origin": choice["stem_origin"], "original_mix": str(mix), "stem": str(stem)}
        try:
            entry["sync"] = check_sync(mix, stem)
        except Exception as exc:
            entry["sync"] = {"mix_stem_sync_verified": False, "status": "tool_error", "error_type": type(exc).__name__}
            entry["tool_errors"].append(entry["sync"])
        reference = refs.get(song["job_id"], {}).get("lyrics")
        entry["external_discrepancies"] = sequence_discrepancies(song["segments"], reference) if reference else []
        for window in sample[song["job_id"]]["windows"]:
            segment = song["segments"][window["line_index"]]
            clips, results, evidence = {}, [], []
            for view, path, provider in (("mix", mix, "openai"), ("stem", stem, "google")):
                clip = root / f"line-{window['line_index']}-{view}.wav"
                try:
                    if not clip.exists():
                        sha = extract_clip(path, window, clip)
                    else:
                        sha = file_sha(clip)
                    clips[view] = {"path": str(clip.resolve()), "sha256": sha,
                                   "global_start": window["start"], "global_end": window["end"],
                                   "local_start": 0, "local_end": window["end"] - window["start"],
                                   "format": "PCM signed 16-bit mono 16000Hz"}
                    result = tools.listen(clip, provider=provider, view=view,
                                          source=source_binding(song), window=window)
                except Exception as exc:
                    result = {"provider": provider, "tool_status": "tool_error", "received_audio": False,
                              "error_type": type(exc).__name__, "calls": 0, "conditioning_texts": []}
                results.append(result)
                evidence.append({**result, "kind": "blind_audio_request"})
            evidence += localized_witnesses(results, segment, window, float(song["duration_seconds"]))
            if "stem" in clips:
                try:
                    candidates = acoustic_endpoint_candidates(clips["stem"]["path"], segment, window, entry["sync"])
                    google = next((r for r in results if r["provider"] == "google" and r["tool_status"] == "ok"), None)
                    for candidate in candidates:
                        candidate["editorial_ambiguity"] = (google or {}).get("response", {}).get("editorial_ambiguity", True)
                        candidate["local_end_seconds"] = candidate["end_seconds"] - window["offset_seconds"]
                        candidate["offset_seconds"] = window["offset_seconds"]
                        candidate["reverb"] = (google or {}).get("response", {}).get("reverb", "uncertain")
                    evidence += candidates
                except Exception as exc:
                    evidence.append({"kind": "endpoint", "tool_status": "tool_error", "error_type": type(exc).__name__})
            b = review_window(song, window, evidence=evidence, commit=args.commit)
            c = review_window(song, window, evidence=evidence, external_reference=reference, commit=args.commit) if reference else None
            entry["windows"].append({"window": window, "clips": clips,
                                     "A_current": deepcopy(segment), "B_audio": b, "C_excel": c})
            print(json.dumps({"job_id": song["job_id"], "line_index": window["line_index"],
                              "providers": [r["tool_status"] for r in results],
                              "content": b["content"]["decision"], "timing": b["timing"]["decision"]}), flush=True)
        if song != original:
            raise AssertionError("shadow_mutated_input")
        entry["latency_seconds"] = round(time.monotonic() - started, 3)
        entry["calls_this_run"] = tools.calls
        entry["document_unchanged_in_memory"] = True
        private_write(final, entry)
        output.append(entry)
    report = {"schema": "reviewer-shadow-functional-canary-v1", "commit": args.commit,
              "songs": output, "automatic_apply_allowed": False, "human_checked_songs": 0,
              "purpose": "functional_canary_not_precision_evaluation",
              "provider_status": dict(Counter(e["tool_status"] for s in output for w in s["windows"]
                  for e in w["B_audio"]["evidence"] if e.get("kind") == "blind_audio_request")),
              "content_decisions": dict(Counter(w["B_audio"]["content"]["decision"] for s in output for w in s["windows"])),
              "timing_decisions": dict(Counter(w["B_audio"]["timing"]["decision"] for s in output for w in s["windows"]))}
    if not (args.output / "report.json").exists():
        private_write(args.output / "report.json", report)
    print(json.dumps({k: v for k, v in report.items() if k != "songs"}), flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    for name in ("snapshot", "sample", "references", "canary", "output"):
        parser.add_argument("--" + name, type=Path, required=True)
    parser.add_argument("--commit", required=True)
    run(parser.parse_args())
