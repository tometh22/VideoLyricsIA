"""One operator-facing projection of a UMG correction case.

The projection is read-only. Command endpoints keep their own transactional
checks; this tells every client the same next action and why it is available.
"""

from __future__ import annotations

from typing import Any, Mapping


_BUSY = {"queued", "processing", "rendering", "editing", "transcribed_pending"}


def project_change_request(
    request: Any, *, job: Any | None, delivery: Any | None,
    publication: Mapping[str, Any] | None,
    verification_current: bool, proposal_status: str | None,
    qc_gate: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Return stage, next action, permitted actions, blockers and evidence."""
    published_revision = publication.get("revision") if publication else None
    requested_revision = getattr(request, "requested_revision", None)
    newer_publication = bool(
        requested_revision is not None and published_revision is not None
        and published_revision > requested_revision
    )
    evidence = {
        "requested_revision": requested_revision,
        "published_revision": published_revision,
        "verified_for_current_cut": verification_current,
        "published_newer_than_request": newer_publication,
    }

    def result(phase: str, action: str, label: str,
               actions: list[str], blockers: list[dict[str, str]] | None = None) -> dict:
        return {
            "phase": phase, "next_action": action, "next_action_label": label,
            "allowed_actions": actions, "blockers": blockers or [],
            "evidence": evidence,
        }

    if getattr(request, "resolved_at", None):
        return result("resolved", "view_resolution", "Ver resolución", ["reopen"])
    if delivery is None or job is None:
        return result("blocked", "reconcile_delivery", "Vincular entrega y video", [], [
            {"code": "delivery_or_job_missing", "message": "No se encontró la entrega o su video."},
        ])
    if getattr(delivery, "removed_at", None):
        return result("blocked", "restore_delivery", "Revisar entrega eliminada", [], [
            {"code": "delivery_removed", "message": "La entrega ya no está visible en el portal."},
        ])
    if getattr(job, "status", None) in _BUSY:
        return result("rendering", "wait_render", "Esperar el video actualizado", ["edit"])
    if getattr(job, "status", None) != "done":
        return result("blocked", "review_render", "Revisar el video", ["edit"], [
            {"code": "render_not_ready", "message": "El video final no está listo."},
        ])

    changed = bool(publication and publication.get("needs_publish"))
    if not changed and not newer_publication:
        if proposal_status in {"ready", "partial", "needs_input"}:
            return result("proposal_ready", "review_proposal", "Revisar propuesta", [
                "review_proposal", "edit", "resolve_manual",
            ])
        return result("needs_edit", "edit", "Aplicar los cambios pedidos", [
            "analyze", "edit", "resolve_manual",
        ])

    if (proposal_status in {"applied", "partially_applied"}
            and not publication.get("internal_approval_current")):
        # Applying a text proposal updates the editor revision while the old
        # video can remain in a done Job. The edit render promotes it to
        # pending_review; do not direct the operator to review old bytes.
        return result("changes_saved", "render_changes", "Generar video actualizado", [
            "render_changes", "edit",
        ])
    if not publication.get("internal_approval_current"):
        return result("needs_review", "review_render", "Revisar y aprobar video", ["edit"], [
            {"code": "internal_approval_missing", "message": "Falta aprobar el corte actual."},
        ])
    if qc_gate and qc_gate.get("blocked"):
        return result("needs_review", "review_qc", "Completar controles del video", ["edit"], [
            {"code": str(qc_gate.get("reason") or "delivery_qc_blocked"),
             "message": "Los controles de esta versión requieren revisión."},
        ])

    prores = publication.get("prores_pending") or []
    if prores:
        if publication.get("prores_configured") is False:
            return result("preparing_files", "configure_prores", "Elegir formato profesional", [
                "configure_prores", "edit",
            ])
        return result("preparing_files", "prepare_prores", "Actualizar archivo profesional", [
            "prepare_prores", "edit",
        ])
    if not verification_current:
        return result("needs_verification", "verify", "Comprobar todo el pedido", [
            "verify", "edit", "resolve_manual",
        ])
    return result("ready_to_publish", "publish", "Publicar actualización", [
        "publish", "edit", "resolve_manual",
    ])
