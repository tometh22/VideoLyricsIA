"""Preflight runner. Executes every registered Check and emits a Markdown
report (human-readable) plus a JSON summary (CI-readable).

Usage (run from backend/):

    python3 -m scripts.preflight.run
    python3 -m scripts.preflight.run --only umg_master_conformance
    python3 -m scripts.preflight.run --skip validator_quality   # skip the $$ one
    python3 -m scripts.preflight.run --umg-master /path/to/file_umg_master.mov
    python3 -m scripts.preflight.run --validator-prompts 3 --validator-budget 3.0

Exit codes:
    0  every P0 check passed (or warned)
    1  at least one P0 check failed or errored
    2  every P0 was skipped (nothing actually verified)
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime
from pathlib import Path

from ._base import Check, Status, execute
from .check_concurrency import ConcurrencyCheck
from .check_production_health import ProductionHealthCheck
from .check_umg_master import UmgMasterCheck
from .check_validator_quality import ValidatorQualityCheck
from .check_volume_caps import VolumeCapsCheck


REPORTS_DIR = Path(__file__).resolve().parent / "reports"


def build_checks(args) -> list[Check]:
    umg_expected = {
        "frame_size": "HD",
        "width": 1920,
        "height": 1080,
        "fps": 23.976 if args.umg_fps == 23.976 else float(args.umg_fps),
        "prores_profile": args.umg_profile,
        "pix_fmt": "yuv422p10le" if args.umg_profile == 3 else "yuv444p10le",
    }
    api_url = (args.api_url or os.environ.get("PRODUCTION_API_URL")
               or "https://genly-ai.up.railway.app")
    return [
        ProductionHealthCheck(api_url),
        VolumeCapsCheck(),
        UmgMasterCheck(args.umg_master, umg_expected),
        ValidatorQualityCheck(
            n_prompts=args.validator_prompts,
            max_total_usd=args.validator_budget,
        ),
        ConcurrencyCheck(
            api_url=api_url,
            username=os.environ.get("PREFLIGHT_USERNAME"),
            password=os.environ.get("PREFLIGHT_PASSWORD"),
            mp3_path=args.concurrency_mp3,
            concurrency=args.concurrency_n,
            timeout_secs=args.concurrency_timeout,
        ),
    ]


# Visual aids in terminal + markdown.
ICON = {
    Status.PASS: "✅",
    Status.FAIL: "❌",
    Status.WARN: "⚠️",
    Status.ERROR: "💥",
    Status.SKIPPED: "⏭️",
}


def write_markdown(results: list, dest: Path, started_at: datetime) -> None:
    p0_results = [r for r, c in results if c.p0]
    other_results = [r for r, c in results if not c.p0]
    p0_failing = [r for r in p0_results if r.status in (Status.FAIL, Status.ERROR)]
    p0_skipped = [r for r in p0_results if r.status == Status.SKIPPED]

    if p0_failing:
        verdict = "🚫 NO-GO"
        verdict_explain = (
            f"{len(p0_failing)} P0 check(s) failed. Resolve before letting UMG "
            "submit a real job."
        )
    elif p0_results and all(r.status == Status.SKIPPED for r in p0_results):
        verdict = "⚠️ INCONCLUSIVE"
        verdict_explain = "every P0 was skipped — nothing was actually verified."
    else:
        verdict = "✅ GO"
        verdict_explain = (
            "every P0 check passed (warnings are non-blocking but worth reading)."
        )

    lines = [
        "# Preflight report",
        "",
        f"- **Verdict:** {verdict}",
        f"- {verdict_explain}",
        f"- Started: {started_at.isoformat(timespec='seconds')}",
        f"- Checks run: {len(results)}",
        "",
        "## Top-line",
        "",
        "| Check | P0 | Status | Duration | Summary |",
        "|---|---|---|---|---|",
    ]
    for result, check in results:
        lines.append(
            f"| `{result.name}` | {'✓' if check.p0 else ''} | "
            f"{ICON[result.status]} {result.status.value} | "
            f"{result.duration_ms} ms | {result.summary} |"
        )

    if p0_skipped:
        lines += ["", "### Skipped P0 checks", ""]
        for r in p0_skipped:
            lines += [f"- `{r.name}`: {r.summary}"]

    # Detail dumps per check.
    lines += ["", "## Details", ""]
    for result, check in results:
        lines += [
            f"### {ICON[result.status]} `{result.name}` — {result.status.value}",
            "",
            f"_{check.description}_",
            "",
            f"**Summary:** {result.summary}",
            "",
        ]
        if result.details:
            lines += [
                "<details><summary>raw details</summary>",
                "",
                "```json",
                json.dumps(result.details, indent=2, default=str),
                "```",
                "",
                "</details>",
                "",
            ]

    dest.write_text("\n".join(lines))


def main() -> int:
    parser = argparse.ArgumentParser(description="GenLy preflight test suite")
    parser.add_argument("--only", action="append", default=[],
                        help="Only run named checks (repeatable)")
    parser.add_argument("--skip", action="append", default=[],
                        help="Skip named checks (repeatable)")
    parser.add_argument("--json", action="store_true",
                        help="Print JSON summary to stdout instead of Markdown")
    parser.add_argument("--api-url", default=None,
                        help="Production API base URL (defaults to PRODUCTION_API_URL env)")
    parser.add_argument("--umg-master", default=None,
                        help="Path to a previously generated _umg_master.mov to verify")
    parser.add_argument("--umg-fps", type=float, default=23.976)
    parser.add_argument("--umg-profile", type=int, default=3,
                        help="ProRes profile id: 3=422 HQ, 4=4444, 5=4444 XQ")
    parser.add_argument("--validator-prompts", type=int, default=5,
                        help="Number of Veo prompts to sample for the validator audit")
    parser.add_argument("--validator-budget", type=float, default=5.0,
                        help="Max USD spend for the validator audit (Veo Fast)")
    parser.add_argument("--concurrency-mp3", default=None,
                        help="Path to a real MP3 used as input for the concurrency stress test")
    parser.add_argument("--concurrency-n", type=int, default=3,
                        help="Number of parallel jobs for the concurrency test")
    parser.add_argument("--concurrency-timeout", type=int, default=900,
                        help="Seconds before the concurrency test marks any job as hung")
    args = parser.parse_args()

    REPORTS_DIR.mkdir(parents=True, exist_ok=True)
    started = datetime.now()

    checks = build_checks(args)
    if args.only:
        checks = [c for c in checks if c.name in args.only]
    if args.skip:
        checks = [c for c in checks if c.name not in args.skip]
    if not checks:
        print("no checks selected after --only/--skip filtering", file=sys.stderr)
        return 1

    results: list = []
    for check in checks:
        if not args.json:
            print(f"→ {check.name} ...", flush=True)
        result = execute(check)
        results.append((result, check))
        if not args.json:
            print(f"  {ICON[result.status]} {result.status.value}: {result.summary}")

    p0 = [(r, c) for r, c in results if c.p0]
    p0_failed = [r for r, _ in p0 if r.status in (Status.FAIL, Status.ERROR)]
    p0_all_skipped = bool(p0) and all(r.status == Status.SKIPPED for r, _ in p0)

    stamp = started.strftime("%Y%m%d-%H%M%S")
    md_path = REPORTS_DIR / f"preflight-{stamp}.md"
    json_path = REPORTS_DIR / f"preflight-{stamp}.json"
    write_markdown(results, md_path, started)
    json_path.write_text(json.dumps(
        [{"check": c.name, "p0": c.p0, "result": r.to_dict()} for r, c in results],
        indent=2, default=str,
    ))

    if args.json:
        print(json_path.read_text())
    else:
        print()
        print(f"Markdown report: {md_path}")
        print(f"JSON report:     {json_path}")
        if p0_failed:
            print(f"\n🚫 NO-GO — {len(p0_failed)} P0 check(s) failed")
        elif p0_all_skipped:
            print("\n⚠️ INCONCLUSIVE — every P0 was skipped")
        else:
            print("\n✅ GO — every P0 check passed")

    if p0_failed:
        return 1
    if p0_all_skipped:
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
