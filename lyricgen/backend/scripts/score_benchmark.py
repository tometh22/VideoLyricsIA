#!/usr/bin/env python3
"""Score baseline_output.json + improvement_output.json against ground_truth.json.

For every job dir under benchmark/dataset/, computes two metrics:

  WER (Word Error Rate)         lower = better, range 0.0-1.0
    Joins all segment texts into one string per source. Compares
    output-vs-ground using `jiwer.wer`. Captures text accuracy
    independent of segment boundaries.

  AOO (Average Onset Offset)    lower = better, in seconds
    For each output segment, finds the closest-text segment in
    ground_truth (Jaccard ≥ 0.4) and computes |out.start - gt.start|.
    Reports mean + p95. Captures timing accuracy.

  Composite                     higher = better, range 0.0-1.0
    1 - (0.5 * WER + 0.5 * normalized_AOO)
    Where normalized_AOO = min(AOO / 2.0, 1.0).
    Lets us track "is this iteration better overall" with one number.

Writes a Markdown report to stdout (and optionally to a file with --out).

Usage:
    cd lyricgen/backend
    pip install jiwer  # one-time
    python scripts/score_benchmark.py
    python scripts/score_benchmark.py --out BENCHMARK_REPORT.md
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from statistics import mean

HERE = Path(__file__).resolve().parent
DATASET = HERE.parent / "benchmark" / "dataset"


def _load(path: Path) -> list[dict] | None:
    if not path.exists():
        return None
    data = json.loads(path.read_text())
    # ground_truth.json is a bare list; baseline/improvement bundles
    # have {segments: [...], source, meta}
    if isinstance(data, list):
        return data
    return data.get("segments")


def _seg_text(segs: list[dict]) -> str:
    return " ".join((s.get("text") or "").strip() for s in segs if (s.get("text") or "").strip())


def _wer(ref_segs: list[dict], hyp_segs: list[dict]) -> float:
    try:
        import jiwer
    except ImportError:
        print("[ERR] jiwer not installed. Run: pip install jiwer", file=sys.stderr)
        sys.exit(2)
    ref = _seg_text(ref_segs).lower()
    hyp = _seg_text(hyp_segs).lower()
    if not ref:
        return 0.0
    return jiwer.wer(ref, hyp)


def _jaccard(a: str, b: str) -> float:
    sa = set((a or "").lower().split())
    sb = set((b or "").lower().split())
    if not sa or not sb:
        return 0.0
    return len(sa & sb) / len(sa | sb)


def _aoo(ground: list[dict], output: list[dict]) -> tuple[float, float, int]:
    """For each output segment, find best-match in ground by text and
    compute absolute start-time offset. Returns (mean_offset, p95_offset, matched_count)."""
    offsets: list[float] = []
    for out_seg in output:
        out_text = (out_seg.get("text") or "").strip()
        if not out_text:
            continue
        best_match = None
        best_score = 0.0
        for gt in ground:
            score = _jaccard(out_text, gt.get("text") or "")
            if score > best_score:
                best_score = score
                best_match = gt
        if best_match is None or best_score < 0.4:
            continue
        try:
            offset = abs(float(out_seg["start"]) - float(best_match["start"]))
            offsets.append(offset)
        except (KeyError, TypeError, ValueError):
            continue
    if not offsets:
        return (0.0, 0.0, 0)
    offsets_sorted = sorted(offsets)
    p95 = offsets_sorted[max(0, int(len(offsets_sorted) * 0.95) - 1)]
    return (mean(offsets), p95, len(offsets))


def _composite(wer: float, aoo_mean: float) -> float:
    norm_aoo = min(aoo_mean / 2.0, 1.0)
    return max(0.0, 1.0 - (0.5 * wer + 0.5 * norm_aoo))


def score_job(job_dir: Path) -> dict | None:
    ground = _load(job_dir / "ground_truth.json")
    baseline = _load(job_dir / "baseline_output.json")
    improvement = _load(job_dir / "improvement_output.json")
    if ground is None:
        return None

    out = {"job_id": job_dir.name, "ground_segments": len(ground)}
    if baseline is not None:
        b_wer = _wer(ground, baseline)
        b_aoo_mean, b_aoo_p95, b_matched = _aoo(ground, baseline)
        out["baseline"] = {
            "wer": b_wer,
            "aoo_mean_s": b_aoo_mean,
            "aoo_p95_s": b_aoo_p95,
            "segments": len(baseline),
            "matched": b_matched,
            "composite": _composite(b_wer, b_aoo_mean),
        }
    if improvement is not None:
        i_wer = _wer(ground, improvement)
        i_aoo_mean, i_aoo_p95, i_matched = _aoo(ground, improvement)
        out["improvement"] = {
            "wer": i_wer,
            "aoo_mean_s": i_aoo_mean,
            "aoo_p95_s": i_aoo_p95,
            "segments": len(improvement),
            "matched": i_matched,
            "composite": _composite(i_wer, i_aoo_mean),
        }
    return out


def render_report(per_job: list[dict]) -> str:
    """Markdown report: per-job table + aggregate deltas."""
    lines: list[str] = []
    lines.append("# Lyrics quality benchmark report")
    lines.append("")
    lines.append(f"Scored {len(per_job)} job(s) under `benchmark/dataset/`")
    lines.append("")
    lines.append("## Per-job results")
    lines.append("")
    lines.append("| Job | Source | WER baseline → tier1 | AOO mean (s) baseline → tier1 | Composite baseline → tier1 |")
    lines.append("|---|---|---|---|---|")
    has_improvement = False
    for r in per_job:
        b = r.get("baseline") or {}
        i = r.get("improvement") or {}
        if i:
            has_improvement = True
        wer_cell = f"{b.get('wer',float('nan')):.3f}" + (f" → {i['wer']:.3f}" if i else "")
        aoo_cell = f"{b.get('aoo_mean_s',float('nan')):.3f}" + (f" → {i['aoo_mean_s']:.3f}" if i else "")
        comp_cell = f"{b.get('composite',float('nan')):.3f}" + (f" → {i['composite']:.3f}" if i else "")
        lines.append(f"| `{r['job_id']}` | `{r.get('source','?')}` | {wer_cell} | {aoo_cell} | {comp_cell} |")
    lines.append("")

    # Aggregates
    if per_job and any(r.get("baseline") for r in per_job):
        lines.append("## Aggregates")
        lines.append("")
        b_wers = [r["baseline"]["wer"] for r in per_job if r.get("baseline")]
        b_aoos = [r["baseline"]["aoo_mean_s"] for r in per_job if r.get("baseline")]
        b_comps = [r["baseline"]["composite"] for r in per_job if r.get("baseline")]
        lines.append(f"- Baseline mean WER: **{mean(b_wers):.3f}** ({len(b_wers)} jobs)")
        lines.append(f"- Baseline mean AOO: **{mean(b_aoos):.3f} s**")
        lines.append(f"- Baseline mean composite: **{mean(b_comps):.3f}**")
        if has_improvement:
            i_wers = [r["improvement"]["wer"] for r in per_job if r.get("improvement")]
            i_aoos = [r["improvement"]["aoo_mean_s"] for r in per_job if r.get("improvement")]
            i_comps = [r["improvement"]["composite"] for r in per_job if r.get("improvement")]
            lines.append("")
            lines.append(f"- Tier-1 mean WER: **{mean(i_wers):.3f}** "
                         f"(Δ = {(mean(i_wers) - mean(b_wers)) * 100:+.1f}%)")
            lines.append(f"- Tier-1 mean AOO: **{mean(i_aoos):.3f} s** "
                         f"(Δ = {(mean(i_aoos) - mean(b_aoos)) * 1000:+.0f} ms)")
            lines.append(f"- Tier-1 mean composite: **{mean(i_comps):.3f}** "
                         f"(Δ = {(mean(i_comps) - mean(b_comps)) * 100:+.1f}%)")
            lines.append("")
            # Decision summary (per plan thresholds)
            wer_drop_pct = (mean(b_wers) - mean(i_wers)) / max(mean(b_wers), 1e-9) * 100
            aoo_drop_pct = (mean(b_aoos) - mean(i_aoos)) / max(mean(b_aoos), 1e-9) * 100
            lines.append("## Decision (per plan thresholds)")
            lines.append("")
            lines.append(f"- WER dropped {wer_drop_pct:.1f}% (target: ≥30%)")
            lines.append(f"- AOO dropped {aoo_drop_pct:.1f}% (target: ≥40%)")
            if wer_drop_pct >= 30 and aoo_drop_pct >= 40:
                verdict = "✅ **Ship Tier 1 to staging**"
            elif max(wer_drop_pct, aoo_drop_pct) >= 30:
                verdict = "🟡 **Ship partial** — only the helper(s) responsible for the improvement"
            elif max(wer_drop_pct, aoo_drop_pct) < 15:
                verdict = "❌ **Do not ship** — re-tune prompts/thresholds before any deploy"
            else:
                verdict = "🟡 **Marginal** — operator judgment call. Consider longer dataset before deciding"
            lines.append("")
            lines.append(f"### Verdict: {verdict}")
    lines.append("")
    return "\n".join(lines)


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--out", type=Path, default=None, help="write report to file (default: stdout)")
    args = p.parse_args()

    if not DATASET.exists():
        print(f"[ERR] dataset dir not found: {DATASET}", file=sys.stderr)
        sys.exit(2)

    per_job: list[dict] = []
    for d in sorted(p for p in DATASET.iterdir() if p.is_dir()):
        # Pull source from improvement_output preferentially (it's the
        # newer run); fall back to baseline.
        bundle_path = d / "improvement_output.json"
        if not bundle_path.exists():
            bundle_path = d / "baseline_output.json"
        source = "?"
        if bundle_path.exists():
            bundle = json.loads(bundle_path.read_text())
            if isinstance(bundle, dict):
                source = bundle.get("source", "?")

        scored = score_job(d)
        if scored is None:
            continue
        scored["source"] = source
        per_job.append(scored)

    if not per_job:
        print("[ERR] no scored jobs (need ground_truth.json + at least baseline_output.json)", file=sys.stderr)
        sys.exit(2)

    report = render_report(per_job)
    if args.out:
        args.out.write_text(report)
        print(f"Wrote report to {args.out}")
    else:
        print(report)


if __name__ == "__main__":
    main()
