"""Read-only complete-song comparison, with original full audio (no clip cutoff)."""
import argparse
import json
import os
from pathlib import Path


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--directory", type=Path, required=True)
    a = p.parse_args()
    manifest = json.loads((a.directory / "manifest.json").read_text())
    rows = []
    for entry in manifest["candidates"]:
        candidate = json.loads(Path(entry["path"]).read_text())
        rows.append({"job_id": entry["job_id"], "mode": entry["mode"],
            "label": entry.get("label", entry["job_id"]),
            "evidence_url": os.path.relpath(entry["path"], a.directory),
            "realignments": candidate.get("realignments", []),
            "baseline": candidate["baseline"], "segments": candidate["segments"],
            "changes": candidate["changes"], "qc": candidate["residual_qc"],
            "baseline_sha256": candidate["baseline_sha256"], "candidate_sha256": candidate["candidate_sha256"]})
    template = Path(__file__).with_name("full_candidate_preview.html").read_text()
    with (a.directory / "index.html").open("x") as f:
        os.chmod(f.name, 0o600)
        f.write(template.replace("__CANDIDATES__", json.dumps(rows, ensure_ascii=False).replace("<", "\\u003c")))
