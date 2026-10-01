"""Freeze import and sample BEFORE audio evaluation; exclusive artifact writes."""
import argparse
from collections import Counter
import json
import os
from pathlib import Path

from reviewer_shadow import freeze_sample
from shadow_reference_import import associate, import_workbook


def private_json(path, value):
    with path.open("x") as handle:
        os.chmod(path, 0o600)
        json.dump(value, handle, ensure_ascii=False, indent=2)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--snapshot", type=Path, required=True)
    parser.add_argument("--workbook", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--base-commit", required=True)
    parser.add_argument("--count", type=int, default=24)
    args = parser.parse_args()
    snapshot = json.loads(args.snapshot.read_text())
    manifest = associate(import_workbook(args.workbook), snapshot["jobs"])
    sample = freeze_sample(snapshot["jobs"], count=args.count, base_commit=args.base_commit)
    sample["source_snapshot_sha256"] = snapshot["snapshot_sha256"]
    sample["source_workbook_sha256"] = manifest["workbook_sha256"]
    args.output.mkdir(mode=0o700, parents=True, exist_ok=True)
    private_json(args.output / "import.json", manifest)
    private_json(args.output / "sample.json", sample)
    print(json.dumps({"rows": len(manifest["rows"]), "availability": manifest["availability_counts"],
                      "associations": manifest["association_counts"], "sample_songs": len(sample["songs"]),
                      "splits": dict(Counter(s["split"] for s in sample["songs"])),
                      "windows": sum(len(s["windows"]) for s in sample["songs"])}))


if __name__ == "__main__":
    main()
