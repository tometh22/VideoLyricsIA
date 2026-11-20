"""Background findings in Delivery QC: what the operator sees before publishing.

The background validator runs long before publication and, in observe mode,
its findings used to end up only in ``jobs.validation_result`` — a JSON nobody
opens. Amor Suicida (b03b8a513dda) is the example: the validator flagged the
gun frame on 29-sep, the job passed under observe, and UMG had to ask twice
("Sacar el arma en la imagen del video"). This module turns those stored
findings — plus a deterministic letterbox scan of the encoded render itself —
into Delivery QC issues next to the lyric checks.

Contract:
* ``DELIVERY_QC_BACKGROUND_MODE`` = off | observe (default) | enforce.
  observe: findings are REVIEW, non-blocking, with a "Revisado" button.
  enforce: the same findings must be acknowledged by a reviewer before an UMG
  publication (REVIEW + blocking; never an objective FAIL: a classifier
  hallucination must not make a delivery impossible).
* never raises; a scan that cannot run is reported as NOT_RUN, never as PASS.
"""
from __future__ import annotations

import os
import re
from typing import Any, Mapping

MODE_ENV = "DELIVERY_QC_BACKGROUND_MODE"

# category -> (code, operator-facing summary). Order = display priority.
# ``text`` and ``atmospherics`` stay out on purpose: text already has the signed
# UMG_BACKGROUND_TEXT check and is noisy on legitimate street scenes;
# atmospherics is an aesthetic policy, not something UMG has rejected.
CATEGORIES: dict[str, tuple[str, str]] = {
    "weapon": ("BG_WEAPON", "El fondo muestra un arma"),
    "gore": ("BG_GORE", "El fondo muestra sangre o carne"),
    "body_parts": ("BG_BODY_PARTS", "El fondo muestra partes del cuerpo"),
    "people": ("BG_PEOPLE", "El fondo muestra una persona"),
    "anatomy": ("BG_ANATOMY", "El fondo tiene anatomía deformada"),
    "brand": ("BG_BRAND", "El fondo muestra una marca comercial"),
    "overlay": ("BG_OVERLAY", "El fondo tiene texto o gráfica superpuesta"),
    "letterbox": ("BG_LETTERBOX", "El fondo trae bordes negros"),
}
CONTENT_CODES = frozenset(code for cat, (code, _s) in CATEGORIES.items() if cat != "letterbox")
LETTERBOX_CODES = frozenset({"BG_LETTERBOX", "MEDIA_LETTERBOX"})

_PREFIX_RE = re.compile(r"^\s*([a-z_]+)\s*:\s*(.*)$", re.S)


def background_qc_mode() -> str:
    value = os.environ.get(MODE_ENV, "observe").strip().lower()
    return value if value in {"off", "observe", "enforce"} else "observe"


def _entries(rows: Any) -> list[tuple[str, str]]:
    out = []
    for row in rows or []:
        text = row.get("type") if isinstance(row, Mapping) else row
        match = _PREFIX_RE.match(str(text or ""))
        if match and match.group(1) in CATEGORIES:
            out.append((match.group(1), match.group(2).strip()))
    return out


def collect_background_findings(
    validation_result: Mapping[str, Any] | None,
    scene_plan: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Category -> reasons from the persisted verdict of the CURRENT background.

    Reads observations and observed (waived) issues: under observe mode those
    are exactly what passed silently. ``rejected_attempts`` are ignored on
    purpose — those assets were discarded and are not in the render.
    """
    found: dict[str, list[str]] = {}
    extended = False
    validation = validation_result if isinstance(validation_result, Mapping) else {}
    sources = [validation]
    for scene in ((scene_plan or {}).get("scenes") or []) if isinstance(scene_plan, Mapping) else []:
        if isinstance(scene, Mapping) and isinstance(scene.get("validation"), Mapping):
            sources.append(scene["validation"])
    for source in sources:
        if source.get("extended_qc") or "hard_violation" in source:
            extended = True
        for category, reason in (
            _entries(source.get("observations")) + _entries(source.get("observed_issues"))
        ):
            reasons = found.setdefault(category, [])
            if reason and reason not in reasons:
                reasons.append(reason)
    return {"findings": found, "extended_evaluated": extended}


def _issue(code: str, summary: str, reasons: list[str], *, detector: str,
           blocking: bool, confidence: float) -> dict[str, Any]:
    detail = "; ".join(reasons[:3]) or summary
    return {
        "code": code, "severity": "WARN", "result_status": "REVIEW",
        "category": "background", "summary": summary,
        "description": (
            f"{detail}. Revisá el fondo completo; si se confirma, regenerá el "
            "fondo desde el editor antes de publicar."
        ),
        "seconds": [0.0], "detector": detector, "confidence": confidence,
        "auto_fixable": False, "manual_verification_required": False,
        "blocking": blocking,
        "evidence": {"reasons": reasons[:6]},
    }


def build_background_issues(job: Any, video_path: str | None) -> tuple[list[dict], dict]:
    """Return (issues, meta) for Delivery QC; empty when the mode is off."""
    mode = background_qc_mode()
    meta: dict[str, Any] = {"mode": mode, "validator_extended": False, "render_scan": None}
    if mode == "off":
        return [], meta
    blocking = mode == "enforce"
    issues: list[dict[str, Any]] = []
    try:
        collected = collect_background_findings(
            getattr(job, "validation_result", None), getattr(job, "scene_plan", None),
        )
    except Exception:  # noqa: BLE001 — display layer must never break QC
        collected = {"findings": {}, "extended_evaluated": False}
    meta["validator_extended"] = collected["extended_evaluated"]
    for category, (code, summary) in CATEGORIES.items():
        reasons = collected["findings"].get(category)
        if reasons:
            issues.append(_issue(
                code, summary, reasons,
                detector="background_letterbox_scan" if category == "letterbox" else "background_validator",
                blocking=blocking, confidence=1.0 if category == "letterbox" else 0.7,
            ))
    # The encoded render is the ground truth for bars: it also covers
    # operator-uploaded art and jobs validated before this check existed.
    try:
        from background_technical_qc import analyze_background
        scan = analyze_background(video_path) if video_path else {
            "ran": False, "abstained": "no_render", "findings": [], "metrics": {}}
    except Exception as exc:  # noqa: BLE001
        scan = {"ran": False, "abstained": f"error:{type(exc).__name__}", "findings": [], "metrics": {}}
    meta["render_scan"] = {"ran": bool(scan.get("ran")), "abstained": scan.get("abstained")}
    for finding in scan.get("findings") or []:
        if finding.get("category") == "letterbox":
            row = _issue(
                "MEDIA_LETTERBOX", "El video final tiene bordes negros", [finding["reason"]],
                detector="background_letterbox_scan", blocking=blocking, confidence=1.0,
            )
            row["evidence"]["letterbox"] = finding.get("evidence")
            issues.append(row)
    return issues, meta
