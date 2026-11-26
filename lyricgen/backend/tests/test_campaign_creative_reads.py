"""Bounded creative reads preserve evidence, campaign scope and export totals."""
import uuid
from contextlib import contextmanager

import pytest
from fastapi import HTTPException
from sqlalchemy import event

import campaign_creative as creative
from database import AuditLog, Job
from tests.test_batch_campaigns import clean_batch_campaign_rows  # noqa: F401
from tests.test_campaign_creative import setup, request  # noqa: F401


@contextmanager
def captured_sql(db):
    statements = []

    def capture(_conn, _cursor, statement, _parameters, _context, _many):
        statements.append(statement)

    engine = db.get_bind()
    event.listen(engine, "before_cursor_execute", capture)
    try:
        yield statements
    finally:
        event.remove(engine, "before_cursor_execute", capture)


def add_job(db, campaign, actor, **values):
    job = Job(job_id=uuid.uuid4().hex[:12], user_id=actor["id"],
              tenant_id=values.pop("tenant_id", campaign.tenant_id),
              artist="Artist", filename="song.wav", status="done", workload_class="batch",
              video_url="video.mp4", segments_json=[{"text": "private lyrics"}],
              transcription_quality={"private": "large machine evidence"}, **values)
    db.add(job)
    db.flush()
    return job


def test_single_video_report_scopes_descendants_and_skips_other_evidence(db, setup, monkeypatch):
    campaign, items, actor = setup
    root = add_job(db, campaign, actor, campaign_id=campaign.id, campaign_item_id=items[0].id)
    child = add_job(db, campaign, actor, parent_job_id=root.job_id,
                    render_params={"campaign_render_evidence": {"video_sha256": "a" * 64}})
    foreign = add_job(db, campaign, actor, parent_job_id=root.job_id, tenant_id="foreign")
    unrelated = add_job(db, campaign, actor)
    db.commit()
    campaign_id, child_id, foreign_id, unrelated_id = campaign.id, child.job_id, foreign.job_id, unrelated.job_id
    portal_reads = []

    def portal(job_ids, _tenant):
        portal_reads.append(job_ids)
        return {child_id: {"umg_portals": ["chile"], "published_revision": 3}}

    monkeypatch.setattr(creative, "portal_history", portal)
    db.expunge_all()
    with captured_sql(db) as statements:
        result = creative.report(campaign_id, actor, db, job_id=child_id, history_limit=0)
    assert [row["job_id"] for row in result["videos"]] == [child_id]
    assert result["videos"][0]["evidence"]["video_sha256"] == "a" * 64
    assert result["videos"][0]["umg_portals"] == ["chile"]
    assert result["videos"][0]["portal_revision"] == 3
    assert result["history"] == result["assignments"] == []
    assert portal_reads == [[child_id]]
    assert not any("FROM audit_log" in sql for sql in statements)
    assert not any("jobs.segments_json" in sql or "jobs.transcription_quality" in sql for sql in statements)
    for forbidden in (foreign_id, unrelated_id):
        with pytest.raises(HTTPException) as error:
            creative.report(campaign_id, actor, db, job_id=forbidden, history_limit=0)
        assert error.value.status_code == 404


def test_report_audit_pages_keep_full_delivery_totals_and_default_export(db, setup, monkeypatch):
    campaign, items, actor = setup
    preview = creative.preview(campaign.id, request(items[:2], contract=True, agreement="50/50"), actor, db)
    creative.apply(campaign.id, creative.CommitRequest(preview_id=preview["preview_id"]), actor, db)
    item = items[0]
    db.refresh(item)
    job = add_job(db, campaign, actor, campaign_id=campaign.id, campaign_item_id=item.id,
                  render_params={"campaign_creative_receipt": {"assignment": item.render_overrides[creative.ASSIGNMENT]},
                                 "campaign_render_evidence": {"video_sha256": "a" * 64}})
    for index in range(4):
        db.add(AuditLog(user_id=actor["id"], action="campaign.creative.individual",
                        detail={"campaign_id": campaign.id, "reason": f"change-{index}"}))
    db.add(AuditLog(user_id=actor["id"], action="campaign.creative.delivered",
                    detail={"campaign_id": campaign.id, "job_id": job.job_id, "video_sha256": "a" * 64}))
    db.commit()
    monkeypatch.setattr(creative, "portal_history", lambda *_: {})
    with captured_sql(db) as statements:
        full = creative.report(campaign.id, actor, db)
    assert sum(group["delivered"] for group in full["groups"]) == 1
    assert "history_pagination" not in full
    # Default report reads each collection once, and remains complete for exports.
    assert sum("FROM batch_campaign_items" in sql for sql in statements) == 1
    assert sum("FROM audit_log" in sql for sql in statements) == 1
    history = []
    offset = 0
    while True:
        with captured_sql(db) as statements:
            page = creative.report(campaign.id, actor, db, history_limit=2, history_offset=offset)
        assert any("FROM audit_log" in sql and "LIMIT" in sql for sql in statements)
        assert page["groups"] == full["groups"]
        history.extend(page["history"])
        if not page["history_pagination"]["has_more"]:
            break
        offset = page["history_pagination"]["next_offset"]
    assert history == full["history"]
    omitted = creative.report(campaign.id, actor, db, history_limit=0)
    assert omitted["history"] == []
    assert omitted["groups"] == full["groups"]


def test_creative_reads_job_status_without_lyric_or_quality_payloads(db, setup):
    campaign, items, actor = setup
    job = add_job(db, campaign, actor, campaign_id=campaign.id, campaign_item_id=items[0].id)
    db.commit()
    campaign_id, job_id = campaign.id, job.job_id
    db.expunge_all()
    with captured_sql(db) as statements:
        result = creative.get_creative(campaign_id, actor, db)
    assert result["items"][0]["job_id"] == job_id
    assert result["items"][0]["status"] == "done"
    assert not any("jobs.segments_json" in sql or "jobs.transcription_quality" in sql for sql in statements)
