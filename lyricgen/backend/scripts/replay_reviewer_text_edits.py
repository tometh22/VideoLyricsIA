"""Two within-song development cases, with later text hidden during listening."""
from dataclasses import replace
import argparse
import json
from pathlib import Path

from reviewer_shadow import ShadowPolicy, source_binding, tokens
from reviewer_shadow_audio import BlindAudioTools, extract_clip, file_sha, private_write


def run(root, output):
    job = next(j for j in json.loads((root / "snapshot.json").read_text())["jobs"]
               if j["job_id"] == "e926daf14d7a")
    output.mkdir(mode=0o700, parents=True, exist_ok=True)
    listener = BlindAudioTools(output / "requests", policy=replace(ShadowPolicy(), max_calls_per_song=4))
    rows = []
    # Fixed development indices identified by pre/post lexical edits, not by
    # whether this agent agrees. Neither belongs to the reserved test split.
    for i in [28, 30]:
        original = job["original_segments"][i]
        start = max(0., float(original["start"]) - 2.)
        end = min(float(job["duration_seconds"]), start + 24., float(original["end"]) + 8.)
        window = {"line_index": i, "start": start, "end": end, "offset_seconds": start}
        row = {"line_index": i, "baseline": original, "window": window, "requests": []}
        for view, provider in [("mix", "openai"), ("stem", "google")]:
            audio = root / "audio" / f"{job['job_id']}-{view}.wav"
            clip = output / f"line-{i}-{view}.wav"
            if not clip.exists():
                extract_clip(audio, window, clip)
            response = listener.listen(clip, provider=provider, view=view,
                source=source_binding(job), window=window)
            row["requests"].append({**response, "clip_sha256": file_sha(clip),
                "clip_path": str(clip.resolve()), "transferred_timestamps": False})
        rows.append(row)
    # Compare only after all paid listening calls have completed.
    for row in rows:
        row["later_comparator"] = job["segments"][row["line_index"]]
        row["human_authorship_verified"] = False
        row["clean_gold"] = False
    private_write(output / "report.json", {"schema": "reviewer-text-development-v1",
        "targets_hidden_during_listening": True, "cases": rows,
        "provider_calls_this_run": listener.calls, "automatic_apply_allowed": False,
        "evaluation_split": "development_not_reserved", "human_precision": None})


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--root", type=Path, required=True)
    p.add_argument("--output", type=Path, required=True)
    args = p.parse_args()
    run(args.root, args.output)
