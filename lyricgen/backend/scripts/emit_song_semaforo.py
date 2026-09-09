#!/usr/bin/env python3
"""Song-level traffic light ("semáforo") v1 from persisted, gold-free signals.

The verdict is emitted BEFORE any human review and stored append-only in
``AuditLog`` (``action = semaforo.verdict.v1``) so the confidence protocol can
build the verdict × real-error matrix afterwards. It is deliberately NOT
written into ``Job.transcription_quality``: the editor returns that payload to
reviewers and the protocol wants them blind to the verdict.

Rule v1 (song level; line-level certification is Capa 1 and comes later):

* RED   if live, or LoRA↔base disagreement >= 0.082 (every pilot song at or
        above that score had baseline WER > 10%), or audio coverage < 0.90,
        or more than 10 unsafe windows, or the quality replay is missing /
        failed (in doubt, degrade).
* GREEN if disagreement <= 0.035 (every pilot song at or below that score was
        easy), coverage >= 0.97, zero unsafe windows, not live, and the quality
        gate did not require review.
* YELLOW otherwise (mostly certified, a bounded number of dubious windows).

Live is never green. Missing signals degrade. Thresholds come from
.context/lora-disagreement-router-pilot-20260902.json (AUC 0.971, 41 songs).

Examples::

    python scripts/emit_song_semaforo.py --tenant universal_music --status pending_review
    python scripts/emit_song_semaforo.py --tenant universal_music --commit --output queue.json
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import os
import sys
from typing import Any

BACKEND_ROOT = os.path.join(os.path.dirname(__file__), "..")
sys.path.insert(0, BACKEND_ROOT)

RULE_VERSION = "semaforo-v2"
ACTION = "semaforo.verdict.v2"
SCORE_VERSION = "stage1-confidence-v1"
SCORE_WEIGHTS = {
    "line_consensus": 0.30,
    "audio_coverage": 0.40,
    "reference_available": 0.15,
    "lid_known": 0.15,
}
# Señal de ruteo: segundos de voz cantada (VAD del stem, independiente del ASR)
# que ningún cartel reclama. Reemplaza al desacuerdo LoRA↔base y a la etiqueta
# "vivo": medido sobre el holdout el 2026-09-02, el desacuerdo ordenó al revés
# (la canción con el desacuerdo más alto de las 30, "Eso Es Real", tenía WER
# 0,08) y 2 de los 4 vivos estaban bien. voiced_gap_s fue la única señal
# persistida que ordenó correctamente los cuatro. Los umbrales son los que el
# producto ya usa en transcription_quality (crítico >= 10 s, aviso >= 3 s).
VOICED_GAP_GREEN_MAX = 3.0
VOICED_GAP_RED_MIN = 10.0
COVERAGE_GREEN_MIN = 0.97
COVERAGE_RED_MAX = 0.90
UNSAFE_WINDOWS_RED_MIN = 11


def _num(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        try:
            value = float(value)
        except (TypeError, ValueError):
            return None
    value = float(value)
    return value if value == value else None


def _bounded(value: Any) -> float | None:
    number = _num(value)
    return None if number is None else max(0.0, min(1.0, number))


def _line_consensus_component(
    quality: dict[str, Any], segments: list[dict[str, Any]],
) -> dict[str, Any]:
    """Return a bounded, auditable per-line independent-consensus signal.

    Selected lines can carry the two agreeing source families directly.  The
    quality replay also persists a paired per-window counter in ``lora_shadow``;
    use that counter only when the selected rows contain no explicit consensus
    annotations.  Missing observations score zero instead of being silently
    replaced by the song-level calibration risk.
    """
    lyric_lines = [
        row for row in segments
        if isinstance(row, dict) and str(row.get("text") or "").strip()
    ]
    explicit = 0
    for row in lyric_lines:
        sources = {
            str(value).strip().casefold()
            for value in (row.get("consensus_sources") or [])
            if str(value).strip()
        }
        if len(sources) >= 2:
            explicit += 1
    if explicit:
        denominator = max(1, len(lyric_lines))
        return {
            "value": round(min(1.0, explicit / denominator), 6),
            "agreed_lines": explicit,
            "observed_lines": denominator,
            "source": "selected_line_consensus_sources",
            "available": True,
        }

    retry = quality.get("retry") if isinstance(quality.get("retry"), dict) else {}
    shadow = retry.get("lora_shadow") if isinstance(retry.get("lora_shadow"), dict) else {}
    comparisons = max(0, int(_num(shadow.get("comparisons")) or 0))
    agreed = max(0, int(_num(shadow.get("with_consensus")) or 0))
    if comparisons:
        return {
            "value": round(min(1.0, agreed / comparisons), 6),
            "agreed_lines": min(agreed, comparisons),
            "observed_lines": comparisons,
            "source": "quality_replay_paired_line_consensus",
            "available": True,
        }
    return {
        "value": 0.0,
        "agreed_lines": 0,
        "observed_lines": 0,
        "source": "unavailable",
        "available": False,
    }


def _reference_available(quality: dict[str, Any]) -> bool:
    hypothesis = quality.get("reference_hypothesis")
    if bool(quality.get("reference_hypothesis_unavailable")) or not isinstance(
        hypothesis, dict,
    ):
        return False
    verification = hypothesis.get("verification")
    return bool(
        hypothesis.get("availability") != "unavailable"
        and str(hypothesis.get("reference_text") or "").strip()
        and isinstance(verification, dict)
        and verification.get("complete_audio") is True
    )


def _confidence_score(
    quality: dict[str, Any], metrics: dict[str, Any],
    segments: list[dict[str, Any]],
) -> tuple[float, dict[str, Any]]:
    consensus = _line_consensus_component(quality, segments)
    coverage = _bounded(metrics.get("audio_coverage"))
    reference_available = _reference_available(quality)
    language = str(metrics.get("language") or "unknown").strip().casefold()
    lid_known = language not in {"", "unknown", "none", "auto"}
    components: dict[str, Any] = {
        "line_consensus": {
            **consensus,
            "weight": SCORE_WEIGHTS["line_consensus"],
        },
        "audio_coverage": {
            "value": round(coverage or 0.0, 6),
            "weight": SCORE_WEIGHTS["audio_coverage"],
            "available": coverage is not None,
        },
        "reference_available": {
            "value": 1.0 if reference_available else 0.0,
            "weight": SCORE_WEIGHTS["reference_available"],
            "available": True,
        },
        "lid_known": {
            "value": 1.0 if lid_known else 0.0,
            "weight": SCORE_WEIGHTS["lid_known"],
            "available": True,
            "language": language or "unknown",
        },
    }
    score = 100.0 * sum(
        float(component["value"]) * float(component["weight"])
        for component in components.values()
    )
    return round(score, 3), components


def song_verdict(
    quality: dict | None,
    paired: dict | None = None,
    segments: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Pure rule: quality payload -> persisted blind routing verdict.

    ``paired`` optionally supplies the pilot-scale disagreement (turbo base vs
    turbo+LoRA on identical chunks, see paired_disagreement_offline.py). When
    given it replaces the runtime ``difficulty_router`` score, which compares
    WhisperX against LoRA and is not on the pilot's scale.
    """
    quality = quality if isinstance(quality, dict) else {}
    metrics = quality.get("metrics") if isinstance(quality.get("metrics"), dict) else {}
    confidence_score, score_components = _confidence_score(
        quality, metrics, [row for row in (segments or []) if isinstance(row, dict)],
    )
    router = metrics.get("difficulty_router") if isinstance(metrics.get("difficulty_router"), dict) else {}
    # El desacuerdo se conserva SÓLO como dato informativo: LoRA y el router
    # están congelados y su escala nunca se validó fuera de la muestra.
    disagreement_source = "runtime_difficulty_router"
    disagreement = _num(router.get("score"))
    if isinstance(paired, dict) and _num(paired.get("disagreement")) is not None:
        disagreement = _num(paired.get("disagreement"))
        disagreement_source = str(paired.get("source") or "paired_offline")
    coverage = _num(metrics.get("audio_coverage"))
    voiced_gap_s = _num(metrics.get("voiced_gap_s"))
    is_live = bool(metrics.get("is_live"))
    unsafe = [w for w in (quality.get("unsafe_windows") or []) if isinstance(w, dict)]
    decision = str(quality.get("decision") or "unknown")
    analysis_status = str(quality.get("analysis_status") or "none")
    retry = quality.get("retry") if isinstance(quality.get("retry"), dict) else {}
    windows_resolved = int(_num(retry.get("windows_resolved")) or 0)

    inputs = {
        "voiced_gap_s": voiced_gap_s,
        "disagreement": disagreement, "disagreement_source": disagreement_source,
        "disagreement_role": "informativo_no_decide",
        "audio_coverage": coverage,
        "voiced_coverage": _num(metrics.get("voiced_coverage")),
        "is_live": is_live, "unsafe_windows": len(unsafe),
        "windows_resolved": windows_resolved, "decision": decision,
        "analysis_status": analysis_status,
        "language": metrics.get("language"),
    }
    reasons: list[str] = []
    if bool(quality.get("reference_hypothesis_unavailable")):
        reasons.append("reference_hypothesis_unavailable")
    if is_live:
        reasons.append("live_never_green")
    if voiced_gap_s is None:
        reasons.append("voiced_gap_missing")
    elif voiced_gap_s >= VOICED_GAP_RED_MIN:
        reasons.append("voiced_gap_high")
    if coverage is None:
        reasons.append("coverage_missing")
    elif coverage < COVERAGE_RED_MAX:
        reasons.append("coverage_low")
    if len(unsafe) >= UNSAFE_WINDOWS_RED_MIN:
        reasons.append("too_many_unsafe_windows")
    if decision in {"retry_failed", "unsafe", "fail", "failed", "blocked"}:
        reasons.append(f"decision_{decision}")
    if unsafe and analysis_status != "complete":
        reasons.append("replay_not_complete")

    if reasons:
        color = "red"
    elif (
        voiced_gap_s <= VOICED_GAP_GREEN_MAX
        and coverage >= COVERAGE_GREEN_MIN
        and not unsafe
        and decision in {"pass", "approved", "safe"}
    ):
        color = "green"
    else:
        color = "yellow"
        if unsafe:
            reasons.append(f"unsafe_windows_{len(unsafe)}")
        if voiced_gap_s > VOICED_GAP_GREEN_MAX:
            reasons.append("voiced_gap_partial")
        if coverage < COVERAGE_GREEN_MIN:
            reasons.append("coverage_partial")
        if decision not in {"pass", "approved", "safe"}:
            reasons.append(f"decision_{decision}")

    # Orden de entrega: los de menor hueco cantado primero. Sin señal, al final.
    rank_key = voiced_gap_s if voiced_gap_s is not None else 9_999.0
    return {
        "rule_version": RULE_VERSION, "color": color, "reasons": reasons,
        "score": confidence_score,
        "score_source": "stage1_signal_composite",
        "score_version": SCORE_VERSION,
        "score_components": score_components,
        "risk": (
            round(bounded_risk, 6)
            if (bounded_risk := _bounded(quality.get("risk"))) is not None
            else None
        ),
        "inputs": inputs, "rank_key": rank_key,
    }


def _existing_verdicts(db, job_ids: list[str]) -> dict[str, dict]:
    from database import AuditLog
    wanted = set(job_ids)
    rows = (
        db.query(AuditLog)
        .filter(AuditLog.action == ACTION)
        .order_by(AuditLog.created_at.asc())
        .all()
    )
    return {
        str(row.detail.get("job_id")): dict(row.detail)
        for row in rows
        if isinstance(row.detail, dict) and str(row.detail.get("job_id")) in wanted
    }


def collect(db, *, tenant: str | None, status: str | None, job_ids: list[str] | None):
    from database import Job
    query = db.query(Job)
    if job_ids:
        query = query.filter(Job.job_id.in_(job_ids))
    if tenant:
        query = query.filter(Job.tenant_id == tenant)
    if status:
        query = query.filter(Job.status == status)
    return query.order_by(Job.created_at.asc()).all()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--tenant")
    parser.add_argument("--status", default="pending_review")
    parser.add_argument("--job-id", action="append", dest="job_ids")
    parser.add_argument("--commit", action="store_true", help="persist verdicts in AuditLog (default: dry run)")
    parser.add_argument("--force", action="store_true", help="re-emit even if a verdict for this rule exists")
    parser.add_argument("--output", help="write the ranked queues as JSON")
    parser.add_argument("--editor-base", default=os.environ.get("SEMAFORO_EDITOR_BASE", "https://staging.genly.pro/edit-lyrics/"))
    parser.add_argument("--disagreement-file", help="JSON from paired_disagreement_offline.py (keyed by sha256, with job_id)")
    args = parser.parse_args()
    paired_by_job: dict[str, dict] = {}
    if args.disagreement_file:
        for row in json.load(open(args.disagreement_file)).values():
            if isinstance(row, dict) and row.get("job_id"):
                paired_by_job[str(row["job_id"])] = row
    if not (args.tenant or args.job_ids):
        parser.error("--tenant or --job-id is required")

    from database import AuditLog, SessionLocal
    db = SessionLocal()
    try:
        jobs = collect(db, tenant=args.tenant, status=args.status, job_ids=args.job_ids)
        existing = _existing_verdicts(db, [j.job_id for j in jobs])
        emitted_at = datetime.now(timezone.utc).isoformat()
        rows = []
        for job in jobs:
            verdict = song_verdict(
                job.transcription_quality,
                paired_by_job.get(job.job_id),
                job.segments_json,
            )
            prior = existing.get(job.job_id)
            reused = bool(prior) and not args.force
            record = {
                "job_id": job.job_id, "filename": job.filename,
                "tenant_id": job.tenant_id, "status": job.status,
                **(prior if reused else verdict),
                "emitted_at": prior.get("emitted_at") if reused else emitted_at,
                "reused_existing": reused,
            }
            if args.commit and not reused:
                db.add(AuditLog(user_id=None, action=ACTION, detail={
                    "job_id": job.job_id, "filename": job.filename,
                    "tenant_id": job.tenant_id, "job_status": job.status,
                    **verdict, "emitted_at": emitted_at, "blind_review": True,
                }))
            rows.append(record)
        if args.commit:
            db.commit()
    finally:
        db.close()

    delivery = sorted(rows, key=lambda r: ({"green": 0, "yellow": 1, "red": 2}[r["color"]], r["rank_key"]))
    learning = sorted(rows, key=lambda r: -r["rank_key"])
    counts = {c: sum(1 for r in rows if r["color"] == c) for c in ("green", "yellow", "red")}
    report = {
        "schema": "song-semaforo-queue-v1", "rule_version": RULE_VERSION,
        "generated_at": emitted_at, "committed": bool(args.commit),
        "songs": len(rows), "counts": counts,
        "delivery_order": [
            {"rank": i + 1, "job_id": r["job_id"], "color": r["color"], "filename": r["filename"],
             "disagreement": r["inputs"].get("disagreement"), "reasons": r["reasons"],
             "editor_url": args.editor_base + r["job_id"]}
            for i, r in enumerate(delivery)
        ],
        "learning_order": [r["job_id"] for r in learning],
    }
    if args.output:
        with open(args.output, "w", encoding="utf-8") as handle:
            json.dump(report, handle, ensure_ascii=False, indent=2)
    print(json.dumps({"songs": len(rows), "counts": counts, "committed": bool(args.commit),
                      "reused_existing": sum(1 for r in rows if r["reused_existing"])}, ensure_ascii=False))
    for r in delivery:
        print(f"{r['color']:6} {str(r['inputs'].get('disagreement'))[:6]:>6}  {r['job_id']}  {r['filename'][:48]}  {','.join(r['reasons'])}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
