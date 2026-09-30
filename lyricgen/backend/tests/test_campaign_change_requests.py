"""The campaign inbox of client change requests: scoped, read-only, paginated."""
from datetime import datetime, timedelta, timezone

import pytest
from fastapi import HTTPException

import campaign_change_requests as inbox
import delivery_freshness
from database import Delivery, DeliveryChangeRequest, Job
from tests.test_campaign_pipeline import add_job, setup  # noqa: F401  (fixture)
from tests.test_batch_campaigns import clean_batch_campaign_rows  # noqa: F401


@pytest.fixture
def enabled(monkeypatch):
    monkeypatch.setenv("CAMPAIGN_CHANGE_REQUESTS_ENABLED", "1")


@pytest.fixture
def cleanup(db):
    created = []
    yield created
    db.rollback()
    if created:
        db.query(DeliveryChangeRequest).filter(DeliveryChangeRequest.delivery_id.in_(created)).delete(synchronize_session=False)
        db.query(Delivery).filter(Delivery.id.in_(created)).delete(synchronize_session=False)
        db.commit()


def publish(db, created, job, *, tenant=None, portal="chile", fresh=True):
    delivery = Delivery(
        job_id=job.job_id, label="Campaña", file_types=["video"], artist_snapshot="Artista",
        song_title_snapshot=f"Canción {job.job_id[:4]}", tenant_snapshot=tenant or job.tenant_id,
        added_by_user_id=job.user_id, portal_id=portal, published_revision=2,
        published_render_fingerprint=delivery_freshness.render_fingerprint(job) if fresh else "outdated",
    )
    db.add(delivery)
    db.flush()
    created.append(delivery.id)
    return delivery


def request(db, delivery, comment, *, days_ago=0, resolved=False, source=None):
    when = datetime.now(timezone.utc) - timedelta(days=days_ago)
    row = DeliveryChangeRequest(
        delivery_id=delivery.id, comment=comment, submitted_at=when, updated_at=when,
        resolved_at=when if resolved else None, resolution_source=source if resolved else None,
        resolution_note="hecho" if resolved else None,
    )
    db.add(row)
    db.flush()
    return row


def test_inbox_is_hidden_behind_its_flag(db, setup):
    campaign, _, actor = setup
    with pytest.raises(HTTPException) as denied:
        inbox.campaign_change_requests(campaign.id, "open", 50, None, actor, db)
    assert denied.value.status_code == 404 and denied.value.detail == {"code": "feature_disabled"}


def test_lists_only_this_campaigns_requests_with_their_state(db, setup, enabled, cleanup):
    campaign, items, actor = setup
    root = add_job(db, campaign, actor, item=items[0], status="done")
    variant = add_job(db, campaign, actor, parent=root, status="done")
    editing = add_job(db, campaign, actor, item=items[1], status="editing", video=False)
    other = add_job(db, campaign, actor, item=items[2], status="done")
    foreign_tenant = add_job(db, campaign, actor, item=items[3], status="done")
    unrelated = add_job(db, campaign, actor, status="done")  # not part of any campaign song
    db.commit()
    a = publish(db, cleanup, variant)                 # published cut of a variant
    b = publish(db, cleanup, editing, portal="argentina")
    c = publish(db, cleanup, other, fresh=False)      # corrected, not yet republished
    publish(db, cleanup, foreign_tenant, tenant="someone-else")
    ghost = publish(db, cleanup, unrelated)
    r_a = request(db, a, "Cambiar la palabra", days_ago=3)
    r_b = request(db, b, "Otro fondo", days_ago=1)
    r_c = request(db, c, "Corregir el final", days_ago=2)
    request(db, a, "Ya hecho", days_ago=5, resolved=True, source="publication")
    request(db, ghost, "De otra canción")
    db.commit()

    result = inbox.campaign_change_requests(campaign.id, "open", 50, None, actor, db)
    assert result["available"] is True
    assert result["counts"]["open"] == 3 and result["counts"]["resolved"] == 1
    assert result["counts"]["oldest_open_at"].startswith((datetime.now(timezone.utc) - timedelta(days=3)).strftime("%Y-%m-%d"))
    by_id = {row["id"]: row for row in result["items"]}
    assert set(by_id) == {r_a.id, r_b.id, r_c.id}
    assert [row["id"] for row in result["items"]] == [r_b.id, r_c.id, r_a.id]  # newest lifecycle change first
    assert by_id[r_a.id]["song_id"] == items[0].id and by_id[r_a.id]["portal_id"] == "chile"
    assert by_id[r_a.id]["step"]["key"] == "correct"
    assert by_id[r_b.id]["step"]["key"] == "rendering" and by_id[r_b.id]["portal_id"] == "argentina"
    assert by_id[r_c.id]["step"]["key"] == "publish"
    assert all("owner_email" not in row for row in result["items"])

    resolved = inbox.campaign_change_requests(campaign.id, "resolved", 50, None, actor, db)["items"]
    assert len(resolved) == 1 and resolved[0]["step"] == {"key": "resolved", "tone": "done", "label": "Resuelto al publicar la v2"}


def test_pagination_walks_every_request_once(db, setup, enabled, cleanup):
    campaign, items, actor = setup
    job = add_job(db, campaign, actor, item=items[0], status="done")
    db.commit()
    delivery = publish(db, cleanup, job)
    expected = [request(db, delivery, f"pedido {n}", days_ago=n).id for n in range(5)]
    db.commit()
    seen, cursor = [], None
    while True:
        page = inbox.campaign_change_requests(campaign.id, "open", 2, cursor, actor, db)
        seen += [row["id"] for row in page["items"]]
        cursor = page["next_cursor"]
        if not cursor:
            break
    assert seen == expected  # newest first, no gaps, no repeats
    with pytest.raises(HTTPException) as bad:
        inbox.campaign_change_requests(campaign.id, "open", 2, "garbage", actor, db)
    assert bad.value.status_code == 400


def test_an_unreachable_portal_reads_as_unknown_not_empty(db, setup, enabled, monkeypatch):
    campaign, items, actor = setup
    add_job(db, campaign, actor, item=items[0], status="done")
    db.commit()

    def broken():
        raise RuntimeError("portal down")

    monkeypatch.setattr(inbox, "scoped_deliveries_db", broken)
    result = inbox.campaign_change_requests(campaign.id, "open", 50, None, actor, db)
    assert result["available"] is False and result["counts"]["open"] is None and result["items"] == []


def test_another_tenant_cannot_open_the_campaign_inbox(db, setup, enabled):
    campaign, _, actor = setup
    with pytest.raises(HTTPException) as denied:
        inbox.campaign_change_requests(campaign.id, "open", 50, None, {**actor, "role": "user", "tenant_id": "other"}, db)
    assert denied.value.status_code == 404
