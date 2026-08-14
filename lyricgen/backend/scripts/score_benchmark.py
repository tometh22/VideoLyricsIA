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
import hashlib
import sys
from pathlib import Path
from statistics import mean, median
from math import ceil

HERE = Path(__file__).resolve().parent
DATASET = HERE.parent / "benchmark" / "dataset"
MANIFEST_PATH = HERE / "benchmark_manifest.json"
TARGET_DATASET_MIN = 30
TARGET_DATASET_MAX = 50
TARGET_LIVE_MIN = 8
TARGET_OPERATOR_P50_MIN = 5.0
TARGET_OPERATOR_P90_MIN = 10.0


def _percentile(values: list[float], quantile: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    if quantile == 0.50:
        return median(ordered)
    # Conservative nearest-rank for an SLA tail: never interpolate away a
    # slow song or round down below it.
    index = max(0, min(len(ordered) - 1, ceil(quantile * len(ordered)) - 1))
    return ordered[index]


def _load(path: Path) -> list[dict] | None:
    if not path.exists():
        return None
    data = json.loads(path.read_text())
    # ground_truth.json is a bare list; baseline/improvement bundles
    # have {segments: [...], source, meta}
    if isinstance(data, list):
        return data
    return data.get("segments")


def _load_bundle(path: Path) -> dict:
    if not path.exists():
        return {}
    data = json.loads(path.read_text())
    return data if isinstance(data, dict) else {"segments": data}


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def validate_manifest() -> list[str]:
    if not MANIFEST_PATH.exists():
        return [f"missing manifest: {MANIFEST_PATH}"]
    manifest = json.loads(MANIFEST_PATH.read_text())
    entries = manifest.get("entries") or []
    errors = []
    expected = {str(entry.get("job_id")) for entry in entries}
    actual = {path.name for path in DATASET.iterdir() if path.is_dir()}
    if actual != expected:
        errors.append(f"dataset ids drift: expected={sorted(expected)} actual={sorted(actual)}")
    for entry in entries:
        if entry.get("is_live_source") != "manual":
            errors.append(
                f"live/studio label is not manually verified: {entry.get('job_id')}"
            )
        job_dir = DATASET / str(entry.get("job_id"))
        checks = {
            job_dir / str(entry.get("audio_file")): entry.get("audio_sha256"),
            job_dir / "ground_truth.json": entry.get("ground_truth_sha256"),
            job_dir / "metadata.json": entry.get("metadata_sha256"),
        }
        for path, expected_hash in checks.items():
            if not path.exists() or not expected_hash or _sha256(path) != expected_hash:
                errors.append(f"manifest drift: {path}")
    return errors


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
    """Greedy ONE-TO-ONE onset match. Output segments (in time order) each
    claim the closest-start ground-truth line whose text matches (Jaccard
    >= 0.4) and isn't already claimed.

    One-to-one matching fixes the repeated-chorus inflation of the old naive
    matcher: a chorus sung 7x would otherwise all match GT occurrence #1,
    manufacturing huge fake offsets (a real bug seen on "No Hay Santos" where
    p95 read 77s). Returns (mean_offset, p95_offset, matched_count)."""
    taken: set[int] = set()
    offsets: list[float] = []
    for out_seg in sorted(output, key=lambda s: float(s.get("start", 0) or 0)):
        out_text = (out_seg.get("text") or "").strip()
        if not out_text:
            continue
        cand: list[tuple[float, int]] = []
        for i, gt in enumerate(ground):
            if i in taken:
                continue
            if _jaccard(out_text, gt.get("text") or "") >= 0.4:
                try:
                    cand.append((abs(float(out_seg["start"]) - float(gt["start"])), i))
                except (KeyError, TypeError, ValueError):
                    continue
        if not cand:
            continue
        cand.sort()
        off, idx = cand[0]
        taken.add(idx)
        offsets.append(off)
    if not offsets:
        # No textual alignment is not perfect timing. Penalize it at the
        # composite metric's saturation point so dropped/collapsed lyrics
        # cannot manufacture an AOO of zero.
        return (2.0, 2.0, 0)
    offsets_sorted = sorted(offsets)
    p95 = offsets_sorted[max(0, int(len(offsets_sorted) * 0.95) - 1)]
    return (mean(offsets), p95, len(offsets))


def _recall(ground: list[dict], output: list[dict]) -> float:
    """Fraction of ground-truth lines that have a text match (Jaccard >= 0.4)
    somewhere in the output. Catches DROPPED lines and collapse — failures
    that WER (which compares concatenated text) can mask."""
    if not ground:
        return 0.0
    hit = sum(
        1 for gt in ground
        if any(_jaccard(gt.get("text") or "", o.get("text") or "") >= 0.4 for o in output)
    )
    return hit / len(ground)


def _composite(wer: float, aoo_mean: float) -> float:
    norm_aoo = min(aoo_mean / 2.0, 1.0)
    return max(0.0, 1.0 - (0.5 * wer + 0.5 * norm_aoo))


def score_job(job_dir: Path) -> dict | None:
    ground = _load(job_dir / "ground_truth.json")
    baseline = _load(job_dir / "baseline_output.json")
    improvement = _load(job_dir / "improvement_output.json")
    if ground is None:
        return None

    metadata_path = job_dir / "metadata.json"
    metadata = json.loads(metadata_path.read_text()) if metadata_path.exists() else {}
    out = {
        "job_id": job_dir.name,
        "ground_segments": len(ground),
        "is_live": bool(metadata.get("is_live")),
        "operator_review_minutes": metadata.get("operator_review_minutes"),
        "operator_time_source": metadata.get("operator_time_source"),
        "operator_pipeline_release": metadata.get("operator_pipeline_release"),
        "timing_source": metadata.get("timing_source"),
        "operator_corrections": metadata.get("operator_corrections") or {},
        "baseline_pipeline_release": (
            (_load_bundle(job_dir / "baseline_output.json").get("meta") or {})
            .get("pipeline_release")
        ),
        "improvement_pipeline_release": (
            (_load_bundle(job_dir / "improvement_output.json").get("meta") or {})
            .get("pipeline_release")
        ),
    }
    if baseline is not None:
        b_wer = _wer(ground, baseline)
        b_aoo_mean, b_aoo_p95, b_matched = _aoo(ground, baseline)
        out["baseline"] = {
            "wer": b_wer,
            "aoo_mean_s": b_aoo_mean,
            "aoo_p95_s": b_aoo_p95,
            "recall": _recall(ground, baseline),
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
            "recall": _recall(ground, improvement),
            "segments": len(improvement),
            "matched": i_matched,
            "composite": _composite(i_wer, i_aoo_mean),
        }
    return out


def _cohort_no_regression(rows: list[dict]) -> bool:
    """Require text, timing and recall health within one corpus stratum."""
    if not rows or not all(row.get("baseline") and row.get("improvement") for row in rows):
        return False
    baseline = [row["baseline"] for row in rows]
    improvement = [row["improvement"] for row in rows]
    ground_count = sum(int(row.get("ground_segments") or 0) for row in rows)
    matched = sum(int(metric.get("matched") or 0) for metric in improvement)
    return (
        mean(metric["wer"] for metric in improvement)
        <= mean(metric["wer"] for metric in baseline)
        and mean(metric["aoo_mean_s"] for metric in improvement)
        <= mean(metric["aoo_mean_s"] for metric in baseline)
        and mean(metric["recall"] for metric in improvement)
        >= mean(metric["recall"] for metric in baseline)
        and mean(metric["recall"] for metric in improvement) >= 0.75
        and ground_count > 0
        and matched / ground_count >= 0.75
    )


def render_report(per_job: list[dict]) -> str:
    """Markdown report: per-job table + aggregate deltas."""
    lines: list[str] = []
    lines.append("# Lyrics quality benchmark report")
    lines.append("")
    lines.append(f"Scored {len(per_job)} job(s) under `benchmark/dataset/`")
    live_count = sum(1 for row in per_job if row.get("is_live"))
    lines.append(f"Corpus mix: {live_count} live / {len(per_job) - live_count} studio")
    lines.append("")

    operator_minutes = [
        float(row["operator_review_minutes"])
        for row in per_job
        if isinstance(row.get("operator_review_minutes"), (int, float))
    ]
    operator_releases = sorted({
        str(row["operator_pipeline_release"])
        for row in per_job if row.get("operator_pipeline_release")
    })
    active_time_count = sum(
        1 for row in per_job if row.get("operator_time_source") == "active_edit_ms"
    )
    review_p50 = _percentile(operator_minutes, 0.50)
    review_p90 = _percentile(operator_minutes, 0.90)
    sample_ok = TARGET_DATASET_MIN <= len(per_job) <= TARGET_DATASET_MAX
    live_ok = live_count >= TARGET_LIVE_MIN
    operation_ok = (
        review_p50 is not None and review_p90 is not None
        and len(operator_minutes) == len(per_job)
        and active_time_count == len(per_job)
        and len(operator_releases) == 1
        and review_p50 < TARGET_OPERATOR_P50_MIN
        and review_p90 < TARGET_OPERATOR_P90_MIN
    )
    lines.append("## Operational quality gate")
    lines.append("")
    lines.append(
        f"- Corpus: **{len(per_job)} songs** (target: {TARGET_DATASET_MIN}–{TARGET_DATASET_MAX}) "
        f"— {'✅' if sample_ok else '❌'}"
    )
    lines.append(
        f"- Live recordings: **{live_count}** (target ≥{TARGET_LIVE_MIN}) "
        f"— {'✅' if live_ok else '❌'}"
    )
    if review_p50 is None:
        lines.append("- Operator time: **missing**. Record `editor_approved.duration_ms` before release.")
    else:
        lines.append(
            f"- Operator-time coverage: **{len(operator_minutes)}/{len(per_job)} songs**"
        )
        lines.append(
            f"- Active-time coverage: **{active_time_count}/{len(per_job)} songs**"
        )
        lines.append(
            f"- Operator pipeline release(s): **{', '.join(operator_releases) or 'missing'}**"
        )
        lines.append(
            f"- Operator time p50: **{review_p50:.2f} min** (target <{TARGET_OPERATOR_P50_MIN:.0f})"
        )
        lines.append(
            f"- Operator time p90: **{review_p90:.2f} min** (target <{TARGET_OPERATOR_P90_MIN:.0f})"
        )
    lines.append(
        f"- Operational target: {'✅ PASS' if operation_ok and sample_ok and live_ok else '❌ NOT MET'}"
    )
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
    p.add_argument(
        "--strict", action="store_true",
        help="exit non-zero unless corpus size and operator p50/p90 targets pass",
    )
    p.add_argument(
        "--pipeline-release", default=None,
        help="release SHA/version expected for every operator review in --strict mode",
    )
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
    if args.strict:
        manifest_errors = validate_manifest()
        minutes = [
            float(row["operator_review_minutes"])
            for row in per_job
            if isinstance(row.get("operator_review_minutes"), (int, float))
        ]
        p50, p90 = _percentile(minutes, 0.50), _percentile(minutes, 0.90)
        live_count = sum(1 for row in per_job if row.get("is_live"))
        complete_outputs = all(row.get("baseline") and row.get("improvement") for row in per_job)
        live_rows = [row for row in per_job if row.get("is_live")]
        studio_rows = [row for row in per_job if not row.get("is_live")]
        no_ml_regression = (
            complete_outputs
            and _cohort_no_regression(per_job)
            and _cohort_no_regression(live_rows)
            and _cohort_no_regression(studio_rows)
        )
        release_coverage = sum(
            1 for row in per_job
            if args.pipeline_release
            and row.get("operator_pipeline_release") == args.pipeline_release
        )
        active_time_coverage = sum(
            1 for row in per_job if row.get("operator_time_source") == "active_edit_ms"
        )
        output_release_coverage = sum(
            1 for row in per_job
            if args.pipeline_release
            and row.get("improvement_pipeline_release") == args.pipeline_release
        )
        if not (TARGET_DATASET_MIN <= len(per_job) <= TARGET_DATASET_MAX
                and live_count >= TARGET_LIVE_MIN
                and len(minutes) == len(per_job)
                and active_time_coverage == len(per_job)
                and release_coverage == len(per_job)
                and output_release_coverage == len(per_job)
                and no_ml_regression
                and not manifest_errors
                and p50 is not None and p90 is not None
                and p50 < TARGET_OPERATOR_P50_MIN
                and p90 < TARGET_OPERATOR_P90_MIN):
            sys.exit(1)


if __name__ == "__main__":
    main()
