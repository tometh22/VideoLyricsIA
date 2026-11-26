"""Cerrar un pedido exige que cada punto esté en la letra publicada, o una
justificación interna por punto: en Cambios (manual, confirmar publicación,
publicar el corte revisado) y en Campañas (cerrar y envío masivo)."""
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from fastapi import HTTPException

import campaign_change_requests as inbox
from database import AuditLog, Delivery, DeliveryChangeRequest, EditorVersion
from delivery_freshness import render_fingerprint
from tests.conftest import auth
from tests.test_campaign_pipeline import add_job, setup  # noqa: F401  (fixture)
from tests.test_campaign_change_requests import cleanup, publish  # noqa: F401  (fixture)
from tests.test_change_request_review_api import _published_without_ticking, manual_request  # noqa: F401
from tests.test_deliveries import (  # noqa: F401  (fixtures, incl. autouse)
    _portal_token_env, _publication_copy_succeeds, all_r2_files_present, approved_job,
)

CHORUS = [
    {"start": 0.0, "end": 3.0, "text": "Ven y súmate a la ronda de los martes"},
    {"start": 9.0, "end": 12.0, "text": "Ven y súmate a la ronda de los martes"},
]
COMMENT = "0:01 Vení sumate a la ronda de los martes // 0:10 Vení sumate a la ronda de los martes"


def approve(db, job, segments, *, at, revision):
    db.add(EditorVersion(id=str(uuid.uuid4()), job_id=job.job_id, tenant_id=job.tenant_id, revision=revision,
                         segments=segments, reason="test", is_approved=True, created_at=at))
    db.flush()


def half_fixed(db, job, request):
    """La versión que vio el cliente y una corrección que arregló sólo la primera aparición."""
    approve(db, job, CHORUS, at=request.submitted_at - timedelta(hours=1), revision=90)
    fixed = [dict(row) for row in CHORUS]
    fixed[0]["text"] = "Vení sumate a la ronda de los martes"
    approve(db, job, fixed, at=datetime.now(timezone.utc) - timedelta(seconds=5), revision=91)
    db.commit()


def test_manual_close_lists_each_point_and_needs_a_reason_for_the_failing_one(client, admin_token, manual_request, db):
    job, _, cr = manual_request
    cr.comment = COMMENT
    db.commit()
    half_fixed(db, job, cr)
    url = f"/admin/change-requests/{cr.id}/resolve"
    shown = client.get(f"/admin/change-requests/{cr.id}/verification", headers=auth(admin_token)).json()
    assert [p["status"] for p in shown["verification"]["points"]] == ["ok", "fail"]
    assert shown["verification"]["revision"] == 91

    blocked = client.post(url, headers=auth(admin_token), json={"resolution_note": "Corregido"})
    assert blocked.status_code == 409, blocked.text
    detail = blocked.json()["detail"]
    assert detail["code"] == "change_request_points_unverified"
    [failing] = detail["failing"]
    db.expire_all()
    assert db.get(DeliveryChangeRequest, cr.id).resolved_at is None

    closed = client.post(url, headers=auth(admin_token), json={
        "resolution_note": "Corregido", "point_overrides": {failing: "El cliente aceptó dejar la segunda así"}})
    assert closed.status_code == 200, closed.text
    db.expire_all()
    stored = db.get(DeliveryChangeRequest, cr.id)
    assert stored.resolution_note == "Corregido"  # la justificación no viaja al portal
    audit = db.query(AuditLog).filter_by(action="delivery.change_request.resolve").order_by(AuditLog.id.desc()).first()
    points = audit.detail["verification"]["points"]
    assert [p.get("override") for p in points if p["status"] == "fail"] == ["El cliente aceptó dejar la segunda así"]


def test_manual_close_is_unchanged_when_every_point_is_in_the_lyrics(client, admin_token, manual_request, db):
    job, _, cr = manual_request
    cr.comment = "0:01 Vení sumate a la ronda de los martes"
    db.commit()
    half_fixed(db, job, cr)
    response = client.post(f"/admin/change-requests/{cr.id}/resolve", headers=auth(admin_token),
                           json={"resolution_note": "Corregido"})
    assert response.status_code == 200, response.text


def test_the_kill_switch_restores_the_previous_close(client, admin_token, manual_request, db, monkeypatch):
    monkeypatch.setenv("CHANGE_REQUEST_CLOSE_VERIFICATION", "0")
    job, _, cr = manual_request
    cr.comment = COMMENT
    db.commit()
    half_fixed(db, job, cr)
    response = client.post(f"/admin/change-requests/{cr.id}/resolve", headers=auth(admin_token),
                           json={"resolution_note": "Corregido"})
    assert response.status_code == 200, response.text


def test_confirming_a_publication_checks_each_point(client, admin_token, manual_request, db):
    job, document, cr, delivery = _published_without_ticking(client, admin_token, manual_request, db)
    cr.comment = COMMENT
    db.commit()
    half_fixed(db, job, cr)
    body = {"reviewed_render_fingerprint": render_fingerprint(job), "reviewed_editor_revision": document.revision,
            "confirmed_items": 2}
    url = f"/admin/change-requests/{cr.id}/confirm-publication"
    blocked = client.post(url, headers=auth(admin_token), json=body)
    assert blocked.status_code == 409, blocked.text
    [failing] = blocked.json()["detail"]["failing"]
    closed = client.post(url, headers=auth(admin_token), json={**body, "point_overrides": {failing: "Verificado en el video"}})
    assert closed.status_code == 200, closed.text
    db.expire_all()
    assert db.get(DeliveryChangeRequest, cr.id).resolution_source == "publication"


def test_publishing_the_reviewed_cut_keeps_a_half_fixed_request_open(client, admin_token, manual_request, db):
    job, document, cr = manual_request
    cr.comment = COMMENT
    job.status = "pending_review"
    job.previous_versions = [{"archived_at": datetime.now(timezone.utc).isoformat()}]
    job.render_params = {"_rendered_segments_revision": document.revision,
                         "_rendered_at": datetime.now(timezone.utc).isoformat()}
    from tests.test_deliveries import _signed_umg_qc_report
    db.flush()
    db.refresh(job)
    job.delivery_qc = _signed_umg_qc_report(job, job.approved_by)
    db.commit()
    half_fixed(db, job, cr)
    body = dict(portal_id="argentina", change_request_id=cr.id,
                reviewed_render_fingerprint=render_fingerprint(job), reviewed_editor_revision=document.revision)
    response = client.post(f"/admin/deliveries/from-job/{job.job_id}", headers=auth(admin_token), json=body)
    assert response.status_code == 200, response.text
    assert response.json()["resolved_change_requests"] == []
    [kept] = response.json()["kept_open_change_requests"]
    assert kept["id"] == cr.id and kept["reason"] == "points_unverified"
    db.expire_all()
    assert db.get(DeliveryChangeRequest, cr.id).resolved_at is None
    assert db.query(AuditLog).filter_by(action="delivery.change_request.points_unverified").count() >= 1


def test_the_list_marks_a_request_that_repeats_a_closed_point(client, admin_token, manual_request, db):
    _, _, cr = manual_request
    cr.comment = "0:14 Dejame salir temprano"
    cr.submitted_at = datetime.now(timezone.utc) - timedelta(days=3)
    cr.resolved_at = datetime.now(timezone.utc) - timedelta(days=2)
    again = DeliveryChangeRequest(delivery_id=cr.delivery_id, comment="0:15 Dejame salir temprano (sigue igual)",
                                  submitted_at=datetime.now(timezone.utc) - timedelta(days=1))
    db.add(again)
    db.commit()
    items = client.get("/admin/change-requests?status=all", headers=auth(admin_token)).json()["items"]
    by_id = {item["id"]: item for item in items}
    assert by_id[again.id]["recurrences"][0]["request_id"] == cr.id
    assert by_id[cr.id]["recurrences"] == []


@pytest.fixture
def campaign_actions(monkeypatch):
    monkeypatch.setenv("CAMPAIGN_CHANGE_REQUESTS_ENABLED", "1")
    monkeypatch.setenv("CAMPAIGN_CHANGE_REQUEST_ACTIONS", "1")


def test_closing_from_a_campaign_needs_the_same_reasons(db, setup, campaign_actions, cleanup):
    campaign, items, actor = setup
    job = add_job(db, campaign, actor, item=items[0], status="done")
    db.commit()
    delivery = publish(db, cleanup, job)
    row = DeliveryChangeRequest(delivery_id=delivery.id, comment=COMMENT,
                                submitted_at=datetime.now(timezone.utc) - timedelta(minutes=30))
    db.add(row)
    db.commit()
    half_fixed(db, job, row)
    shown = inbox.campaign_change_request_verification(campaign.id, row.id, actor, db, db)
    assert [p["status"] for p in shown["verification"]["points"]] == ["ok", "fail"]
    with pytest.raises(HTTPException) as blocked:
        inbox.resolve_campaign_change_request(campaign.id, row.id, inbox.ResolveBody(resolution_note="Listo"), actor, db, db)
    assert blocked.value.status_code == 409
    [failing] = blocked.value.detail["failing"]
    db.rollback()
    result = inbox.resolve_campaign_change_request(
        campaign.id, row.id, inbox.ResolveBody(resolution_note="Listo", point_overrides={failing: "Se dejó así a pedido"}),
        actor, db, db)
    assert result["ok"] is True
    db.expire_all()
    assert db.get(DeliveryChangeRequest, row.id).resolution_note == "Listo"
    assert db.get(Delivery, delivery.id) is not None
