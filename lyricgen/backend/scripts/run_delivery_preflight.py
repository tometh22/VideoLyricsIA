#!/usr/bin/env python3
"""Run delivery QC from one JSON manifest.

Input shape::

  {
    "metadata": {"artist": "...", "title": "...", "version": "..."},
    "asset": {"filename": "...", "duration": 240, "rendered_title": "..."},
    "segments": [{"start": 1.2, "end": 3.4, "text": "..."}],
    "approved_lyrics": [{"text": "..."}],
    "reference_trusted": true,
    "fps": 30
  }
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

BACKEND = Path(__file__).resolve().parents[1]
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

from delivery_preflight import build_delivery_preflight  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="Generate a Genly delivery QC report")
    parser.add_argument("manifest", type=Path)
    parser.add_argument("--output", "-o", type=Path)
    args = parser.parse_args()
    payload = json.loads(args.manifest.read_text(encoding="utf-8"))
    report = build_delivery_preflight(
        metadata=payload.get("metadata"),
        segments=payload.get("segments") or [],
        approved_lyrics=payload.get("approved_lyrics"),
        reference_trusted=bool(payload.get("reference_trusted", False)),
        asset=payload.get("asset"),
        quality=payload.get("quality"),
        reference_health=payload.get("reference_health"),
        acoustic_findings=payload.get("acoustic_findings"),
        fps=float(payload.get("fps", 30.0)),
    )
    rendered = json.dumps(report, ensure_ascii=False, indent=2) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered, encoding="utf-8")
    else:
        print(rendered, end="")
    return 2 if report["decision"] == "BLOCK" else 0


if __name__ == "__main__":
    raise SystemExit(main())
