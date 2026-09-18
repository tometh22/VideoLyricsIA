"""Evidence-based states for the review -> render -> publish workflow."""
from datetime import datetime, timezone


def timestamp(value):
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value.replace('Z', '+00:00'))
        except ValueError:
            return None
    if not isinstance(value, datetime):
        return None
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


def latest_overwrite(job):
    dates = [timestamp(row.get('archived_at')) for row in (job.previous_versions or [])
             if isinstance(row, dict)]
    return max((value for value in dates if value), default=None)


def render_state(job, document, request):
    revision = int(document.revision if document is not None else (job.segments_revision or 0))
    params = job.render_params or {}
    finished = job.status in {'done', 'pending_review'}
    recorded_revision = params.get('_rendered_segments_revision')
    rendered_at = timestamp(params.get('_rendered_at'))
    requested_at = timestamp(request.submitted_at)
    if recorded_revision is not None:
        rendered = bool(finished and recorded_revision == revision and rendered_at
                        and (not requested_at or rendered_at >= requested_at))
    else:
        # Legacy rows have no render revision. An archived overwrite AFTER
        # the last durable edit and request is evidence; status alone isn't.
        rendered_at = latest_overwrite(job)
        saved_at = timestamp(document.updated_at) if document is not None else None
        rendered = bool(finished and rendered_at and saved_at
                        and rendered_at >= saved_at
                        and (not requested_at or rendered_at >= requested_at))
    return {
        'editor_revision': revision,
        'render_matches_editor': rendered,
        'rendered_at': rendered_at.isoformat() if rendered_at else None,
        'can_render': job.status in {'done', 'pending_review', 'rejected'},
    }


def case_state(*, publication, proposal_status=None, resolved_at=None,
               resolution_source=None, proposal_enabled=True, available=True,
               pending_manual=0):
    """One conservative operator projection. Commands still revalidate authority.

    Published state describes persisted delivery metadata, NOT an independent
    consumer verification. Missing data never advances a completed step.
    """
    def state(key, step, label, detail, tone, actions):
        if pending_manual and not resolved_at:
            detail += f' Hay {pending_manual} instrucciones sin comprobación automática: debés revisarlas manualmente en el corte final.'
        return dict(key=key, activeStep=step, label=label, detail=detail,
                    tone=tone, allowed_actions=actions, pending_manual=pending_manual,
                    schema_version='correction-workflow-v1')
    if resolved_at:
        if resolution_source == 'publication':
            return state('resolved', 5, 'Pedido resuelto al publicar',
                         'El registro vincula este cierre a una versión publicada. La aprobación del cliente es independiente.',
                         'done', ['reopen'])
        return state('resolved', 0, 'Pedido cerrado sin publicación',
                     'Cierre manual: no confirma que se haya generado o publicado otro video.',
                     'idle', ['reopen'])
    if not available or not publication:
        return state('unknown', 0, 'Estado por verificar',
                     'No hay evidencia suficiente del trabajo y la entrega. Actualizá el estado.',
                     'attention', ['refresh'])
    p = publication
    status = p.get('job_status')
    if status in {'queued', 'processing', 'rendering', 'editing', 'transcribed_pending'}:
        return state('rendering', 2, 'En cola' if status == 'queued' else 'Generando corte nuevo',
                     'El trabajo continúa en segundo plano. Esto todavía no publica en el portal.',
                     'busy', ['refresh'])
    if status == 'error':
        return state('blocked', 2, 'La generación necesita atención',
                     'Abrí el trabajo para revisar el error y su recuperación. No vuelvas a aplicar los cambios guardados.',
                     'attention', ['edit', 'refresh'])
    base = ['edit', 'resolve'] + (['analyze'] if proposal_enabled else [])
    # A proposal is advisory, not a mandatory apply receipt: corrections may
    # have been made in the editor or campaign history. A current, identified
    # cut can be reviewed for publication without reapplying an old proposal.
    # This does not attest any proposal operation or clear manual warnings.
    if (proposal_status in {'ready', 'partial', 'needs_input', 'stale', 'dismissed'}
            and status in {'done', 'pending_review'}
            and p.get('render_matches_editor') is True
            and p.get('needs_publish') is True
            and isinstance(p.get('render_fingerprint'), str)
            and p['render_fingerprint'].strip()
            and type(p.get('editor_revision')) is int
            and p['editor_revision'] >= 0):
        action = 'prepare_master' if p.get('prores_pending') else 'publish'
        return state('publish', 3,
                     'Preparar archivo profesional' if action == 'prepare_master'
                     else 'Revisar video y publicar actualización',
                     'El corte corresponde a la letra guardada. La propuesta sigue pendiente: revisá el pedido completo en el video antes de publicar; no hace falta volver a aplicarla si lo corregiste a mano.',
                     'attention', base + ['review_proposal', action])
    if proposal_status in {'ready', 'partial', 'needs_input', 'stale', 'dismissed'}:
        key = 'analyze' if proposal_status in {'stale', 'dismissed'} else 'apply'
        return state(key, 0 if key == 'analyze' else 1,
                     'Hay que recalcular' if key == 'analyze' else 'Revisar propuesta y partes pendientes',
                     'La existencia de un render o master no confirma que estas instrucciones estén atendidas.',
                     'attention', base + ['review_proposal'] + (['review_render'] if p.get('can_render') else []))
    if p.get('render_matches_editor') is False and p.get('can_render'):
        return state('render', 2, 'Revisar cambios guardados y generar video',
                     'Confirmá la letra guardada. El video todavía no contiene esta revisión.',
                     'action', base + ['review_render'])
    if p.get('render_matches_editor') is not True:
        return state('unknown', 0, 'Correspondencia del render por verificar',
                     'No se comprobó que el corte use la revisión guardada.',
                     'attention', base + ['refresh'])
    if p.get('prores_pending'):
        return state('publish', 3, 'Preparar archivo profesional',
                     'El master debe corresponder al corte revisado antes de publicar.',
                     'attention', base + ['prepare_master'])
    if p.get('needs_publish') is True:
        return state('publish', 3, 'Revisar video y publicar actualización',
                     'Publicar confirma este corte y este pedido. El cliente conserva su aprobación independiente.',
                     'attention', base + ['publish'])
    if p.get('needs_publish') is False and p.get('revision'):
        return state('review', 3, 'Publicación registrada · revisar pedido',
                     'La entrega coincide según el registro. Verificá lo solicitado antes de cerrar con un motivo.',
                     'action', base)
    return state('unknown', 0, 'Publicación por verificar',
                 'Actualizá el estado antes de confirmar la entrega.', 'attention', base + ['refresh'])
