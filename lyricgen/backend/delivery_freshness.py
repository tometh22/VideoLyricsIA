"""Does the portal serve the cut the client actually approved?

The deliverables portal never stores a file or a frozen URL. It rebuilds
the R2 key from (tenant, job_id, file_type) and signs it on demand, and
the render pipeline writes every re-render to that same key. So a
correction reaches the client with no new link — which is the behaviour
we want — but also with no trace: same row, same date, same green
"aprobado por UMG" pill over content they never saw.

Two things went wrong because of that, both observed in production:

1. **The publish gate could not tell fresh from stale.** It asked R2
   whether `umg_master.mov` existed. After an edit the PRE-EDIT master is
   still sitting at that key (the re-transcode runs asynchronously and
   overwrites it minutes later), so the gate said yes and the portal
   handed UMG the old broadcast master next to the new MP4. Incident
   2026-08-03; `pipeline.run_edit_pipeline` invalidates the derivative by
   dropping `s3_keys["umg_master"]`, which makes the job row — not R2 —
   the freshness oracle. `prores_pending()` below reads that oracle.

2. **Nothing distinguished a new version from a re-send.** Re-publishing
   refreshed the label and the date and left the approval and the open
   change requests untouched. `render_fingerprint()` gives the publish
   path a content identity, so it can tell "I corrected this and shipped
   it" from "I clicked the button twice".

Everything here is best-effort and must never break a render or a
publish: a portal database that is momentarily unreachable is worse
handled by a 500 on the operator's screen than by a stale badge.
"""

from __future__ import annotations

import hashlib
import json
import logging
from datetime import datetime, timezone
from typing import Iterable

logger = logging.getLogger(__name__)

# The two lazily-transcoded broadcast derivatives. They are the reason
# freshness needs a dedicated concept: every other deliverable is written
# synchronously by the render, these two arrive later.
PRORES_FILE_TYPES = ("umg_master", "umg_short")

# Why a delivery is mid-flight. Kept short and stable — the portal and
# the admin both branch on these strings.
STALE_EDITING = "editing"        # a re-render was requested, files will change
STALE_PRORES = "prores_pending"  # MP4 is fresh, broadcast master still transcoding
STALE_FAILED = "edit_failed"     # the re-render died; nobody is working on it

# Reasons that justify telling the client "we are applying changes". A failed
# edit is deliberately NOT one of them: the row stays flagged so the operator
# still sees it needs attention, but an indefinite "actualizando…" on a client
# portal when nobody is working on it is a promise we are not keeping.
STALE_IN_FLIGHT = (STALE_EDITING, STALE_PRORES)


def render_fingerprint(job) -> str:
    """Identidad del render que produjo los archivos que hoy están en R2.

    Dos publicaciones con el mismo fingerprint son el mismo corte: el
    operador apretó el botón dos veces y no hay nada que re-revisar. Uno
    distinto significa que los bytes detrás de la descarga del cliente
    cambiaron.

    **Sólo entran señales de que un render TERMINÓ.** La primera versión
    de esta función mezclaba también los inputs —`segments_revision`,
    `render_params`, `umg_spec`— y eso estaba mal en las dos direcciones:

    - **Falso positivo, con daño real.** Tocar cualquiera de esos campos
      sin re-renderizar movía el fingerprint, y publicar entonces borraba
      la aprobación del cliente y **cerraba sus pedidos de cambio
      abiertos** con un "resuelto en la versión N" sobre un video que
      nadie tocó. `/save-segments`, `/reanchor` y habilitar ProRes llegan
      todos por ahí. Reproducido el 2026-09-16: escribir `umg_spec` en 29
      jobs marcó las 29 entregas como desactualizadas sin que cambiara un
      solo byte.
    - **Falso negativo, peor.** `/retry` y `edit_art_track` re-renderizan
      de cero y NO tocan ninguno de esos campos (`edit_count` vuelve a 0,
      `previous_versions` no se toca, `render_params` se re-arma igual),
      así que el fingerprint quedaba **idéntico** y una corrección
      entera se informaba como "es el mismo corte que ya estaba
      publicado".

    `completed_at` cierra el caso de `/retry`: esos dos caminos lo ponen
    en NULL y `jobs.update_job` escribe uno nuevo en la siguiente
    transición terminal, mientras que una edición lo deja intacto. Junto
    con `edit_count` y el archivo de entregables pisados cubre los tres
    caminos que reemplazan archivos, y **nada más**.

    `s3_keys` sigue afuera a propósito: las keys son determinísticas y
    por lo tanto idénticas entre renders — ese es el problema original.
    """
    previous = job.previous_versions if isinstance(job.previous_versions, list) else []
    payload = {
        "job": job.job_id,
        # Cuántas veces se pisaron los entregables, por los dos lados: el
        # contador de cuota del revisor y el archivo en R2 de lo pisado.
        "edits": job.edit_count or 0,
        "overwrites": len(previous),
        "last_overwrite": (previous[-1] or {}).get("archived_at") if previous else None,
        # Cuándo terminó el render vigente. Es lo único que distingue un
        # /retry (que reinicia el resto de los contadores) de un reenvío.
        "completed": getattr(job, "completed_at", None),
    }
    blob = json.dumps(payload, sort_keys=True, default=str).encode()
    return hashlib.sha256(blob).hexdigest()


def has_prores_deliverable(job) -> bool:
    """Whether this job owes broadcast masters at all.

    Gated on `umg_spec` rather than on `delivery_profile`, for the reason
    the 2026-08-03 incident taught us: campaign jobs carry a UMG spec
    while their profile stays "youtube", and a profile-gated check left
    exactly those jobs serving a stale master forever.
    """
    if job is None:
        return False
    if job.umg_spec:
        return True
    if (job.delivery_profile or "youtube") in ("umg", "both"):
        return True
    keys = job.s3_keys or {}
    return any(keys.get(ft) for ft in PRORES_FILE_TYPES)


def _aware(value):
    """SQLite devuelve datetimes sin tzinfo y Postgres con: compararlos
    levanta TypeError, y acá lo comería el llamador dejando la frescura muda."""
    if value is None or getattr(value, "tzinfo", None):
        return value
    return value.replace(tzinfo=timezone.utc)


# Which rendered output each lazy derivative is transcoded FROM. The
# pairing is what makes staleness decidable: an edit re-uploads the
# source and drops the derivative's key in the same pipeline run.
_PRORES_SOURCE = {"umg_master": "video", "umg_short": "short"}


def prores_pending(
    job,
    file_types: Iterable[str] | None = None,
    *,
    re_rendered: bool | None = None,
) -> list[str]:
    """ProRes derivatives that no longer match the video next to them.

    R2 cannot answer this: after an edit the pre-edit .mov is still at
    its deterministic key and happily answers a HEAD. The job row can —
    `run_edit_pipeline` drops `s3_keys["umg_master"]` when it
    invalidates, and the prewarm writes it back only when the fresh
    transcode is up.

    But "key absent" alone is not enough, and specificity matters more
    than reach here: a false positive costs a multi-GB transcode and
    blocks the operator from publishing anything at all. Measured
    against production on 2026-09-15, "the master key is missing" alone
    matched ~40 of the 215 active publications, and most of them were
    never edited (edit_count=0). Two legacy shapes produce it honestly:

    - jobs predating key tracking, whose `s3_keys` is empty while their
      files sit perfectly in R2 (the same trap
      `main._deliverables_never_produced` documents), and
    - jobs whose master key was clobbered by the pre-2026-05-26
      wholesale `update_job(s3_keys=...)` write that race-lost against
      the prewarm (see `pipeline._upload_deliverables_to_r2`).

    So we require the conjunction that only a post-edit job satisfies:

    1. an overwriting re-render actually happened — `previous_versions`
       has an entry, appended by `_snapshot_previous_deliverables`
       immediately before the upload that replaced the files;
    2. the SOURCE of the derivative is tracked, i.e. that re-render
       wrote a fresh `video` / `short`; and
    3. the derivative itself is not tracked — the invalidation dropped
       it and no prewarm has re-published it.

    Anything else falls through to the caller's existence check, which
    is the behaviour these rows have always had.

    `file_types` narrows the question to what a particular delivery
    publishes — a job that never produced a vertical short must not be
    held back waiting for `umg_short`.
    """
    if job is None:
        return []
    wanted = tuple(file_types) if file_types is not None else PRORES_FILE_TYPES
    # Cuando el llamador dice qué entregables publica ESTA entrega, eso pesa
    # más que las columnas del job. Encontrado en vivo el 2026-09-15 con la
    # entrega 289 (job f7752c6feed4): `delivery_profile="youtube"`, `umg_spec`
    # en JSON `null` —que no es SQL NULL, así que `umg_spec IS NOT NULL` da
    # true y engaña a cualquier query— y las dos keys .mov borradas por el
    # edit. O sea: en la fila del job no quedaba UNA sola señal de que debía
    # un master, y sin embargo el portal estaba ofreciendo uno de 4,3 GB del
    # corte anterior. La evidencia vivía en `file_types` de la entrega, que
    # es el contrato con el cliente; el perfil del job es un detalle interno
    # que ya falló por tercera vez (ver has_prores_deliverable).
    published_as_prores = file_types is not None and any(
        ft in wanted for ft in PRORES_FILE_TYPES
    )
    if not published_as_prores and not has_prores_deliverable(job):
        return []
    # Evidencia de que un render pisó los archivos. Por defecto sale del
    # archivo de entregables previos, que sólo escribe `run_edit_pipeline`.
    #
    # `/retry` y `edit_art_track` NO lo escriben: limpian `s3_keys` y
    # re-renderizan de cero, así que con la regla por defecto pasaban
    # derecho y se podía publicar el master PRE-retry al lado del MP4
    # nuevo — el incidente 2026-08-03 por otra puerta. El llamador que
    # conoce la fila publicada puede probarlo de otra forma (el render
    # terminó DESPUÉS de la última publicación) y pasarlo acá.
    if re_rendered is None:
        previous = job.previous_versions if isinstance(job.previous_versions, list) else []
        re_rendered = bool(previous)
    if not re_rendered:
        return []
    keys = job.s3_keys or {}
    return [
        ft for ft in PRORES_FILE_TYPES
        if ft in wanted
        and keys.get(_PRORES_SOURCE[ft])
        and not keys.get(ft)
    ]


def mark_deliveries_stale(job_id: str, reason: str = STALE_EDITING) -> int:
    """Flag every active publication of `job_id` as mid-flight.

    Called when a re-render is requested. Until the operator publishes
    the result, the portal says "se están aplicando cambios" instead of
    presenting the download as final — the honest answer during the
    window where the MP4 has already been replaced and the broadcast
    master has not.

    Returns how many rows were flagged. Never raises: the edit must
    proceed even if the portal database is unreachable.
    """
    try:
        from database import Delivery, scoped_deliveries_db

        now = datetime.now(timezone.utc)
        with scoped_deliveries_db() as ddb:
            rows = (
                ddb.query(Delivery)
                .filter(Delivery.job_id == job_id)
                .filter(Delivery.removed_at.is_(None))
                .all()
            )
            flagged = 0
            for row in rows:
                # Keep the original stale_since: the window the client
                # cares about starts at the first edit, not the last.
                if row.stale_since is None:
                    row.stale_since = now
                row.stale_reason = reason
                flagged += 1
            if flagged:
                ddb.commit()
            return flagged
    except Exception as exc:
        logger.warning(
            "[DELIVERY] could not flag deliveries stale for job=%s: %s", job_id, exc,
        )
        return 0


def clear_size_cache(r2_keys: Iterable[str]) -> int:
    """Drop the cached byte size of each key so the portal re-reads R2.

    The listing caches `head_object` sizes in Redis for 30 days, which is
    correct for an immutable object and wrong for ours: after a
    re-render the portal advertised the previous file's weight — the one
    clue a client had that anything changed, pointing the wrong way.

    Returns the number of keys dropped; never raises (a cold cache is a
    slower listing, not a broken one).
    """
    keys = [k for k in r2_keys if k]
    if not keys:
        return 0
    try:
        from queue_jobs import _init_redis

        client, _, _ = _init_redis()
        if client is None:
            return 0
        dropped = 0
        for key in keys:
            try:
                dropped += int(bool(client.delete("dlsize:" + key)))
            except Exception:
                continue
        return dropped
    except Exception as exc:
        logger.warning("[DELIVERY] size cache invalidation skipped: %s", exc)
        return 0


def needs_publish(job, delivery) -> bool:
    """Whether ``delivery`` still points at the cut from before an edit.

    Current rows have a render fingerprint and can be compared directly.
    Rows created before fingerprints were introduced cannot.  For those
    legacy rows, ``stale_since`` is the durable evidence written when a
    re-render is requested.  A failed edit deliberately does not count: its
    files were not replaced and the operator must retry the render first.

    Keeping this decision here makes the admin status and the publish
    endpoint agree.  Otherwise the UI can offer publishing while the backend
    treats it as a no-op (or, as happened with legacy rows, hide the action
    even after a successful correction).
    """
    if job is None or delivery is None:
        return False
    published_fingerprint = getattr(
        delivery, "published_render_fingerprint", None,
    )
    current_fingerprint = render_fingerprint(job)
    if published_fingerprint and current_fingerprint:
        return published_fingerprint != current_fingerprint
    return bool(
        getattr(delivery, "stale_since", None)
        and getattr(delivery, "stale_reason", None) in STALE_IN_FLIGHT
    )


def publication_state(job, delivery) -> dict:
    """What the operator needs to know about one published row.

    Answers the question the admin UI could not answer before: is what
    the client can download right now the same thing I just corrected?

    - `needs_publish`: the job has been re-rendered since this row was
      published, so the portal still describes the old cut (and, for the
      broadcast master, may still be serving it).
    - `prores_pending`: the fresh MP4 is up but the master has not been
      re-transcoded yet. Publishing now would ship a mismatched pair.
    - `awaiting_review`: new content was published and the client has not
      approved it yet.
    """
    published_fingerprint = getattr(delivery, "published_render_fingerprint", None)
    current_fingerprint = render_fingerprint(job) if job is not None else None
    # La fila publicada permite una segunda prueba que el job solo no da:
    # si el render terminó DESPUÉS de publicarse, los archivos que el
    # cliente ve ya no son los que se publicaron.
    re_rendered = None
    completed = _aware(getattr(job, "completed_at", None)) if job is not None else None
    published_at = _aware(
        getattr(delivery, "content_updated_at", None) or delivery.added_at
    )
    if completed is not None and published_at is not None:
        previous = (
            job.previous_versions if isinstance(job.previous_versions, list) else []
        )
        re_rendered = bool(previous) or completed > published_at
    pending = prores_pending(
        job, delivery.file_types or [], re_rendered=re_rendered,
    ) if job is not None else []
    # A row published before this column existed has no fingerprint to
    # compare against. Treat it as current: claiming "needs publish" on
    # every historical delivery would bury the rows that really do.
    # Current rows compare exact render identity; legacy rows use durable
    # stale evidence written by a successful edit path.
    changed = needs_publish(job, delivery)
    return {
        "revision": delivery.published_revision or 1,
        "content_updated_at": (
            delivery.content_updated_at.isoformat()
            if delivery.content_updated_at else None
        ),
        "approved_at": (
            delivery.approved_at.isoformat() if delivery.approved_at else None
        ),
        "approved_by_label": delivery.approved_by_label,
        "stale_since": (
            delivery.stale_since.isoformat() if delivery.stale_since else None
        ),
        "stale_reason": delivery.stale_reason,
        "needs_publish": changed,
        "prores_pending": pending,
        "awaiting_review": bool(
            delivery.content_updated_at and delivery.approved_at is None
        ),
        "job_status": getattr(job, "status", None),
    }
