"""The editor's polling payload must expose final-render QC state."""

import uuid

from database import Job


def test_status_includes_delivery_qc(client, admin_token, db):
    me = client.get(
        "/auth/me", headers={"Authorization": f"Bearer {admin_token}"},
    ).json()
    job_id = uuid.uuid4().hex[:12]
    report = {
        "schema_version": "genly-delivery-qc-runtime-v1",
        "status": "COMPLETE",
        "mode": "observe",
        "decision": "PASS",
        "summary": {"open_count": 0, "fail_count": 0, "warn_count": 0},
        "approval": {"blocked": False, "can_approve": True},
    }
    db.add(Job(
        job_id=job_id,
        user_id=me["id"],
        tenant_id=me["tenant_id"],
        artist="QC Artist",
        song_title="QC Song",
        filename="qc.wav",
        status="pending_review",
        progress=100,
        delivery_qc=report,
    ))
    db.commit()

    response = client.get(
        f"/status/{job_id}",
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert response.status_code == 200, response.text
    assert response.json()["delivery_qc"] == report
