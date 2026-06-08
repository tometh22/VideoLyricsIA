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


def _aoo(ground: list[dict], output: list[dict]) -> tuple[float, float, int, float]:
    """For each output segment, find best-match in ground by text and compute
    start-time offset. Returns (mean_abs, p95_abs, matched_count, p95_deoffset).

    `p95_deoffset` subtracts the median SIGNED offset before taking |·| — it
    isolates per-line drift from a constant global shift (e.g. Rotor's ground
    truth starts at 39.64s; if our zero differs, abs-AOO is inflated by that
    constant. de-offset answers "once aligned at the same zero, how tight are
    the line onsets?")."""
    abs_off: list[float] = []
    signed: list[float] = []
    for out_seg in output:
        out_text = (out_seg.get("text") or "").strip()
        if not out_text:
            continue
        try:
            out_start = float(out_seg["start"])
        except (KeyError, TypeError, ValueError):
            continue
        # Repeated-line aware matching: among ALL ground lines whose text is a
        # strong match (jaccard ≥ 0.6), pick the one CLOSEST IN TIME — not the
        # globally-best-text. Choruses repeat the same line up to ~8×; picking by
        # text alone pairs an output line with a far-away repeat → bogus 30-50s
        # offsets. Nearest-in-time is the correct pairing for identical repeats
        # (you can't distinguish instances otherwise) and is the standard karaoke
        # timing eval. Falls back to best-text (≥0.4) if no strong match exists.
        candidates = [(_jaccard(out_text, gt.get("text") or ""), gt) for gt in ground]
        strong = [(sc, gt) for sc, gt in candidates if sc >= 0.6]
        if strong:
            best_match = min(strong, key=lambda t: abs(out_start - float(t[1]["start"])))[1]
        else:
            sc, gt = max(candidates, key=lambda t: t[0]) if candidates else (0.0, None)
            best_match = gt if sc >= 0.4 else None
        if best_match is None:
            continue
        try:
            d = out_start - float(best_match["start"])
            abs_off.append(abs(d))
            signed.append(d)
        except (KeyError, TypeError, ValueError):
            continue
    if not abs_off:
        return (0.0, 0.0, 0, 0.0)
    def _p95(xs: list[float]) -> float:
        s = sorted(xs)
        return s[max(0, int(len(s) * 0.95) - 1)]
    from statistics import median
    med = median(signed)
    deoff = [abs(s - med) for s in signed]
    return (mean(abs_off), _p95(abs_off), len(abs_off), _p95(deoff))


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
        b_aoo_mean, b_aoo_p95, b_matched, b_aoo_p95_deoff = _aoo(ground, baseline)
        out["baseline"] = {
            "wer": b_wer,
            "aoo_mean_s": b_aoo_mean,
            "aoo_p95_s": b_aoo_p95,
            "aoo_p95_deoffset_s": b_aoo_p95_deoff,
            "segments": len(baseline),
            "matched": b_matched,
            "composite": _composite(b_wer, b_aoo_mean),
        }
    if improvement is not None:
        i_wer = _wer(ground, improvement)
        i_aoo_mean, i_aoo_p95, i_matched, i_aoo_p95_deoff = _aoo(ground, improvement)
        out["improvement"] = {
            "wer": i_wer,
            "aoo_mean_s": i_aoo_mean,
            "aoo_p95_s": i_aoo_p95,
            "aoo_p95_deoffset_s": i_aoo_p95_deoff,
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
    lines.append("| Job | Source | WER b→t1 | AOO mean (s) b→t1 | AOO p95 (s) b→t1 | AOO p95 de-offset (s) b→t1 | matched/GT | Composite b→t1 |")
    lines.append("|---|---|---|---|---|---|---|---|")
    has_improvement = False
    for r in per_job:
        b = r.get("baseline") or {}
        i = r.get("improvement") or {}
        if i:
            has_improvement = True
        wer_cell = f"{b.get('wer',float('nan')):.3f}" + (f" → {i['wer']:.3f}" if i else "")
        aoo_cell = f"{b.get('aoo_mean_s',float('nan')):.3f}" + (f" → {i['aoo_mean_s']:.3f}" if i else "")
        p95_cell = f"{b.get('aoo_p95_s',float('nan')):.3f}" + (f" → {i['aoo_p95_s']:.3f}" if i else "")
        deoff_cell = f"{b.get('aoo_p95_deoffset_s',float('nan')):.3f}" + (f" → {i['aoo_p95_deoffset_s']:.3f}" if i else "")
        matched_cell = f"{b.get('matched','?')}/{r.get('ground_segments','?')}"
        comp_cell = f"{b.get('composite',float('nan')):.3f}" + (f" → {i['composite']:.3f}" if i else "")
        lines.append(f"| `{r['job_id']}` | `{r.get('source','?')}` | {wer_cell} | {aoo_cell} | {p95_cell} | {deoff_cell} | {matched_cell} | {comp_cell} |")
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
