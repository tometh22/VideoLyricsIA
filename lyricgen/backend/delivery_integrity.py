"""¿Lo que el portal promete existe, y es lo que dijimos que era?

Esto existe porque las dos veces que falló nos enteramos de casualidad.

El 2026-09-15 el portal de Chile ofrecía, de 34 entregas activas, **28
"ProRes Master (broadcast)" que no existían en R2** — la publicación masiva
los anunciaba creyendo que el portal los transcodifica al primer download, y
el portal no hace eso: firma la key determinística y listo. Nadie tenía forma
de saberlo: en la pantalla del cliente el archivo se ve como cualquier otro
hasta que lo aprieta. Lo encontramos porque alguien preguntó por otra cosa.

Y el mismo día, una entrega servía un master de un corte anterior con el
pedido de cambios marcado como resuelto al lado, porque la fila del job había
perdido su key y ningún chequeo mira eso.

Las dos clases son detectables en una pasada barata, y las dos son invisibles
sin ella:

- **fantasma**: la fila anuncia un `file_type` cuyo objeto no está en R2. El
  cliente ve un entregable que no se puede descargar.
- **desactualizada**: el render del job cambió después de publicarse, así que
  el portal está sirviendo (o está a punto de servir) algo que nadie aprobó.
  Sólo se puede evaluar cuando este entorno es dueño del Job: las entregas
  de campaña viven en la DB del portal mientras sus jobs viven en la de
  staging, así que en producción muchas quedan "no evaluables" y eso NO es un
  hallazgo.
- **en vuelo hace mucho**: una fila marcada como "aplicando cambios" que
  quedó así. Se cura publicando, y si nadie publica hay que verlo.

No arregla nada por sí solo: reporta. Arreglar un entregable es siempre una
decisión con un humano adentro — la lección del 2026-09-15 fue que la fila
puede mentir en las dos direcciones, y que hay que medir el artefacto antes
de tocarlo.
"""

from __future__ import annotations

import logging
import os
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

import storage
from delivery_retention import DELIVERY_FILENAMES

logger = logging.getLogger(__name__)

# Horas que una fila puede estar marcada como "en vuelo" antes de que sea
# raro. Un re-render más el transcode del master tarda minutos; un día
# significa que el edit murió o que nadie publicó el resultado.
STALE_IN_FLIGHT_HOURS = int(os.environ.get("DELIVERY_STALE_ALERT_HOURS", "24"))

# Tope de objetos a chequear por pasada. Con ~215 entregas activas × 5
# archivos son ~1000 HEAD una vez por día, que a R2 no le mueve la aguja;
# el tope es para que un crecimiento de 10x no convierta la auditoría en el
# trabajo más caro del reaper.
MAX_OBJECT_CHECKS = int(os.environ.get("DELIVERY_AUDIT_MAX_CHECKS", "2000"))


def _key(tenant: str, job_id: str, file_type: str) -> str | None:
    """Key determinística de R2, con el mismo saneado que usa el escritor.

    Deliberadamente sale de `DELIVERY_FILENAMES` en vez de una copia local:
    si mañana se agrega un entregable, la auditoría lo incluye sin que nadie
    se acuerde de tocar este archivo.
    """
    filename = DELIVERY_FILENAMES.get(file_type)
    if not filename:
        return None
    return (
        f"{storage._safe_filename(tenant)}"
        f"/{storage._safe_filename(job_id)}"
        f"/{storage._safe_filename(filename)}"
    )


def audit_active_deliveries(*, now: datetime | None = None) -> dict:
    """Recorre las entregas activas del portal y devuelve lo que no cierra.

    Nunca levanta: esto corre dentro del ciclo del reaper y una auditoría que
    tira abajo el reaper cuesta más que la auditoría.
    """
    report: dict = {
        "checked": 0, "objects_checked": 0,
        "phantom": [], "outdated": [], "in_flight_too_long": [],
        "unevaluable": 0,
    }
    try:
        from database import Delivery, Job, SessionLocal, scoped_deliveries_db
        import delivery_freshness

        now = now or datetime.now(timezone.utc)
        cutoff = now - timedelta(hours=STALE_IN_FLIGHT_HOURS)

        with scoped_deliveries_db() as ddb:
            rows = (
                ddb.query(Delivery)
                .filter(Delivery.removed_at.is_(None))
                .all()
            )
            snapshot = [
                {
                    "id": d.id, "job_id": d.job_id, "portal_id": d.portal_id or "argentina",
                    "tenant": d.tenant_snapshot, "artist": d.artist_snapshot,
                    "song": d.song_title_snapshot,
                    "file_types": list(d.file_types or []),
                    "fingerprint": d.published_render_fingerprint,
                    "stale_since": d.stale_since, "stale_reason": d.stale_reason,
                }
                for d in rows
            ]
        report["checked"] = len(snapshot)

        # ── fantasmas: el objeto que la fila promete no está en R2 ──
        if storage.is_enabled():
            checks: list[tuple[int, str, str]] = []
            for row in snapshot:
                for ft in row["file_types"]:
                    key = _key(row["tenant"], row["job_id"], ft)
                    if key:
                        checks.append((row["id"], ft, key))
            if len(checks) > MAX_OBJECT_CHECKS:
                logger.warning(
                    "[DELIVERY-AUDIT] %d objetos a chequear supera el tope %d; "
                    "se audita un prefijo", len(checks), MAX_OBJECT_CHECKS,
                )
                checks = checks[:MAX_OBJECT_CHECKS]
            report["objects_checked"] = len(checks)

            def _missing(check):
                did, ft, key = check
                try:
                    return None if storage.object_exists(key) else (did, ft)
                except Exception:
                    # Un fallo de red no es un archivo faltante. Callar acá es
                    # lo correcto: un falso positivo entrena a ignorar la alerta.
                    return None

            by_id = {row["id"]: row for row in snapshot}
            with ThreadPoolExecutor(max_workers=16) as pool:
                for result in pool.map(_missing, checks):
                    if not result:
                        continue
                    did, ft = result
                    row = by_id[did]
                    report["phantom"].append({
                        "delivery_id": did, "portal_id": row["portal_id"],
                        "job_id": row["job_id"], "file_type": ft,
                        "song": f"{row['artist']} — {row['song']}",
                    })

        # ── desactualizadas: el render cambió después de publicar ──
        job_ids = [row["job_id"] for row in snapshot if row["fingerprint"]]
        jobs: dict = {}
        if job_ids:
            db = SessionLocal()
            try:
                for job in db.query(Job).filter(Job.job_id.in_(job_ids)).all():
                    jobs[job.job_id] = delivery_freshness.render_fingerprint(job)
            finally:
                db.close()
        for row in snapshot:
            if not row["fingerprint"]:
                # Publicada antes de que existiera el fingerprint: no hay con
                # qué comparar. Se cuenta, no se alerta.
                report["unevaluable"] += 1
                continue
            current = jobs.get(row["job_id"])
            if current is None:
                # El Job es de otro entorno. Tampoco es un hallazgo.
                report["unevaluable"] += 1
                continue
            if current != row["fingerprint"]:
                report["outdated"].append({
                    "delivery_id": row["id"], "portal_id": row["portal_id"],
                    "job_id": row["job_id"],
                    "song": f"{row['artist']} — {row['song']}",
                })

        # ── en vuelo hace demasiado ──
        # `_aware` porque SQLite devuelve datetimes sin tzinfo y Postgres con:
        # comparar los dos mundos levanta TypeError, y acá lo comería el
        # except de arriba dejando una auditoría muda que parece verde.
        from batch_campaigns import _aware
        for row in snapshot:
            desde = _aware(row["stale_since"])
            if desde and desde < cutoff:
                report["in_flight_too_long"].append({
                    "delivery_id": row["id"], "portal_id": row["portal_id"],
                    "job_id": row["job_id"], "reason": row["stale_reason"],
                    "since": row["stale_since"].isoformat(),
                    "song": f"{row['artist']} — {row['song']}",
                })
    except Exception as exc:
        logger.warning("[DELIVERY-AUDIT] auditoría incompleta: %s", exc)
        report["error"] = str(exc)[:200]
    return report


def log_audit(report: dict) -> None:
    """Escribe el resultado y cuenta las métricas.

    En silencio si todo cierra: una auditoría que habla todos los días sin
    tener nada deja de leerse en una semana.
    """
    total = (
        len(report.get("phantom", []))
        + len(report.get("outdated", []))
        + len(report.get("in_flight_too_long", []))
    )
    if not total:
        logger.info(
            "[DELIVERY-AUDIT] %d entregas activas OK (%d objetos, %d no evaluables)",
            report.get("checked", 0), report.get("objects_checked", 0),
            report.get("unevaluable", 0),
        )
        return
    for item in report.get("phantom", []):
        logger.error(
            "[DELIVERY-AUDIT] FANTASMA entrega=%s portal=%s %s: promete %s y no está en R2",
            item["delivery_id"], item["portal_id"], item["song"], item["file_type"],
        )
    for item in report.get("outdated", []):
        logger.error(
            "[DELIVERY-AUDIT] DESACTUALIZADA entrega=%s portal=%s %s: el render cambió "
            "después de publicarse", item["delivery_id"], item["portal_id"], item["song"],
        )
    for item in report.get("in_flight_too_long", []):
        logger.warning(
            "[DELIVERY-AUDIT] EN VUELO hace mucho entrega=%s %s: %s desde %s",
            item["delivery_id"], item["song"], item["reason"], item["since"],
        )
    try:
        from ops_metrics import increment
        increment("delivery_audit_phantom", len(report.get("phantom", [])))
        increment("delivery_audit_outdated", len(report.get("outdated", [])))
        increment("delivery_audit_in_flight", len(report.get("in_flight_too_long", [])))
    except Exception:
        pass
