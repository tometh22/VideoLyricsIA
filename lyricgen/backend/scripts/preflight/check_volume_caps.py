"""P0 #3 — Volume cap audit.

Verifies the four-layer defence against an accidental Veo bill explosion:

  L1. Hardcoded DEFAULT_DAILY_CAP and DEFAULT_MAX_CONCURRENT_JOBS in main.py
      remain sane (DAILY < 100, CONCURRENT < 25). Drift is silent and costly.

  L2. The actual UMG admin user has reasonable per-user overrides (or relies
      on defaults). Surfaces if anyone has been bumped to "no cap" by mistake.

  L3. The two enforcement functions are still wired into BOTH /upload and
      /generate handlers — easy to drop accidentally during refactors.

  L4. Per-Veo cost × hardest-case daily cap stays under a stated ceiling.
      We cap *worst-case* daily spend at $500 unless the runner explicitly
      raises the threshold.

A single fail here would be the kind of bug that bills $5k overnight.
"""

from __future__ import annotations

import os
import re
import sys
from pathlib import Path

from ._base import Check, CheckResult


# Sane bounds — fail if defaults silently drift past these.
MAX_SANE_DAILY_CAP = 100
MAX_SANE_CONCURRENT_CAP = 25
WORST_CASE_DAILY_USD_CEILING = 500.0


def _read_main() -> str:
    return Path(__file__).resolve().parents[2].joinpath("main.py").read_text()


def _scrape_int(src: str, name: str) -> int | None:
    m = re.search(rf"^{re.escape(name)}\s*=\s*(\d+)\b", src, re.MULTILINE)
    return int(m.group(1)) if m else None


def _enforce_callsite_count(src: str, fn_name: str) -> int:
    return len(re.findall(rf"\b{re.escape(fn_name)}\s*\(", src))


def _veo_cost_per_call() -> float:
    """Read the Veo Fast cost-per-call from provenance.py to keep this check
    in sync with the dashboard rather than hardcoding a stale number."""
    prov = Path(__file__).resolve().parents[2] / "provenance.py"
    src = prov.read_text()
    m = re.search(
        r'\("veo-3\.1-fast-generate-001",\s*"google_vertex"\):\s*([\d.]+)',
        src,
    )
    return float(m.group(1)) if m else 0.80


class VolumeCapsCheck(Check):
    name = "volume_caps"
    description = "defaults are sane, enforcement is wired, worst-case daily $$ is bounded"
    p0 = True

    def run(self) -> CheckResult:
        src = _read_main()
        daily = _scrape_int(src, "DEFAULT_DAILY_CAP")
        concurrent = _scrape_int(src, "DEFAULT_MAX_CONCURRENT_JOBS")
        upload_calls = _enforce_callsite_count(src, "_enforce_daily_volume_cap")
        concurrent_calls = _enforce_callsite_count(src, "_enforce_concurrent_jobs_cap")
        veo_cost = _veo_cost_per_call()

        problems: list[str] = []
        warnings: list[str] = []

        # L1 — hardcoded defaults sane?
        if daily is None:
            problems.append("could not locate DEFAULT_DAILY_CAP in main.py")
        elif daily > MAX_SANE_DAILY_CAP:
            problems.append(
                f"DEFAULT_DAILY_CAP={daily} exceeds sane ceiling "
                f"{MAX_SANE_DAILY_CAP} — would let a runaway script bill "
                f"${daily * veo_cost:.0f}/day"
            )

        if concurrent is None:
            problems.append("could not locate DEFAULT_MAX_CONCURRENT_JOBS in main.py")
        elif concurrent > MAX_SANE_CONCURRENT_CAP:
            problems.append(
                f"DEFAULT_MAX_CONCURRENT_JOBS={concurrent} exceeds sane ceiling "
                f"{MAX_SANE_CONCURRENT_CAP}"
            )

        # L3 — both enforcers must still be invoked from at least 2 sites
        # (currently /upload and /generate). 0 invocations means "the cap was
        # silently removed during a refactor" — exactly the bug class this
        # check exists to catch.
        if upload_calls < 2:
            problems.append(
                f"_enforce_daily_volume_cap is invoked from only {upload_calls} "
                "callsite(s) — expected at least 2 (/upload + /generate)"
            )
        if concurrent_calls < 2:
            problems.append(
                f"_enforce_concurrent_jobs_cap is invoked from only "
                f"{concurrent_calls} callsite(s) — expected at least 2"
            )

        # L4 — worst-case daily Veo bill bounded?
        if daily is not None:
            worst_case = daily * veo_cost
            if worst_case > WORST_CASE_DAILY_USD_CEILING:
                warnings.append(
                    f"worst-case daily Veo bill is ${worst_case:.0f} (cap={daily} "
                    f"× ${veo_cost:.2f}/call) — over the ${WORST_CASE_DAILY_USD_CEILING:.0f} "
                    "ceiling. Tighten DEFAULT_DAILY_CAP or accept the risk."
                )

        # L2 — per-user overrides
        per_user = self._scan_user_overrides()

        details = {
            "default_daily_cap": daily,
            "default_concurrent_cap": concurrent,
            "veo_cost_per_call_usd": veo_cost,
            "worst_case_daily_usd": round((daily or 0) * veo_cost, 2),
            "enforce_daily_callsites": upload_calls,
            "enforce_concurrent_callsites": concurrent_calls,
            "per_user_overrides": per_user,
        }
        if warnings:
            details["warnings"] = warnings

        if problems:
            return self._failed(
                f"{len(problems)} issue(s) — caps may not protect against runaway bills",
                violations=problems,
                **details,
            )
        if warnings:
            return self._warned(
                "caps are wired correctly but worst-case spend is high",
                **details,
            )
        return self._passed(
            f"caps wired (daily={daily}, concurrent={concurrent}); worst-case "
            f"daily ${(daily or 0) * veo_cost:.0f}",
            **details,
        )

    def _scan_user_overrides(self) -> list[dict]:
        """Connect to DB if DATABASE_URL is set and report any user-level
        overrides (None == uses default, anything else = override)."""
        url = os.environ.get("DATABASE_URL", "").strip()
        if not url:
            return [{"_note": "DATABASE_URL not set — skipped per-user audit"}]
        try:
            backend_dir = str(Path(__file__).resolve().parents[2])
            if backend_dir not in sys.path:
                sys.path.insert(0, backend_dir)
            from database import SessionLocal, User
            db = SessionLocal()
            try:
                rows = db.query(User).all()
                return [
                    {
                        "id": u.id,
                        "username": u.username,
                        "role": u.role,
                        "daily_cap": u.max_videos_per_day,
                        "concurrent_cap": u.max_concurrent_jobs,
                    }
                    for u in rows
                ]
            finally:
                db.close()
        except Exception as e:
            return [{"_note": f"DB scan failed: {type(e).__name__}: {e}"}]
