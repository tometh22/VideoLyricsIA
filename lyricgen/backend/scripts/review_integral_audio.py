"""Complete acoustic development run; isolated input and frozen predictions.

prepare reads the old snapshot only to export rev0. listen/analyze accept ONLY
that sanitized file, never the human revision. evaluate is a separate command.
No runtime assistant limits, document imports, suggestions, or DB writes.
"""
import argparse
from copy import deepcopy
from dataclasses import replace
import json
from pathlib import Path
import subprocess
import time

from reviewer_integral import windows, union_seconds, locate_words, spectral_continuity
from reviewer_shadow import ShadowPolicy, source_binding, select_endpoint, tokens
from reviewer_shadow_audio import BlindAudioTools, extract_clip, file_sha, private_write, pcm
from reviewer_phrase_alignment import align_phrase
from reviewer_text_patch import propose_patches
from shadow_reference_import import digest


def prepare(root, output):
    jobs = json.loads((root / "snapshot.json").read_text())["jobs"]
    j = next(j for j in jobs if j["job_id"] == "e926daf14d7a")
    if j["original_revision"] != 0 or digest(j["original_segments"]) != j["original_sha256"]:
        raise ValueError("unreliable_original_snapshot")
    # Explicit allowlist: no current rows, human changes, locks or targets.
    source = {k: j[k] for k in ["job_id", "audio_sha256", "audio_revision", "duration_seconds"]}
    source.update(segments=deepcopy(j["original_segments"]), segments_revision=0,
                  segments_sha256=j["original_sha256"])
    output.mkdir(mode=0o700, parents=True, exist_ok=True)
    private_write(output / "input.json", source)


def listen(root, output):
    song = json.loads((output / "input.json").read_text())
    mix = root / "audio" / (song["job_id"] + "-mix.wav")
    if file_sha(mix) != song["audio_sha256"]:
        raise ValueError("source_audio_mismatch")
    planned = windows(song["duration_seconds"])
    listener = BlindAudioTools(output / "requests", policy=replace(ShadowPolicy(),
        max_windows_per_song=len(planned), max_calls_per_song=2 * len(planned)))
    started, rows = time.monotonic(), []
    for i, window in enumerate(planned):
        clip = output / f"window-{i:02d}.wav"
        if not clip.exists():
            extract_clip(mix, window, clip)
        results = []
        for provider in ["openai", "google"]:
            results.append(listener.listen(clip, provider=provider, view="mix",
                source=source_binding(song), window=window))
            print(json.dumps({"window": i, "provider": provider,
                "status": results[-1]["tool_status"]}), flush=True)
        rows.append({"window": window, "clip_path": str(clip.resolve()),
                     "clip_sha256": file_sha(clip), "requests": results})
    private_write(output / "listening.json", {"source": source_binding(song),
        "windows": rows, "calls_this_run": listener.calls,
        "latency_seconds": round(time.monotonic() - started, 3),
        "observed_cost_usd": None, "cost_status": "usage_retained_invoice_not_available",
        "coverage_seconds": {p: union_seconds([(r["window"]["start"], r["window"]["end"])
            for r in rows if any(q["provider"] == p and q["tool_status"] == "ok" and
                q["received_audio"] for q in r["requests"])]) for p in ["openai", "google"]}})


def analyze(root, output):
    song = json.loads((output / "input.json").read_text())
    listening = json.loads((output / "listening.json").read_text())
    mix = root / "audio" / (song["job_id"] + "-mix.wav")
    if file_sha(mix) != song["audio_sha256"] or source_binding(song) != listening["source"]:
        raise ValueError("stale_audio_or_revision")
    signal = pcm(mix, rate=16000)
    started, results = time.monotonic(), []
    for i, row in enumerate(song["segments"]):
        next_start = song["segments"][i + 1]["start"] if i + 1 < len(song["segments"]) else song["duration_seconds"]
        selected, text_candidates = [], []
        for window in listening["windows"]:
            if window["window"]["end"] <= row["start"] or window["window"]["start"] >= row["end"]:
                continue
            whisper = next(r for r in window["requests"] if r["provider"] == "openai")
            loc = locate_words(row["text"], whisper, window["window"], row)
            selected.append({"window": window["window"], "localization": loc})
            for patch in propose_patches(row["text"], window["requests"]):
                patch_location = locate_words(patch["text"], whisper, window["window"], row)
                text_candidates.append({"patch": patch, "window": window["window"],
                    "occurrence": patch_location,
                    "eligible": patch_location['status'] == 'unique_overlapping_occurrence'})
        located = [x for x in selected if x["localization"].get("selected")]
        # Baseline occurrence context, never a current/human endpoint input.
        start = max(0., row["start"] - 1.)
        end = min(song["duration_seconds"], start + 24., max(row["end"] + .1, next_start))
        window = {"start": start, "end": end, "offset_seconds": start}
        alignment = align_phrase(mix, row["text"], window)
        words = alignment["words"]
        alternative = spectral_continuity(signal, 16000, words[-1]["global_start"],
            words[-1]["global_end"], end) if words else {"status": "alignment_failed", "candidate_end": None}
        candidate = alternative.get("candidate_end")
        evidence = [] if candidate is None else [{"end_seconds": candidate,
            "clock_source": "acoustic_tool", "tool_status": "ok",
            "target_voice_verified": False, "phonetic_end_supported": False,
            "mix_stem_sync_verified": False, "cross_signal_transfer": False}]
        selector = select_endpoint(row, evidence, next_start=next_start, duration=song["duration_seconds"])
        result = {"line_index": i, "baseline": row, "occurrences": selected,
            "text_candidates": text_candidates,
            "phrase_recognized": bool(located), "ctc": alignment,
            "ctc_end": words[-1]["global_end"] if words else None,
            "recognition_end_hypotheses": [x["localization"]["selected"]["end"] for x in located],
            "alternative": alternative, "selector": selector,
            "remaining_defect": "target_voice_and_phonetic_boundary_unverified" if candidate else alternative["status"],
            "protected": bool(row.get("locked") or row.get("operator_locked"))}
        results.append(result)
        private_write(output / f"line-{i:02d}.json", result)
        print(json.dumps({"line": i, "localized": bool(located), "alternative": alternative["status"]}), flush=True)
    # Uncovered sung-event hypotheses are investigated, not declared omissions.
    outside = []
    for w in listening["windows"]:
        google = next(r for r in w["requests"] if r["provider"] == "google")
        if google["tool_status"] != "ok":
            continue
        for event in google["response"].get("events", []):
            if event.get("kind") not in {"sung", "vocalization"}:
                continue
            try:
                a, b = w["window"]["start"] + float(event["start"]), w["window"]["start"] + float(event["end"])
            except (KeyError, TypeError, ValueError):
                continue
            if not w["window"]["start"] <= a < b <= w["window"]["end"]:
                continue
            covered = union_seconds([(max(a, s["start"]), min(b, s["end"])) for s in song["segments"]
                                     if min(b, s["end"]) > max(a, s["start"])])
            if b - a - covered > .3:
                outside.append({"event": event, "global_start": a, "global_end": b,
                    "uncovered_seconds": round(b - a - covered, 3), "omission_certified": False,
                    "reason": "audio_model_event_outside_baseline_requires_occurrence_verification"})
    payload = {"schema": "integral-development-v1", "source": source_binding(song),
        "implementation_commit": subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip(),
        "human_targets_in_input": False, "lines": results, "outside_events": outside,
        "latency_seconds": round(time.monotonic() - started, 3), "automatic_apply_allowed": False}
    private_write(output / "predictions-frozen.json", payload)
    private_write(output / "freeze.json", {"prediction_sha256": file_sha(output / "predictions-frozen.json"),
        "input_sha256": file_sha(output / "input.json"), "frozen_at": time.time()})


def control(root, output):
    """Frozen Luciano no-damage/repeat control; reuse previous native alignment."""
    evidence = json.loads((root / 'sustain-occurrence-v1.json').read_text())
    old = evidence['occurrence_bounded_alignment']
    word = old['words'][-1]
    mix = root / 'audio' / '497451e63958-mix.wav'
    result = spectral_continuity(pcm(mix, rate=16000), 16000,
        word['global_start'], word['global_end'], old['window']['end'])
    private_write(output / 'luciano-control.json', {'baseline_end': 179.32,
        'ctc_end': word['global_end'], 'alternative': result,
        'existing_repeat_guard': evidence['anchor_check'], 'timing_change_applied': False,
        'audio_sha256': file_sha(mix), 'cached_alignment_source': file_sha(root / 'sustain-occurrence-v1.json'),
        'need_for_extension_certified': False, 'human_correctness_verified': False})


def evaluate(root, output):
    frozen = json.loads((output / "freeze.json").read_text())
    if file_sha(output / "predictions-frozen.json") != frozen["prediction_sha256"]:
        raise ValueError("predictions_changed_after_freeze")
    if file_sha(output / "input.json") != frozen["input_sha256"]:
        raise ValueError("input_changed_after_freeze")
    predictions = json.loads((output / "predictions-frozen.json").read_text())
    # This is the ONLY inference-stage command allowed to open current timings.
    current = next(j for j in json.loads((root / "snapshot.json").read_text())["jobs"]
                   if j["job_id"] == predictions["source"]["job_id"])
    rows = []
    for result in predictions["lines"]:
        i, baseline = result["line_index"], result["baseline"]
        human = current["segments"][i]
        delta = human["end"] - baseline["end"]
        eligible = bool(human.get("locked") or human.get("operator_locked")) and abs(delta) > .15 and tokens(human["text"]) == tokens(baseline["text"])
        item = {"line_index": i, "historical_delta": delta, "human_end": human["end"],
                "operational_comparator_eligible": eligible, "clean_gold": False,
                "contamination": "auto_trim_not_readjudicated", "methods": {}}
        for method, end in [("ctc", result["ctc_end"]), ("spectral", result["alternative"].get("candidate_end"))]:
            before = abs(baseline["end"] - human["end"])
            after = abs(end - human["end"]) if end is not None else None
            item["methods"][method] = {"end": end, "absolute_error_seconds": after,
                "improved": after is not None and after < before,
                "worsened": after is not None and after > before,
                "within_150ms": after is not None and after <= .15}
        rows.append(item)
    private_write(output / "evaluation.json", {"freeze": frozen, "development_only": True,
        "clean_gold_count": 0, "rows": rows, "documents_modified": False})


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("phase", choices=["prepare", "listen", "analyze", "evaluate", "control"])
    p.add_argument("--root", type=Path, required=True)
    p.add_argument("--output", type=Path, required=True)
    a = p.parse_args()
    globals()[a.phase](a.root, a.output)
