"""La auditoría diaria de lo que el portal promete.

Las dos clases de falla que cubre se descubrieron de casualidad el
2026-09-15 — 28 masters que no existían en el portal de Chile, y una entrega
sirviendo un corte anterior — así que lo que se testea acá es sobre todo que
la auditoría las VEA, y que no invente hallazgos donde no hay.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest

import delivery_integrity as di


def _delivery(db, **kwargs):
    from database import Delivery

    row = Delivery(
        job_id=kwargs.get("job_id") or uuid.uuid4().hex[:12],
        label="Campaña",
        file_types=kwargs.get("file_types", ["umg_master", "video"]),
        artist_snapshot="Artista", song_title_snapshot="Canción",
        tenant_snapshot="universal_music",
        portal_id=kwargs.get("portal_id", "chile"),
        added_by_user_id=1, added_at=datetime.now(timezone.utc),
        removed_at=kwargs.get("removed_at"),
        published_render_fingerprint=kwargs.get("fingerprint"),
        stale_since=kwargs.get("stale_since"),
        stale_reason=kwargs.get("stale_reason"),
    )
    db.add(row); db.commit()
    return row


@pytest.fixture
def limpio(db):
    """Aísla la auditoría de las filas que dejaron otros tests."""
    from database import Delivery, DeliveryChangeRequest

    creadas = []
    yield creadas
    ids = [r.id for r in creadas]
    if ids:
        db.query(DeliveryChangeRequest).filter(
            DeliveryChangeRequest.delivery_id.in_(ids)
        ).delete(synchronize_session=False)
        db.query(Delivery).filter(Delivery.id.in_(ids)).delete(synchronize_session=False)
        db.commit()


def _solo(report, clave, ids):
    """Los hallazgos de `clave` que corresponden a nuestras filas."""
    return [x for x in report[clave] if x["delivery_id"] in ids]


def test_ve_el_entregable_que_la_fila_promete_y_no_existe(db, limpio):
    """El caso del 2026-09-15: 28 de 34 entregas del portal de Chile
    ofrecían un "ProRes Master (broadcast)" que no estaba en R2. En la
    pantalla del cliente se ve igual que cualquier otro archivo."""
    row = _delivery(db, file_types=["umg_master", "video"])
    limpio.append(row)

    def existe(key):
        return not key.endswith("umg_master.mov")

    with (
        patch.object(di.storage, "is_enabled", return_value=True),
        patch.object(di.storage, "object_exists", side_effect=existe),
    ):
        rep = di.audit_active_deliveries()

    fantasmas = _solo(rep, "phantom", {row.id})
    assert len(fantasmas) == 1
    assert fantasmas[0]["file_type"] == "umg_master"
    assert fantasmas[0]["portal_id"] == "chile"


def test_no_inventa_nada_cuando_todo_esta_en_su_lugar(db, limpio):
    row = _delivery(db)
    limpio.append(row)
    with (
        patch.object(di.storage, "is_enabled", return_value=True),
        patch.object(di.storage, "object_exists", return_value=True),
    ):
        rep = di.audit_active_deliveries()
    assert _solo(rep, "phantom", {row.id}) == []
    assert _solo(rep, "outdated", {row.id}) == []


def test_un_fallo_de_red_no_se_reporta_como_archivo_faltante(db, limpio):
    """Un falso positivo entrena al operador a ignorar la alerta, que es peor
    que no tenerla."""
    row = _delivery(db)
    limpio.append(row)
    with (
        patch.object(di.storage, "is_enabled", return_value=True),
        patch.object(di.storage, "object_exists", side_effect=RuntimeError("R2 timeout")),
    ):
        rep = di.audit_active_deliveries()
    assert _solo(rep, "phantom", {row.id}) == []


def test_no_mira_las_entregas_dadas_de_baja(db, limpio):
    row = _delivery(db, removed_at=datetime.now(timezone.utc))
    limpio.append(row)
    with (
        patch.object(di.storage, "is_enabled", return_value=True),
        patch.object(di.storage, "object_exists", return_value=False),
    ):
        rep = di.audit_active_deliveries()
    assert _solo(rep, "phantom", {row.id}) == []


def test_ve_la_entrega_cuyo_render_cambio_despues_de_publicarse(db, limpio):
    """La otra clase: el job se re-renderizó y nadie volvió a publicar, así
    que el portal sirve algo que el cliente no aprobó."""
    from database import Job

    job_id = uuid.uuid4().hex[:12]
    job = Job(
        job_id=job_id, user_id=1, tenant_id="universal_music",
        artist="Artista", song_title="Canción", filename="s.wav",
        status="done", edit_count=2,
    )
    db.add(job); db.commit()
    row = _delivery(db, job_id=job_id, fingerprint="fingerprint-de-otro-render")
    limpio.append(row)
    try:
        with (
            patch.object(di.storage, "is_enabled", return_value=True),
            patch.object(di.storage, "object_exists", return_value=True),
        ):
            rep = di.audit_active_deliveries()
        desactualizadas = _solo(rep, "outdated", {row.id})
        assert len(desactualizadas) == 1
        assert desactualizadas[0]["job_id"] == job_id
    finally:
        db.query(Job).filter(Job.job_id == job_id).delete()
        db.commit()


def test_una_fila_sin_fingerprint_no_es_un_hallazgo(db, limpio):
    """Publicada antes de que existiera la columna: no hay con qué comparar.
    Contarla como desactualizada enterraría las que sí lo están."""
    row = _delivery(db, fingerprint=None)
    limpio.append(row)
    with (
        patch.object(di.storage, "is_enabled", return_value=True),
        patch.object(di.storage, "object_exists", return_value=True),
    ):
        rep = di.audit_active_deliveries()
    assert _solo(rep, "outdated", {row.id}) == []
    assert rep["unevaluable"] >= 1


def test_un_job_de_otro_entorno_no_es_un_hallazgo(db, limpio):
    """Las entregas de campaña viven en la DB del portal mientras sus jobs
    viven en la de staging. En producción muchas no son evaluables, y eso no
    es un problema que haya que reportar todos los días."""
    row = _delivery(db, job_id="noexisteaqui", fingerprint="algun-fingerprint")
    limpio.append(row)
    with (
        patch.object(di.storage, "is_enabled", return_value=True),
        patch.object(di.storage, "object_exists", return_value=True),
    ):
        rep = di.audit_active_deliveries()
    assert _solo(rep, "outdated", {row.id}) == []


def test_ve_la_fila_que_quedo_en_vuelo(db, limpio):
    """Marcada "aplicando cambios" hace un día: el edit murió o nadie publicó
    el resultado. Sólo publicar limpia ese flag, así que nada la destraba."""
    row = _delivery(
        db,
        stale_since=datetime.now(timezone.utc) - timedelta(hours=di.STALE_IN_FLIGHT_HOURS + 2),
        stale_reason="editing",
    )
    limpio.append(row)
    with (
        patch.object(di.storage, "is_enabled", return_value=True),
        patch.object(di.storage, "object_exists", return_value=True),
    ):
        rep = di.audit_active_deliveries()
    colgadas = _solo(rep, "in_flight_too_long", {row.id})
    assert len(colgadas) == 1
    assert colgadas[0]["reason"] == "editing"


def test_una_edicion_recien_pedida_no_alarma(db, limpio):
    row = _delivery(
        db, stale_since=datetime.now(timezone.utc) - timedelta(minutes=5),
        stale_reason="editing",
    )
    limpio.append(row)
    with (
        patch.object(di.storage, "is_enabled", return_value=True),
        patch.object(di.storage, "object_exists", return_value=True),
    ):
        rep = di.audit_active_deliveries()
    assert _solo(rep, "in_flight_too_long", {row.id}) == []


def test_las_keys_salen_del_mapa_compartido_y_no_de_una_copia():
    """Si mañana se agrega un entregable al portal, la auditoría lo incluye
    sin que nadie se acuerde de tocar este módulo."""
    from delivery_retention import DELIVERY_FILENAMES

    for file_type, filename in DELIVERY_FILENAMES.items():
        assert di._key("universal_music", "abc123", file_type) == (
            f"universal_music/abc123/{filename}"
        )
    assert di._key("t", "j", "inexistente") is None


def test_la_auditoria_nunca_tira_abajo_al_reaper(monkeypatch):
    """Corre dentro del ciclo del reaper: si se cae, se lleva puesto el
    reaper, que hace cosas más importantes que auditar."""
    def boom():
        raise RuntimeError("portal db caída")

    monkeypatch.setattr("database.scoped_deliveries_db", boom)
    rep = di.audit_active_deliveries()
    assert "error" in rep
    assert rep["phantom"] == []


def test_el_reaper_la_corre_una_vez_por_dia():
    import inspect
    import reaper

    src = inspect.getsource(reaper)
    assert "_DELIVERY_AUDIT_INTERVAL_S" in src
    assert "audit_active_deliveries" in src
    # Bajo el mismo lock de un solo runner que el barrido de retención, así
    # N réplicas no auditan en paralelo contra la DB externa del portal.
    assert src.index("_DELIVERY_RETENTION_SWEEP_INTERVAL_S") < src.index(
        "_DELIVERY_AUDIT_INTERVAL_S"
    )
