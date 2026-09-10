import copy
import hashlib
import io
import json
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from zipfile import ZipFile

import pytest
from fastapi import HTTPException
import campaign_creative as creative
from database import AuditLog, BatchCampaign, BatchCampaignItem, EditorDocument, Job, User
from tests.test_batch_campaigns import clean_batch_campaign_rows  # noqa: F401


@pytest.fixture
def setup(db, monkeypatch):
    monkeypatch.setenv("BATCH_CAMPAIGN_ENABLED", "1")
    monkeypatch.setenv("VEO_MODEL", "veo-3.1-fast-generate-001")
    user = db.query(User).first()
    campaign = BatchCampaign(id=uuid.uuid4().hex[:12], tenant_id="creative-test", created_by=user.id,
                             name="Chile ñ", kind="lyric_video", status="active", default_render_params={"stage1_pipeline": {"promotion_limit": 39}})
    db.add(campaign); db.flush()
    items = []
    for n in range(39):
        item = BatchCampaignItem(id=str(uuid.uuid4()), campaign_id=campaign.id, tenant_id=campaign.tenant_id,
                                 ordinal=n + 1, filename=f"song{n}.wav", title=f"Tema {n}", artist="Artista", size_bytes=100,
                                 sha256=hashlib.sha256(str(n).encode()).hexdigest(), render_overrides={"source_reference": {"status": "absent"}})
        db.add(item); items.append(item)
    db.commit()
    return campaign, items, {"id": user.id, "role": "admin", "tenant_id": campaign.tenant_id}


def request(items, **kw):
    return creative.PlanRequest(revision=0, item_ids=[i.id for i in items], reason="Acuerdo de estilos", groups=[
        creative.Group(id="photo", name="Foto y efecto", weight=50, requirement="photo_effect", settings={"movement_style": "foto-parallax", "effect": "rain"}),
        creative.Group(id="veo", name="Veo", weight=50, requirement="veo", model="veo-3.1-fast-generate-001", settings={"movement_style": "estandar"})], **kw)


def test_repartition_stable_exact_preserves_sources_and_native_settings(db, setup):
    campaign, items, actor = setup
    body = request(items, contract=True, agreement="50% foto + efecto, 50% Veo", rounding_note="Cliente acepta 20 fotos y 19 Veo")
    result = creative.preview(campaign.id, body, actor, db)
    assert result["counts"] == [20, 19] and result["rounded"]
    assert len(result["changes"]) == 39
    assert "source_reference" not in json.dumps(result)
    same = creative.preview(campaign.id, body, actor, db)
    assert result["changes"] == same["changes"]
    committed = creative.apply(campaign.id, creative.CommitRequest(preview_id=result["preview_id"]), actor, db)
    assert committed["revision"] == 1
    assert creative.apply(campaign.id, creative.CommitRequest(preview_id=result["preview_id"]), actor, db)["deduplicated"]
    db.refresh(campaign)
    assert campaign.default_render_params["stage1_pipeline"] == {"promotion_limit": 39}
    for item in items:
        db.refresh(item)
        assert item.render_overrides["source_reference"] == {"status": "absent"}
    assert not db.query(Job).filter_by(campaign_id=campaign.id).count()
    assert creative.report(campaign.id, actor, db)["groups"][0]["assigned"] == 20


def test_rounding_needs_explicit_record_and_scope_is_not_assumed(db, setup):
    campaign, items, actor = setup
    with pytest.raises(HTTPException, match="") as exc:
        creative.preview(campaign.id, request(items, contract=True, agreement="50/50"), actor, db)
    assert exc.value.status_code == 422
    with pytest.raises(HTTPException) as exc:
        creative.get_creative(campaign.id, {**actor, "role": "user", "tenant_id": "foreign"}, db)
    assert exc.value.status_code == 404
    bad = request(items[:2]); bad.item_ids.append("not-in-campaign")
    with pytest.raises(HTTPException):
        creative.preview(campaign.id, bad, actor, db)


def test_campaign_creation_cannot_inject_an_unaudited_creative_agreement(db, setup):
    from batch_campaigns import create_campaign, CampaignCreate
    _, _, actor = setup
    with pytest.raises(HTTPException) as error:
        create_campaign(CampaignCreate(name="Forged", default_render_params={
            "creative_plan": {"revision": 1, "contract": {"actor": 999, "agreement": "Forged"}}}), actor, db)
    assert error.value.status_code == 422


def test_excel_can_be_read_with_zero_totals_and_all_unrendered_assignments(db, setup):
    openpyxl = pytest.importorskip("openpyxl")
    campaign, items, actor = setup
    result = creative.preview(campaign.id, request(items[:2], contract=True, agreement="50/50"), actor, db)
    creative.apply(campaign.id, creative.CommitRequest(preview_id=result["preview_id"]), actor, db)
    content = creative.export_xlsx(campaign.id, actor, db).body
    workbook = openpyxl.load_workbook(io.BytesIO(content))
    assert workbook.sheetnames == ["Acuerdo", "Videos", "Cambios", "Asignaciones"]
    assert len(list(workbook["Asignaciones"].values)) == 40
    assert list(workbook["Acuerdo"].values)[7][4:8] == ("0", "0", "0", "0")
    assert "source_reference" not in str(list(workbook["Cambios"].values))
    csv = creative.export_csv(campaign.id, actor, db).body.decode()
    assert "50/50" in csv and "Sin generar" in csv


@pytest.mark.parametrize("settings", [{"effect": ""}, {"effect": "foto_viva"}, {"animate_image": True}, {"movement_style": "estatico"}, {"enable_scenes": True}])
def test_photo_effect_cannot_silently_become_something_else(db, setup, settings):
    campaign, items, actor = setup
    body = request(items[:2]); body.groups[0].settings.update(settings)
    with pytest.raises(HTTPException):
        creative.preview(campaign.id, body, actor, db)


def test_stale_preview_conflict_and_safe_undo(db, setup):
    campaign, items, actor = setup
    body = request(items[:2])
    result = creative.preview(campaign.id, body, actor, db)
    items[0].render_overrides = {**items[0].render_overrides, "font": "anton"}; db.commit()
    with pytest.raises(HTTPException) as exc:
        creative.apply(campaign.id, creative.CommitRequest(preview_id=result["preview_id"]), actor, db)
    assert exc.value.status_code == 409
    fresh = creative.preview(campaign.id, body, actor, db)
    creative.apply(campaign.id, creative.CommitRequest(preview_id=fresh["preview_id"]), actor, db)
    creative.undo(campaign.id, creative.UndoRequest(revision=1, operation_id=fresh["preview_id"], reason="Corregir selección"), actor, db)
    db.refresh(items[0]); assert items[0].render_overrides["font"] == "anton"
    with pytest.raises(HTTPException):
        creative.undo(campaign.id, creative.UndoRequest(revision=2, operation_id=fresh["preview_id"], reason="No repetir"), actor, db)


def test_individual_pin_and_partial_bulk_keep_contract_group(db, setup):
    campaign, items, actor = setup
    result = creative.preview(campaign.id, request(items[:2]), actor, db)
    creative.apply(campaign.id, creative.CommitRequest(preview_id=result["preview_id"]), actor, db)
    item = items[0]; db.refresh(item)
    old = copy.deepcopy(item.render_overrides[creative.ASSIGNMENT])
    job = Job(job_id=uuid.uuid4().hex[:12], user_id=actor["id"], tenant_id=campaign.tenant_id, campaign_id=campaign.id,
              campaign_item_id=item.id, workload_class="batch", artist="A", filename="a.wav", status="transcribed_pending")
    db.add(job); db.commit()
    result = creative.save_individual(campaign.id, item.id, creative.IndividualRequest(revision=1, settings={"font": "anton"}), actor, db)
    assert result["revision"] == 2
    assert creative.save_individual(campaign.id, item.id, creative.IndividualRequest(revision=2, settings={"font": "anton"}), actor, db)["unchanged"]
    body = creative.PlanRequest(revision=2, item_ids=[i.id for i in items[:2]], reason="Tamaño común", groups=[creative.Group(id="size", name="Grande", weight=100, settings={"font_scale": 1.3})])
    preview = creative.preview(campaign.id, body, actor, db)
    assert preview["skipped"] == [item.id]
    body.replace_exceptions = True
    preview = creative.preview(campaign.id, body, actor, db)
    creative.apply(campaign.id, creative.CommitRequest(preview_id=preview["preview_id"]), actor, db)
    db.refresh(item)
    assert item.render_overrides[creative.ASSIGNMENT]["group_id"] == old["group_id"]
    assert item.render_overrides["font"] == "anton"
    assert item.render_overrides["font_scale"] == 1.3


def test_lock_and_render_progress_block_bulk(db, setup):
    campaign, items, actor = setup
    job = Job(job_id=uuid.uuid4().hex[:12], user_id=actor["id"], tenant_id=campaign.tenant_id, campaign_id=campaign.id,
              campaign_item_id=items[0].id, workload_class="batch", artist="A", filename="a.wav", status="transcribed_pending")
    db.add(job); db.flush()
    doc = EditorDocument(job_id=job.job_id, tenant_id=campaign.tenant_id, current_segments=[], revision=0,
                         lock_user_id=actor["id"], lock_expires_at=datetime.now(timezone.utc) + timedelta(minutes=5))
    db.add(doc); db.commit()
    with pytest.raises(HTTPException):
        creative.preview(campaign.id, request(items[:2]), actor, db)
    doc.lock_expires_at = None; job.status = "processing"; db.commit()
    with pytest.raises(HTTPException):
        creative.preview(campaign.id, request(items[:2]), actor, db)


def test_history_includes_variants_but_never_foreign_campaign_or_tenant(db, setup):
    campaign, items, actor = setup
    def add(**kw):
        row = Job(job_id=uuid.uuid4().hex[:12], user_id=actor["id"], tenant_id=kw.pop("tenant_id", campaign.tenant_id),
                  artist="A", filename="a.wav", status="done", workload_class="batch", **kw)
        db.add(row); db.flush(); return row
    root = add(campaign_id=campaign.id, campaign_item_id=items[0].id, video_url="video")
    child = add(parent_job_id=root.job_id, video_url="video")
    grandchild = add(parent_job_id=child.job_id, video_url="video")
    add(parent_job_id=root.job_id, tenant_id="foreign", video_url="video")
    add(video_url="video")
    db.commit()
    assert {r["job_id"] for r in creative.video_history(campaign.id, actor, db)["items"]} == {root.job_id, child.job_id, grandchild.job_id}


def test_contract_evidence_and_delivery_do_not_count_an_intent(db, setup):
    campaign, items, actor = setup
    preview = creative.preview(campaign.id, request(items[:2], contract=True, agreement="50/50"), actor, db)
    creative.apply(campaign.id, creative.CommitRequest(preview_id=preview["preview_id"]), actor, db)
    item = next(i for i in items[:2] if i.render_overrides[creative.ASSIGNMENT]["requirement"] == "photo_effect")
    assignment = item.render_overrides[creative.ASSIGNMENT]
    job = Job(job_id=uuid.uuid4().hex[:12], user_id=actor["id"], tenant_id=campaign.tenant_id, campaign_id=campaign.id,
              campaign_item_id=item.id, workload_class="batch", artist="=BAD()", filename="a.wav", status="done", video_url="video",
              approved_at=datetime.now(timezone.utc), render_params={"campaign_creative_receipt": {"assignment": assignment}})
    db.add(job); db.commit()
    assert creative.video_history(campaign.id, actor, db)["items"][0]["compliance"] == "pending"
    evidence = {"background_kind": "image", "effect_applied": "rain", "video_sha256": "a" * 64, "models": [], "degraded": False}
    job.render_params = {**job.render_params, "campaign_render_evidence": evidence}; db.commit()
    assert creative.video_history(campaign.id, actor, db)["items"][0]["compliance"] == "verified"
    body = creative.DeliveryRequest(job_id=job.job_id, video_sha256="a" * 64, destination="Portal Chile")
    creative.record_delivery(campaign.id, body, actor, db)
    assert creative.record_delivery(campaign.id, body, actor, db)["deduplicated"]
    data = creative.report(campaign.id, actor, db)
    assert sum(g["delivered"] for g in data["groups"]) == 1
    csv = creative.export_csv(campaign.id, actor, db).body.decode()
    assert "'=BAD()" in csv
    workbook = creative.export_xlsx(campaign.id, actor, db)
    with ZipFile(io.BytesIO(workbook.body)) as z:
        assert "xl/worksheets/sheet3.xml" in z.namelist()
        assert b"<f>" not in z.read("xl/worksheets/sheet2.xml")
    job.render_params = {**job.render_params, "campaign_render_evidence": {**evidence, "video_sha256": "b" * 64}}
    db.commit()
    assert sum(g["delivered"] for g in creative.report(campaign.id, actor, db)["groups"]) == 0


def test_catalogue_and_request_contract_covers_editor_options():
    fixture = Path(__file__).parents[2] / "frontend/src/shared/renderParity.json"
    assert creative.CATALOG == json.loads(fixture.read_text())["catalogs"]
    assert creative.normalize_settings({"scene_source": "lyrics", "background_hint": "old prompt"}) == {"match_lyrics": True, "bg_verbatim": False, "background_hint": ""}
    for invalid in ({"source_reference": {}}, {"font_scale": float("nan")}, {"enable_scenes": "false"}, {"umg_frame_size": "1920x1080"}):
        with pytest.raises(HTTPException):
            creative.normalize_settings(invalid)


def test_generation_snapshot_is_revision_bound_and_native(db, setup):
    campaign, items, actor = setup
    preview = creative.preview(campaign.id, request(items[:2]), actor, db)
    creative.apply(campaign.id, creative.CommitRequest(preview_id=preview["preview_id"]), actor, db)
    item = items[0]; db.refresh(item)
    job = Job(job_id=uuid.uuid4().hex[:12], user_id=actor["id"], tenant_id=campaign.tenant_id, campaign_id=campaign.id,
              campaign_item_id=item.id, workload_class="batch", artist="A", filename="a.wav", status="lyrics_approved")
    db.add(job); db.flush()
    settings = creative.effective_settings(campaign, item.render_overrides)
    with pytest.raises(HTTPException):
        creative.generation_receipt(db, job, "0", settings, actor)
    creative.generation_receipt(db, job, "1", settings, actor); db.commit()
    assert job.render_params["campaign_creative_receipt"]["settings"] == settings
    assert job.status == "lyrics_approved"  # only the native generate endpoint publishes work


def test_lite_is_an_explicit_campaign_model_without_changing_ordinary_jobs(db, setup):
    from campaign_models import VEO_LITE, model_for_campaign_job
    campaign, items, actor = setup
    body = creative.PlanRequest(revision=0, item_ids=[items[0].id], reason="Contrato Lite", groups=[
        creative.Group(id="lite", name="Veo Lite", weight=100, requirement="veo", model=VEO_LITE,
                       settings={"movement_style": "estandar"})])
    preview = creative.preview(campaign.id, body, actor, db)
    creative.apply(campaign.id, creative.CommitRequest(preview_id=preview["preview_id"]), actor, db)
    job = Job(job_id=uuid.uuid4().hex[:12], user_id=actor["id"], tenant_id=campaign.tenant_id, campaign_id=campaign.id,
              campaign_item_id=items[0].id, workload_class="batch", artist="Test", filename="synthetic.wav", status="lyrics_approved")
    db.add(job); db.flush()
    settings = creative.effective_settings(campaign, items[0].render_overrides)
    creative.generation_receipt(db, job, "1", settings, actor); db.commit()
    assert model_for_campaign_job(job.job_id, "ordinary-static-model") == VEO_LITE
    assert model_for_campaign_job(None, "ordinary-static-model") == "ordinary-static-model"
    other = Job(job_id=uuid.uuid4().hex[:12], user_id=actor["id"], tenant_id=campaign.tenant_id,
                artist="Ordinary", filename="test.wav", status="done", render_params=job.render_params)
    db.add(other); db.commit()
    assert model_for_campaign_job(other.job_id, "ordinary-static-model") == "ordinary-static-model"
    body.groups[0].model = "untrusted-model"
    body.revision = 1
    with pytest.raises(HTTPException):
        creative.preview(campaign.id, body, actor, db)


def test_http_assignment_approval_generate_outbox_history_chain(db, setup, client, admin_token, monkeypatch, tmp_path):
    import main
    import batch_campaigns as batch
    from database import JobOutboxEvent
    from reference_hypothesis import build_unavailable
    from tests.test_main_generate_with_job_id import _wav_bytes
    campaign, items, actor = setup
    auth = {"Authorization": f"Bearer {admin_token}"}
    response = client.get(f"/batch/campaigns/{campaign.id}/creative", headers=auth)
    assert response.status_code == 200
    body = request(items[:2])
    preview = client.post(f"/batch/campaigns/{campaign.id}/creative/preview", headers=auth, json=body.model_dump())
    assert preview.status_code == 200, preview.text
    applied = client.post(f"/batch/campaigns/{campaign.id}/creative/apply", headers=auth, json={"preview_id": preview.json()["preview_id"]})
    assert applied.status_code == 200, applied.text
    db.expire_all()
    item = items[0]
    segments = [{"segment_id": "line-1", "start": 0., "end": 1., "text": "Texto de prueba"}]
    audio_sha = "b" * 64
    job = Job(job_id=uuid.uuid4().hex[:12], user_id=actor["id"], tenant_id=campaign.tenant_id, campaign_id=campaign.id,
              campaign_item_id=item.id, workload_class="batch", artist=item.artist, song_title=item.title,
              filename="a.wav", status="transcribed_pending", segments_json=segments, segments_revision=0,
              input_audio_sha256=audio_sha, audio_revision=1, transcription_quality={"reference_hypothesis": build_unavailable(audio_sha256=audio_sha, audio_revision=1), "manual_full_review_required": True})
    db.add(job); db.add(EditorDocument(job_id=job.job_id, tenant_id=campaign.tenant_id,
                                      current_segments=segments, original_segments=segments, revision=0)); db.commit()
    directory = tmp_path / job.job_id; directory.mkdir(); (directory / "a.wav").write_bytes(_wav_bytes())
    monkeypatch.setattr(main, "OUTPUTS_DIR", str(tmp_path))
    monkeypatch.setattr(main, "_enforce_memory_pressure", lambda: None)
    # No provider execution. Keep the real durable outbox transaction.
    monkeypatch.setattr("transactional_outbox.dispatch_outbox_event", lambda *a, **k: {"status": "pending"})
    fields = creative.effective_settings(campaign, item.render_overrides)
    fields = {k: str(v).lower() if isinstance(v, bool) else str(v) for k, v in fields.items() if v is not None}
    fields.update(job_id=job.job_id, artist=item.artist, song_title=item.title,
                  segments_json=json.dumps(segments), base_revision="0", campaign_creative_revision="1")
    denied = client.post("/generate", headers=auth, data=fields)
    assert denied.status_code == 409 and not db.query(JobOutboxEvent).filter_by(job_id=job.job_id, event_type="pipeline.enqueue").count()
    batch.approve_campaign_lyrics(campaign.id, job.job_id, batch.LyricsApprovalRequest(
        editor_revision=0, confirmed_line_ids=["line-1"], lyrics_confirmed=True, timings_confirmed=True,
        heard_against_audio=True), actor, db)
    sent = client.post("/generate", headers=auth, data=fields)
    assert sent.status_code == 200, sent.text
    db.expire_all()
    event = db.query(JobOutboxEvent).filter_by(job_id=job.job_id, event_type="pipeline.enqueue").one()
    assert event.payload["pipeline_kwargs"]["effect"] == fields["effect"]
    assert event.payload["pipeline_kwargs"]["movement_style"] == fields["movement_style"]
    assert job.render_params["campaign_creative_receipt"]["assignment"]["revision"] == 1
    history = client.get(f"/batch/campaigns/{campaign.id}/videos", headers=auth)
    assert [r["job_id"] for r in history.json()["items"]] == [job.job_id]
    again = client.post("/generate", headers=auth, data=fields)
    assert again.status_code in (200, 409)
    assert db.query(JobOutboxEvent).filter_by(job_id=job.job_id, event_type="pipeline.enqueue").count() == 1
