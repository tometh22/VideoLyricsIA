#!/usr/bin/env python3
"""Create private development manifests; no network access or production writes."""
import argparse
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from local_genly_corpus import prepare


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--examples", type=Path, required=True)
    parser.add_argument("--audio-map", type=Path, required=True)
    parser.add_argument("--registry", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    report = prepare(args.examples, args.audio_map, args.registry, args.output)
    print(json.dumps({k: v for k, v in report.items() if k != "assignments"}, indent=2))


if __name__ == "__main__":
    main()
