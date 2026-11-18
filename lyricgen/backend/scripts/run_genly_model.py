#!/usr/bin/env python3
"""Run a full-song local draft model, serializing recognition and alignment."""
import argparse
import gc
import json
import os
from pathlib import Path
import subprocess
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from genly_model_runtime import assemble_candidate, attach_endpoint_extensions, cached_stem_for_audio, plan_windows, recognize_song
from local_correction_selector import load_artifact, rank_segments
from local_genly_review import LocalWhisper, normalize_snapshot
from local_lora_probe import available_memory_gib, cached_snapshot, private_json
from reviewer_shadow_audio import file_sha, probe


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--snapshot", type=Path, required=True)
    parser.add_argument("--audio", type=Path, required=True)
    parser.add_argument("--selector", type=Path, required=True)
    parser.add_argument("--model", choices=["base", "target"], default="base")
    parser.add_argument("--adapter", type=Path)
    parser.add_argument("--stem-manifest", type=Path, help="Private cached stem map; selects the exact original SHA")
    parser.add_argument("--device", choices=["cpu", "mps"], default="cpu")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--recognition", type=Path, help="Resume alignment from a bound recognition artifact")
    parser.add_argument("--recognize-only", action="store_true")
    parser.add_argument("--skip-pyin", action="store_true")
    parser.add_argument("--activity-vendor", type=Path, help="Optional pinned singing-activity checkout")
    parser.add_argument("--activity-receipt", type=Path, action="append", default=[])
    parser.add_argument("--skip-endpoint-extensions", action="store_true")
    parser.add_argument("--max-windows", type=int, default=128)
    parser.add_argument("--execute", action="store_true")
    args = parser.parse_args()
    os.environ.update(HF_HUB_OFFLINE="1", TRANSFORMERS_OFFLINE="1", TOKENIZERS_PARALLELISM="false")
    song = normalize_snapshot(json.loads(args.snapshot.read_text()))
    if file_sha(args.audio) != song["audio_sha256"]:
        raise ValueError("audio_identity_changed")
    duration = probe(args.audio)
    windows = plan_windows(duration, max_windows=args.max_windows)
    model_name = "openai/whisper-base" if args.model == "base" else "openai/whisper-large-v3-turbo"
    checkpoint = None if args.recognition else cached_snapshot(model_name)
    ranking = rank_segments(load_artifact(args.selector), song["segments"])
    available = available_memory_gib(subprocess.check_output(["vm_stat"], text=True))
    # Local base training was measured at ~0.8 GiB RSS / 1.31 GiB MPS;
    # inference uses a conservative 1.8 GiB admission budget, not the old 3 GiB.
    required = 1.8 if args.model == "base" else 6.0
    summary = {"status": "preflight", "model": model_name, "duration_seconds": duration,
               "planned_windows": len(windows), "planned_full_coverage": windows[-1]["end"] == duration,
               "available_memory_gib": round(available, 3), "required_memory_gib": required,
               "hosted_provider_calls": 0, "publication_enabled": False}
    if not args.execute:
        print(json.dumps(summary))
        return 0
    if available < required:
        print(json.dumps({**summary, "status": "blocked_memory"}))
        return 2
    if (args.output / "report.json").exists() or (not args.recognition and (args.output / "recognition.json").exists()):
        raise ValueError("artifact_exists_choose_new_output_directory")
    args.output.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(args.output, 0o700)
    started = time.monotonic()
    stem = cached_stem_for_audio(args.stem_manifest, song["audio_sha256"])
    if stem and file_sha(Path(stem["stem_path"])) != stem["stem_sha256"]:
        raise ValueError("cached_stem_hash_changed")
    if args.recognition:
        recognition = json.loads(args.recognition.read_text())
        if abs(float(recognition["duration_seconds"]) - duration) > .05:
            raise ValueError("recognition_duration_does_not_match_audio")
    else:
        import torch
        torch.set_num_threads(2)
        if args.device == "mps":
            torch.mps.set_per_process_memory_fraction(.12 if args.model == "base" else .35)
        listener = LocalWhisper(checkpoint, adapter_path=args.adapter, device=args.device)
        recognition = recognize_song(song, args.audio, args.output / "clips", listener=listener,
                                     ranking=ranking, max_windows=args.max_windows, stem=stem)
        recognition["base_model"] = model_name
        recognition["adapter_loaded"] = bool(args.adapter)
        private_json(args.output / "recognition.json", recognition)
        del listener
        gc.collect()
        if args.device == "mps":
            torch.mps.empty_cache()
    if args.recognize_only:
        print(json.dumps({**summary, "status": "recognition_finished", "observations": len(recognition["observations"])}))
        return 0
    # CTC loads only after ASR and adapter memory have been released.
    from reviewer_phrase_alignment import align_phrase
    from reviewer_endpoint_interval import measure
    activities = [json.loads(path.read_text()) for path in args.activity_receipt]
    if args.activity_vendor:
        from reviewer_singing_activity import run_activity
        for window in recognition["windows"]:
            expanded = {"start": max(0., window["start"] - 1.), "end": min(duration, window["end"] + 1.)}
            activity = run_activity(args.activity_vendor, args.audio, expanded)
            private_json(args.output / "activity" / f"window-{window['id']:04d}.json", activity)
            activities.append(activity)
    report = assemble_candidate(song, args.audio, recognition, aligner=align_phrase,
                                endpoint_measure=None if args.skip_pyin else measure, activity_receipts=activities)
    if stem and not args.skip_endpoint_extensions:
        from genly_model_acoustic_policy import endpoint_extension_candidates
        os.environ["VOCAL_CONTINUITY_MODE"] = "shadow"
        evidence = endpoint_extension_candidates(song, args.audio, Path(stem["stem_path"]),
                                                 candidate_segments=report["candidate_segments"])
        report = attach_endpoint_extensions(report, song, evidence)
    report["elapsed_seconds"] = round(time.monotonic() - started, 3)
    report["provenance"].update(base_model=recognition.get("base_model"),
                                adapter_loaded=recognition.get("adapter_loaded", False),
                                cached_stem_available=bool(stem),
                                selector_artifact_sha256=file_sha(args.selector))
    private_json(args.output / "report.json", report)
    private_json(args.output / "candidate.json", {"source": report["source"], "segments": report["candidate_segments"],
                                                 "model_id": report["model_id"], "quality_validated": False})
    print(json.dumps({"status": report["status"], "model_id": report["model_id"],
                      "coverage": report["coverage"], "elapsed_seconds": report["elapsed_seconds"],
                      "report": str(args.output / "report.json"), "quality_validated": False}))
    return 0


if __name__ == "__main__":
    main()
