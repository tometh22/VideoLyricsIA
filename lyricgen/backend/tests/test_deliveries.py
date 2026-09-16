"""Tests for the UMG deliveries portal endpoints.

Scope:
  POST /admin/deliveries/from-job/{job_id}   — gating + replace-not-duplicate
  DELETE /admin/deliveries/{id}              — admin JWT path
  GET /api/deliveries/items                  — listing shape + portal token gate
  DELETE /api/deliveries/{id}                — portal token path

Why these tests matter:
  These endpoints are the only way to publish/delete from the UMG portal
  in v2 (the static items.json + gen_page.py flow is gone). A regression
  here means an admin can't ship corrected videos to Universal Music
  through the UI — the exact pain point the feature was meant to fix.

Test approach:
  We stub `storage.object_exists` to True so the POST handler doesn't
  reach R2. R2 is integration-tested elsewhere; we're isolating the
  delivery business logic here.
"""
from __future__ import annotations

import os
from datetime import datetime, timezone
from unittest.mock import patch

import pytest
from tests.conftest import auth


PORTAL_TOKEN = os.environ.get("DELIVERY_PORTAL_TOKEN", "test-portal-token")


@pytest.fixture(autouse=True)
def _portal_token_env():
    """Make sure DELIVERY_PORTAL_TOKEN is set during this module's tests
    so the public endpoints accept our test token. Restored after."""
    old = os.environ.get("DELIVERY_PORTAL_TOKEN")
    os.environ["DELIVERY_PORTAL_TOKEN"] = PORTAL_TOKEN
    yield
    if old is None:
        os.environ.pop("DELIVERY_PORTAL_TOKEN", None)
    else:
        os.environ["DELIVERY_PORTAL_TOKEN"] = old


@pytest.fixture
def approved_job(db, admin_token, client):
    """Create an approved job in the DB that the delivery endpoints can use.

    Bypasses /upload + worker — we just need a row with the right shape.
    """
    from database import Job, User
    # Admin user id from the token
    me = client.get("/auth/me", headers=auth(admin_token)).json()
    job = Job(
        job_id="testjob12345",
        user_id=me["id"],
        tenant_id="default",
        artist="Test Artist",
        song_title="Test Song",
        filename="test.mp3",
        status="done",
        delivery_profile="umg",
        umg_spec={"frame_size": "HD", "fps": 29.97},
        approved_by=me["id"],
        approved_at=datetime.now(timezone.utc),
        # Las tres URLs son parte de la forma REAL de un job entregable: el
        # pipeline las escribe en el mismo update_job(files=...) que dispara
        # la subida a R2. Sin ellas el fixture modelaba un job imposible
        # (done + approved y sin un solo entregable), y desde que
        # _deliverables_never_produced distingue "nunca se produjo" de
        # "todavía no está en R2" esa forma imposible cambiaba el resultado
        # de los tests de abajo.
        video_url=f"/download/testjob12345/video",
        short_url=f"/download/testjob12345/short",
        thumbnail_url=f"/download/testjob12345/thumbnail",
    )
    db.add(job)
    db.commit()
    db.refresh(job)
    yield job
    # Cleanup: remove the job + any deliveries we created against it.
    # Change requests reference deliveries via a FK (delivery_change_requests
    # _delivery_id_fkey) — Postgres enforces it even though the local SQLite
    # test DB silently ignores it, so deleting the CRs first is required or
    # this teardown 500s and leaves testjob12345 behind, breaking every
    # subsequent test that reuses this fixture's hardcoded job_id.
    from database import (
        ChangeRequestProposal, Delivery, DeliveryChangeRequest,
        EditorDocument, EditorVersion, JobOutboxEvent,
    )
    delivery_ids = [
        d.id for d in db.query(Delivery).filter(Delivery.job_id == "testjob12345").all()
    ]
    if delivery_ids:
        db.query(DeliveryChangeRequest).filter(
            DeliveryChangeRequest.delivery_id.in_(delivery_ids)
        ).delete(synchronize_session=False)
    db.query(ChangeRequestProposal).filter(
        ChangeRequestProposal.job_id == "testjob12345"
    ).delete(synchronize_session=False)
    db.query(JobOutboxEvent).filter(
        JobOutboxEvent.job_id == "testjob12345"
    ).delete(synchronize_session=False)
    db.query(EditorVersion).filter(
        EditorVersion.job_id == "testjob12345"
    ).delete(synchronize_session=False)
    db.query(EditorDocument).filter(
        EditorDocument.job_id == "testjob12345"
    ).delete(synchronize_session=False)
    db.query(Delivery).filter(Delivery.job_id == "testjob12345").delete()
    db.query(Job).filter(Job.id == job.id).delete()
    db.commit()


@pytest.fixture
def all_r2_files_present():
    """Stub storage.object_exists → True so the POST handler thinks the
    5 deliverable files are sitting in R2. Real R2 is hit in production."""
    with patch("main.storage.object_exists", return_value=True):
        yield


def test_admin_can_create_delivery(client, admin_token, approved_job, all_r2_files_present):
    res = client.post(
        f"/admin/deliveries/from-job/{approved_job.job_id}",
        headers=auth(admin_token),
        json={},
    )
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["ok"] is True
    assert body["job_id"] == approved_job.job_id
    assert body["label"] == "Renderizado"  # first delivery for this song
    assert body["replaced"] is False


def test_missing_prores_is_prepared_instead_of_returning_dead_end(
    client, admin_token, approved_job,
):
    """El prewarm post-render es best-effort. Si faltan sólo los .mov,
    Enviar a UMG los encola de forma explícita y responde 202 para que el
    cliente espere y reintente la publicación."""
    def object_exists(key):
        return not key.endswith(("umg_master.mov", "umg_short.mov"))

    with (
        patch("main.storage.object_exists", side_effect=object_exists),
        patch(
            "main.enqueue_prores_prewarm",
            side_effect=lambda _job_id, file_type, *, force=False: (
                f"rq:{file_type}" if force else None
            ),
        ) as enqueue,
    ):
        res = client.post(
            f"/admin/deliveries/from-job/{approved_job.job_id}",
            headers=auth(admin_token),
            json={},
        )

    assert res.status_code == 202, res.text
    body = res.json()
    assert body["status"] == "preparing_prores"
    assert body["missing"] == ["umg_master", "umg_short"]
    assert body["enqueued"] == ["umg_master", "umg_short"]
    assert enqueue.call_count == 2
    assert all(call.kwargs == {"force": True} for call in enqueue.call_args_list)


def test_youtube_only_job_requests_prores_configuration(
    client, admin_token, approved_job, db,
):
    approved_job.umg_spec = None
    approved_job.delivery_profile = "youtube"
    db.commit()

    def object_exists(key):
        return not key.endswith(("umg_master.mov", "umg_short.mov"))

    with (
        patch("main.storage.object_exists", side_effect=object_exists),
        patch("main.enqueue_prores_prewarm") as enqueue,
    ):
        res = client.post(
            f"/admin/deliveries/from-job/{approved_job.job_id}",
            headers=auth(admin_token),
            json={},
        )

    assert res.status_code == 409
    detail = res.json()["detail"]
    assert detail["code"] == "prores_required"
    assert detail["missing"] == ["umg_master", "umg_short"]
    enqueue.assert_not_called()


def test_missing_render_output_still_blocks_delivery(
    client, admin_token, approved_job,
):
    def object_exists(key):
        return not key.endswith("thumbnail.jpg")

    with (
        patch("main.storage.object_exists", side_effect=object_exists),
        patch("main.enqueue_prores_prewarm") as enqueue,
    ):
        res = client.post(
            f"/admin/deliveries/from-job/{approved_job.job_id}",
            headers=auth(admin_token),
            json={},
        )

    assert res.status_code == 409
    assert "thumbnail" in res.json()["detail"]
    enqueue.assert_not_called()


def test_non_admin_cannot_create_delivery(client, user_token, approved_job, all_r2_files_present):
    res = client.post(
        f"/admin/deliveries/from-job/{approved_job.job_id}",
        headers=auth(user_token),
        json={},
    )
    assert res.status_code == 403


def test_unapproved_job_rejected(client, admin_token, db, all_r2_files_present):
    from database import Job
    # Same shape as approved_job but status != done.
    me = client.get("/auth/me", headers=auth(admin_token)).json()
    job = Job(
        job_id="pending12345",
        user_id=me["id"],
        tenant_id="default",
        artist="Pending Artist",
        song_title="Pending Song",
        filename="pending.mp3",
        status="pending_review",
        delivery_profile="umg",
    )
    db.add(job)
    db.commit()
    try:
        res = client.post(
            f"/admin/deliveries/from-job/pending12345",
            headers=auth(admin_token),
            json={},
        )
        assert res.status_code == 400
        assert "approved" in res.json()["detail"].lower()
    finally:
        db.query(Job).filter(Job.id == job.id).delete()
        db.commit()


def test_resending_replaces_not_duplicates(client, admin_token, approved_job, all_r2_files_present):
    """Second POST for the same job_id should UPDATE the existing row, not
    create a new one. This was the manual replace-corrected-version workflow
    we used to do by hand in items.json."""
    res1 = client.post(
        f"/admin/deliveries/from-job/{approved_job.job_id}",
        headers=auth(admin_token), json={},
    )
    assert res1.status_code == 200
    first_id = res1.json()["delivery_id"]
    assert res1.json()["replaced"] is False

    res2 = client.post(
        f"/admin/deliveries/from-job/{approved_job.job_id}",
        headers=auth(admin_token), json={"label": "Renderizado v2"},
    )
    assert res2.status_code == 200
    assert res2.json()["replaced"] is True
    assert res2.json()["delivery_id"] == first_id
    assert res2.json()["label"] == "Renderizado v2"


def test_portal_token_required_for_items(client):
    res = client.get("/api/deliveries/items")  # no token
    assert res.status_code == 401


def test_portal_items_lists_active_deliveries(client, admin_token, approved_job, all_r2_files_present):
    # Publish first
    client.post(
        f"/admin/deliveries/from-job/{approved_job.job_id}",
        headers=auth(admin_token), json={},
    )
    # Then list
    res = client.get("/api/deliveries/items", headers={"X-Portal-Token": PORTAL_TOKEN})
    assert res.status_code == 200
    assert res.headers["cache-control"] == "private, no-store, max-age=0"
    assert res.headers["pragma"] == "no-cache"
    payload = res.json()
    assert "songs" in payload
    assert "file_type_labels" in payload
    # Find our test song in the listing
    songs = [s for s in payload["songs"] if s["artist"] == "Test Artist"]
    assert len(songs) == 1
    versions = songs[0]["versions"]
    assert len(versions) == 1
    v = versions[0]
    assert v["job_id"] == approved_job.job_id
    assert v["label"] == "Renderizado"
    # delivery_id is what the portal uses for DELETE
    assert isinstance(v.get("delivery_id"), int)
    # 5 files expected (umg_master, umg_short, video, short, thumbnail)
    assert len(v["files"]) == 5


def test_chile_portal_does_not_expose_non_chile_delivery(
    client, admin_token, approved_job, all_r2_files_present,
):
    """The Chile surface only exposes rows explicitly sent to Chile.

    The fixture is published with the default Argentina destination, which
    Chile must not inherit even when both portals use the same token.
    """
    client.post(
        f"/admin/deliveries/from-job/{approved_job.job_id}",
        headers=auth(admin_token), json={},
    )
    res = client.get(
        "/api/deliveries/items",
        headers={"X-Portal-Token": PORTAL_TOKEN, "X-Portal-Id": "chile"},
    )
    assert res.status_code == 200
    assert not any(
        song["artist"] == "Test Artist" for song in res.json()["songs"]
    )


def test_same_job_can_be_published_to_both_portals(
    client, admin_token, approved_job, all_r2_files_present,
):
    """The destination is part of the delivery identity, not the job.

    This is the regression test for the admin workflow: a video sent first to
    Argentina must remain independently publishable to Chile.
    """
    argentina = client.post(
        f"/admin/deliveries/from-job/{approved_job.job_id}",
        headers=auth(admin_token), json={},
    )
    chile = client.post(
        f"/admin/deliveries/from-job/{approved_job.job_id}",
        headers=auth(admin_token), json={"portal_id": "chile"},
    )
    assert argentina.status_code == 200, argentina.text
    assert chile.status_code == 200, chile.text
    assert argentina.json()["portal_id"] == "argentina"
    assert chile.json()["portal_id"] == "chile"
    assert argentina.json()["delivery_id"] != chile.json()["delivery_id"]

    argentina_items = client.get(
        "/api/deliveries/items",
        headers={"X-Portal-Token": PORTAL_TOKEN, "X-Portal-Id": "argentina"},
    ).json()
    chile_items = client.get(
        "/api/deliveries/items",
        headers={"X-Portal-Token": PORTAL_TOKEN, "X-Portal-Id": "chile"},
    ).json()
    assert any(song["artist"] == "Test Artist" for song in argentina_items["songs"])
    assert any(song["artist"] == "Test Artist" for song in chile_items["songs"])

    status = client.get(
        f"/status/{approved_job.job_id}", headers=auth(admin_token),
    ).json()
    assert set(status["umg_portals"]) == {"argentina", "chile"}


def test_chile_publish_accepts_source_from_any_tenant(
    client, admin_token, approved_job, all_r2_files_present,
):
    """The selected Chile destination, not the source tenant, controls visibility."""
    res = client.post(
        f"/admin/deliveries/from-job/{approved_job.job_id}",
        headers=auth(admin_token), json={"portal_id": "chile"},
    )
    assert res.status_code == 200, res.text
    items = client.get(
        "/api/deliveries/items",
        headers={"X-Portal-Token": PORTAL_TOKEN, "X-Portal-Id": "chile"},
    ).json()
    assert any(song["artist"] == "Test Artist" for song in items["songs"])


def test_portal_can_delete(client, admin_token, approved_job, all_r2_files_present):
    # Publish first
    res = client.post(
        f"/admin/deliveries/from-job/{approved_job.job_id}",
        headers=auth(admin_token), json={},
    )
    delivery_id = res.json()["delivery_id"]

    # Delete via portal endpoint
    res = client.delete(
        f"/api/deliveries/{delivery_id}",
        headers={"X-Portal-Token": PORTAL_TOKEN},
    )
    assert res.status_code == 200

    # Items endpoint should no longer return this delivery
    items = client.get("/api/deliveries/items", headers={"X-Portal-Token": PORTAL_TOKEN}).json()
    songs = [s for s in items["songs"] if s["artist"] == "Test Artist"]
    assert songs == []


def test_admin_delete_via_jwt(client, admin_token, approved_job, all_r2_files_present):
    res = client.post(
        f"/admin/deliveries/from-job/{approved_job.job_id}",
        headers=auth(admin_token), json={},
    )
    delivery_id = res.json()["delivery_id"]

    res = client.delete(f"/admin/deliveries/{delivery_id}", headers=auth(admin_token))
    assert res.status_code == 200


def test_portal_can_prepare_missing_prores_for_its_delivery(
    client, admin_token, approved_job, all_r2_files_present,
):
    published = client.post(
        f"/admin/deliveries/from-job/{approved_job.job_id}",
        headers=auth(admin_token), json={"portal_id": "chile"},
    )
    assert published.status_code == 200, published.text
    delivery_id = published.json()["delivery_id"]

    with patch("main.enqueue_prores_prewarm", return_value="prewarm:test") as enqueue:
        res = client.post(
            f"/api/deliveries/{delivery_id}/prepare-prores",
            headers={"X-Portal-Token": PORTAL_TOKEN, "X-Portal-Id": "chile"},
            json={"file_type": "umg_master"},
        )
    assert res.status_code == 202, res.text
    assert res.json()["status"] == "queued"
    enqueue.assert_called_once_with(approved_job.job_id, "umg_master", force=True)


def test_portal_can_prepare_staging_delivery_without_local_job(
    client, admin_token, approved_job, all_r2_files_present, db,
):
    """The shared portal DB contains deliveries created by staging, while
    production's jobs DB deliberately does not contain those Job rows."""
    from database import Job

    published = client.post(
        f"/admin/deliveries/from-job/{approved_job.job_id}",
        headers=auth(admin_token), json={"portal_id": "chile"},
    )
    assert published.status_code == 200, published.text
    delivery_id = published.json()["delivery_id"]
    db.query(Job).filter(Job.id == approved_job.id).delete()
    db.commit()

    with patch(
        "main.enqueue_delivery_prores_prewarm", return_value="portal-prewarm:test",
    ) as enqueue:
        res = client.post(
            f"/api/deliveries/{delivery_id}/prepare-prores",
            headers={"X-Portal-Token": PORTAL_TOKEN, "X-Portal-Id": "chile"},
            json={"file_type": "umg_master"},
        )

    assert res.status_code == 202, res.text
    assert res.json()["status"] == "queued"
    enqueue.assert_called_once_with(
        approved_job.job_id, "umg_master", "default", frame_size="HD",
    )


def test_portal_can_prepare_legacy_mp4_only_delivery(
    client, admin_token, approved_job, all_r2_files_present, db,
):
    published = client.post(
        f"/admin/deliveries/from-job/{approved_job.job_id}",
        headers=auth(admin_token), json={"portal_id": "chile"},
    )
    assert published.status_code == 200, published.text
    delivery_id = published.json()["delivery_id"]
    approved_job.umg_spec = None
    db.commit()

    with patch(
        "main.enqueue_delivery_prores_prewarm", return_value="portal-prewarm:test",
    ) as enqueue:
        res = client.post(
            f"/api/deliveries/{delivery_id}/prepare-prores",
            headers={"X-Portal-Token": PORTAL_TOKEN, "X-Portal-Id": "chile"},
            json={"file_type": "umg_short"},
        )

    assert res.status_code == 202, res.text
    enqueue.assert_called_once_with(
        approved_job.job_id, "umg_short", "default", frame_size="HD",
    )


def test_portal_cannot_prepare_prores_from_the_other_portal(
    client, admin_token, approved_job, all_r2_files_present,
):
    published = client.post(
        f"/admin/deliveries/from-job/{approved_job.job_id}",
        headers=auth(admin_token), json={"portal_id": "chile"},
    )
    assert published.status_code == 200, published.text
    chile_delivery_id = published.json()["delivery_id"]
    with patch("main.enqueue_prores_prewarm"):
        res = client.post(
            f"/api/deliveries/{chile_delivery_id}/prepare-prores",
            headers={"X-Portal-Token": PORTAL_TOKEN, "X-Portal-Id": "argentina"},
            json={"file_type": "umg_master"},
        )
    assert res.status_code == 404


def test_status_endpoint_includes_is_in_umg_portal(
    client, admin_token, approved_job, all_r2_files_present,
):
    """The frontend reads this flag to decide if the "Enviar a UMG" button
    should render as "✓ Ya en UMG" or as the active call-to-action."""
    # Before publish: false
    before = client.get(f"/status/{approved_job.job_id}", headers=auth(admin_token)).json()
    assert before.get("is_in_umg_portal") is False

    # After publish: true
    client.post(
        f"/admin/deliveries/from-job/{approved_job.job_id}",
        headers=auth(admin_token), json={},
    )
    after = client.get(f"/status/{approved_job.job_id}", headers=auth(admin_token)).json()
    assert after.get("is_in_umg_portal") is True


def test_deliveries_routed_to_external_db(
    client, admin_token, approved_job, all_r2_files_present, db, tmp_path, monkeypatch,
):
    """Cross-env: cuando get_deliveries_db apunta a otra DB (simula staging
    publicando en el portal de prod), la fila Delivery se escribe en esa DB
    EXTERNA —no en la local—, con added_by_user_id mapeado a un admin de esa
    DB. El Job se lee de la local; is_in_umg_portal lee de la externa."""
    import main
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from database import Base, Delivery, User, get_deliveries_db

    ext_engine = create_engine(
        f"sqlite:///{tmp_path}/external.db",
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(bind=ext_engine)
    ExtSession = sessionmaker(bind=ext_engine, autoflush=False, expire_on_commit=False)

    # "Admin de prod" en la DB externa; las escrituras de deliveries mapean a él.
    ext = ExtSession()
    ext_admin = User(username="prod_admin_ext", hashed_password="x", role="admin")
    ext.add(ext_admin)
    ext.commit()
    ext_admin_id = ext_admin.id
    ext.close()

    def _override_ddb():
        s = ExtSession()
        try:
            yield s
        finally:
            s.close()

    main.app.dependency_overrides[get_deliveries_db] = _override_ddb
    monkeypatch.setattr(main, "deliveries_added_by", lambda _local_id: ext_admin_id)
    try:
        res = client.post(
            f"/admin/deliveries/from-job/{approved_job.job_id}",
            headers=auth(admin_token), json={},
        )
        assert res.status_code == 200, res.text

        # La fila vive en la DB EXTERNA, con el added_by mapeado (no el local).
        ext = ExtSession()
        rows = ext.query(Delivery).filter(Delivery.job_id == approved_job.job_id).all()
        assert len(rows) == 1
        assert rows[0].added_by_user_id == ext_admin_id
        ext.close()

        # ...y NO en la DB local.
        local_count = db.query(Delivery).filter(Delivery.job_id == approved_job.job_id).count()
        assert local_count == 0

        # is_in_umg_portal lo lee de la externa (job aprobado → sí consulta).
        st = client.get(f"/status/{approved_job.job_id}", headers=auth(admin_token)).json()
        assert st.get("is_in_umg_portal") is True

        # El listado del portal (también ruteado a la externa) lo muestra.
        items = client.get(
            "/api/deliveries/items", headers={"X-Portal-Token": PORTAL_TOKEN}
        ).json()
        assert any(s["artist"] == "Test Artist" for s in items["songs"])
    finally:
        main.app.dependency_overrides.pop(get_deliveries_db, None)
        Base.metadata.drop_all(bind=ext_engine)


# ---------------------------------------------------------------------------
# Change requests: UMG pide correcciones desde el portal (POST .../change-
# request), el operador las resuelve desde el admin (GET/POST /admin/
# change-requests). Panel re-agregado 2026-07-24 tras detectar que se había
# sacado la UI (2026-06-02) pero el portal seguía ofreciendo el botón — los
# pedidos se guardaban sin que nadie los viera.
# ---------------------------------------------------------------------------

def test_change_request_requires_portal_token(client, admin_token, approved_job, all_r2_files_present):
    client.post(f"/admin/deliveries/from-job/{approved_job.job_id}",
                headers=auth(admin_token), json={})
    items = client.get("/api/deliveries/items", headers={"X-Portal-Token": PORTAL_TOKEN}).json()
    delivery_id = items["songs"][0]["versions"][0]["delivery_id"]

    res = client.post(f"/api/deliveries/{delivery_id}/change-request",
                       json={"comment": "poner la tipografía más grande"})
    assert res.status_code == 401


def test_change_request_empty_comment_rejected(client, admin_token, approved_job, all_r2_files_present):
    client.post(f"/admin/deliveries/from-job/{approved_job.job_id}",
                headers=auth(admin_token), json={})
    items = client.get("/api/deliveries/items", headers={"X-Portal-Token": PORTAL_TOKEN}).json()
    delivery_id = items["songs"][0]["versions"][0]["delivery_id"]

    res = client.post(
        f"/api/deliveries/{delivery_id}/change-request",
        headers={"X-Portal-Token": PORTAL_TOKEN},
        json={"comment": "   "},
    )
    assert res.status_code == 400


def test_change_request_submit_and_admin_lists_it(
    client, admin_token, approved_job, all_r2_files_present,
):
    """El pedido llega al portal, y el admin lo ve pendiente hasta resolverlo."""
    client.post(f"/admin/deliveries/from-job/{approved_job.job_id}",
                headers=auth(admin_token), json={})
    items = client.get("/api/deliveries/items", headers={"X-Portal-Token": PORTAL_TOKEN}).json()
    delivery_id = items["songs"][0]["versions"][0]["delivery_id"]

    with patch("main.emails.send_umg_change_request_notification") as mock_notify:
        res = client.post(
            f"/api/deliveries/{delivery_id}/change-request",
            headers={"X-Portal-Token": PORTAL_TOKEN},
            json={"comment": "poner la tipografía más grande"},
        )
        assert res.status_code == 200, res.text
        cr_id = res.json()["id"]

        # Fire-and-forget en un thread daemon — darle un instante a correr.
        import time
        time.sleep(0.2)

    # La notificación se disparó con el contexto correcto (best-effort:
    # nunca debe tirar la request aunque SMTP no esté configurado en test).
    mock_notify.assert_called_once()
    call_args = mock_notify.call_args.args
    assert call_args[0] == "Test Artist"        # artist
    assert call_args[2] == "poner la tipografía más grande"  # comment
    assert call_args[3] == delivery_id          # delivery_id
    assert call_args[4] == approved_job.job_id  # job_id

    # El admin lo ve como pendiente.
    pending = client.get("/admin/change-requests?status=pending",
                          headers=auth(admin_token)).json()
    assert pending["pending_count"] == 1
    assert pending["resolved_count"] == 0
    item = next(i for i in pending["items"] if i["id"] == cr_id)
    assert item["comment"] == "poner la tipografía más grande"
    assert item["delivery"]["artist"] == "Test Artist"
    assert item["delivery"]["job_id"] == approved_job.job_id
    assert item["resolved_at"] is None

    # Resolver lo saca de "pending" y lo pasa a "resolved".
    res = client.post(f"/admin/change-requests/{cr_id}/resolve",
                       headers=auth(admin_token),
                       json={"resolution_note": "re-renderizado con fuente más grande"})
    assert res.status_code == 200

    pending = client.get("/admin/change-requests?status=pending",
                          headers=auth(admin_token)).json()
    assert pending["pending_count"] == 0

    resolved = client.get("/admin/change-requests?status=resolved",
                           headers=auth(admin_token)).json()
    assert resolved["resolved_count"] == 1
    resolved_item = next(i for i in resolved["items"] if i["id"] == cr_id)
    assert resolved_item["resolution_note"] == "re-renderizado con fuente más grande"

    # Reabrir lo vuelve a poner pendiente.
    res = client.post(f"/admin/change-requests/{cr_id}/reopen", headers=auth(admin_token))
    assert res.status_code == 200
    pending = client.get("/admin/change-requests?status=pending",
                          headers=auth(admin_token)).json()
    assert pending["pending_count"] == 1


def test_change_request_proposal_applies_revisioned_text_but_stays_open_until_publish(
    client, admin_token, approved_job, all_r2_files_present, db, monkeypatch,
):
    monkeypatch.setenv("CHANGE_REQUEST_ASSIST_ENABLED", "1")
    monkeypatch.setenv("CHANGE_REQUEST_APPLY_ENABLED", "1")
    approved_job.segments_json = [
        {"start": 0.0, "end": 2.0, "text": "Texto equivocado"},
        {"start": 2.2, "end": 4.0, "text": "Otra línea"},
    ]
    approved_job.segments_revision = 0
    db.commit()
    delivery_id = client.post(
        f"/admin/deliveries/from-job/{approved_job.job_id}",
        headers=auth(admin_token), json={},
    ).json()["delivery_id"]
    with patch("main.emails.send_umg_change_request_notification"):
        cr_id = client.post(
            f"/api/deliveries/{delivery_id}/change-request",
            headers={"X-Portal-Token": PORTAL_TOKEN},
            json={"comment": "0:01 Texto correcto"},
        ).json()["id"]

    generated = client.post(
        f"/admin/change-requests/{cr_id}/proposals",
        headers=auth(admin_token),
    )
    assert generated.status_code == 200, generated.text
    proposal = generated.json()["proposal"]
    operation_ids = [
        row["id"] for row in proposal["operations"] if row["applicable"]
    ]
    assert len(operation_ids) == 1

    with patch("main._dispatch_editor_quality_outbox"):
        applied = client.post(
            f"/admin/change-requests/{cr_id}/proposals/{proposal['id']}/apply",
            headers=auth(admin_token),
            json={
                "base_revision": proposal["base_revision"],
                "operation_ids": operation_ids,
                "idempotency_key": "test-change-request-apply-0001",
            },
        )
    assert applied.status_code == 200, applied.text
    db.expire_all()
    from database import DeliveryChangeRequest, Job
    job = db.query(Job).filter(Job.job_id == approved_job.job_id).one()
    request = db.query(DeliveryChangeRequest).filter(
        DeliveryChangeRequest.id == cr_id
    ).one()
    assert job.segments_json[0]["text"] == "Texto correcto"
    assert job.segments_revision == 1
    assert request.resolved_at is None

    # Network retries with the same key must not create another editor
    # revision or lose the editor handoff URL.
    with patch("main._dispatch_editor_quality_outbox"):
        repeated = client.post(
            f"/admin/change-requests/{cr_id}/proposals/{proposal['id']}/apply",
            headers=auth(admin_token),
            json={
                "base_revision": proposal["base_revision"],
                "operation_ids": operation_ids,
                "idempotency_key": "test-change-request-apply-0001",
            },
        )
    assert repeated.status_code == 200, repeated.text
    assert repeated.json()["idempotent"] is True
    assert repeated.json()["revision"] == 1
    assert repeated.json()["editor_url"].startswith(
        f"/videos/{approved_job.job_id}/edit-lyrics"
    )
    db.expire_all()
    assert db.query(Job).filter(Job.job_id == approved_job.job_id).one().segments_revision == 1


def test_change_request_dismissed_proposal_can_be_recalculated(
    client, admin_token, approved_job, all_r2_files_present, db, monkeypatch,
):
    monkeypatch.setenv("CHANGE_REQUEST_ASSIST_ENABLED", "1")
    approved_job.segments_json = [{"start": 0.0, "end": 2.0, "text": "Viejo"}]
    approved_job.segments_revision = 0
    db.commit()
    delivery_id = client.post(
        f"/admin/deliveries/from-job/{approved_job.job_id}",
        headers=auth(admin_token), json={},
    ).json()["delivery_id"]
    with patch("main.emails.send_umg_change_request_notification"):
        cr_id = client.post(
            f"/api/deliveries/{delivery_id}/change-request",
            headers={"X-Portal-Token": PORTAL_TOKEN},
            json={"comment": '0:01 debe decir "Nuevo"'},
        ).json()["id"]
    proposal = client.post(
        f"/admin/change-requests/{cr_id}/proposals", headers=auth(admin_token),
    ).json()["proposal"]
    dismissed = client.post(
        f"/admin/change-requests/{cr_id}/proposals/{proposal['id']}/dismiss",
        headers=auth(admin_token), json={"reason": "incorrect_parse"},
    )
    assert dismissed.status_code == 200, dismissed.text

    recalculated = client.post(
        f"/admin/change-requests/{cr_id}/proposals", headers=auth(admin_token),
    )
    assert recalculated.status_code == 200, recalculated.text
    payload = recalculated.json()
    assert payload["recalculated"] is True
    assert payload["proposal"]["id"] == proposal["id"]
    assert payload["proposal"]["status"] == "ready"


def test_change_request_apply_rejects_stale_editor_revision(
    client, admin_token, approved_job, all_r2_files_present, db, monkeypatch,
):
    monkeypatch.setenv("CHANGE_REQUEST_ASSIST_ENABLED", "1")
    monkeypatch.setenv("CHANGE_REQUEST_APPLY_ENABLED", "1")
    approved_job.segments_json = [{"start": 0.0, "end": 2.0, "text": "Viejo"}]
    approved_job.segments_revision = 0
    db.commit()
    delivery_id = client.post(
        f"/admin/deliveries/from-job/{approved_job.job_id}",
        headers=auth(admin_token), json={},
    ).json()["delivery_id"]
    with patch("main.emails.send_umg_change_request_notification"):
        cr_id = client.post(
            f"/api/deliveries/{delivery_id}/change-request",
            headers={"X-Portal-Token": PORTAL_TOKEN},
            json={"comment": '0:01 debe decir "Nuevo"'},
        ).json()["id"]
    proposal = client.post(
        f"/admin/change-requests/{cr_id}/proposals", headers=auth(admin_token),
    ).json()["proposal"]
    operation_id = next(row["id"] for row in proposal["operations"] if row["applicable"])

    # Simulate a concurrent editor save after proposal generation.
    approved_job.segments_revision = 1
    approved_job.segments_json = [{"start": 0.0, "end": 2.0, "text": "Edición humana"}]
    db.commit()
    response = client.post(
        f"/admin/change-requests/{cr_id}/proposals/{proposal['id']}/apply",
        headers=auth(admin_token),
        json={
            "base_revision": proposal["base_revision"],
            "operation_ids": [operation_id],
            "idempotency_key": "test-change-request-stale-0001",
        },
    )
    assert response.status_code == 409
    assert "stale" in str(response.json()["detail"])


def test_change_request_notification_failure_does_not_break_submit(
    client, admin_token, approved_job, all_r2_files_present,
):
    """Si el envío de mail explota (SMTP caído, etc.) el pedido se guarda
    igual — la notificación es best-effort, nunca debe tirar la request de
    UMG. La excepción queda contenida en el thread daemon."""
    client.post(f"/admin/deliveries/from-job/{approved_job.job_id}",
                headers=auth(admin_token), json={})
    items = client.get("/api/deliveries/items", headers={"X-Portal-Token": PORTAL_TOKEN}).json()
    delivery_id = items["songs"][0]["versions"][0]["delivery_id"]

    with patch("main.emails.send_umg_change_request_notification",
               side_effect=RuntimeError("smtp down")):
        res = client.post(
            f"/api/deliveries/{delivery_id}/change-request",
            headers={"X-Portal-Token": PORTAL_TOKEN},
            json={"comment": "cambiar el fondo"},
        )
        assert res.status_code == 200, res.text


# ---------------------------------------------------------------------------
# Entrega PARCIAL — un job puede terminar bien sin short y/o sin thumbnail
# (incidente UMG Chile 2026-08-21, ver pipeline._accessory_failed).
# ---------------------------------------------------------------------------

def test_job_sin_short_se_puede_entregar_igual(client, admin_token, approved_job, db):
    """El caso que motivó todo: el master salió perfecto y el short falló.

    Antes esto era un callejón sin salida — el gate exigía los 5 archivos y
    respondía 409 "Files not yet in R2: short. Wait for the render to
    finish" PARA SIEMPRE, esperando algo que ya se sabía que no iba a
    existir. El operador no tenía forma de entregarle a UMG un video que
    estaba impecable.
    """
    approved_job.short_url = None  # el pipeline lo dejó en NULL: no hay short
    db.commit()

    def object_exists(key):
        return not key.endswith(("short.mp4", "umg_short.mov"))

    with (
        patch("main.storage.object_exists", side_effect=object_exists),
        patch("main.enqueue_prores_prewarm") as enqueue,
    ):
        res = client.post(
            f"/admin/deliveries/from-job/{approved_job.job_id}",
            headers=auth(admin_token), json={},
        )

    assert res.status_code == 200, res.text
    enqueue.assert_not_called()
    items = client.get("/api/deliveries/items",
                       headers={"X-Portal-Token": PORTAL_TOKEN}).json()
    tipos = {f["type"] for f in items["songs"][0]["versions"][0]["files"]}
    assert tipos == {"umg_master", "video", "thumbnail"}, (
        "la entrega parcial no debe ofrecer short ni umg_short"
    )


def test_short_ausente_en_R2_pero_columna_seteada_sigue_bloqueando(
    client, admin_token, approved_job,
):
    """La contracara, y la razón de exigir DOS señales.

    Si la columna está seteada, el job SÍ produjo el short: que no esté en
    R2 significa "el render/upload todavía no terminó" o "algo se rompió" —
    los dos casos donde el 409 es la respuesta correcta. Sin este chequeo
    habríamos entregado a UMG sin el short, en silencio, cada vez que un
    upload iba con retraso.
    """
    def object_exists(key):
        return not key.endswith("short.mp4")

    with (
        patch("main.storage.object_exists", side_effect=object_exists),
        patch("main.enqueue_prores_prewarm"),
    ):
        res = client.post(
            f"/admin/deliveries/from-job/{approved_job.job_id}",
            headers=auth(admin_token), json={},
        )

    assert res.status_code == 409
    assert "short" in res.json()["detail"]


def test_media_token_404_cuando_el_entregable_no_existe(
    client, admin_token, approved_job, db,
):
    """Sin esto, /media-token entregaba token SIEMPRE, la URL resultante era
    truthy y todos los guards `url && ...` del frontend eran decorativos: el
    tab "Short" montaba un <video> que 404eaba y BatchProgress contaba 0
    fallos porque `<a download>.click()` no puede leer el status HTTP."""
    approved_job.short_url = None
    db.commit()

    res = client.get(f"/media-token/{approved_job.job_id}/short",
                     headers=auth(admin_token))
    assert res.status_code == 404

    # El master sigue disponible: la entrega es parcial, no está rota.
    res = client.get(f"/media-token/{approved_job.job_id}/video",
                     headers=auth(admin_token))
    assert res.status_code == 200, res.text


# ───────────────────────────────────────────────────────────────────────────
# Ciclo de vida de una corrección: editar → publicar → el cliente revisa.
#
# El portal reconstruye la key de R2 y la firma, y el render escribe en esa
# misma key. Así que una corrección llega al cliente sin link nuevo — lo que
# queremos — y sin rastro en ninguna fila: mismo label, misma fecha, misma
# pastilla verde de "aprobado" sobre un corte que nunca vio. Estos tests
# fijan las dos consecuencias que se vieron en producción.
# ───────────────────────────────────────────────────────────────────────────

def _edit_the_render(db, job):
    """Simula lo que deja un re-render de edición en la fila del job.

    run_edit_pipeline archiva los entregables previos, re-sube el MP4 e
    invalida las keys del ProRes; el prewarm las reescribe cuando el master
    fresco está arriba. Reproducir esa forma es lo que hace verificable el
    estado intermedio.
    """
    job.edit_count = (job.edit_count or 0) + 1
    job.segments_revision = (job.segments_revision or 0) + 1
    job.previous_versions = (job.previous_versions or []) + [{
        "version": len(job.previous_versions or []) + 1,
        "archived_at": datetime.now(timezone.utc).isoformat(),
        "keys": {"video": "default/testjob12345/lyric_video.mp4.v1"},
    }]
    db.commit()


def test_stale_prores_blocks_publication_instead_of_shipping_a_mismatched_pair(
    client, admin_token, approved_job, db,
):
    """El .mov PRE-EDIT sigue en su key y contesta el HEAD.

    Con la sola prueba de existencia el gate daba OK y el portal entregaba
    el master viejo al lado del MP4 nuevo (incidente 2026-08-03). El oráculo
    es la fila: el edit borró s3_keys["umg_master"] y el prewarm todavía no
    la reescribió.
    """
    approved_job.s3_keys = {
        "video": "default/testjob12345/lyric_video.mp4",
        "short": "default/testjob12345/short.mp4",
        "thumbnail": "default/testjob12345/thumbnail.jpg",
        "umg_short": "default/testjob12345/umg_short.mov",
    }
    _edit_the_render(db, approved_job)

    with (
        # TODOS los objetos están en R2 — incluido el master viejo.
        patch("main.storage.object_exists", return_value=True),
        patch(
            "main.enqueue_prores_prewarm",
            side_effect=lambda _job_id, file_type, *, force=False: f"rq:{file_type}",
        ) as enqueue,
    ):
        res = client.post(
            f"/admin/deliveries/from-job/{approved_job.job_id}",
            headers=auth(admin_token), json={},
        )

    assert res.status_code == 202, res.text
    body = res.json()
    assert body["status"] == "preparing_prores"
    assert body["stale"] == ["umg_master"]
    assert body["missing"] == ["umg_master"]
    enqueue.assert_called_once_with(approved_job.job_id, "umg_master", force=True)


def test_publishing_a_corrected_cut_reopens_the_client_review(
    client, admin_token, approved_job, db, all_r2_files_present,
):
    """Contenido nuevo = versión nueva, aprobación de baja, pedido cerrado.

    Los tres eran manuales o no existían: el cliente veía su pedido
    "pendiente" y su propia pastilla verde sobre una corrección que nunca
    revisó, sin nada que lo invitara a volver a bajar el archivo.
    """
    from database import Delivery, DeliveryChangeRequest

    first = client.post(
        f"/admin/deliveries/from-job/{approved_job.job_id}",
        headers=auth(admin_token), json={},
    )
    assert first.status_code == 200, first.text
    assert first.json()["revision"] == 1
    delivery_id = first.json()["delivery_id"]

    # El cliente aprueba y después pide un cambio sobre esa versión.
    assert client.post(
        f"/api/deliveries/{delivery_id}/approve",
        headers={"X-Portal-Token": PORTAL_TOKEN}, json={},
    ).status_code == 200
    with patch("main.emails.send_umg_change_request_notification"):
        cr_id = client.post(
            f"/api/deliveries/{delivery_id}/change-request",
            headers={"X-Portal-Token": PORTAL_TOKEN},
            json={"comment": "falta una línea en el estribillo"},
        ).json()["id"]

    _edit_the_render(db, approved_job)

    second = client.post(
        f"/admin/deliveries/from-job/{approved_job.job_id}",
        headers=auth(admin_token), json={},
    )
    assert second.status_code == 200, second.text
    body = second.json()
    assert body["replaced"] is True
    assert body["content_changed"] is True
    assert body["revision"] == 2
    assert body["resolved_change_requests"] == [cr_id]

    row = db.query(Delivery).filter(Delivery.id == delivery_id).first()
    db.refresh(row)
    assert row.published_revision == 2
    assert row.content_updated_at is not None
    # La aprobación era sobre el corte anterior: el portal vuelve a ofrecer
    # Aprobar / Rechazar sin tocar nada del lado del cliente.
    assert row.approved_at is None
    assert row.approved_by_label is None

    cr = db.query(DeliveryChangeRequest).filter(DeliveryChangeRequest.id == cr_id).first()
    db.refresh(cr)
    assert cr.resolved_at is not None
    assert cr.resolved_by_revision == 2
    assert cr.resolution_source == "publication"


def test_resending_the_same_cut_keeps_the_approval_and_the_open_request(
    client, admin_token, approved_job, db, all_r2_files_present,
):
    """El contrapeso del test anterior.

    Si un reenvío contara como versión nueva, cada doble clic anularía una
    aprobación legítima de UMG y les pediría revisar de nuevo algo idéntico.
    """
    from database import Delivery, DeliveryChangeRequest

    delivery_id = client.post(
        f"/admin/deliveries/from-job/{approved_job.job_id}",
        headers=auth(admin_token), json={},
    ).json()["delivery_id"]
    client.post(
        f"/api/deliveries/{delivery_id}/approve",
        headers={"X-Portal-Token": PORTAL_TOKEN}, json={},
    )
    with patch("main.emails.send_umg_change_request_notification"):
        cr_id = client.post(
            f"/api/deliveries/{delivery_id}/change-request",
            headers={"X-Portal-Token": PORTAL_TOKEN},
            json={"comment": "consulta, no cambio"},
        ).json()["id"]

    again = client.post(
        f"/admin/deliveries/from-job/{approved_job.job_id}",
        headers=auth(admin_token), json={},
    )
    assert again.status_code == 200, again.text
    assert again.json()["content_changed"] is False
    assert again.json()["revision"] == 1
    assert again.json()["resolved_change_requests"] == []

    row = db.query(Delivery).filter(Delivery.id == delivery_id).first()
    db.refresh(row)
    assert row.published_revision == 1
    assert row.approved_at is not None

    cr = db.query(DeliveryChangeRequest).filter(DeliveryChangeRequest.id == cr_id).first()
    db.refresh(cr)
    assert cr.resolved_at is None


def test_a_row_published_before_fingerprints_existed_keeps_its_approval(
    client, admin_token, approved_job, db, all_r2_files_present,
):
    """Migración: filas viejas no tienen con qué comparar.

    Tratarlas como contenido nuevo habría dado de baja, de una sola vez,
    todas las aprobaciones vigentes del portal.
    """
    from database import Delivery

    delivery_id = client.post(
        f"/admin/deliveries/from-job/{approved_job.job_id}",
        headers=auth(admin_token), json={},
    ).json()["delivery_id"]
    client.post(
        f"/api/deliveries/{delivery_id}/approve",
        headers={"X-Portal-Token": PORTAL_TOKEN}, json={},
    )
    row = db.query(Delivery).filter(Delivery.id == delivery_id).first()
    row.published_render_fingerprint = None
    db.commit()

    _edit_the_render(db, approved_job)
    again = client.post(
        f"/admin/deliveries/from-job/{approved_job.job_id}",
        headers=auth(admin_token), json={},
    )
    assert again.json()["content_changed"] is False
    db.refresh(row)
    assert row.approved_at is not None


def test_portal_listing_tells_the_client_there_is_a_new_version(
    client, admin_token, approved_job, db, all_r2_files_present,
):
    """Sin estos campos el cliente no tenía UN indicio de que el archivo
    detrás de su descarga cambió: mismo label, misma fecha, mismo peso
    (cacheado 30 días) y su propia aprobación intacta."""
    delivery_id = client.post(
        f"/admin/deliveries/from-job/{approved_job.job_id}",
        headers=auth(admin_token), json={},
    ).json()["delivery_id"]
    client.post(
        f"/api/deliveries/{delivery_id}/approve",
        headers={"X-Portal-Token": PORTAL_TOKEN}, json={},
    )

    _edit_the_render(db, approved_job)
    client.post(
        f"/admin/deliveries/from-job/{approved_job.job_id}",
        headers=auth(admin_token), json={},
    )

    items = client.get(
        "/api/deliveries/items", headers={"X-Portal-Token": PORTAL_TOKEN},
    ).json()
    version = next(
        v for song in items["songs"] for v in song["versions"]
        if v["delivery_id"] == delivery_id
    )
    assert version["revision"] == 2
    assert version["content_updated_at"] is not None
    assert version["awaiting_review"] is True
    assert version["approved_at"] is None
    # Publicar cierra la ventana de "se están aplicando cambios".
    assert version["updating"] is False


def test_requesting_a_re_render_marks_the_publication_as_updating(
    client, admin_token, approved_job, db, all_r2_files_present,
):
    """La ventana honesta: desde que se pide el re-render, el portal deja de
    presentar la descarga como final. El MP4 se reemplaza minutos antes que
    el master, así que el aviso tiene que empezar antes, no después."""
    from database import Delivery

    delivery_id = client.post(
        f"/admin/deliveries/from-job/{approved_job.job_id}",
        headers=auth(admin_token), json={},
    ).json()["delivery_id"]

    # Sin entregables trackeados no hay nada publicado que marcar; con
    # s3_keys el /retry sabe que sí.
    approved_job.s3_keys = {"video": "default/testjob12345/lyric_video.mp4"}
    approved_job.input_r2_key = "inputs/default/testjob12345/song.mp3"
    approved_job.status = "error"
    db.commit()

    with patch("main.enqueue_pipeline", return_value="rq:1"):
        retry = client.post(
            f"/retry/{approved_job.job_id}", headers=auth(admin_token),
        )
    assert retry.status_code in (200, 202), retry.text

    row = db.query(Delivery).filter(Delivery.id == delivery_id).first()
    db.refresh(row)
    assert row.stale_since is not None
    assert row.stale_reason == "editing"

    items = client.get(
        "/api/deliveries/items", headers={"X-Portal-Token": PORTAL_TOKEN},
    ).json()
    version = next(
        v for song in items["songs"] for v in song["versions"]
        if v["delivery_id"] == delivery_id
    )
    assert version["updating"] is True
    assert version["updating_reason"] == "editing"


def test_a_dead_re_render_stops_promising_the_client_work_in_progress(
    client, admin_token, approved_job, db, all_r2_files_present,
):
    """Si el edit muere, la fila sigue marcada para el operador pero el
    portal deja de decir "estamos aplicando cambios": prometerle trabajo en
    curso a un cliente cuando nadie está trabajando es peor que no decir
    nada, y no tiene forma de destrabarse solo."""
    from database import Delivery

    delivery_id = client.post(
        f"/admin/deliveries/from-job/{approved_job.job_id}",
        headers=auth(admin_token), json={},
    ).json()["delivery_id"]

    row = db.query(Delivery).filter(Delivery.id == delivery_id).first()
    row.stale_since = datetime.now(timezone.utc)
    row.stale_reason = "edit_failed"
    db.commit()

    version = next(
        v for song in client.get(
            "/api/deliveries/items", headers={"X-Portal-Token": PORTAL_TOKEN},
        ).json()["songs"] for v in song["versions"]
        if v["delivery_id"] == delivery_id
    )
    assert version["updating"] is False
    # El operador sí lo ve: el motivo viaja igual.
    assert version["updating_reason"] == "edit_failed"


def test_stale_master_without_a_prores_spec_says_what_is_actually_wrong(
    client, admin_token, approved_job, db,
):
    """Los dos casos frenan, pero no significan lo mismo.

    Entrega 289 (2026-09-15): `delivery_profile="youtube"` y `umg_spec` en
    JSON null sobre un job que YA se entregó como UMG y cuyo master de 4,3 GB
    sigue descargable, del corte anterior. Decirle al operador "este video fue
    generado sólo para YouTube" lo manda a buscar el problema al lugar
    equivocado mientras el cliente se lleva el archivo viejo.
    """
    approved_job.umg_spec = None
    approved_job.delivery_profile = "youtube"
    approved_job.s3_keys = {
        "video": "default/testjob12345/lyric_video.mp4",
        "short": "default/testjob12345/short.mp4",
        "thumbnail": "default/testjob12345/thumbnail.jpg",
    }
    _edit_the_render(db, approved_job)

    with (
        # El .mov viejo sigue en R2 y contesta el HEAD.
        patch("main.storage.object_exists", return_value=True),
        patch("main.enqueue_prores_prewarm") as enqueue,
    ):
        res = client.post(
            f"/admin/deliveries/from-job/{approved_job.job_id}",
            headers=auth(admin_token), json={},
        )

    assert res.status_code == 409, res.text
    detail = res.json()["detail"]
    assert detail["code"] == "prores_stale_without_spec"
    assert detail["stale"] == ["umg_master", "umg_short"]
    assert "ANTES de la edición" in detail["message"]
    # No se encola nada: sin spec no hay con qué transcodificar.
    enqueue.assert_not_called()


def test_republishing_does_not_rename_the_delivery(
    client, admin_token, approved_job, db, all_r2_files_present,
):
    """Visto en vivo al reparar la entrega 289 (2026-09-15).

    `_compute_default_delivery_label` cuenta las entregas activas de esa
    canción, y la fila que se está actualizando se cuenta a sí misma: publicar
    de nuevo rebautizaba "Campaña" como "Opción 2" — una segunda opción que no
    existe, en la pantalla del cliente. Con "Publicar actualización" como
    botón, pasaría en cada corrección.
    """
    from database import Delivery

    first = client.post(
        f"/admin/deliveries/from-job/{approved_job.job_id}",
        headers=auth(admin_token), json={"label": "Campaña"},
    )
    assert first.status_code == 200, first.text
    delivery_id = first.json()["delivery_id"]
    assert first.json()["label"] == "Campaña"

    _edit_the_render(db, approved_job)
    again = client.post(
        f"/admin/deliveries/from-job/{approved_job.job_id}",
        headers=auth(admin_token), json={},
    )
    assert again.status_code == 200, again.text
    assert again.json()["label"] == "Campaña"
    row = db.query(Delivery).filter(Delivery.id == delivery_id).first()
    db.refresh(row)
    assert row.label == "Campaña"

    # Un label explícito sigue mandando.
    renamed = client.post(
        f"/admin/deliveries/from-job/{approved_job.job_id}",
        headers=auth(admin_token), json={"label": "Renderizado v3"},
    )
    assert renamed.json()["label"] == "Renderizado v3"


def test_a_genuinely_new_delivery_still_gets_opcion_n(
    client, admin_token, approved_job, db, all_r2_files_present,
):
    """El contrapeso: la convención de "Opción N" es para una entrega nueva de
    la misma canción, y esa sí tiene que numerarse."""
    client.post(
        f"/admin/deliveries/from-job/{approved_job.job_id}",
        headers=auth(admin_token), json={},
    )
    # Otro job, misma canción y artista: es una segunda opción de verdad.
    from database import Job
    me = client.get("/auth/me", headers=auth(admin_token)).json()
    sibling = Job(
        job_id="testjob54321", user_id=me["id"], tenant_id="default",
        artist=approved_job.artist, song_title=approved_job.song_title,
        filename="test.mp3", status="done", delivery_profile="umg",
        umg_spec={"frame_size": "HD", "fps": 24.0, "prores_profile": 3},
        approved_by=me["id"], approved_at=datetime.now(timezone.utc),
        video_url="/download/testjob54321/video",
        short_url="/download/testjob54321/short",
        thumbnail_url="/download/testjob54321/thumbnail",
    )
    db.add(sibling)
    db.commit()
    try:
        res = client.post(
            f"/admin/deliveries/from-job/{sibling.job_id}",
            headers=auth(admin_token), json={},
        )
        assert res.status_code == 200, res.text
        assert res.json()["label"] == "Opción 2"
    finally:
        from database import Delivery, DeliveryChangeRequest
        ids = [d.id for d in db.query(Delivery).filter(Delivery.job_id == "testjob54321").all()]
        if ids:
            db.query(DeliveryChangeRequest).filter(
                DeliveryChangeRequest.delivery_id.in_(ids)
            ).delete(synchronize_session=False)
        db.query(Delivery).filter(Delivery.job_id == "testjob54321").delete()
        db.query(Job).filter(Job.job_id == "testjob54321").delete()
        db.commit()


def test_items_identifies_its_portal_scope(client, admin_token, approved_job, all_r2_files_present):
    """El portal de Chile falla CERRADO si el listado no se identifica.

    `index.template.html` compara `data.portal_id !== "chile"` y, si no
    coincide, tira "El backend Chile todavía no está actualizado" y muestra
    CERO entregas. Es a propósito: evita que umgchile.genly.pro renderice el
    listado global de un backend viejo. Producción ya devolvía el campo y
    staging no, así que promover staging vaciaba el portal del cliente el día
    del deploy (verificado en vivo el 2026-09-15). Este test es la única cosa
    que impide que se vuelva a caer en la promoción.
    """
    client.post(f"/admin/deliveries/from-job/{approved_job.job_id}",
                headers=auth(admin_token), json={"portal_id": "chile"})
    for portal in ("argentina", "chile"):
        res = client.get(
            "/api/deliveries/items",
            headers={"X-Portal-Token": PORTAL_TOKEN, "X-Portal-Id": portal},
        )
        assert res.status_code == 200, res.text
        assert res.json()["portal_id"] == portal, (
            f"el listado de {portal} no se identifica; el portal falla cerrado"
        )


def test_el_portal_no_encola_transcodes_con_la_cola_llena(
    client, admin_token, approved_job, db, all_r2_files_present,
):
    """Este endpoint saltea la backpressure a propósito (`force=True`), y eso
    está bien para UN click humano que está esperando su archivo.

    Lo que no está bien es una avalancha: medido el 2026-09-16, entre los dos
    portales hay 178 archivos ausentes, o sea 178 botones a un click, en la
    MISMA cola que sirve los renders de cliente. Si ya hay trabajo esperando,
    este pedido no es urgente y se rechaza con 503 en vez de ponerse delante.
    """
    res = client.post(
        f"/admin/deliveries/from-job/{approved_job.job_id}",
        headers=auth(admin_token), json={"portal_id": "chile"},
    )
    delivery_id = res.json()["delivery_id"]

    with (
        patch("main.queue_depth", return_value={"enterprise": 50}),
        patch("main.enqueue_prores_prewarm") as enqueue,
        patch("main.enqueue_delivery_prores_prewarm") as enqueue_snapshot,
    ):
        busy = client.post(
            f"/api/deliveries/{delivery_id}/prepare-prores",
            headers={"X-Portal-Token": PORTAL_TOKEN, "X-Portal-Id": "chile"},
            json={"file_type": "umg_short"},
        )
    assert busy.status_code == 503, busy.text
    assert busy.json()["detail"]["code"] == "prores_queue_busy"
    assert busy.headers.get("Retry-After") == "300"
    # Y lo importante: no encoló nada por ninguno de los dos caminos.
    enqueue.assert_not_called()
    enqueue_snapshot.assert_not_called()


def test_con_la_cola_libre_el_portal_sigue_preparando(
    client, admin_token, approved_job, db, all_r2_files_present,
):
    """El contrapeso: el freno no puede volver inútil al botón. Con la cola
    tranquila, un click humano sigue saltando la backpressure — que es
    exactamente para lo que está."""
    res = client.post(
        f"/admin/deliveries/from-job/{approved_job.job_id}",
        headers=auth(admin_token), json={"portal_id": "chile"},
    )
    delivery_id = res.json()["delivery_id"]

    with (
        patch("main.queue_depth", return_value={"enterprise": 0}),
        patch("main.enqueue_prores_prewarm", return_value="rq:1") as enqueue,
    ):
        ok = client.post(
            f"/api/deliveries/{delivery_id}/prepare-prores",
            headers={"X-Portal-Token": PORTAL_TOKEN, "X-Portal-Id": "chile"},
            json={"file_type": "umg_short"},
        )
    assert ok.status_code == 202, ok.text
    enqueue.assert_called_once()
    assert enqueue.call_args.kwargs == {"force": True}


def test_sin_redis_el_freno_no_bloquea_el_portal(
    client, admin_token, approved_job, db, all_r2_files_present,
):
    """Si no se puede leer la cola, no hay cola que proteger: fallar cerrado
    acá dejaría al cliente sin poder pedir su archivo por un problema nuestro
    de observabilidad."""
    res = client.post(
        f"/admin/deliveries/from-job/{approved_job.job_id}",
        headers=auth(admin_token), json={"portal_id": "chile"},
    )
    delivery_id = res.json()["delivery_id"]

    with (
        patch("main.queue_depth", side_effect=RuntimeError("redis caído")),
        patch("main.enqueue_prores_prewarm", return_value="rq:1") as enqueue,
    ):
        ok = client.post(
            f"/api/deliveries/{delivery_id}/prepare-prores",
            headers={"X-Portal-Token": PORTAL_TOKEN, "X-Portal-Id": "chile"},
            json={"file_type": "umg_short"},
        )
    assert ok.status_code == 202, ok.text
    enqueue.assert_called_once()
