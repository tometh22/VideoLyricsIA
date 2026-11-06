"""Accounting, account isolation and guarded assignment integration tests."""
import copy
import uuid
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
import delivery_plan as plan
from database import AuditLog, BatchCampaign, BatchCampaignItem, Delivery, EditorDocument, Job, User
from tests.test_batch_campaigns import clean_batch_campaign_rows  # noqa: F401


@pytest.fixture
def setup(db, monkeypatch):
    monkeypatch.setenv("BATCH_CAMPAIGN_ENABLED", "1")
    monkeypatch.setenv("BATCH_CAMPAIGN_SCOPES", "plan-test")
    monkeypatch.setattr(plan, "peer_session", lambda: None)
    user = db.query(User).first()
    actor = {"id": user.id, "role": "admin", "tenant_id": "plan-test"}
    c = BatchCampaign(id=uuid.uuid4().hex[:12], name="Pedido", tenant_id=actor["tenant_id"], created_by=user.id,
                      kind="lyric_video", destination_portal="argentina", status="active", default_render_params={})
    db.add(c); db.flush()

    def item(status="lyrics_approved", kind=None, pinned=False, campaign=None, video=False):
        campaign = campaign or c
        i = BatchCampaignItem(id=str(uuid.uuid4()), campaign_id=campaign.id, tenant_id=campaign.tenant_id,
                             ordinal=1, filename="audio.wav", title="Canción", artist="Artista", size_bytes=10,
                             sha256=uuid.uuid4().hex, upload_state="uploaded", render_overrides={"font": "anton", "source_reference": {"text": "private"}})
        if kind:
            i.render_overrides.update(plan._settings(kind))
            i.render_overrides[plan.ASSIGNMENT] = {"requirement": kind, "pinned": pinned, "revision": 0,
                                                  "group_id": kind, "group_name": kind, "model": plan.VEO_LITE if kind == "veo" else ""}
        db.add(i); db.flush()
        rp = {"background_ai_generated": True}
        if video:
            rp["campaign_render_evidence"] = {"video_sha256": uuid.uuid4().hex, "background_kind": "image" if kind == "photo_effect" else "video",
                "effect_applied": "bokeh" if kind == "photo_effect" else "", "background_origin": {"model": "veo-3.1-lite-generate-001"}}
        j = Job(job_id=uuid.uuid4().hex[:12], tenant_id=campaign.tenant_id, user_id=user.id, campaign_id=campaign.id,
                campaign_item_id=i.id, artist="Artista", filename="audio.wav", song_title="Canción", status=status, workload_class="batch",
                video_url="video" if video else None, render_params=rp)
        db.add(j); db.flush()
        return i, j

    def publish(j, country="argentina", removed=False):
        d = Delivery(job_id=j.job_id, label="Render", file_types=["video", "umg_master"], artist_snapshot=j.artist,
                     song_title_snapshot=j.song_title or "", tenant_snapshot=j.tenant_id, portal_id=country,
                     added_by_user_id=user.id, added_at=datetime(2026, 10, 5, tzinfo=timezone.utc),
                     removed_at=datetime.now(timezone.utc) if removed else None)
        db.add(d); db.flush(); return d
    db.query(Delivery).filter(Delivery.tenant_snapshot == "plan-test").delete(synchronize_session=False)
    db.commit()
    yield c, actor, item, publish
    db.rollback()
    db.query(Delivery).filter(Delivery.tenant_snapshot == "plan-test").delete(synchronize_session=False)
    db.commit()


def test_balance_compensates_delivered_and_reports_impossible_or_odd_targets():
    assert plan.balance({"veo": 312, "photo_effect": 112}, 448) == {
        "veo": 124, "photo_effect": 324, "total": 448, "projected_veo": 436,
        "projected_photo_effect": 436, "remaining_gap": 0, "odd_total": False}
    assert plan.balance({"veo": 20}, 3)["remaining_gap"] == 17
    assert plan.balance({}, 3)["photo_effect"] == 2


def test_ai_flag_does_not_mean_veo_and_failed_calls_do_not_prove_generation():
    job = SimpleNamespace(render_params={"background_ai_generated": True})
    failed = SimpleNamespace(response_summary="error: provider failed", tool_name="veo", created_at=datetime.now(timezone.utc))
    assert plan.rendered_type(job, [failed]) == "unknown"
    job.render_params["campaign_render_evidence"] = {"background_kind": "image", "effect_applied": "bokeh"}
    assert plan.rendered_type(job) == "photo_effect"
    job.render_params["art_track"] = True
    assert plan.rendered_type(job) == "art_track"


def test_distinct_portals_not_producer_tenants_and_no_double_count(db, setup):
    c, actor, make, publish = setup
    _, j = make(status="done", kind="photo_effect", video=True)
    publish(j); publish(j)  # legacy duplicate publication
    c2 = BatchCampaign(id=uuid.uuid4().hex[:12], tenant_id=c.tenant_id, created_by=actor["id"], name="Chile", kind="lyric_video", status="active", destination_portal="chile")
    db.add(c2); db.flush()
    _, foreign = make(status="done", kind="veo", video=True, campaign=c2); publish(foreign, "chile")
    make(kind=None)
    db.commit()
    ar = plan.collect(db, db, actor, portal_id="argentina")
    cl = plan.collect(db, db, actor, portal_id="chile")
    assert ar["buckets"]["delivered"]["photo_effect"] == 1
    assert ar["buckets"]["delivered"]["veo"] == 0
    assert ar["buckets"]["generated"]["photo_effect"] == 0
    assert ar["buckets"]["pending"]["unknown"] == 1
    assert cl["buckets"]["delivered"]["veo"] == 1
    with pytest.raises(HTTPException) as error:
        plan.collect(db, db, {**actor, "role": "user"}, portal_id="argentina")
    assert error.value.status_code == 403


@pytest.mark.parametrize("environment,origin", [("production", "https://staging.genly.pro"), ("staging", "https://genly.pro")])
def test_account_links_identify_peer_and_missing_original_jobs(db, setup, monkeypatch, environment, origin):
    from sqlalchemy import create_engine
    from sqlalchemy.orm import Session
    from database import Base
    _, actor, _, publish = setup
    monkeypatch.delenv("RAILWAY_ENVIRONMENT_NAME", raising=False)
    monkeypatch.setenv("ENVIRONMENT", environment)
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    try:
        with Session(engine) as peer:
            peer.add(User(id=actor["id"], username="peer-audit", hashed_password="unused", tenant_id=actor["tenant_id"]))
            campaign = BatchCampaign(id=uuid.uuid4().hex[:12], name="Remote", tenant_id=actor["tenant_id"], created_by=actor["id"], kind="lyric_video", status="active", destination_portal="argentina")
            peer.add(campaign); peer.flush()
            jobs = [Job(job_id=uuid.uuid4().hex[:12], campaign_id=campaign.id, tenant_id=actor["tenant_id"], user_id=actor["id"], artist="Remote artist", song_title="Remote song", filename="remote.wav", status="done", video_url="video", render_params={}) for _ in range(2)]
            peer.add_all(jobs); peer.commit()
            publish(jobs[0])
            missing = SimpleNamespace(job_id=uuid.uuid4().hex[:12], tenant_id=actor["tenant_id"], artist="Legacy", song_title="Missing original")
            publish(missing); db.commit()
            result = plan.collect(db, db, actor, portal_id="argentina", peer=peer)
            assert result["peer_frontend_url"] == origin
            rows = {r["job_id"]: r for r in result["items"]}
            assert rows[jobs[0].job_id]["source"] == rows[jobs[1].job_id]["source"] == "peer"
            assert rows[missing.job_id]["source"] == "missing"
            assert {i["source"] for i in result["issues"]} == {"peer", "missing"}
    finally:
        engine.dispose()


def test_generated_unpublished_pins_discard_and_cancelled_are_handled(db, setup):
    c, actor, make, publish = setup
    _, j = make(status="done", kind="veo", video=True); publish(j)
    make(status="pending_review", kind="veo", video=True)
    make(kind="photo_effect", pinned=True)
    make(); make(); make()
    i, _ = make(); i.discard_record = {"reason": "instrumental"}
    db.commit()
    result = plan.collect(db, db, actor, campaign_id=c.id)
    assert result["buckets"]["delivered"]["veo"] == 1
    assert result["buckets"]["generated"]["veo"] == 1
    assert result["suggestion"]["veo"] == 1
    assert result["suggestion"]["photo_effect"] == 2
    assert result["suggestion"]["total"] == 3
    assert all("private" not in str(row) for row in result["items"])


def test_preview_apply_preserves_design_pins_and_does_not_generate(db, setup):
    c, actor, make, publish = setup
    for _ in range(4): make()
    pinned, _ = make(kind="photo_effect", pinned=True)
    db.commit()
    before = copy.deepcopy(pinned.render_overrides)
    result = plan.get_plan(campaign_id=c.id, current_user=actor, db=db, ddb=db)
    proposed = plan.preview(plan.PreviewRequest(campaign_id=c.id, fingerprint=result["fingerprint"]), actor, db, db)
    assert len(proposed["changes"]) == 4
    assert all((i.render_overrides or {}).get(plan.ASSIGNMENT) is None for i in db.query(BatchCampaignItem).filter(BatchCampaignItem.campaign_id == c.id).all() if i.id != pinned.id)
    plan.apply(plan.ApplyRequest(preview_id=proposed["preview_id"]), actor, db, db)
    assert plan.apply(plan.ApplyRequest(preview_id=proposed["preview_id"]), actor, db, db)["deduplicated"]
    db.refresh(pinned); assert pinned.render_overrides == before
    result = plan.collect(db, db, actor, campaign_id=c.id)
    assert result["buckets"]["pending"] == {"veo": 2, "photo_effect": 3, "unknown": 0}
    assert all(i.render_overrides["font"] == "anton" for i in db.query(BatchCampaignItem).filter_by(campaign_id=c.id))
    assert {j.status for j in db.query(Job).filter_by(campaign_id=c.id)} == {"lyrics_approved"}


def test_stale_preview_and_open_editor_never_apply(db, setup):
    c, actor, make, publish = setup
    i, j = make(); db.commit()
    result = plan.collect(db, db, actor, campaign_id=c.id)
    proposed = plan.preview(plan.PreviewRequest(campaign_id=c.id, fingerprint=result["fingerprint"]), actor, db, db)
    db.add(EditorDocument(job_id=j.job_id, tenant_id=c.tenant_id, current_segments=[], revision=0,
                          lock_user_id=actor["id"], lock_expires_at=datetime.now(timezone.utc) + timedelta(minutes=5)))
    db.commit()
    with pytest.raises(HTTPException) as error:
        plan.apply(plan.ApplyRequest(preview_id=proposed["preview_id"]), actor, db, db)
    assert error.value.status_code == 409
    assert not i.render_overrides.get(plan.ASSIGNMENT)


def test_period_is_explicit_and_first_publication_survives_resend(db, setup):
    c, actor, make, publish = setup
    c.default_render_params = {"delivery_plan_period": "2026-09"}
    _, j = make(status="done", kind="veo", video=True); publish(j)
    db.add(AuditLog(user_id=actor["id"], action="delivery.create", created_at=datetime(2026, 9, 10, tzinfo=timezone.utc), detail={"job_id": j.job_id, "portal_id": "argentina"}))
    make(); db.commit()
    sept = plan.collect(db, db, actor, portal_id="argentina", period="2026-09")
    octo = plan.collect(db, db, actor, portal_id="argentina", period="2026-10")
    assert sept["buckets"]["delivered"]["veo"] == 1
    assert sept["buckets"]["pending"]["unknown"] == 1
    assert octo["buckets"]["delivered"]["veo"] == 0
    assert not octo["items"]
    with pytest.raises(HTTPException): plan.check_period("2026-99")


def test_account_association_never_moves_existing_client(db, setup):
    c, actor, _, _ = setup
    with pytest.raises(HTTPException):
        plan.associate(c.id, plan.Association(portal_id="chile"), actor, db)
    plan.associate(c.id, plan.Association(portal_id="argentina", period="2026-10"), actor, db)
    db.refresh(c); assert c.default_render_params["delivery_plan_period"] == "2026-10"


def test_unknown_delivered_blocks_auto_balance_and_art_tracks_do_not_count(db, setup):
    c, actor, make, publish = setup
    _, j = make(status="done", video=True); j.render_params = {"background_ai_generated": True}; publish(j)
    _, art = make(status="done", video=True); art.render_params = {"art_track": True}; publish(art)
    make(); db.commit()
    result = plan.collect(db, db, actor, campaign_id=c.id)
    assert result["excluded_art_tracks"] == 1
    assert result["buckets"]["delivered"]["unknown"] == 1
    with pytest.raises(HTTPException):
        plan.preview(plan.PreviewRequest(campaign_id=c.id, fingerprint=result["fingerprint"]), actor, db, db)


def test_unpublished_new_version_cannot_reclassify_portal_delivery(db, setup):
    c, actor, make, publish = setup
    _, j = make(status="done", kind="photo_effect", video=True)
    d = publish(j); d.published_render_fingerprint = "older-render"
    db.commit()
    result = plan.collect(db, db, actor, campaign_id=c.id)
    assert result["buckets"]["delivered"]["unknown"] == 1
    assert result["buckets"]["delivered"]["photo_effect"] == 0
    assert result["issues"][0]["job_id"] == j.job_id


def test_portal_campaign_requires_explicit_background_before_generation(db, setup):
    import campaign_creative
    c, actor, make, _ = setup
    i, j = make()
    db.commit()
    with pytest.raises(HTTPException) as error:
        campaign_creative.generation_receipt(db, j, "", {}, actor)
    assert error.value.status_code == 409
    assert "Plan de entrega" in error.value.detail
    c.kind = "art_track"
    db.flush()
    # Art Tracks have a portal destination but are outside the 50/50 mix.
    campaign_creative.generation_receipt(db, j, "", {}, actor)


def test_account_preview_without_peer_never_balances_partial_data(db, setup):
    c, actor, make, _ = setup
    make(); db.commit()
    result = plan.get_plan(portal_id="argentina", current_user=actor, db=db, ddb=db)
    assert any("otro entorno" in text for text in result["blockers"])
    with pytest.raises(HTTPException):
        plan.preview(plan.PreviewRequest(portal_id="argentina", fingerprint=result["fingerprint"]), actor, db, db)


def test_multi_campaign_apply_is_atomic_when_one_campaign_changes(db, setup, monkeypatch):
    c, actor, make, _ = setup
    c2 = BatchCampaign(id=uuid.uuid4().hex[:12], name="Segundo pedido", tenant_id=c.tenant_id,
                      created_by=actor["id"], kind="lyric_video", destination_portal="argentina", status="active", default_render_params={})
    db.add(c2); db.flush()
    i1, _ = make(); i2, _ = make(campaign=c2); db.commit()
    # A separate source with no campaigns/publications is sufficient for this
    # transaction test; collect() still exercises the real two-campaign read.
    original = plan._collect
    def complete_report(*args, **kw):
        result = original(*args, **kw)
        result["blockers"] = [s for s in result["blockers"] if "otro entorno" not in s]
        return result
    monkeypatch.setattr(plan, "_collect", complete_report)
    result = plan.get_plan(portal_id="argentina", current_user=actor, db=db, ddb=db)
    proposed = plan.preview(plan.PreviewRequest(portal_id="argentina", fingerprint=result["fingerprint"]), actor, db, db)
    assert {r["campaign_id"] for r in proposed["changes"]} == {c.id, c2.id}
    i2.render_overrides = {**i2.render_overrides, "font": "poppins-bold"}; db.commit()
    with pytest.raises(HTTPException):
        plan.apply(plan.ApplyRequest(preview_id=proposed["preview_id"]), actor, db, db)
    db.refresh(i1); db.refresh(i2)
    assert plan.ASSIGNMENT not in i1.render_overrides and plan.ASSIGNMENT not in i2.render_overrides
