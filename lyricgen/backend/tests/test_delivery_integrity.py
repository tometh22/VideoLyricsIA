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
        published_file_keys=kwargs.get("published_file_keys"),
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
    """Un entregable del render que no está en R2 no lo genera nadie: es un
    fantasma. (El MP4 y el thumbnail los escribe el pipeline; si faltan,
    faltan.)"""
    row = _delivery(db, file_types=["umg_master", "video"])
    limpio.append(row)

    def existe(key):
        return "missing" if key.endswith("lyric_video.mp4") else "exists"

    with (
        patch.object(di.storage, "is_enabled", return_value=True),
        patch.object(di.storage, "object_status_bounded", side_effect=existe),
    ):
        rep = di.audit_active_deliveries()

    fantasmas = _solo(rep, "phantom", {row.id})
    assert len(fantasmas) == 1
    assert fantasmas[0]["file_type"] == "video"
    assert fantasmas[0]["portal_id"] == "chile"


def test_no_inventa_nada_cuando_todo_esta_en_su_lugar(db, limpio):
    row = _delivery(db)
    limpio.append(row)
    with (
        patch.object(di.storage, "is_enabled", return_value=True),
        patch.object(di.storage, "object_status_bounded", return_value="exists"),
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
        patch.object(di.storage, "object_status_bounded", side_effect=RuntimeError("R2 timeout")),
    ):
        rep = di.audit_active_deliveries()
    assert _solo(rep, "phantom", {row.id}) == []


def test_no_mira_las_entregas_dadas_de_baja(db, limpio):
    row = _delivery(db, removed_at=datetime.now(timezone.utc))
    limpio.append(row)
    with (
        patch.object(di.storage, "is_enabled", return_value=True),
        patch.object(di.storage, "object_status_bounded", return_value="missing"),
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
            patch.object(di.storage, "object_status_bounded", return_value="exists"),
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
        patch.object(di.storage, "object_status_bounded", return_value="exists"),
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
        patch.object(di.storage, "object_status_bounded", return_value="exists"),
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
        patch.object(di.storage, "object_status_bounded", return_value="exists"),
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
        patch.object(di.storage, "object_status_bounded", return_value="exists"),
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
    # The scheduler still invokes both helpers; its LOCAL advisory lock
    # does not prove exclusion across environments sharing deliveries.
    assert src.index("_DELIVERY_RETENTION_SWEEP_INTERVAL_S") < src.index(
        "_DELIVERY_AUDIT_INTERVAL_S"
    )


def test_un_prores_prometido_ausente_es_un_fantasma(db, limpio):
    """An advertised ProRes is required, not silently assumed to be on demand."""
    row = _delivery(db, file_types=["umg_master", "umg_short", "video"])
    limpio.append(row)
    with (
        patch.object(di.storage, "is_enabled", return_value=True),
        patch.object(di.storage, "object_status_bounded",
                     side_effect=lambda k: "missing" if k.endswith(".mov") else "exists"),
    ):
        rep = di.audit_active_deliveries()
    assert sorted(x["file_type"] for x in _solo(rep, "phantom", {row.id})) == ["umg_master", "umg_short"]
    assert _solo(rep, "on_demand", {row.id}) == []


def test_una_auditoria_caida_no_se_informa_verde(caplog):
    """Informaba "0 entregas activas OK" cuando se había caído entera. Verde
    y mudo es peor que no tenerla: nadie vuelve a mirar."""
    import logging
    with caplog.at_level(logging.ERROR):
        di.log_audit({"checked": 0, "objects_checked": 0, "phantom": [],
                      "outdated": [], "in_flight_too_long": [], "on_demand": [],
                      "error": "portal db caída"})
    assert any("NO es un resultado" in r.getMessage() for r in caplog.records)


def test_cero_objetos_chequeados_no_es_un_dia_limpio(caplog):
    """Si R2 está deshabilitado, el chequeo de archivos ausentes —la razón de
    ser del módulo— no corrió, y el reporte decía "todo OK"."""
    import logging
    with caplog.at_level(logging.ERROR):
        di.log_audit({"checked": 215, "objects_checked": 0, "phantom": [],
                      "outdated": [], "in_flight_too_long": [], "on_demand": []})
    assert any("CERO objetos" in r.getMessage() for r in caplog.records)


def test_snapshot_missing_entry_never_checks_new_working_object(db, limpio):
    row = _delivery(db, file_types=["video", "short"],
                    published_file_keys={"video": "old-published/video"})
    limpio.append(row)
    seen = []

    def status(key):
        seen.append(key)
        return "exists"

    with (patch.object(di.storage, "is_enabled", return_value=True),
          patch.object(di.storage, "object_status_bounded", side_effect=status)):
        report = di.audit_active_deliveries()
    assert "old-published/video" in seen
    assert not any(row.job_id in key for key in seen)
    failures = _solo(report, "manifest_failures", {row.id})
    assert len(failures) == 1
    assert failures[0]["file_type"] == "short"
    assert failures[0]["reason"] == "missing_snapshot_key"


@pytest.mark.parametrize("storage_status", ["unavailable", "403", None, "unexpected"])
def test_unknown_status_cannot_be_missing_or_healthy(db, limpio, storage_status):
    row = _delivery(db, file_types=["video"])
    limpio.append(row)
    with (patch.object(di.storage, "is_enabled", return_value=True),
          patch.object(di.storage, "object_status_bounded", return_value=storage_status)):
        report = di.audit_active_deliveries()
    assert not _solo(report, "phantom", {row.id})
    assert len(_solo(report, "unknown", {row.id})) == 1
    assert report["complete"] is False


def test_missing_snapshot_master_is_not_lazy_generation(db, limpio):
    row = _delivery(db, file_types=["umg_master"],
                    published_file_keys={"umg_master": "published/master.mov.frozen"})
    limpio.append(row)
    with (patch.object(di.storage, "is_enabled", return_value=True),
          patch.object(di.storage, "object_status_bounded", return_value="missing")):
        report = di.audit_active_deliveries()
    assert len(_solo(report, "phantom", {row.id})) == 1
    assert not _solo(report, "on_demand", {row.id})


def test_published_a_and_working_b_is_candidate_not_corrupt_publication(db, limpio):
    from database import Job
    jid = uuid.uuid4().hex[:12]
    job = Job(job_id=jid, user_id=1, tenant_id="universal_music", artist="Artista",
              song_title="Canción", filename="s.wav", status="done", edit_count=3)
    db.add(job)
    db.commit()
    row = _delivery(db, job_id=jid, fingerprint="published-a", file_types=["video"],
                    published_file_keys={"video": "immutable-a/video"})
    limpio.append(row)
    try:
        with (patch.object(di.storage, "is_enabled", return_value=True),
              patch.object(di.storage, "object_status_bounded", return_value="exists")):
            report = di.audit_active_deliveries()
        assert not _solo(report, "outdated", {row.id})
        assert len(_solo(report, "candidate_available", {row.id})) == 1
    finally:
        db.query(Job).filter(Job.job_id == jid).delete()
        db.commit()


def test_storage_disabled_reports_unchecked_and_incomplete(db, limpio):
    row = _delivery(db, file_types=["video"])
    limpio.append(row)
    with (patch.object(di.storage, "is_enabled", return_value=False),
          patch.object(di.storage, "object_status_bounded", side_effect=AssertionError("must not call"))):
        report = di.audit_active_deliveries()
    assert report["objects_unchecked"] >= 1
    assert report["storage_status"] == "unavailable"
    assert report["objects_checked"] == 0 and not report["complete"]


def test_budget_does_not_claim_full_coverage(db, limpio, monkeypatch):
    for _ in range(3):
        limpio.append(_delivery(db, file_types=["video", "short"]))
    monkeypatch.setattr(di, "MAX_OBJECT_CHECKS", 2)
    with (patch.object(di.storage, "is_enabled", return_value=True),
          patch.object(di.storage, "object_status_bounded", return_value="exists") as check):
        report = di.audit_active_deliveries(cursor=0)
    assert check.call_count == report["objects_checked"] == 2
    assert report["objects_unchecked"] >= 4 and not report["complete"]
    assert report["cursor_mode"] == "explicit" and report["next_cursor"] == 2


@pytest.mark.parametrize("count,budget", [(17, 5), (500, 32), (7, 2), (4, 100)])
def test_default_rotation_covers_stable_catalogue_without_starvation(monkeypatch, count, budget):
    import math
    monkeypatch.setattr(di, "MAX_OBJECT_CHECKS", budget)
    now = datetime(2026, 9, 18, tzinfo=timezone.utc)
    seen = set()
    for day in range(math.ceil(count / budget)):
        selected, _ = di._budgeted_checks(list(range(count)), now=now + timedelta(days=day))
        assert len(selected) <= budget
        assert len(selected) == len(set(selected))
        seen.update(selected)
    assert seen == set(range(count))


def test_explicit_cursor_can_complete_same_day_sweep(monkeypatch):
    monkeypatch.setattr(di, "MAX_OBJECT_CHECKS", 3)
    now = datetime(2026, 9, 18, tzinfo=timezone.utc)
    cursor, seen = 0, set()
    for _ in range(4):
        selected, cursor = di._budgeted_checks(list(range(10)), now=now, cursor=cursor)
        seen.update(selected)
    assert seen == set(range(10))


@pytest.mark.parametrize("budget", [0, -10])
def test_nonpositive_budget_never_checks_objects(monkeypatch, budget):
    monkeypatch.setattr(di, "MAX_OBJECT_CHECKS", budget)
    assert di._budgeted_checks([1, 2], now=datetime.now(timezone.utc)) == ([], 0)


@pytest.mark.parametrize("manifest", [{}, [], "bad", {"video": None}, {"video": 7}])
def test_invalid_manifest_never_falls_back_to_mutable_key(manifest):
    key, problem = di._published_key(
        {"published_file_keys": manifest, "tenant": "t", "job_id": "j"}, "video")
    assert key is None and problem


def test_audit_closes_deliveries_transaction_before_storage(db, limpio, monkeypatch):
    from contextlib import contextmanager
    from database import SessionLocal
    row = _delivery(db, file_types=["video"])
    limpio.append(row)
    sessions = []

    @contextmanager
    def scoped():
        with SessionLocal() as session:
            sessions.append(session)
            yield session

    def status(_key):
        assert sessions and all(not s.in_transaction() for s in sessions)
        return "exists"

    monkeypatch.setattr("database.scoped_deliveries_db", scoped)
    with (patch.object(di.storage, "is_enabled", return_value=True),
          patch.object(di.storage, "object_status_bounded", side_effect=status)):
        report = di.audit_active_deliveries()
    assert "error" not in report
    assert not report["unknown"]


@pytest.mark.parametrize("extra", [{"error": "database_unavailable"},
                                   {"objects_unchecked": 1},
                                   {"unknown": [{"delivery_id": 1}]}])
def test_incomplete_audit_never_logs_a_green_result(caplog, monkeypatch, extra):
    import logging
    monkeypatch.setattr(di, "_increment_audit_metrics", lambda report: None)
    report = {"checked": 1, "objects_checked": 1, "complete": False, **extra}
    with caplog.at_level(logging.INFO):
        di.log_audit(report)
    assert not any("sin fallos" in record.getMessage() or "activas OK" in record.getMessage()
                   for record in caplog.records)


def test_deadline_returns_without_waiting_for_running_probes(monkeypatch):
    """Real Future wait timeout; no threads/network and no timing sleeps."""
    import time
    from concurrent.futures import Future

    class HungPool:
        def __init__(self):
            self.futures = []

        def submit(self, _fn, _check):
            future = Future()
            future.set_running_or_notify_cancel()
            self.futures.append(future)
            return future

    pool = HungPool()
    monkeypatch.setattr(di, "_OBJECT_AUDIT_POOL", pool)
    checks = [(i, "video", f"published/{i}") for i in range(2000)]
    started = time.monotonic()
    outcomes, attempted, unchecked = di._inspect_budgeted(checks, deadline=started + 0.03)
    elapsed = time.monotonic() - started
    assert elapsed < 1.0, "audit waited for unfinished network work"
    assert len(pool.futures) == attempted == 4
    assert unchecked == 1996
    assert [status for _, status in outcomes] == ["deadline_exceeded"] * 4
    # Late success cannot mutate the already-returned audit into green.
    for future in pool.futures:
        future.set_result("exists")
    assert all(status == "deadline_exceeded" for _, status in outcomes)


def test_deadline_cancels_queued_probes_and_reports_them_unchecked(monkeypatch):
    import time
    from concurrent.futures import Future

    class QueuedPool:
        def __init__(self):
            self.futures = []

        def submit(self, _fn, _check):
            future = Future()
            self.futures.append(future)
            return future

    pool = QueuedPool()
    monkeypatch.setattr(di, "_OBJECT_AUDIT_POOL", pool)
    outcomes, attempted, unchecked = di._inspect_budgeted(
        [(i, "video", str(i)) for i in range(10)], deadline=time.monotonic() + 0.01)
    assert outcomes == [] and attempted == 0 and unchecked == 10
    assert len(pool.futures) <= 4
    assert all(future.cancelled() for future in pool.futures)


def test_exhausted_deadline_never_starts_storage_work(monkeypatch):
    import time
    from unittest.mock import Mock
    pool = Mock()
    monkeypatch.setattr(di, "_OBJECT_AUDIT_POOL", pool)
    assert di._inspect_budgeted([(1, "video", "v")], deadline=time.monotonic() - 1) == ([], 0, 1)
    pool.submit.assert_not_called()


def test_scheduler_waking_after_deadline_cannot_report_success(monkeypatch):
    from concurrent.futures import Future
    clock = [0.0]
    class ReadyPool:
        def submit(self, *_):
            value = Future()
            value.set_result('exists')
            return value
    def late_wait(pending, **_):
        clock[0] = 2.0
        return set(pending), set()
    monkeypatch.setattr(di, '_OBJECT_AUDIT_POOL', ReadyPool())
    monkeypatch.setattr(di, 'wait', late_wait)
    monkeypatch.setattr(di.time, 'monotonic', lambda: clock[0])
    outcomes, attempted, unchecked = di._inspect_budgeted([(1, 'video', 'key')], deadline=1.0)
    assert attempted == 1 and unchecked == 0
    assert outcomes == [((1, 'video', 'key'), 'deadline_exceeded')]


def test_probe_scheduler_never_submits_more_than_four_without_a_completion(monkeypatch):
    import time
    from concurrent.futures import Future

    class ReadyPool:
        def __init__(self):
            self.submitted = 0
            self.uncollected = set()

        def submit(self, fn, check):
            assert len(self.uncollected) < 4
            future = Future()
            future.set_result(fn(check))
            self.uncollected.add(future)
            self.submitted += 1
            return future

    pool = ReadyPool()

    def collect(pending, **_kwargs):
        finished = {next(iter(pending))}
        pool.uncollected.difference_update(finished)
        return finished, set(pending) - finished

    monkeypatch.setattr(di, "_OBJECT_AUDIT_POOL", pool)
    monkeypatch.setattr(di, "wait", collect)
    monkeypatch.setattr(di.storage, "object_status_bounded", lambda key: "exists")
    outcomes, attempted, unchecked = di._inspect_budgeted(
        [(i, "video", str(i)) for i in range(100)], deadline=time.monotonic() + 5)
    assert pool.submitted == attempted == len(outcomes) == 100
    assert unchecked == 0 and all(status == "exists" for _, status in outcomes)
