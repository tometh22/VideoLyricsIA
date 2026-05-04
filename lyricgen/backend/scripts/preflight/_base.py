"""Shared scaffolding for preflight checks.

Every check is a small class that:
  - Declares a stable `name` (used in CLI flags and report headers).
  - Implements `run()`, returning a CheckResult.
  - May raise — the runner catches and marks the check as `error`.
"""

from __future__ import annotations

import time
import traceback
from dataclasses import dataclass, field, asdict
from enum import Enum
from typing import Any


class Status(str, Enum):
    PASS = "pass"
    FAIL = "fail"
    WARN = "warn"
    ERROR = "error"     # uncaught exception in the check itself
    SKIPPED = "skipped"


@dataclass
class CheckResult:
    name: str
    status: Status
    summary: str
    details: dict[str, Any] = field(default_factory=dict)
    duration_ms: int = 0

    def to_dict(self) -> dict:
        d = asdict(self)
        d["status"] = self.status.value
        return d


class Check:
    """Subclass and override `name`, `description`, and `run()`."""

    name: str = "unnamed"
    description: str = ""
    p0: bool = False  # if True, a fail blocks go-live (used for top-line GO/NO-GO)

    def run(self) -> CheckResult:
        raise NotImplementedError

    # Helpers subclasses can call.
    def _passed(self, summary: str, **details) -> CheckResult:
        return CheckResult(self.name, Status.PASS, summary, details)

    def _failed(self, summary: str, **details) -> CheckResult:
        return CheckResult(self.name, Status.FAIL, summary, details)

    def _warned(self, summary: str, **details) -> CheckResult:
        return CheckResult(self.name, Status.WARN, summary, details)

    def _skipped(self, summary: str, **details) -> CheckResult:
        return CheckResult(self.name, Status.SKIPPED, summary, details)


def execute(check: Check) -> CheckResult:
    """Run a check, catching anything thrown so the suite never aborts mid-run."""
    t0 = time.time()
    try:
        result = check.run()
    except NotImplementedError:
        raise
    except Exception as e:
        result = CheckResult(
            check.name,
            Status.ERROR,
            f"uncaught exception: {type(e).__name__}: {e}",
            {"traceback": traceback.format_exc()},
        )
    result.duration_ms = int((time.time() - t0) * 1000)
    return result
