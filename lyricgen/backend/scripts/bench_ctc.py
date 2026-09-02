#!/usr/bin/env python3
"""Regression benchmark for the PRODUCTION ctc_align module over the
Rotor GT datasets. Run before/after every engine change (Etapa B gate).

  CTC_ALIGN_ENABLED=1 python scripts/bench_ctc.py [slugs...]

Uses GT text (isolates timing) on the cached stems. Prints per-song
median / p95 / max / %>1s of onset offset vs Rotor, plus a global line.
"""
from __future__ import annotations

import json
import statistics
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
DATASET = Path(__file__).resolve().parent.parent / "benchmark" / "dataset"
DEFAULT = ["rotor_megustas", "rotor_sinoesmuy", "rotor_puesto", "rotor_nada_fue"]


def main(slugs):
    import ctc_align
    all_offs = []
    for slug in slugs:
        d = DATASET / slug
        stem = d / "stem.wav"
        if not stem.exists():
            print(f"{slug}: SIN STEM CACHEADO — corré exp_ctc_align primero")
            continue
        gt = json.loads((d / "ground_truth.json").read_text())
        segs = [{"text": g["text"], "start": 0.0, "end": 0.0} for g in gt]
        t0 = time.time()
        out = ctc_align.retime_segments(str(stem), segs, job_id=f"bench-{slug}")
        dt = time.time() - t0
        if not out:
            print(f"{slug}: DECLINÓ")
            continue
        offs = sorted(abs(o["start"] - g["start"]) for o, g in zip(out, gt))
        all_offs.extend(offs)
        p95 = offs[max(0, int(len(offs) * 0.95) - 1)]
        big = sum(1 for o in offs if o > 1.0)
        print(f"{slug:18s} med {statistics.median(offs):5.2f}s  p95 {p95:6.2f}s  "
              f"max {max(offs):6.2f}s  >1s {big:2d}/{len(offs):2d}  ({dt:.0f}s)")
    if all_offs:
        all_offs.sort()
        p95 = all_offs[max(0, int(len(all_offs) * 0.95) - 1)]
        print(f"{'GLOBAL':18s} med {statistics.median(all_offs):5.2f}s  p95 {p95:6.2f}s  "
              f"max {max(all_offs):6.2f}s  >1s {sum(1 for o in all_offs if o > 1.0):2d}/{len(all_offs)}")


if __name__ == "__main__":
    main(sys.argv[1:] or DEFAULT)
