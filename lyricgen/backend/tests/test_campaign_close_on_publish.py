"""A campaign send closes ONLY the client requests the operator ticked, and only
when the published cut really answers them."""
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

import art_track_campaigns as atc
import campaign_change_requests as inbox
import delivery_freshness
from database import AuditLog, Delivery, DeliveryBatchItem, DeliveryChangeRequest, Job, SessionLocal
from tests.test_art_track_campaigns import _campaign_for, _seed_campaign_job, clean_art_rows, publication_storage  # noqa: F401
from tests.test_bulk_delivery_robustness import KEYS, storage_ready  # noqa: F401

NOW = datetime.now(timezone.utc)


@pytest.fixture
def actions(monkeypatch):
    monkeypatch.setenv("CAMPAIGN_CHANGE_REQUESTS_ENABLED", "1")
    monkeypatch.setenv("CAMPAIGN_CHANGE_REQUEST_ACTIONS", "1")


@pytest.fixture
def cleanup():
    created = []
    yield created
    with SessionLocal() as db:
        if created:
            db.query(DeliveryChangeRequest).filter(DeliveryChangeRequest.delivery_id.in_(created)).delete(synchronize_session=False)
            db.query(Delivery).filter(Delivery.id.in_(created)).delete(synchronize_session=False)
            db.commit()


def scenario(client, admin_token, cleanup, name):
    """A corrected, re-rendered song whose old cut is still in the Chile portal."""
    campaign = _campaign_for(client, admin_token, name)
    job_id = _seed_campaign_job(campaign, umg_spec=None, s3_keys=dict(KEYS))
    with SessionLocal() as db:
        job = db.query(Job).filter_by(job_id=job_id).one()
        job.completed_at = NOW - timedelta(hours=1)  # the corrected render finished an hour ago
        delivery = Delivery(job_id=job_id, label="Campaña", file_types=["video"], artist_snapshot="Artist",
                            song_title_snapshot="Song", tenant_snapshot=campaign.tenant_id, added_by_user_id=job.user_id,
                            portal_id="chile", published_revision=1, published_render_fingerprint="old-cut")
        db.add(delivery); db.flush()
        cleanup.append(delivery.id)
        before = [DeliveryChangeRequest(delivery_id=delivery.id, comment=text, submitted_at=NOW - timedelta(days=days), updated_at=NOW - timedelta(days=days))
                  for text, days in (("Cambiar la palabra", 2), ("Otro detalle", 1))]
        after = DeliveryChangeRequest(delivery_id=delivery.id, comment="Pedido posterior al corte", submitted_at=NOW - timedelta(minutes=5), updated_at=NOW)
        db.add_all([*before, after]); db.commit()
        return campaign, job_id, delivery.id, [r.id for r in before], after.id


def send(client, admin_token, campaign, job_id, ids, key, note=None):
    body = {"job_ids": [job_id], "destination_portal": "chile", "idempotency_key": key}
    if ids is not None:
        body["resolve_requests"] = {job_id: ids}
    if note:
        body["resolution_note"] = note
    return client.post(f"/batch/campaigns/{campaign.id}/deliveries", headers={"Authorization": f"Bearer {admin_token}"}, json=body)


def state(request_id):
    with SessionLocal() as db:
        row = db.get(DeliveryChangeRequest, request_id)
        return row.resolved_at is not None, row.resolution_source, row.resolution_note, row.resolved_by_revision


def test_only_ticked_requests_are_closed_with_the_operators_note(client, admin_token, storage_ready, actions, cleanup):
    campaign, job_id, delivery_id, (first, second), later = scenario(client, admin_token, cleanup, "closes")
    response = send(client, admin_token, campaign, job_id, [first], "close-on-publish-1", "Se cambió la palabra final.")
    assert response.status_code == 202, response.text
    atc.process_delivery_batch(response.json()["operation_id"])
    assert state(first) == (True, "publication", "Se cambió la palabra final.", 2)
    assert state(second)[0] is False      # not ticked: stays open
    assert state(later)[0] is False       # newer than the cut: never closed by it
    with SessionLocal() as db:
        item = db.query(DeliveryBatchItem).filter_by(delivery_batch_id=response.json()["operation_id"]).one()
        assert item.status == "sent" and item.receipt["change_requests"] == {"resolved": [first], "skipped": []}
        audit = [a for a in db.query(AuditLog).filter_by(action="delivery.update") if (a.detail or {}).get("delivery_batch_id") == item.delivery_batch_id]
        assert audit and audit[0].detail["resolved_change_requests"] == [first]
        assert db.get(Delivery, delivery_id).published_revision == 2


def test_default_note_names_the_published_version(client, admin_token, storage_ready, actions, cleanup):
    campaign, job_id, _, (first, _), _ = scenario(client, admin_token, cleanup, "default-note")
    op = send(client, admin_token, campaign, job_id, [first], "close-default-note-1").json()["operation_id"]
    atc.process_delivery_batch(op)
    assert state(first)[2] == "Resuelto al publicar la versión 2."


def test_a_send_without_ticks_keeps_every_request_open(client, admin_token, storage_ready, actions, cleanup):
    campaign, job_id, _, (first, second), later = scenario(client, admin_token, cleanup, "no-ticks")
    op = send(client, admin_token, campaign, job_id, None, "no-ticks-operation-1").json()["operation_id"]
    atc.process_delivery_batch(op)
    assert [state(r)[0] for r in (first, second, later)] == [False, False, False]


@pytest.mark.parametrize("kind", ["newer", "foreign", "resolved"])
def test_an_unclosable_tick_rejects_the_whole_send(client, admin_token, storage_ready, actions, cleanup, kind):
    campaign, job_id, _, (first, second), later = scenario(client, admin_token, cleanup, "reject-" + kind)
    if kind == "resolved":
        with SessionLocal() as db:
            db.get(DeliveryChangeRequest, first).resolved_at = NOW; db.commit()
    ids = {"newer": [later], "foreign": [10_000_000], "resolved": [first]}[kind]
    response = send(client, admin_token, campaign, job_id, ids, "reject-tick-" + kind + "-1")
    assert response.status_code == 409, response.text
    detail = response.json()["detail"]
    assert detail["code"] == "change_request_not_closable"
    assert detail["reason"] == {"newer": "newer_than_cut", "foreign": "not_found", "resolved": "not_open"}[kind]
    with SessionLocal() as db:
        assert db.query(DeliveryBatchItem).filter_by(job_id=job_id).count() == 0  # nothing was created


def test_ticking_is_refused_while_the_feature_is_off(client, admin_token, storage_ready, monkeypatch, cleanup):
    monkeypatch.delenv("CAMPAIGN_CHANGE_REQUEST_ACTIONS", raising=False)
    campaign, job_id, _, (first, _), _ = scenario(client, admin_token, cleanup, "flag-off")
    response = send(client, admin_token, campaign, job_id, [first], "flag-off-operation-1")
    assert response.status_code == 409 and response.json()["detail"] == {"code": "feature_disabled"}


def test_a_cut_edited_after_review_still_publishes_but_closes_nothing(client, admin_token, storage_ready, actions, cleanup):
    campaign, job_id, _, (first, _), _ = scenario(client, admin_token, cleanup, "edited")
    op = send(client, admin_token, campaign, job_id, [first], "edited-after-review-1").json()["operation_id"]
    with SessionLocal() as db:
        job = db.query(Job).filter_by(job_id=job_id).one()
        job.segments_revision = int(job.segments_revision or 0) + 1; db.commit()
    atc.process_delivery_batch(op)
    assert state(first)[0] is False
    with SessionLocal() as db:
        item = db.query(DeliveryBatchItem).filter_by(delivery_batch_id=op).one()
        assert item.status == "sent"
        assert item.receipt["change_requests"] == {"resolved": [], "skipped": [{"id": first, "reason": "cut_changed_after_review"}]}


def test_a_request_closed_meanwhile_is_skipped_not_overwritten(client, admin_token, storage_ready, actions, cleanup):
    campaign, job_id, _, (first, _), _ = scenario(client, admin_token, cleanup, "closed-meanwhile")
    op = send(client, admin_token, campaign, job_id, [first], "closed-meanwhile-op-1").json()["operation_id"]
    with SessionLocal() as db:
        row = db.get(DeliveryChangeRequest, first)
        row.resolved_at = NOW; row.resolution_source = "manual"; row.resolution_note = "Cerrado a mano"; db.commit()
    atc.process_delivery_batch(op)
    assert state(first)[1:3] == ("manual", "Cerrado a mano")
    with SessionLocal() as db:
        assert db.query(DeliveryBatchItem).filter_by(delivery_batch_id=op).one().receipt["change_requests"]["skipped"] == [{"id": first, "reason": "not_open"}]


def test_inbox_offers_exactly_what_the_send_accepts(client, admin_token, storage_ready, actions, cleanup):
    campaign, job_id, _, (first, second), later = scenario(client, admin_token, cleanup, "inbox-agrees")
    with SessionLocal() as db:
        actor = {"id": campaign.created_by, "role": "admin", "tenant_id": campaign.tenant_id}
        items = {r["id"]: r for r in inbox.campaign_change_requests(campaign.id, "open", 50, None, actor, db)["items"]}
    assert [items[r]["closable_on_publish"] for r in (first, second, later)] == [True, True, False]
    assert items[first]["current_job_id"] == job_id and items[first]["step"]["key"] == "publish"


def _job(**params):
    return SimpleNamespace(status="done", completed_at=NOW, previous_versions=[], render_params=params, segments_revision=3,
                           s3_keys={}, umg_spec=None, job_id="j", tenant_id="t")


def test_a_render_that_does_not_match_the_saved_editor_is_not_closable(monkeypatch):
    monkeypatch.setattr(delivery_freshness, "needs_publish", lambda job, delivery: True)
    request = SimpleNamespace(resolved_at=None, submitted_at=NOW - timedelta(days=1))
    delivery = SimpleNamespace()
    document = SimpleNamespace(revision=4, updated_at=NOW)
    stale = _job(_rendered_segments_revision=3, _rendered_at=NOW.isoformat())      # editor moved to rev 4
    fresh = _job(_rendered_segments_revision=4, _rendered_at=NOW.isoformat())
    assert inbox.closable_on_publish(request, stale, delivery, document) == (False, "render_not_current")
    assert inbox.closable_on_publish(request, fresh, delivery, document) == (True, "ok")
    assert inbox.closable_on_publish(request, fresh, delivery, None) == (True, "ok")
    assert inbox.closable_on_publish(SimpleNamespace(resolved_at=NOW, submitted_at=NOW), fresh, delivery, document) == (False, "not_open")
    assert inbox.closable_on_publish(request, SimpleNamespace(**{**vars(fresh), "status": "rendering"}), delivery, document) == (False, "job_not_ready")
