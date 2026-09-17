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
from delivery_ocr import inspect_rendered_text, select_identity_observation
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

# Reports generated before the evidence-based checklist was introduced did
# not persist ``result_status`` or ``manual_verification_required``. Keep
# their stable UMG codes review-only when read by the approval gate, otherwise
# an old generic reminder would be misclassified as an objective failure.
LEGACY_MANUAL_CHECK_CODES = frozenset(
    code for code, _summary, _description in MANDATORY_REVIEW_CHECKS
)

# A finding's severity describes its risk; its result describes what the
# detector actually established.  Keeping both lets the UI distinguish an
# unsigned human check from an objectively failed render.
CHECK_DEFINITIONS = (
    ("media_container", "Archivo de video válido", "ffprobe", {
        "MEDIA_ASSET_MISSING", "MEDIA_PROBE_FAILED", "MEDIA_VIDEO_STREAM_MISSING",
    }),
    ("media_audio", "Pista de audio presente", "ffprobe", {"MEDIA_AUDIO_STREAM_MISSING"}),
    ("media_duration", "Duración consistente", "ffprobe", {
        "MEDIA_DURATION_INVALID", "MEDIA_DURATION_MISMATCH",
    }),
    ("media_delivery_spec", "Perfil técnico de entrega", "ffprobe", {
        "MEDIA_WIDTH_MISMATCH", "MEDIA_HEIGHT_MISMATCH", "MEDIA_CODEC_MISMATCH",
        "MEDIA_PIX_FMT_MISMATCH", "MEDIA_FPS_MISMATCH",
    }),
    ("metadata_title", "Título coincide con metadata", "metadata_vs_render_manifest", {
        "METADATA_TITLE_MISMATCH", "OCR_TITLE_MISMATCH",
    }),
    ("metadata_artist", "Artista coincide con metadata", "metadata_vs_render_manifest", {
        "METADATA_ARTIST_MISMATCH", "OCR_ARTIST_MISMATCH",
    }),
    ("metadata_version", "Versión coincide con metadata", "metadata_vs_render_manifest", {
        "METADATA_VERSION_MISMATCH",
    }),
    ("timeline", "Timeline de letras válida", "timeline_invariants", {
        "INVALID_LYRIC_RANGE", "LYRIC_OUTSIDE_ASSET", "LYRIC_OVERLAP",
    }),
    ("lyrics_quality", "Texto y calidad de transcripción", "transcription_quality_v6", {
        "UPSTREAM_QUALITY_REVIEW", "REFERENCE_TEXT_UNATTESTED",
        "REFERENCE_TIMELINE_INCOMPLETE", "LYRIC_ORTHOGRAPHY_MISMATCH",
        "LYRIC_TOKEN_TYPO", "LYRIC_TERMINAL_PERIOD",
        "LYRIC_REPEAT_INCONSISTENCY", "LYRIC_FRAGMENTATION",
        "LYRIC_END_BEFORE_WORD_END",
    }),
    ("ocr_title", "Texto visible del title card", "final_frame_ocr", {"OCR_TITLE_MISMATCH"}),
    ("ocr_lyrics", "Texto visible de las letras", "final_frame_ocr", {"OCR_LYRIC_MISMATCH"}),
)


def mandatory_reviewer_issues() -> list[dict[str, Any]]:
    """Return unsigned checks as review requirements, not false failures.

    ``severity=FAIL`` is retained for compatibility with existing persisted
    reports and analytics.  ``result_status=REVIEW`` is the authoritative
    meaning: the video has not failed this check; a reviewer still has to sign
    it before a batch delivery is considered fully reviewed.
    """
    return [{
        "code": code,
        "severity": "FAIL",
        "result_status": "REVIEW",
        "category": "umg_manual_checklist",
        "summary": summary,
        "description": description,
        "seconds": [0.0],
        "detector": "mandatory_signed_reviewer_checklist",
        "confidence": 1.0,
        "auto_fixable": False,
        "manual_verification_required": True,
        # The reviewer can still sign this reminder, but an unsigned generic
        # checklist is not evidence that the video failed.
        "blocking": False,
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
    if row.get("result_status") not in {"PASS", "FAIL", "REVIEW", "NOT_RUN"}:
        row["result_status"] = "REVIEW" if row.get("manual_verification_required") or row.get("severity") != "FAIL" else "FAIL"
    row.setdefault("blocking", bool(row.get("result_status") == "FAIL" or row.get("manual_verification_required")))
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


def _issue_result_status(issue: Mapping[str, Any]) -> str:
    value = str(issue.get("result_status") or "").upper()
    if value in {"PASS", "FAIL", "REVIEW", "NOT_RUN"}:
        return value
    if str(issue.get("code") or "") in LEGACY_MANUAL_CHECK_CODES:
        return "REVIEW"
    return "REVIEW" if issue.get("manual_verification_required") or issue.get("severity") != "FAIL" else "FAIL"


def _check_status(rows: Sequence[Mapping[str, Any]]) -> str:
    """Collapse findings for one detector into one honest check result."""
    if any(row.get("status") == "OPEN" and _issue_result_status(row) == "FAIL" for row in rows):
        return "FAIL"
    if any(row.get("status") == "OPEN" and _issue_result_status(row) == "REVIEW" for row in rows):
        return "REVIEW"
    return "PASS"


def _check_evidence(rows: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Combine detector evidence without treating a list as dict pairs."""
    evidence = []
    for row in rows:
        value = row.get("evidence")
        if isinstance(value, Mapping):
            evidence.append(deepcopy(dict(value)))
        elif isinstance(value, list):
            evidence.extend(deepcopy(dict(item)) for item in value if isinstance(item, Mapping))
    return evidence


def _build_check_results(
    *,
    issues: Sequence[Mapping[str, Any]],
    media: Mapping[str, Any],
    ocr: Mapping[str, Any],
    quality: Mapping[str, Any],
    spec: Mapping[str, Any],
    rendered: Mapping[str, Any],
) -> list[dict[str, Any]]:
    """Build the operator-facing checklist from actual detector evidence.

    A check is PASS only when its detector ran and found no issue.  Disabled,
    unavailable, or uncalibrated detectors are explicitly NOT_RUN so their
    absence can never look like a clean result.
    """
    rows = list(issues)
    results: list[dict[str, Any]] = []
    media_probe = media.get("probe") or {}
    ocr_observations = list(ocr.get("observations") or [])
    ocr_abstentions = list(ocr.get("abstentions") or [])
    quality_verdict = str(quality.get("decision") or quality.get("verdict") or "").strip().lower()

    for check_id, label, detector, codes in CHECK_DEFINITIONS:
        matched = [row for row in rows if str(row.get("code")) in codes]
        status = _check_status(matched)
        reason = ""
        if check_id == "media_delivery_spec" and not spec:
            status, reason = "NOT_RUN", "No se definió un perfil técnico para comparar."
        elif check_id == "metadata_title" and not matched and not rendered.get("rendered_title"):
            status, reason = "NOT_RUN", "No hubo texto de title card para comparar."
        elif check_id == "metadata_artist" and not matched and not rendered.get("rendered_artist"):
            status, reason = "NOT_RUN", "No hubo texto de artista para comparar."
        elif check_id == "metadata_version" and not matched and not rendered.get("rendered_version"):
            status, reason = "NOT_RUN", "El render no informa una versión visible para comparar."
        elif check_id in {"ocr_title", "ocr_lyrics"}:
            kinds = {"title"} if check_id == "ocr_title" else {"lyric"}
            has_kind = any(str(row.get("kind")) in kinds for row in ocr_observations)
            if not has_kind and not matched:
                status = "NOT_RUN"
                reason = (ocr_abstentions[0].get("reason") if ocr_abstentions else "Sin observación OCR aplicable")
        elif check_id == "lyrics_quality" and not quality_verdict and not matched:
            # Deterministic timeline/text checks still ran, but the upstream
            # quality verdict itself was not supplied by the transcription
            # engine.
            status, reason = "NOT_RUN", "El motor de calidad no entregó un veredicto."
        elif check_id in {"media_container", "media_audio", "media_duration"} and not media_probe and not matched:
            status, reason = "NOT_RUN", "No hubo un probe técnico disponible."
        results.append({
            "check_id": check_id,
            "label": label,
            "status": status,
            "detector": detector,
            "blocking": status == "FAIL" or any(
                row.get("manual_verification_required") and row.get("status") == "OPEN"
                for row in matched
            ),
            "issue_ids": [str(row.get("issue_id")) for row in matched if row.get("issue_id")],
            "evidence": _check_evidence(matched),
            "reason": reason,
        })

    for code, summary, description in MANDATORY_REVIEW_CHECKS:
        # The title metadata check is already represented by the evidence-based
        # ``metadata_title`` result above. Keeping the generic UMG reminder in
        # this checklist produced two rows with the same label and confused
        # reviewers about which one was authoritative.
        if code == "UMG_TITLE_METADATA":
            continue
        matched = [row for row in rows if row.get("code") == code]
        # These are always present for batch reports.  Their REVIEW status is
        # intentional: they are awaiting an operator's visual attestation,
        # while the gate remains reserved for objective failures.
        results.append({
            "check_id": code.lower(), "label": summary,
            "status": "REVIEW" if any(row.get("status") == "OPEN" for row in matched) else "PASS",
            "detector": "mandatory_signed_reviewer_checklist", "blocking": False,
            "issue_ids": [str(row.get("issue_id")) for row in matched if row.get("issue_id")],
            "evidence": [], "reason": description if matched else "No se ejecutó este check.",
        })
    return results


def refresh_check_results(report: Mapping[str, Any]) -> dict[str, Any]:
    """Refresh check badges after a reviewer resolves one finding."""
    row = dict(report)
    checks = [dict(item) for item in (row.get("checks") or []) if isinstance(item, Mapping)]
    if not checks:
        return row
    issues = {
        str(item.get("issue_id")): item
        for item in (row.get("issues") or []) if isinstance(item, Mapping)
    }
    for check in checks:
        matched = [issues[issue_id] for issue_id in check.get("issue_ids") or [] if issue_id in issues]
        if not matched:
            continue
        check["status"] = _check_status(matched)
        check["blocking"] = check["status"] == "FAIL"
    row["checks"] = checks
    check_summary = {
        "total": len(checks),
        **{
            status.lower(): sum(item.get("status") == status for item in checks)
            for status in ("PASS", "FAIL", "REVIEW", "NOT_RUN")
        },
    }
    row["check_summary"] = check_summary
    blocking_checks = [item for item in checks if item.get("blocking") and item.get("status") in {"FAIL", "REVIEW"}]
    row["decision"] = "BLOCK" if blocking_checks else "REVIEW" if check_summary["review"] or check_summary["not_run"] else "PASS"
    return row


def approval_gate(report: Mapping[str, Any] | None, mode: str | None = None) -> dict[str, Any]:
    actual_mode = mode or effective_delivery_qc_mode()
    if actual_mode != "enforce":
        return {"blocked": False, "can_approve": True, "reason": "observe_only"}
    if not report or report.get("status") in {"STALE", "RUNNING", "FAILED"}:
        return {"blocked": True, "can_approve": False, "reason": "fresh_preflight_required"}
    open_rows = [row for row in report.get("issues") or [] if row.get("status") == "OPEN"]
    open_fail = [row for row in open_rows if _issue_result_status(row) == "FAIL" and row.get("blocking", True)]
    if open_fail:
        return {
            "blocked": True, "can_approve": False,
            "reason": "open_fail", "issue_ids": [row["issue_id"] for row in open_fail],
        }
    open_review = [row for row in open_rows if _issue_result_status(row) == "REVIEW"]
    if open_review:
        return {
            "blocked": False, "can_approve": True,
            "reason": "review_recommended",
            "issue_ids": [row["issue_id"] for row in open_review],
        }
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
    ocr_observations = ocr.get("observations") or []
    title_ocr = select_identity_observation(
        ocr_observations, metadata={"title": job.song_title, "artist": job.artist}, field="title",
    )
    artist_ocr = select_identity_observation(
        ocr_observations, metadata={"title": job.song_title, "artist": job.artist}, field="artist",
    )
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
    checks = _build_check_results(
        issues=issues, media=media, ocr=ocr, quality=quality, spec=spec,
        rendered={"rendered_title": (title_ocr or {}).get("text"),
                  "rendered_artist": (artist_ocr or {}).get("text")},
    )
    check_summary = {
        "total": len(checks),
        **{
            status.lower(): sum(row.get("status") == status for row in checks)
            for status in ("PASS", "FAIL", "REVIEW", "NOT_RUN")
        },
    }
    open_rows = [row for row in issues if row.get("status") == "OPEN"]
    summary = {
        "issue_count": len(issues), "open_count": len(open_rows),
        # ``severity`` describes risk; ``result_status`` describes what the
        # detector actually established.  Manual reminders retain FAIL
        # severity for old analytics but must not inflate real-failure counts.
        "fail_count": sum(_issue_result_status(row) == "FAIL" for row in open_rows),
        "warn_count": sum(_issue_result_status(row) == "REVIEW" for row in open_rows),
        "segment_count": len(segments),
    }
    blocking_checks = [row for row in checks if row.get("blocking") and row.get("status") in {"FAIL", "REVIEW"}]
    report: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION, "mode": mode, "status": "COMPLETE",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "segments_revision": int(job.segments_revision or 0),
        "segments_hash": current_hash,
        "render_identity": {"path_basename": os.path.basename(video_path), "edit_count": int(job.edit_count or 0)},
        "decision": "BLOCK" if blocking_checks else "REVIEW" if check_summary["review"] or check_summary["not_run"] else "PASS",
        "summary": summary, "check_summary": check_summary,
        "checks": checks, "issues": issues,
        # Missing automation is explicit in each check as NOT_RUN. It can no
        # longer masquerade as a failed video or silently become a pass.
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
