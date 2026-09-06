"""One directed intervention: cap widened context before next occurrence."""
import argparse
import json
from pathlib import Path

from reviewer_endpoint_interval import measure
from reviewer_phrase_alignment import align_phrase, compare_occurrence_anchors
from reviewer_shadow_audio import private_write


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--root", type=Path, required=True)
    p.add_argument("--output", type=Path, required=True)
    a = p.parse_args()
    old = json.loads((a.root / "localization-v2.json").read_text())
    song = next(s for s in old["songs"] if s["job_id"] == "497451e63958")
    row = next(w for w in song["windows"] if w["line_index"] == 50)
    # Known next occurrence from immutable timeline; a search ceiling hypothesis,
    # not proof of acoustic onset. Never copy another occurrence's duration.
    next_start = 184.3
    window = {**row["arms"][-1]["window"], "end": next_start}
    mix = a.root / "audio/497451e63958-mix.wav"
    aligned = align_phrase(mix, row["current"]["text"], window)
    anchors = compare_occurrence_anchors(row["arms"][0], aligned)
    interval = measure(mix, aligned, next_occurrence_start=next_start)
    private_write(a.output, {"job_id": song["job_id"], "line_index": 50,
        "intervention": "exclude_next_occurrence_from_expanded_search",
        "baseline_alignment": row["arms"][0], "unbounded_occurrence_alignment": row["arms"][-1],
        "occurrence_bounded_alignment": aligned, "anchor_check": anchors,
        "sustain_interval": interval, "timing_change_applied": False,
        "candidate_correctness_human_verified": False})
