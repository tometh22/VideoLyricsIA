"""CHANGE_REQUEST_PUBLISH_GUARD_ENABLED en los caminos individuales:
"Enviar a UMG" (/admin/deliveries/from-job), la publicación revisada desde
Cambios y "confirmar publicación"."""
import uuid
from datetime import datetime, timedelta, timezone

import pytest

from database import AuditLog, DeliveryChangeRequest, EditorVersion
from delivery_freshness import render_fingerprint
from tests.conftest import auth
from tests.test_change_request_review_api import _published_without_ticking, manual_request  # noqa: F401
from tests.test_deliveries import _portal_token_env, _publication_copy_succeeds, all_r2_files_present, approved_job  # noqa: F401
from tests.test_publication_case_binding import _prepare, _publish, fake_r2  # noqa: F401

PERO = '0:46 la línea es "Pintamos el mono, pero NOS DA lo mismo"'


@pytest.fixture
def guard_on(monkeypatch):
    monkeypatch.setenv("CHANGE_REQUEST_PUBLISH_GUARD_ENABLED", "1")


def approve(db, job, at, line, revision=1):
    db.add(EditorVersion(id=str(uuid.uuid4()), job_id=job.job_id, tenant_id=job.tenant_id, revision=revision,
                         segments=[{"start": 43.97, "end": 46.97, "text": line}], created_at=at,
                         reason="approve", is_approved=True))
    db.commit()


def test_resending_a_version_older_than_an_open_request_is_refused(client, admin_token, approved_job, db, all_r2_files_present, guard_on):
    first = client.post(f"/admin/deliveries/from-job/{approved_job.job_id}", headers=auth(admin_token), json={})
    assert first.status_code == 200, first.text
    approve(db, approved_job, datetime.now(timezone.utc) - timedelta(days=20), "Barricada policial")
    request = DeliveryChangeRequest(delivery_id=first.json()["delivery_id"], comment="0:19 Barricada policial, hay que enfrentar",
                                    submitted_at=datetime.now(timezone.utc) - timedelta(days=5))
    db.add(request); db.commit()

    blocked = client.post(f"/admin/deliveries/from-job/{approved_job.job_id}", headers=auth(admin_token), json={})
    assert blocked.status_code == 409, blocked.text
    detail = blocked.json()["detail"]
    assert detail["code"] == "change_request_newer_than_version"
    assert [item["id"] for item in detail["requests"]] == [request.id]
    assert detail["requests"][0]["submitted_at"] and f"#{request.id}" in detail["message"]

    no_reason = client.post(f"/admin/deliveries/from-job/{approved_job.job_id}", headers=auth(admin_token),
                            json={"publish_anyway": True})
    assert no_reason.status_code == 422 and no_reason.json()["detail"]["code"] == "override_reason_required"

    forced = client.post(f"/admin/deliveries/from-job/{approved_job.job_id}", headers=auth(admin_token),
                         json={"publish_anyway": True, "override_reason": "Reenvío pedido por UMG para otro territorio"})
    assert forced.status_code == 200, forced.text
    audit = (db.query(AuditLog).filter(AuditLog.action == "delivery.change_request_guard.override")
             .order_by(AuditLog.id.desc()).first())
    assert audit.detail["job_id"] == approved_job.job_id and audit.detail["change_request_ids"] == [request.id]
    assert audit.detail["reason"] == "Reenvío pedido por UMG para otro territorio"
    assert audit.detail["source"] == "admin_publish"


def test_flag_off_resend_is_unchanged(client, admin_token, approved_job, db, all_r2_files_present):
    first = client.post(f"/admin/deliveries/from-job/{approved_job.job_id}", headers=auth(admin_token), json={})
    approve(db, approved_job, datetime.now(timezone.utc) - timedelta(days=20), "Barricada policial")
    db.add(DeliveryChangeRequest(delivery_id=first.json()["delivery_id"], comment="0:19 Barricada",
                                 submitted_at=datetime.now(timezone.utc) - timedelta(days=5)))
    db.commit()
    assert client.post(f"/admin/deliveries/from-job/{approved_job.job_id}", headers=auth(admin_token), json={}).status_code == 200


@pytest.mark.parametrize("line, closes", [
    ("Pintamos el mono, que nos da lo mismo", False),
    ("Pintamos el mono, pero nos da lo mismo", True),
])
def test_reviewed_publication_keeps_a_text_request_open_when_the_text_is_missing(
    client, admin_token, approved_job, db, fake_r2, guard_on, line, closes,
):
    job_id, _delivery_id, ids, body = _prepare(client, admin_token, db, approved_job, fake_r2)
    db.get(DeliveryChangeRequest, ids[0]).comment = PERO
    db.commit()
    approve(db, approved_job, datetime.now(timezone.utc) - timedelta(seconds=30), line, revision=99)
    response = _publish(client, admin_token, job_id, **body)
    assert response.status_code == 200, response.text   # the publication itself never stops
    db.expire_all()
    request = db.get(DeliveryChangeRequest, ids[0])
    if closes:
        assert response.json()["resolved_change_requests"] == [ids[0]] and request.resolution_source == "publication"
        return
    assert response.json()["resolved_change_requests"] == []
    [kept] = response.json()["kept_open_change_requests"]
    assert kept["id"] == ids[0] and kept["reason"] == "requested_text_not_found"
    assert kept["missing"] == ["Pintamos el mono, pero NOS DA lo mismo"]
    assert request.resolved_at is None
    audit = (db.query(AuditLog).filter(AuditLog.action == "delivery.change_request.requested_text_not_found")
             .order_by(AuditLog.id.desc()).first())
    assert audit.detail["change_request_id"] == ids[0] and audit.detail["source"] == "admin_publish"


def test_confirm_publication_refuses_when_the_requested_text_is_not_published(client, admin_token, manual_request, db, guard_on):
    job, document, cr, _delivery = _published_without_ticking(client, admin_token, manual_request, db)
    cr.comment = PERO
    db.commit()
    approve(db, job, datetime.now(timezone.utc) - timedelta(seconds=30), "Pintamos el mono, que nos da lo mismo", revision=99)
    response = client.post(f"/admin/change-requests/{cr.id}/confirm-publication", headers=auth(admin_token),
                           json={"reviewed_render_fingerprint": render_fingerprint(job),
                                 "reviewed_editor_revision": document.revision})
    assert response.status_code == 409, response.text
    assert response.json()["detail"]["code"] == "requested_text_not_found"
    assert response.json()["detail"]["missing"] == ["Pintamos el mono, pero NOS DA lo mismo"]
    db.expire_all()
    assert db.get(DeliveryChangeRequest, cr.id).resolved_at is None
