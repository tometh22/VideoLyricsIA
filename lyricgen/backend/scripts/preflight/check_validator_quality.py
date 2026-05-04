"""P0 #5 — Content validator quality (Guideline 15).

Generates N Veo backgrounds across a curated prompt set chosen to stress
both directions of the validator:

  - GREEN prompts (clean cinematic backgrounds Veo handles well: ocean,
    forest, abstract textures). These should pass.
  - YELLOW prompts (urban scenes Veo sometimes pollutes with shop signs or
    licence plates). These probe how strict the validator really is.

For each generated background we run validate_video and bucket the result.
Three things we care about:

  - false-negative rate: GREEN prompt that the validator wrongly flagged.
    Too many means UMG jobs will spuriously enter validation_failed.
  - true-positive rate: YELLOW prompt that the validator correctly caught.
  - cost: each call hits Veo Fast (~$0.80). The runner gates total spend.

Cached prompts hit the R2 cache layer added in pipeline.py, so re-runs are
free after the first pass.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

from ._base import Check, CheckResult
from . import _clients


# Curated prompts. Format: (label, prompt, expected_outcome, category).
# expected_outcome is the validator verdict we *want* (a passing background
# should be marked safe; a known-noisy urban prompt may legitimately fail).
PROMPTS: list[tuple[str, str, str, str]] = [
    (
        "ocean",
        "Cinematic shot of a stormy ocean at dusk, dramatic dark clouds, "
        "powerful waves crashing against rocky cliffs",
        "pass", "GREEN",
    ),
    (
        "forest",
        "Slow drone shot through a misty pine forest at dawn, sunbeams "
        "filtering through tree canopy, dew on leaves",
        "pass", "GREEN",
    ),
    (
        "abstract",
        "Abstract liquid ink swirling in slow motion, deep blues and golds, "
        "macro lens, no figure",
        "pass", "GREEN",
    ),
    (
        "city_at_night",
        "Cinematic night street scene with rain and reflections on wet asphalt, "
        "moody neon ambience, blurred lights",
        "either", "YELLOW",  # Veo may put text on signage; either outcome is informative.
    ),
    (
        "stage_lights",
        "Concert stage lighting effects in slow motion, lasers, smoke, no "
        "performers visible",
        "pass", "GREEN",
    ),
]


def _load_dotenv_once():
    backend = Path(__file__).resolve().parents[2]
    from dotenv import load_dotenv
    load_dotenv(backend / ".env")


class ValidatorQualityCheck(Check):
    name = "validator_quality"
    description = (
        "generate N Veo backgrounds, run validator, report false-positive and "
        "true-positive rates"
    )
    p0 = True

    def __init__(self, n_prompts: int = 5, max_total_usd: float = 5.0):
        self.n_prompts = n_prompts
        self.max_total_usd = max_total_usd

    def run(self) -> CheckResult:
        _load_dotenv_once()

        if not os.environ.get("GOOGLE_APPLICATION_CREDENTIALS"):
            return self._skipped(
                "GOOGLE_APPLICATION_CREDENTIALS not set — cannot generate Veo "
                "backgrounds for validator audit"
            )

        out_root = Path(__file__).resolve().parent / "reports" / "validator_videos"
        out_root.mkdir(parents=True, exist_ok=True)

        prompts = PROMPTS[: self.n_prompts]
        veo_cost = 0.80   # Veo Fast: $0.10/sec × 8s
        max_calls = int(self.max_total_usd / veo_cost)
        if len(prompts) > max_calls:
            prompts = prompts[:max_calls]

        results: list[dict] = []
        cost_estimate = 0.0
        false_neg = 0
        true_pos = 0

        for label, prompt, expected, category in prompts:
            mp4 = out_root / f"{label}.mp4"
            try:
                _clients.generate_veo(prompt, str(mp4))
                cost_estimate += veo_cost
            except Exception as e:
                results.append({
                    "label": label,
                    "category": category,
                    "expected": expected,
                    "outcome": "veo_error",
                    "error": f"{type(e).__name__}: {e}",
                })
                continue

            v = _clients.validate_video(str(mp4))
            actual = "pass" if v["passed"] else "fail"

            entry = {
                "label": label,
                "category": category,
                "prompt": prompt,
                "expected": expected,
                "actual": actual,
                "issues": v.get("issues", []),
                "frames_checked": v.get("frames_checked", 0),
                "file": str(mp4),
            }

            if expected == "pass" and actual == "fail":
                false_neg += 1
                entry["judgement"] = "false_negative"
            elif expected == "either" and actual == "fail":
                true_pos += 1
                entry["judgement"] = "true_positive"
            elif expected == "pass" and actual == "pass":
                entry["judgement"] = "ok"
            else:
                entry["judgement"] = "yellow_passed"

            results.append(entry)

        green = [r for r in results if r.get("category") == "GREEN" and "actual" in r]
        green_pass_rate = (
            sum(1 for r in green if r["actual"] == "pass") / len(green)
            if green else 0.0
        )

        details = {
            "results": results,
            "green_pass_rate": round(green_pass_rate, 2),
            "false_negatives": false_neg,
            "true_positives_yellow": true_pos,
            "estimated_veo_cost_usd": round(cost_estimate, 2),
            "note": (
                "false_negative = clean prompt the validator wrongly flagged. "
                "true_positive = stress prompt the validator correctly caught. "
                "Pass criterion: GREEN pass-rate >= 0.80 AND false_negatives <= 1."
            ),
        }

        if green_pass_rate < 0.80:
            return self._failed(
                f"GREEN pass-rate is {green_pass_rate:.0%} (< 80%) — validator "
                f"is too strict; legitimate UMG jobs would be auto-rejected",
                **details,
            )
        if false_neg > 1:
            return self._failed(
                f"{false_neg} false negatives on GREEN prompts — flagging "
                f"clean cinematic backgrounds",
                **details,
            )
        return self._passed(
            f"validator OK: {green_pass_rate:.0%} GREEN pass, "
            f"{false_neg} FN, {true_pos} TP on YELLOW",
            **details,
        )
