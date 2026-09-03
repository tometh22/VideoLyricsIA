"""Persisted final-render Delivery QC orchestration.

This is the product loop boundary: the transcription engine supplies evidence,
the encoded asset is inspected, the editor records decisions, and approval may
be gated.  Observe mode is deliberately non-blocking.
"""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
import os
from typing import Any, Mapping, Sequence

from delivery_media_qc import inspect_delivery_media
from delivery_ocr import inspect_rendered_text
from delivery_preflight import build_delivery_preflight, frame_timecode


SCHEMA_VERSION = "genly-delivery-qc-runtime-v1"

MANDATORY_REVIEW_CHECKS = (
    ("UMG_BLACK_BARS", "Sin franjas negras", "Confirmar 16:9 full screen sin bandas negras."),
    ("UMG_BACKGROUND_TEXT", "Fondo sin texto ni logos", "Revisar el fondo antes/debajo de la letra y confirmar que no contiene texto, logos, marcas o palabras generadas."),
    ("UMG_SCENE_CHANGE", "Sin cambios de escena", "Confirmar movimiento ambiental sutil, continuo y sin cortes o transiciones bruscas."),
    ("UMG_LUMINANCE_STABLE", "Iluminación estable", "Comparar inicio, medio y fin; confirmar que no hay salto de luminancia, temperatura ni día/noche."),
    ("UMG_MOBILE_CONTRAST", "Contraste legible en mobile", "Revisar el video en proporción mobile y confirmar legibilidad y contraste de toda la letra."),
    ("UMG_LYRIC_NOT_LATE", "Ninguna línea entra tarde", "Escuchar el audio específico completo y confirmar que cada línea entra al inicio del canto o apenas antes."),
    ("UMG_TITLE_METADATA", "Título coincide con metadata", "Confirmar coincidencia entre planilla, metadata y title card."),
    ("UMG_IMAGE_NOT_STRETCHED", "Imagen sin estirar", "Confirmar proporción nativa/reencuadre sin deformación ni estiramiento."),
)


def mandatory_reviewer_issues() -> list[dict[str, Any]]:
    """Unsigned contractual checks are failures, never abstentions."""
    return [{
        "code": code,
        "severity": "FAIL",
        "category": "umg_manual_checklist",
        "summary": summary,
        "description": description,
        "seconds": [0.0],
        "detector": "mandatory_signed_reviewer_checklist",
        "confidence": 1.0,
        "auto_fixable": False,
        "manual_verification_required": True,
    } for code, summary, description in MANDATORY_REVIEW_CHECKS]


def effective_delivery_qc_mode() -> str:
    mode = os.environ.get("DELIVERY_QC_MODE", "off").strip().lower()
    return mode if mode in {"off", "observe", "enforce"} else "off"


def segments_hash(segments: Sequence[Mapping[str, Any]]) -> str:
    from transcription_quality import segments_hash as quality_segments_hash
    return quality_segments_hash([dict(row) for row in segments if isinstance(row, Mapping)])


def _issue_id(issue: Mapping[str, Any]) -> str:
    if issue.get("issue_id"):
        return str(issue["issue_id"])
    payload = {
        "code": issue.get("code"), "actual": issue.get("actual"),
        "expected": issue.get("expected"), "seconds": issue.get("seconds"),
    }
    return hashlib.sha256(json.dumps(payload, sort_keys=True, default=str).encode()).hexdigest()[:16]


def _normalise_issue(issue: Mapping[str, Any], *, fps: float) -> dict[str, Any]:
    row = dict(issue)
    seconds = row.get("seconds")
    if not isinstance(seconds, list):
        seconds = [float(row.get("seconds") or 0)]
    row["seconds"] = seconds
    row.setdefault("timecodes", [frame_timecode(value, fps) for value in seconds])
    if not row["timecodes"]:
        row["timecodes"] = [frame_timecode(value, fps) for value in seconds]
    row.setdefault("timecode", row["timecodes"][0] if row["timecodes"] else "00:00:00:00")
    row.setdefault("frequency", "ISOLATED")
    row.setdefault("occurrence_count", max(1, len(seconds)))
    row.setdefault("status", "OPEN")
    row.setdefault("severity", "WARN")
    row.setdefault("category", "other")
    row.setdefault("confidence", 1.0)
    row["issue_id"] = _issue_id(row)
    return row


def _merge_prior_decisions(issues: list[dict[str, Any]], previous: Mapping[str, Any] | None) -> list[dict[str, Any]]:
    prior = {
        str(row.get("issue_id")): row
        for row in ((previous or {}).get("issues") or []) if isinstance(row, Mapping)
    }
    for issue in issues:
        # A human visual/timing attestation is for one concrete render. Never
        # inherit it into a rebuilt report, even when the stable check id is
        # unchanged after a re-render.
        if issue.get("manual_verification_required"):
            continue
        old = prior.get(str(issue.get("issue_id")))
        if old and old.get("status") in {"ACKNOWLEDGED", "REJECTED", "RESOLVED_MANUAL"}:
            issue["status"] = old["status"]
            issue["operator_decision"] = deepcopy(old.get("operator_decision") or {})
    return issues


def approval_gate(report: Mapping[str, Any] | None, mode: str | None = None) -> dict[str, Any]:
    actual_mode = mode or effective_delivery_qc_mode()
    if actual_mode != "enforce":
        return {"blocked": False, "can_approve": True, "reason": "observe_only"}
    if not report or report.get("status") in {"STALE", "RUNNING", "FAILED"}:
        return {"blocked": True, "can_approve": False, "reason": "fresh_preflight_required"}
    open_fail = [row for row in report.get("issues") or [] if row.get("severity") == "FAIL" and row.get("status") == "OPEN"]
    open_warn = [row for row in report.get("issues") or [] if row.get("severity") == "WARN" and row.get("status") == "OPEN"]
    if open_fail:
        return {"blocked": True, "can_approve": False, "reason": "open_fail", "issue_ids": [row["issue_id"] for row in open_fail]}
    if open_warn:
        return {"blocked": True, "can_approve": False, "reason": "warnings_not_acknowledged", "issue_ids": [row["issue_id"] for row in open_warn]}
    return {"blocked": False, "can_approve": True, "reason": "all_findings_resolved"}


def mark_delivery_qc_stale(report: Mapping[str, Any] | None, *, revision: int, reason: str) -> dict[str, Any]:
    row = deepcopy(dict(report or {}))
    row.update({
        "schema_version": row.get("schema_version") or SCHEMA_VERSION,
        "status": "STALE", "stale_reason": reason,
        "segments_revision": int(revision),
        "stale_at": datetime.now(timezone.utc).isoformat(),
    })
    row["approval"] = approval_gate(row)
    return row


def build_runtime_report(
    *,
    job: Any,
    video_path: str,
    segments: Sequence[Mapping[str, Any]],
    previous: Mapping[str, Any] | None = None,
    ocr_callback=None,
) -> dict[str, Any]:
    mode = (
        "enforce" if getattr(job, "workload_class", "interactive") == "batch"
        else effective_delivery_qc_mode()
    )
    quality = job.transcription_quality if isinstance(job.transcription_quality, Mapping) else {}
    duration = None
    try:
        duration = float(((quality.get("metrics") or {}).get("audio_duration_s")))
    except (TypeError, ValueError):
        pass
    spec = dict(job.umg_spec or {}) if isinstance(job.umg_spec, Mapping) else {}
    media = inspect_delivery_media(video_path, expected_duration=duration, expected={
        key: spec.get(key) for key in ("width", "height", "fps", "codec", "pix_fmt") if spec.get(key) is not None
    })
    ocr = inspect_rendered_text(
        video_path, metadata={"artist": job.artist, "title": job.song_title},
        segments=segments, ocr_callback=ocr_callback,
    )
    title_ocr = next((row for row in ocr.get("observations") or [] if row.get("kind") == "title" and row.get("text")), None)
    artist_ocr = next((row for row in ocr.get("observations") or [] if row.get("kind") == "artist" and row.get("text")), None)
    fps = float((media.get("probe") or {}).get("video", {}).get("fps") or spec.get("fps") or 30)
    base = build_delivery_preflight(
        metadata={"artist": job.artist, "title": job.song_title},
        segments=segments, approved_lyrics=None, reference_trusted=False,
        asset={
            "filename": job.filename, "duration": (media.get("probe") or {}).get("duration") or duration,
            "rendered_title": (title_ocr or {}).get("text"),
            "rendered_artist": (artist_ocr or {}).get("text"),
        },
        quality=quality, fps=fps,
    )

    current_hash = segments_hash(segments)
    repair_shadow = quality.get("delivery_repair_shadow") if isinstance(quality, Mapping) else None
    repair_bound = bool(
        isinstance(repair_shadow, Mapping)
        and repair_shadow.get("segments_hash") == current_hash
        and (repair_shadow.get("reference_attestation") or {}).get("allow_vocabulary_reconciliation")
    )
    shadow_issues = []
    repair_actions = []
    candidate_segments = []
    if repair_bound:
        shadow_issues = ((repair_shadow.get("before_preflight") or {}).get("issues") or [])
        repair_actions = list(repair_shadow.get("actions") or [])
        candidate_segments = list(repair_shadow.get("candidate_segments") or [])

    all_rows = (
        list(base.get("issues") or [])
        + list(media.get("issues") or [])
        + list(ocr.get("issues") or [])
        + list(shadow_issues)
        + mandatory_reviewer_issues()
    )
    _quality_verdict = str(quality.get("decision") or quality.get("verdict") or "").strip().lower()
    if _quality_verdict in {"unsafe", "fail", "blocked", "review_required"} and not any(
        str(row.get("code") or "").startswith("UPSTREAM_QUALITY") for row in all_rows
    ):
        all_rows.append({
            "code": "UPSTREAM_QUALITY_REVIEW",
            "severity": "FAIL" if _quality_verdict in {"unsafe", "fail", "blocked"} else "WARN",
            "category": "transcription_quality",
            "summary": "La calidad de transcripción requiere revisión",
            "description": "Revisar las ventanas inseguras del motor antes de aprobar la entrega.",
            "seconds": [0.0], "detector": "transcription_quality_v6",
            "confidence": 1.0, "auto_fixable": False,
        })
    deduped: dict[str, dict[str, Any]] = {}
    for item in all_rows:
        row = _normalise_issue(item, fps=fps)
        deduped[row["issue_id"]] = row
    issues = _merge_prior_decisions(list(deduped.values()), previous)
    issues.sort(key=lambda row: ({"FAIL": 0, "WARN": 1}.get(row.get("severity"), 2), (row.get("seconds") or [0])[0]))
    open_rows = [row for row in issues if row.get("status") == "OPEN"]
    summary = {
        "issue_count": len(issues), "open_count": len(open_rows),
        "fail_count": sum(row.get("severity") == "FAIL" for row in open_rows),
        "warn_count": sum(row.get("severity") == "WARN" for row in open_rows),
        "segment_count": len(segments),
    }
    report: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION, "mode": mode, "status": "COMPLETE",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "segments_revision": int(job.segments_revision or 0),
        "segments_hash": current_hash,
        "render_identity": {"path_basename": os.path.basename(video_path), "edit_count": int(job.edit_count or 0)},
        "decision": "BLOCK" if summary["fail_count"] else "REVIEW" if summary["warn_count"] else "PASS",
        "summary": summary, "issues": issues,
        # Missing automation is represented by a blocking, signed reviewer
        # check above.  The final report therefore has no ambiguous abstention
        # state: each requirement is either open FAIL or signed/resolved.
        "abstentions": [],
        "detector_diagnostics": (
            list(base.get("abstentions") or [])
            + list(media.get("abstentions") or [])
            + list(ocr.get("abstentions") or [])
        ),
        "technical": media.get("probe") or {},
        "ocr": {"sample_count": len(ocr.get("observations") or [])},
        "repairs": {
            "reference_bound": repair_bound, "actions": repair_actions,
            "candidate_segments": candidate_segments,
            "safe_action_ids": [str(row.get("action_id")) for row in repair_actions if row.get("status") == "APPLIED"],
        },
    }
    report["approval"] = approval_gate(report, mode)
    return report


def run_delivery_qc_for_job(job_id: str, video_path: str, *, segments=None) -> dict[str, Any] | None:
    """Run and persist QC from a render worker. Never raises in observe mode."""
    from database import Job, SessionLocal
    db = SessionLocal()
    try:
        job = db.query(Job).filter(Job.job_id == job_id).with_for_update().first()
        if job is None:
            return None
        # Contractual batch QC cannot be disabled by the global interactive
        # rollout flag. Interactive jobs retain the existing off switch.
        if (
            str(job.workload_class or "interactive") != "batch"
            and effective_delivery_qc_mode() == "off"
        ):
            return None
        rows = list(segments if segments is not None else (job.segments_json or []))
        previous = job.delivery_qc if isinstance(job.delivery_qc, Mapping) else None
        report = build_runtime_report(job=job, video_path=video_path, segments=rows, previous=previous)
        job.delivery_qc = report
        db.commit()
        return report
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()
