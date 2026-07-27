"""Analyze a QA run directory: load per-song results, execute detectors,
write analysis.json + report.md, return (verdict, report_path).

Can run standalone (`python analyze_run.py --run-dir ...`) or be invoked
in-process by `run_qa_suite.main()`."""
from __future__ import annotations

import argparse
import json
import os
import statistics
import sys
import time
from pathlib import Path
from typing import Optional

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent.parent))   # backend/
sys.path.insert(0, str(HERE.parent))           # backend/scripts/

from qa._loader import load_golden_set, resolve_expected   # noqa: E402
from qa._detectors import run_all                            # noqa: E402
from qa._cost import estimate_from_log, aggregate            # noqa: E402
from preflight._base import Status                           # noqa: E402


def _classify(detector_results) -> str:
    fails = sum(1 for r in detector_results if r.status == Status.FAIL or r.status == Status.ERROR)
    warns = sum(1 for r in detector_results if r.status == Status.WARN)
    if fails:
        return "FAIL"
    if warns:
        return "WARN"
    return "PASS"


def analyze(run_dir: str) -> tuple[int, str]:
    """Analyze a single run directory. Writes analysis.json + report.md.
    Returns (exit_code, report_path). Exit 0 on PASS/WARN, 1 on FAIL."""
    run_dir = Path(run_dir).resolve()
    if not run_dir.exists():
        raise FileNotFoundError(run_dir)

    gs = load_golden_set(run_dir / "golden_set.yaml")
    defaults = gs.get("defaults") or {}
    songs_by_id = {s["id"]: s for s in (gs.get("songs") or [])}

    meta = {}
    meta_path = run_dir / "meta.json"
    if meta_path.exists():
        meta = json.loads(meta_path.read_text())

    summary = {}
    summary_path = run_dir / "summary.json"
    if summary_path.exists():
        summary = json.loads(summary_path.read_text())

    per_song: dict = {}
    per_song_costs: dict = {}
    runs_dir = run_dir / "runs"

    for song_id, song in songs_by_id.items():
        json_path = runs_dir / f"{song_id}.json"
        log_path = runs_dir / f"{song_id}.log"
        if not json_path.exists():
            per_song[song_id] = {
                "status": "SKIPPED",
                "summary": "no run.json (song was not executed)",
                "detectors": [],
            }
            continue
        run = json.loads(json_path.read_text())
        expected = resolve_expected(song, defaults)
        audio_dur = (run.get("meta") or {}).get("audio_duration_s")
        # Cost from logs.
        cost = estimate_from_log(log_path) if log_path.exists() else {}
        run["cost"] = cost
        per_song_costs[song_id] = cost
        results = run_all(run, expected, audio_dur)
        status = _classify(results)
        per_song[song_id] = {
            "status": status,
            "song": {"id": song_id, "artist": song.get("artist"), "title": song.get("title")},
            "expected": expected,
            "actual": {
                "timing_source": run.get("source"),
                "n_segments": len(run.get("segments") or []),
                "first_start_s": (run.get("segments") or [{}])[0].get("start") if run.get("segments") else None,
                "elapsed_s": (run.get("telemetry") or {}).get("elapsed_seconds"),
                "audio_duration_s": audio_dur,
            },
            "cost": cost,
            "detectors": [r.to_dict() for r in results],
            "log": str(log_path),
        }

    n_fail = sum(1 for v in per_song.values() if v["status"] == "FAIL")
    n_warn = sum(1 for v in per_song.values() if v["status"] == "WARN")
    n_pass = sum(1 for v in per_song.values() if v["status"] == "PASS")
    n_skip = sum(1 for v in per_song.values() if v["status"] == "SKIPPED")

    cost_agg = aggregate(per_song_costs)
    timing_sources = {}
    elapseds = []
    for v in per_song.values():
        ts = (v.get("actual") or {}).get("timing_source")
        if ts:
            timing_sources[ts] = timing_sources.get(ts, 0) + 1
        e = (v.get("actual") or {}).get("elapsed_s")
        if e is not None:
            elapseds.append(e)

    verdict = "PASS"
    if n_fail:
        verdict = "FAIL"
    elif n_warn:
        verdict = "WARN"

    analysis = {
        "verdict": verdict,
        "counts": {"pass": n_pass, "warn": n_warn, "fail": n_fail, "skipped": n_skip},
        "timing_source_distribution": timing_sources,
        "cost": cost_agg,
        "latency": {
            "p50_s": round(statistics.median(elapseds), 1) if elapseds else None,
            "p95_s": round(_p95(elapseds), 1) if len(elapseds) >= 2 else None,
        },
        "per_song": per_song,
        "meta": meta,
        "summary": summary,
    }
    (run_dir / "analysis.json").write_text(json.dumps(analysis, indent=2, default=str))

    report_path = run_dir / "report.md"
    report_path.write_text(_render_markdown(analysis, run_dir))

    print(f"\nQA verdict: {verdict} — {n_pass} PASS, {n_warn} WARN, {n_fail} FAIL, {n_skip} SKIPPED")
    print(f"Cost estimate: ${cost_agg['usd']['total']}  (demucs {cost_agg['calls']['demucs']} · "
          f"whisperX {cost_agg['calls']['whisperx']} · FA {cost_agg['calls']['forced_align']} · "
          f"throttle 429: {cost_agg['throttle_429_count']})")
    print(f"Report: {report_path}")
    return (1 if n_fail else 0), str(report_path)


def _p95(xs):
    if not xs:
        return None
    s = sorted(xs)
    k = max(0, int(round(len(s) * 0.95)) - 1)
    return s[k]


def _render_markdown(analysis: dict, run_dir: Path) -> str:
    """Generate the human-readable report — failures up top."""
    out: list[str] = []
    v = analysis["verdict"]
    counts = analysis["counts"]
    out.append(f"# QA suite report — {run_dir.name}")
    out.append("")
    emoji = {"PASS": "🟢", "WARN": "🟡", "FAIL": "🔴"}.get(v, "⚪")
    out.append(f"## Verdict: {emoji} **{v}** — {counts['pass']} PASS · {counts['warn']} WARN · {counts['fail']} FAIL · {counts['skipped']} SKIPPED")
    out.append("")

    # Per-song summary table.
    out.append("| Song | Status | Timing source | Elapsed | First vocal | Lines | Issues |")
    out.append("|---|---|---|---|---|---|---|")
    for sid, v_row in analysis["per_song"].items():
        actual = v_row.get("actual") or {}
        n_issues = sum(1 for d in (v_row.get("detectors") or []) if d.get("status") in ("fail", "warn", "error"))
        out.append(
            f"| {sid} | {v_row['status']} | "
            f"{(actual.get('timing_source') or '—')} | "
            f"{(round(actual.get('elapsed_s'),1) if actual.get('elapsed_s') is not None else '—')}s | "
            f"{(round(actual.get('first_start_s'),1) if actual.get('first_start_s') is not None else '—')}s | "
            f"{actual.get('n_segments') or '—'} | "
            f"{n_issues} |"
        )
    out.append("")

    # FAIL section first.
    fails = [
        (sid, d)
        for sid, v in analysis["per_song"].items()
        for d in (v.get("detectors") or [])
        if d.get("status") in ("fail", "error")
    ]
    if fails:
        out.append("## 🔴 Critical issues (FAIL)")
        out.append("")
        for sid, d in fails:
            out.append(f"### {sid} — `{d['name']}`")
            out.append(f"- {d['summary']}")
            details = d.get("details") or {}
            for k, val in details.items():
                if k == "traceback":
                    out.append(f"  - {k}: (see analysis.json)")
                else:
                    out.append(f"  - {k}: `{val}`")
            out.append("")

    # WARN section.
    warns = [
        (sid, d)
        for sid, v in analysis["per_song"].items()
        for d in (v.get("detectors") or [])
        if d.get("status") == "warn"
    ]
    if warns:
        out.append("## 🟡 Warnings")
        out.append("")
        for sid, d in warns:
            details = d.get("details") or {}
            details_str = " · ".join(f"{k}={v!r}" for k, v in details.items())
            out.append(f"- **{sid}** `{d['name']}`: {d['summary']}{(' · ' + details_str) if details_str else ''}")
        out.append("")

    # Run metadata.
    out.append("## Run metadata")
    out.append("")
    meta = analysis.get("meta") or {}
    out.append(f"- mode: `{meta.get('mode', '—')}`")
    out.append(f"- git_sha: `{meta.get('git_sha', '—')}`")
    out.append(f"- started: `{meta.get('started_at', '—')}`")
    out.append(f"- finished: `{meta.get('finished_at', '—')}`")
    flags = meta.get("env_flags") or {}
    if flags:
        out.append(f"- env flags: " + ", ".join(f"`{k}={v}`" for k, v in flags.items()))
    cost = analysis.get("cost") or {}
    usd = cost.get("usd") or {}
    out.append(
        f"- cost: ${usd.get('total', 0):.4f} "
        f"(demucs ${usd.get('demucs', 0):.2f}, whisperX ${usd.get('whisperx', 0):.2f}, FA ${usd.get('forced_align', 0):.2f})"
    )
    latency = analysis.get("latency") or {}
    out.append(f"- latency: p50={latency.get('p50_s')}s, p95={latency.get('p95_s')}s")
    out.append(f"- timing_source distribution: `{analysis.get('timing_source_distribution')}`")
    out.append(f"- throttle 429 count: {cost.get('throttle_429_count', 0)}")
    out.append("")

    # Per-song detail.
    out.append("## Per-song detail")
    out.append("")
    for sid, v in analysis["per_song"].items():
        out.append(f"### {sid}")
        actual = v.get("actual") or {}
        exp = v.get("expected") or {}
        out.append(f"- expected.timing_source: `{exp.get('timing_source')}`")
        out.append(f"- actual.timing_source: `{actual.get('timing_source')}`")
        out.append(f"- segments: {actual.get('n_segments')}  · first start: {actual.get('first_start_s')}s  · elapsed: {actual.get('elapsed_s')}s")
        cost = v.get("cost") or {}
        usd = cost.get("usd") or {}
        out.append(f"- cost: ${usd.get('total', 0):.4f}  (demucs ×{cost.get('demucs_calls',0)}, whisperX ×{cost.get('whisperx_calls',0)}, FA ×{cost.get('forced_align_calls',0)})")
        out.append("- detectors:")
        for d in (v.get("detectors") or []):
            badge = {"pass": "✓", "fail": "✗", "warn": "△", "error": "!", "skipped": "·"}.get(d.get("status"), "?")
            out.append(f"    - {badge} **{d['name']}** ({d['status']}) — {d['summary']}")
        out.append(f"- log: `{v.get('log', '—')}`")
        out.append("")

    return "\n".join(out)


def main():
    p = argparse.ArgumentParser(description="Analyze a QA run directory.")
    p.add_argument("--run-dir", required=True, help="Path to the run directory (contains golden_set.yaml + runs/)")
    args = p.parse_args()
    exit_code, _ = analyze(args.run_dir)
    sys.exit(exit_code)


if __name__ == "__main__":
    main()
