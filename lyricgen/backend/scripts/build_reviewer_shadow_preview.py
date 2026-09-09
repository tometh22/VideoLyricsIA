"""Generate a private, local synchronized playback artifact; no real export claim."""
import argparse
import json
import os
from pathlib import Path


def build(report, snapshot, output):
    jobs = {s["job_id"]: s for s in snapshot["jobs"]}
    rows = []
    for song in report["songs"]:
        for window in song["windows"]:
            b = window["B_audio"]
            i = window["window"]["line_index"]
            segments = jobs[song["job_id"]]["segments"]
            clip = Path(window["clips"]["mix"]["path"])
            relative = os.path.relpath(clip, output.parent)
            if relative.startswith(".."):
                raise ValueError("preview clips must live inside artifact directory")
            rows.append({"job_id": song["job_id"], "artist": song["artist"], "title": song["title"],
                         "case": song["case"], "source": song["source"], "commit": report["commit"],
                         "line_index": i, "current": window["A_current"], "offset": window["window"]["offset_seconds"],
                         "clip_duration": window["window"]["end"] - window["window"]["start"],
                         "mix_clip": relative, "clip_identity": window["clips"],
                         "next_start": segments[i + 1]["start"] if i + 1 < len(segments) else None,
                         "candidates": [e for e in b["evidence"] if e.get("kind") == "endpoint" and "end_seconds" in e],
                         "content": b["content"], "timing": b["timing"], "sync": song["sync"],
                         "requests": [e for e in b["evidence"] if e.get("kind") == "blind_audio_request"]})
    template = Path(__file__).with_name("reviewer_shadow_preview.html").read_text()
    content = template.replace("__SHADOW_DATA__", json.dumps(rows, ensure_ascii=False).replace("<", "\\u003c"))
    with output.open("x") as handle:
        os.chmod(output, 0o600)
        handle.write(content)
    return len(rows)


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--report", type=Path, required=True)
    p.add_argument("--snapshot", type=Path, required=True)
    p.add_argument("--output", type=Path, required=True)
    a = p.parse_args()
    print(json.dumps({"preview_windows": build(json.loads(a.report.read_text()), json.loads(a.snapshot.read_text()), a.output),
                      "human_checked": False, "umg_export_verified": False}))
