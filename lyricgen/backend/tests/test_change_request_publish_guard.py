"""CHANGE_REQUEST_PUBLISH_GUARD_ENABLED: no re-publicar una versión aprobada
antes de un pedido abierto, y no cerrar "por publicación" un pedido de texto
cuyo texto no está en la letra publicada."""
import uuid
from datetime import datetime, timedelta, timezone

import pytest

import art_track_campaigns as atc
import change_request_publish_guard as guard
from database import (
    AuditLog, Delivery, DeliveryBatchItem, DeliveryChangeRequest, EditorDocument, EditorVersion, Job, SessionLocal,
)
from tests.test_art_track_campaigns import _campaign_for, _seed_campaign_job, clean_art_rows  # noqa: F401
from tests.test_bulk_delivery_robustness import KEYS, storage_ready  # noqa: F401
from tests.test_campaign_close_on_publish import actions, cleanup, scenario, send, state  # noqa: F401

NOW = datetime.now(timezone.utc)
PERO = '0:46 la línea es "Pintamos el mono, pero NOS DA lo mismo"'


def seg(start, end, text):
    return {"start": start, "end": end, "text": text}


# --------------------------------------------------------------------------
# Texto pedido
# --------------------------------------------------------------------------

def phrases(comment):
    return [(p["text"], p["times"]) for p in guard.extract_requests(comment)["phrases"]]


def test_extracts_timestamped_phrases_separated_by_slashes():
    assert phrases("1:12 es un mambo de Xuxú // 1:13 un manual de Kapelusz") == [
        ("es un mambo de Xuxú", [72.0]), ("un manual de Kapelusz", [73.0])]
    assert phrases("0:23 y 0:44 Y anda a lavarteloS, anda a lavarteloS") == [
        ("Y anda a lavarteloS, anda a lavarteloS", [23.0, 44.0])]
    assert phrases("Corregir letra en 1:22 y 2:38 :  y estás rogando que otro amor") == [
        ("y estás rogando que otro amor", [82.0, 158.0])]


def test_quoted_requests_keep_only_what_the_client_asks_for():
    assert phrases("Falta la frase: “dormite ya” en dos instancias: la primera comienza en 0:46 y termina en 0:48.") == [
        ("dormite ya", [46.0, 48.0])]
    # 'y dice "…"' es lo que hoy está mal, no lo pedido.
    texts = [text for text, _ in phrases(PERO + ' y dice "pintamos el mono, QUE NOS DA lo mismo", fijarse en "PERO".')]
    assert texts == ["Pintamos el mono, pero NOS DA lo mismo", "PERO"]


def test_text_after_an_instruction_colon_is_the_lyric_and_a_leading_a_is_kept():
    assert phrases("3:59 falta frase: a Enrique por toda la ciudad") == [("a Enrique por toda la ciudad", [239.0])]
    assert phrases("0:19 sacarle los signos de pregunta a la frase y corregir: a ver si sos tan macho") == [
        ("a ver si sos tan macho", [19.0])]
    assert phrases("0:59 me enojé e hice: crack crack crack") == [("me enojé e hice: crack crack crack", [59.0])]
    assert phrases("No aparece letra en 0:15, debería decir: tú ves tu amor") == [("tú ves tu amor", [15.0])]


@pytest.mark.parametrize("comment, kinds", [
    ("Sincronizar bien la frase: 1:31 He pagado un gran peaje", ["timing"]),
    ("corregir sincronización desde 2:49", ["timing"]),
    ('1:34 que acá entre antes la línea de "Presley..." entra muy', ["timing"]),
    ("cambiar fondo porfa. Que no aparezcan armas. Gracias!", ["visual"]),
    ("Quitar el perrito que tiene dos cuerpos (el de la derecha) o corregirlo. Gracias", ["visual"]),
    ("1:04, 1:14, 2:51 y 3:01 sacarle los signos de pregunta a las frases", ["other"]),
    ("Revisar que las frases completas esten en 1 sola pantalla", ["other"]),
])
def test_timing_visual_and_free_form_requests_have_no_text_to_check(comment, kinds):
    extracted = guard.extract_requests(comment)
    assert extracted["phrases"] == [] and extracted["kinds"] == kinds
    assert guard.check_requested_text(comment, [seg(0, 300, "cualquier letra")])["status"] == "not_checkable"


def test_request_158_stays_open_against_the_lyrics_published_on_6_october():
    published = [seg(40.69, 43.59, "Un poco transformada para que suene igual"),
                 seg(43.97, 46.97, "Pintamos el mono, que nos da lo mismo")]
    result = guard.check_requested_text(PERO, published)
    assert result["status"] == "not_found"
    assert [m["text"] for m in result["missing"]] == ["Pintamos el mono, pero NOS DA lo mismo"]
    fixed = [published[0], seg(43.97, 46.97, "Pintamos el mono, pero nos da lo mismo")]
    assert guard.check_requested_text(PERO, fixed)["status"] == "found"


def test_caps_accents_punctuation_and_apostrophes_are_normalized():
    comment = "0:14 anda a lavarteloS // 2:41 ENGÜALICHA'O bailé hasta el amanecer"
    ok = [seg(14.5, 18, "Anda a lavártelos"), seg(162.6, 166.4, "Engüalicha´o, bailé hasta el amanecer")]
    assert guard.check_requested_text(comment, ok)["status"] == "found"
    # La S en mayúscula ES el pedido: sin ella el texto no está.
    wrong = [seg(14.5, 18, "Anda a lavártelo"), ok[1]]
    assert [m["text"] for m in guard.check_requested_text(comment, wrong)["missing"]] == ["anda a lavarteloS"]


def test_a_phrase_may_span_lines_and_a_misquoted_time_falls_back_to_the_whole_song():
    comment = "1:10 Tengo veinte años, tengo que laburar de lunes a viernes"
    lines = [seg(28, 30.5, "Tengo veinte años, tengo que laburar"), seg(30.6, 33, "de lunes a viernes")]
    result = guard.check_requested_text(comment, lines)
    assert result["status"] == "found" and result["found"][0]["where"] == "elsewhere"
    near = guard.check_requested_text("0:29 " + comment[5:], lines)
    assert near["found"][0]["where"] == "near"


def test_visual_only_requests():
    assert guard.visual_only("cambiar fondo porfa. Que no aparezcan armas. Gracias!")
    assert not guard.visual_only("0:31: por el camino // QUITAR BANDERAS CHILENAS. Graciaas")
    assert not guard.visual_only("corregir sincronización desde 2:49")


# --------------------------------------------------------------------------
# Envío por campaña
# --------------------------------------------------------------------------

@pytest.fixture
def guard_on(monkeypatch):
    monkeypatch.setenv("CHANGE_REQUEST_PUBLISH_GUARD_ENABLED", "1")


@pytest.fixture
def portal_rows():
    created = {"deliveries": [], "jobs": []}
    yield created
    with SessionLocal() as db:
        if created["deliveries"]:
            db.query(DeliveryChangeRequest).filter(DeliveryChangeRequest.delivery_id.in_(created["deliveries"])).delete(synchronize_session=False)
            db.query(Delivery).filter(Delivery.id.in_(created["deliveries"])).delete(synchronize_session=False)
        if created["jobs"]:
            db.query(EditorVersion).filter(EditorVersion.job_id.in_(created["jobs"])).delete(synchronize_session=False)
            db.query(EditorDocument).filter(EditorDocument.job_id.in_(created["jobs"])).delete(synchronize_session=False)
        db.commit()


def approve_version(job_id, tenant_id, at, revision=1, segments=None):
    with SessionLocal() as db:
        db.add(EditorVersion(id=str(uuid.uuid4()), job_id=job_id, tenant_id=tenant_id, revision=revision,
                             segments=segments or [seg(0, 2, "letra")], created_at=at, reason="approve", is_approved=True))
        db.commit()


def published(job_id, tenant_id, rows, *, comment=None, submitted=None, fingerprint="old-cut"):
    """A delivery already in the Chile portal, optionally with one open request."""
    with SessionLocal() as db:
        job = db.query(Job).filter_by(job_id=job_id).one()
        delivery = Delivery(job_id=job_id, label="Campaña", file_types=["video"], artist_snapshot="Artist",
                            song_title_snapshot="Song", tenant_snapshot=tenant_id, added_by_user_id=job.user_id,
                            portal_id="chile", published_revision=1, published_render_fingerprint=fingerprint)
        db.add(delivery); db.flush()
        rows["deliveries"].append(delivery.id)
        request_id = None
        if comment is not None:
            request = DeliveryChangeRequest(delivery_id=delivery.id, comment=comment, submitted_at=submitted, updated_at=submitted)
            db.add(request); db.flush()
            request_id = request.id
        db.commit()
        return delivery.id, request_id


def stale_song(client, admin_token, portal_rows, name, comment="0:19 Barricada policial, hay que enfrentar"):
    """Barricada: approved 9-sep, re-sent while a 2-oct request is open."""
    campaign = _campaign_for(client, admin_token, name)
    job_id = _seed_campaign_job(campaign, umg_spec=None, s3_keys=dict(KEYS))
    portal_rows["jobs"].append(job_id)
    approve_version(job_id, campaign.tenant_id, NOW - timedelta(days=3))
    _, request_id = published(job_id, campaign.tenant_id, portal_rows, comment=comment, submitted=NOW - timedelta(days=1))
    return campaign, job_id, request_id


def post_send(client, admin_token, campaign, job_ids, key, **extra):
    return client.post(f"/batch/campaigns/{campaign.id}/deliveries", headers={"Authorization": f"Bearer {admin_token}"},
                       json={"job_ids": job_ids, "destination_portal": "chile", "idempotency_key": key, **extra})


def items(operation_id):
    with SessionLocal() as db:
        return {row.job_id: row for row in db.query(DeliveryBatchItem).filter_by(delivery_batch_id=operation_id)}


def test_flag_off_keeps_todays_behaviour(client, admin_token, storage_ready, portal_rows):
    campaign, job_id, _ = stale_song(client, admin_token, portal_rows, "guard-off")
    response = post_send(client, admin_token, campaign, [job_id], "guard-off-send-0001")
    assert response.status_code == 202, response.text
    assert "blocked_by_change_request" not in response.json()


def test_a_stale_song_is_left_out_and_the_rest_of_the_send_proceeds(client, admin_token, storage_ready, guard_on, portal_rows):
    campaign, stale, request_id = stale_song(client, admin_token, portal_rows, "guard-split")
    fresh = _seed_campaign_job(campaign, umg_spec=None, s3_keys=dict(KEYS))
    response = post_send(client, admin_token, campaign, [stale, fresh], "guard-split-send-001")
    assert response.status_code == 202, response.text
    body = response.json()
    assert body["total_count"] == 1
    [entry] = body["blocked_by_change_request"]
    assert entry["job_id"] == stale and [r["id"] for r in entry["requests"]] == [request_id]
    assert entry["requests"][0]["submitted_at"] and entry["version"]["source"] == "editor_version"
    assert set(items(body["operation_id"])) == {fresh}


def test_all_blocked_is_a_409_with_the_requests_and_creates_nothing(client, admin_token, storage_ready, guard_on, portal_rows):
    campaign, job_id, request_id = stale_song(client, admin_token, portal_rows, "guard-all")
    response = post_send(client, admin_token, campaign, [job_id], "guard-all-blocked-01")
    assert response.status_code == 409, response.text
    detail = response.json()["detail"]
    assert detail["code"] == "change_request_newer_than_version"
    assert [r["id"] for r in detail["blocked"][0]["requests"]] == [request_id]
    with SessionLocal() as db:
        assert db.query(DeliveryBatchItem).filter_by(job_id=job_id).count() == 0


def test_publish_anyway_needs_a_reason_and_is_audited(client, admin_token, storage_ready, guard_on, portal_rows):
    campaign, job_id, request_id = stale_song(client, admin_token, portal_rows, "guard-override")
    missing = post_send(client, admin_token, campaign, [job_id], "guard-override-0001", publish_anyway=True, override_reason="  ")
    assert missing.status_code == 422 and missing.json()["detail"]["code"] == "override_reason_required"
    response = post_send(client, admin_token, campaign, [job_id], "guard-override-0002",
                         publish_anyway=True, override_reason="El cliente pidió la versión anterior por mail.")
    assert response.status_code == 202, response.text
    assert response.json()["publish_guard_overrides"] == [job_id]
    operation = response.json()["operation_id"]
    item = items(operation)[job_id]
    assert item.change_request_intent["publish_guard_override"]["request_ids"] == [request_id]
    with SessionLocal() as db:
        audit = [row for row in db.query(AuditLog).filter_by(action="delivery.change_request_guard.override")
                 if (row.detail or {}).get("delivery_batch_id") == operation]
        assert len(audit) == 1
        assert audit[0].detail["change_request_ids"] == [request_id]
        assert audit[0].detail["reason"] == "El cliente pidió la versión anterior por mail."
    atc.process_delivery_batch(operation)
    item = items(operation)[job_id]
    assert item.status == "sent" and "change_requests" not in (item.receipt or {})


def test_the_worker_rechecks_a_request_that_arrived_after_the_send(client, admin_token, storage_ready, guard_on, portal_rows):
    campaign = _campaign_for(client, admin_token, "guard-worker")
    job_id = _seed_campaign_job(campaign, umg_spec=None, s3_keys=dict(KEYS))
    portal_rows["jobs"].append(job_id)
    approve_version(job_id, campaign.tenant_id, NOW - timedelta(days=3))
    delivery_id, _ = published(job_id, campaign.tenant_id, portal_rows)
    response = post_send(client, admin_token, campaign, [job_id], "guard-worker-send-01")
    assert response.status_code == 202, response.text
    with SessionLocal() as db:
        late = DeliveryChangeRequest(delivery_id=delivery_id, comment="0:10 otra letra", submitted_at=datetime.now(timezone.utc),
                                     updated_at=datetime.now(timezone.utc))
        db.add(late); db.commit()
        late_id = late.id
    atc.process_delivery_batch(response.json()["operation_id"])
    item = items(response.json()["operation_id"])[job_id]
    assert item.status == "failed" and item.error_code == "change_request_newer_than_version"
    assert f"#{late_id}" in item.error_detail
    assert "change_request_newer_than_version" in atc.NON_RETRYABLE_ITEM_ERRORS


def test_a_request_on_the_parent_cut_blocks_its_regenerated_child(client, admin_token, storage_ready, guard_on, portal_rows):
    campaign, parent, request_id = stale_song(client, admin_token, portal_rows, "guard-lineage")
    child = _seed_campaign_job(campaign, umg_spec=None, s3_keys=dict(KEYS))
    portal_rows["jobs"].append(child)
    with SessionLocal() as db:
        db.query(Job).filter_by(job_id=child).one().parent_job_id = parent
        db.commit()
    approve_version(child, campaign.tenant_id, NOW - timedelta(days=2))
    response = post_send(client, admin_token, campaign, [child], "guard-lineage-send-1")
    assert response.status_code == 409
    assert [r["id"] for r in response.json()["detail"]["blocked"][0]["requests"]] == [request_id]
    approve_version(child, campaign.tenant_id, NOW - timedelta(hours=1), revision=2)  # corrected after the request
    assert post_send(client, admin_token, campaign, [child], "guard-lineage-send-2").status_code == 202


def test_a_visual_request_is_answered_by_a_newer_render_not_a_lyric_approval(client, admin_token, storage_ready, guard_on, portal_rows):
    campaign, job_id, _ = stale_song(client, admin_token, portal_rows, "guard-visual", comment="cambiar fondo porfa. Gracias!")
    with SessionLocal() as db:
        db.query(Job).filter_by(job_id=job_id).one().completed_at = NOW - timedelta(days=2)
        db.commit()
    assert post_send(client, admin_token, campaign, [job_id], "guard-visual-send-01").status_code == 409
    with SessionLocal() as db:
        db.query(Job).filter_by(job_id=job_id).one().completed_at = NOW - timedelta(hours=1)   # new background rendered
        db.commit()
    assert post_send(client, admin_token, campaign, [job_id], "guard-visual-send-02").status_code == 202


def test_guard_preview_lists_the_blocked_songs_of_a_selection(client, admin_token, storage_ready, guard_on, portal_rows):
    campaign, stale, request_id = stale_song(client, admin_token, portal_rows, "guard-preview")
    fresh = _seed_campaign_job(campaign, umg_spec=None, s3_keys=dict(KEYS))
    response = client.get(f"/batch/campaigns/{campaign.id}/deliveries/change-request-guard?job_ids={stale},{fresh}",
                          headers={"Authorization": f"Bearer {admin_token}"})
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["enabled"] is True and [entry["job_id"] for entry in body["blocked"]] == [stale]
    assert body["blocked"][0]["requests"][0]["id"] == request_id


# --------------------------------------------------------------------------
# Cierre por publicación en el envío por campaña
# --------------------------------------------------------------------------

def ticked_text_request(client, admin_token, cleanup, name, published_line):
    campaign, job_id, delivery_id, (first, _second), later = scenario(client, admin_token, cleanup, name)
    with SessionLocal() as db:
        db.get(DeliveryChangeRequest, first).comment = PERO
        db.delete(db.get(DeliveryChangeRequest, later))   # would (rightly) trip the version rule
        db.add(EditorVersion(id=str(uuid.uuid4()), job_id=job_id, tenant_id=campaign.tenant_id, revision=3,
                             segments=[seg(43.97, 46.97, published_line)], created_at=NOW - timedelta(hours=3),
                             reason="approve", is_approved=True))
        db.commit()
    return campaign, job_id, first


def test_a_ticked_text_request_stays_open_when_its_text_is_not_published(client, admin_token, storage_ready, actions, guard_on, cleanup):
    campaign, job_id, first = ticked_text_request(client, admin_token, cleanup, "text-missing", "Pintamos el mono, que nos da lo mismo")
    response = send(client, admin_token, campaign, job_id, [first], "text-missing-send-01")
    assert response.status_code == 202, response.text
    atc.process_delivery_batch(response.json()["operation_id"])
    assert state(first)[0] is False
    with SessionLocal() as db:
        item = db.query(DeliveryBatchItem).filter_by(delivery_batch_id=response.json()["operation_id"]).one()
        assert item.status == "sent"            # never blocks the publication
        [skipped] = item.receipt["change_requests"]["skipped"]
        assert skipped["reason"] == "requested_text_not_found"
        assert skipped["missing"] == ["Pintamos el mono, pero NOS DA lo mismo"]
        audit = db.query(AuditLog).filter_by(action="delivery.change_request.requested_text_not_found").all()
        assert any((row.detail or {}).get("change_request_id") == first for row in audit)


def test_a_ticked_text_request_closes_when_its_text_is_published(client, admin_token, storage_ready, actions, guard_on, cleanup):
    campaign, job_id, first = ticked_text_request(client, admin_token, cleanup, "text-found", "Pintamos el mono, pero nos da lo mismo")
    response = send(client, admin_token, campaign, job_id, [first], "text-found-send-001")
    atc.process_delivery_batch(response.json()["operation_id"])
    assert state(first)[:2] == (True, "publication")
