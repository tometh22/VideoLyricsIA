"""Endpoint POST /jobs/{id}/edit-art-track — editar un Art Track ya generado.

Los art tracks ("official audio": cover + waveform, sin letra) no pasan por el
wizard de letra, pero sí se pueden re-renderizar con otra portada / efecto /
título / línea legal. El endpoint mergea los ejes en render_params, actualiza
las columnas de título/artista y re-encola vía enqueue_pipeline(art_track=True)
—el mismo camino que /retry, gratis (no consume cuota)—.

Estos tests pinean el contrato: solo art tracks, solo estados editables, y el
enqueue lleva art_track=True + effect + label_line + el cover cacheado.
"""
from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, patch

import pytest


def _admin_identity(client, admin_token):
    me = client.get(
        "/auth/me", headers={"Authorization": f"Bearer {admin_token}"}
    ).json()
    return me["id"], me["tenant_id"]


def _seed_art_track_job(user_id, tenant_id, *, status="pending_review",
                        art_track=True, effect="", label_line=""):
    from database import SessionLocal, Job
    db = SessionLocal()
    try:
        job_id = uuid.uuid4().hex[:12]
        render_params = {}
        if art_track:
            render_params["art_track"] = True
        if effect:
            render_params["effect"] = effect
        if label_line:
            render_params["label_line"] = label_line
        db.add(Job(
            job_id=job_id,
            user_id=user_id,
            tenant_id=tenant_id,
            artist="Amanda Pujó",
            song_title="Hacia el Espacio",
            style="oscuro",
            filename="hacia_el_espacio.wav",
            status=status,
            current_step="done",
            progress=100,
            delivery_profile="youtube",
            input_r2_key=f"inputs/{tenant_id}/{job_id}/hacia_el_espacio.wav",
            bg_r2_key_cached=f"inputs/{tenant_id}/{job_id}/bg_custom.jpg",
            render_params=render_params,
        ))
        db.commit()
        return job_id
    finally:
        db.close()


def _read_job(job_id):
    from database import SessionLocal, Job
    db = SessionLocal()
    try:
        return db.query(Job).filter(Job.job_id == job_id).first()
    finally:
        db.close()


def test_edit_art_track_reenqueues_with_new_params(client, admin_token):
    """Happy path: cambia efecto + título + línea legal (sin cover nuevo) →
    200, status editing, y enqueue_pipeline recibe art_track=True + el nuevo
    effect/label_line + el cover cacheado."""
    uid, tid = _admin_identity(client, admin_token)
    job_id = _seed_art_track_job(uid, tid, effect="", label_line="")

    with patch("main.enqueue_pipeline") as enq:
        res = client.post(
            f"/jobs/{job_id}/edit-art-track",
            headers={"Authorization": f"Bearer {admin_token}"},
            data={
                "effect": "bokeh",
                "art_track_preset": "colombia_static",
                "song_title": "Hacia el Espacio (Remaster)",
                "artist": "Amanda Pujó",
                "label_line": "℗ 2026 Universal Music Chile",
            },
        )

    assert res.status_code == 200, res.text
    body = res.json()
    assert body["status"] == "editing"
    assert body["cover_replaced"] is False

    # enqueue con los kwargs correctos
    assert enq.called
    kwargs = enq.call_args.kwargs
    assert kwargs["art_track"] is True
    assert kwargs["art_track_preset"] == "colombia_static"
    assert kwargs["effect"] == ""
    assert kwargs["label_line"] == "℗ 2026 Universal Music Chile"
    # reusa el cover cacheado (no se subió uno nuevo)
    assert kwargs["bg_r2_key"].endswith("bg_custom.jpg")

    # persistencia: render_params + columnas + status
    row = _read_job(job_id)
    assert row.status == "editing"
    assert row.render_params["art_track_preset"] == "colombia_static"
    assert row.render_params["effect"] == ""
    assert row.render_params["label_line"] == "℗ 2026 Universal Music Chile"
    assert row.render_params["art_track"] is True
    assert row.song_title == "Hacia el Espacio (Remaster)"


def test_edit_art_track_clears_effect_and_label(client, admin_token):
    """Valores vacíos = limpiar: un art track con efecto/línea legal se puede
    dejar sin efecto y sin línea legal enviando strings vacíos."""
    uid, tid = _admin_identity(client, admin_token)
    job_id = _seed_art_track_job(
        uid, tid, effect="snow", label_line="℗ 2025 Sello")

    with patch("main.enqueue_pipeline") as enq:
        res = client.post(
            f"/jobs/{job_id}/edit-art-track",
            headers={"Authorization": f"Bearer {admin_token}"},
            data={"effect": "", "label_line": ""},
        )

    assert res.status_code == 200, res.text
    kwargs = enq.call_args.kwargs
    assert kwargs["effect"] == ""
    assert kwargs["label_line"] == ""
    row = _read_job(job_id)
    assert row.render_params["effect"] == ""
    assert row.render_params["label_line"] == ""


def test_edit_rejects_non_art_track(client, admin_token):
    """Un job de lyric video normal no se puede editar por este endpoint
    (se re-rendería sin letra)."""
    uid, tid = _admin_identity(client, admin_token)
    job_id = _seed_art_track_job(uid, tid, art_track=False)

    with patch("main.enqueue_pipeline") as enq:
        res = client.post(
            f"/jobs/{job_id}/edit-art-track",
            headers={"Authorization": f"Bearer {admin_token}"},
            data={"effect": "bokeh"},
        )

    assert res.status_code == 400
    assert not enq.called


def test_edit_rejects_while_processing(client, admin_token):
    """Solo estados editables (pending_review/done/rejected). Mientras procesa
    no se edita (para eso está /retry si falló)."""
    uid, tid = _admin_identity(client, admin_token)
    job_id = _seed_art_track_job(uid, tid, status="processing")

    with patch("main.enqueue_pipeline") as enq:
        res = client.post(
            f"/jobs/{job_id}/edit-art-track",
            headers={"Authorization": f"Bearer {admin_token}"},
            data={"effect": "bokeh"},
        )

    assert res.status_code == 400
    assert not enq.called


@pytest.mark.parametrize("replace_cover", [False, True])
def test_admin_corrects_other_tenant_art_track_under_original_owner(
        client, admin_token, user_token, db, replace_cover):
    from database import AuditLog, User
    actor_id, actor_tenant = _admin_identity(client, admin_token)
    owner = client.get("/auth/me", headers={"Authorization": f"Bearer {user_token}"}).json()
    owner_row = db.query(User).filter(User.id == owner["id"]).one()
    owner_row.plan_id = "1000"
    db.commit()
    assert owner["tenant_id"] != actor_tenant
    job_id = _seed_art_track_job(owner["id"], owner["tenant_id"], status="done")
    cover_key = f"inputs/{owner['tenant_id']}/{job_id}/new-cover.jpg"

    with patch("main.enqueue_pipeline") as enq, patch(
        "main._save_custom_background", return_value=("/tmp/cover.jpg", cover_key)
    ) as save_cover:
        res = client.post(
            f"/jobs/{job_id}/edit-art-track",
            headers={"Authorization": f"Bearer {admin_token}"},
            data={"artist": "Coti Ft. Enrique Iglesias"},
            files={"background_file": ("cover.jpg", b"cover", "image/jpeg")} if replace_cover else None,
        )

    assert res.status_code == 200, res.text
    assert enq.call_args.kwargs["tenant_id"] == owner["tenant_id"]
    assert enq.call_args.kwargs["plan"] == "1000"
    assert enq.call_args.kwargs["artist"] == "Coti Ft. Enrique Iglesias"
    assert enq.call_args.kwargs["art_track"] is True
    if replace_cover:
        assert save_cover.call_args.args[3] == owner["tenant_id"]
        assert save_cover.call_args.kwargs == {"persist_cache": False, "unique_filename": True}
        assert enq.call_args.kwargs["bg_r2_key"] == cover_key
    else:
        save_cover.assert_not_called()
    row = _read_job(job_id)
    assert row.status == "editing"
    assert row.tenant_id == owner["tenant_id"]
    assert row.user_id == owner["id"]
    audit = db.query(AuditLog).filter(AuditLog.action == "admin.cross_tenant_access").all()
    assert any(event.user_id == actor_id and event.detail == {
        "job_id": job_id, "job_tenant": owner["tenant_id"], "kind": "edit_art_track",
    } for event in audit)


def test_regular_user_cannot_edit_other_tenant_art_track(client, admin_token, user_token, monkeypatch):
    uid, tid = _admin_identity(client, admin_token)
    job_id = _seed_art_track_job(uid, tid)
    monkeypatch.setenv("ART_TRACK_GLOBALLY_ENABLED", "1")

    with patch("main.enqueue_pipeline") as enq:
        res = client.post(
            f"/jobs/{job_id}/edit-art-track",
            headers={"Authorization": f"Bearer {user_token}"},
            data={"effect": "bokeh"},
        )

    assert res.status_code == 404
    assert not enq.called
    assert _read_job(job_id).status == "pending_review"


@pytest.mark.parametrize("probe_result,status,code", [
    ("unavailable", 503, "art_track_storage_unavailable"),
    (None, 503, "art_track_storage_unavailable"),
    ("missing", 422, None),
])
def test_storage_preflight_distinguishes_unavailable_from_missing(
        client, admin_token, probe_result, status, code):
    import storage
    uid, tid = _admin_identity(client, admin_token)
    job_id = _seed_art_track_job(uid, tid, status="done", effect="snow")
    with patch("storage.is_enabled", return_value=True), patch(
        "main._bounded_storage_probe", new_callable=AsyncMock, return_value=probe_result,
    ) as probe, patch("storage.object_exists") as unbounded, patch("main.enqueue_pipeline") as enqueue:
        res = client.post(f"/jobs/{job_id}/edit-art-track",
                          headers={"Authorization": f"Bearer {admin_token}"},
                          data={"artist": "Changed", "effect": "bokeh"})
    assert res.status_code == status, res.text
    if code:
        assert res.json()["detail"]["code"] == code
    probe.assert_awaited_once_with(storage.object_status_bounded, _read_job(job_id).input_r2_key)
    unbounded.assert_not_called()
    enqueue.assert_not_called()
    row = _read_job(job_id)
    assert (row.artist, row.status, row.render_params["effect"]) == ("Amanda Pujó", "done", "snow")


def test_disconnect_rolls_back_entire_edit_and_returns_cors_error(client, admin_token, db):
    from database import Job, JobOutboxEvent
    from sqlalchemy.exc import OperationalError
    uid, tid = _admin_identity(client, admin_token)
    job_id = _seed_art_track_job(uid, tid, status="done", effect="snow")
    row = db.query(Job).filter(Job.job_id == job_id).one()
    row.s3_keys = {"video": "published.mp4"}
    db.commit()
    error = OperationalError("SELECT outbox", {}, Exception("SSL connection has been closed unexpectedly"))
    with patch("transactional_outbox.create_pipeline_outbox_event", side_effect=error), patch(
        "main.enqueue_pipeline"
    ) as enqueue, patch("main.delivery_freshness.mark_deliveries_stale") as stale, patch(
        "main._save_custom_background", return_value=("/tmp/cover.jpg", "new-cover.jpg")
    ):
        res = client.post(f"/jobs/{job_id}/edit-art-track",
                          headers={"Authorization": f"Bearer {admin_token}", "Origin": "http://localhost:5173"},
                          data={"artist": "Changed", "effect": "bokeh", "label_line": "New label"},
                          files={"background_file": ("cover.jpg", b"cover", "image/jpeg")})
    assert res.status_code == 503, res.text
    assert res.json()["detail"]["code"] == "art_track_save_unconfirmed"
    assert res.headers.get("access-control-allow-origin") in ("*", "http://localhost:5173")
    enqueue.assert_not_called()
    stale.assert_not_called()
    row = _read_job(job_id)
    assert row.artist == "Amanda Pujó"
    assert row.status == "done"
    assert row.render_params == {"art_track": True, "effect": "snow"}
    assert row.bg_r2_key_cached.endswith("bg_custom.jpg")
    assert row.s3_keys == {"video": "published.mp4"}
    assert db.query(JobOutboxEvent).filter(JobOutboxEvent.job_id == job_id).count() == 0


def test_external_io_has_no_open_transaction_and_stale_marker_follows_commit(
        client, admin_token, db):
    import main
    from database import Job, JobOutboxEvent, SessionLocal
    uid, tid = _admin_identity(client, admin_token)
    job_id = _seed_art_track_job(uid, tid, status="done")
    row = db.query(Job).filter(Job.job_id == job_id).one()
    row.s3_keys = {"video": "published.mp4"}
    db.commit()
    seen = []

    async def probe(*args):
        assert not db.in_transaction()
        seen.append("storage")
        return "exists"

    def cancel(*args):
        assert not db.in_transaction()
        seen.append("redis")

    def mark(*args):
        assert not db.in_transaction()
        with SessionLocal() as check:
            assert check.query(Job).filter(Job.job_id == job_id).one().status == "editing"
            assert check.query(JobOutboxEvent).filter(JobOutboxEvent.job_id == job_id).count() == 1
        seen.append("committed")

    main.app.dependency_overrides[main.get_db] = lambda: db
    try:
        with patch("storage.is_enabled", return_value=True), patch(
            "main._bounded_storage_probe", side_effect=probe
        ), patch("queue_jobs.cancel_rq_job", side_effect=cancel), patch(
            "main.delivery_freshness.mark_deliveries_stale", side_effect=mark
        ), patch("main.enqueue_pipeline"):
            res = client.post(f"/jobs/{job_id}/edit-art-track",
                              headers={"Authorization": f"Bearer {admin_token}"},
                              data={"artist": "Changed"})
        assert res.status_code == 200, res.text
        assert seen == ["storage", "redis", "redis", "committed"]
    finally:
        main.app.dependency_overrides.pop(main.get_db, None)


def test_concurrent_edit_during_preflight_is_not_overwritten(client, admin_token):
    from database import Job, SessionLocal
    uid, tid = _admin_identity(client, admin_token)
    job_id = _seed_art_track_job(uid, tid, status="done")

    async def probe(*args):
        with SessionLocal() as concurrent:
            row = concurrent.query(Job).filter(Job.job_id == job_id).one()
            row.artist = "Other operator's correction"
            concurrent.commit()
        return "exists"

    with patch("storage.is_enabled", return_value=True), patch(
        "main._bounded_storage_probe", side_effect=probe
    ), patch("main.enqueue_pipeline") as enqueue:
        res = client.post(f"/jobs/{job_id}/edit-art-track",
                          headers={"Authorization": f"Bearer {admin_token}"},
                          data={"artist": "Outdated correction"})
    assert res.status_code == 409, res.text
    enqueue.assert_not_called()
    row = _read_job(job_id)
    assert row.artist == "Other operator's correction"
    assert row.status == "done"


def test_disconnect_after_commit_reports_uncertain_save_without_duplicate_render(client, admin_token, db):
    from database import JobOutboxEvent
    from sqlalchemy.exc import OperationalError
    uid, tid = _admin_identity(client, admin_token)
    job_id = _seed_art_track_job(uid, tid, status="done")
    error = OperationalError("SELECT outbox", {}, Exception("SSL connection has been closed unexpectedly"))
    with patch("transactional_outbox.dispatch_outbox_event", side_effect=error), patch("main.enqueue_pipeline") as enqueue:
        res = client.post(f"/jobs/{job_id}/edit-art-track",
                          headers={"Authorization": f"Bearer {admin_token}"},
                          data={"artist": "Saved correction"})
    assert res.status_code == 503, res.text
    assert res.json()["detail"]["code"] == "art_track_save_unconfirmed"
    assert _read_job(job_id).artist == "Saved correction"
    assert _read_job(job_id).status == "editing"
    assert db.query(JobOutboxEvent).filter(JobOutboxEvent.job_id == job_id).count() == 1
    enqueue.assert_not_called()
