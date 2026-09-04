"""Reviewer decisions and external label results must persist safely."""

import uuid

from database import Job, ProductEvent


def _me(client, token):
    return client.get(
        "/auth/me", headers={"Authorization": f"Bearer {token}"},
    ).json()


def _job(db, owner, *, status="COMPLETE"):
    job = Job(
        job_id=uuid.uuid4().hex[:12],
        user_id=owner["id"],
        tenant_id=owner["tenant_id"],
        artist="QC Artist",
        song_title="QC Song",
        filename="qc.wav",
        status="pending_review",
        progress=100,
        delivery_qc={
            "schema_version": "genly-delivery-qc-runtime-v1",
            "status": status,
            "mode": "observe",
            "decision": "REVIEW",
            "summary": {"open_count": 1, "fail_count": 0, "warn_count": 1},
            "issues": [{
                "issue_id": "issue-1",
                "status": "OPEN",
                "severity": "WARN",
                "category": "timing",
                "code": "LYRIC_OVERLAP",
            }],
        },
    )
    db.add(job)
    db.commit()
    return job


def test_reviewer_decision_persists_and_closes_issue(client, user_token, db):
    owner = _me(client, user_token)
    job = _job(db, owner)

    response = client.post(
        f"/jobs/{job.job_id}/delivery-qc/issues/issue-1/decision",
        headers={"Authorization": f"Bearer {user_token}"},
        json={"decision": "acknowledged", "reason": "audio_preview_checked"},
    )

    assert response.status_code == 200, response.text
    report = response.json()["delivery_qc"]
    assert report["issues"][0]["status"] == "ACKNOWLEDGED"
    assert report["issues"][0]["operator_decision"]["reason"] == "audio_preview_checked"
    assert report["summary"] == {"open_count": 0, "fail_count": 0, "warn_count": 0}
    db.expire_all()
    stored = db.query(Job).filter(Job.job_id == job.job_id).one()
    assert stored.delivery_qc["issues"][0]["status"] == "ACKNOWLEDGED"
    event = db.query(ProductEvent).filter(
        ProductEvent.job_id == job.job_id,
        ProductEvent.name == "delivery_qc_issue_decision",
    ).one()
    assert event.properties["decision"] == "acknowledged"


def test_reviewer_decision_rejects_stale_report(client, user_token, db):
    owner = _me(client, user_token)
    job = _job(db, owner, status="STALE")

    response = client.post(
        f"/jobs/{job.job_id}/delivery-qc/issues/issue-1/decision",
        headers={"Authorization": f"Bearer {user_token}"},
        json={"decision": "acknowledged"},
    )

    assert response.status_code == 409
    assert response.json()["detail"] == "delivery_qc_report_stale"


def test_reviewer_cannot_decide_another_tenants_issue(
    client, admin_token, user_token, db,
):
    admin = _me(client, admin_token)
    job = _job(db, admin)

    response = client.post(
        f"/jobs/{job.job_id}/delivery-qc/issues/issue-1/decision",
        headers={"Authorization": f"Bearer {user_token}"},
        json={"decision": "acknowledged"},
    )

    assert response.status_code == 404


def test_mandatory_check_requires_signed_manual_resolution(client, user_token, db):
    owner = _me(client, user_token)
    job = _job(db, owner)
    report = dict(job.delivery_qc)
    report["summary"] = {"open_count": 1, "fail_count": 1, "warn_count": 0}
    report["issues"][0].update({
        "severity": "FAIL",
        "code": "UMG_BLACK_BARS",
        "manual_verification_required": True,
    })
    job.delivery_qc = report
    db.commit()

    rejected = client.post(
        f"/jobs/{job.job_id}/delivery-qc/issues/issue-1/decision",
        headers={"Authorization": f"Bearer {user_token}"},
        json={"decision": "acknowledged"},
    )
    assert rejected.status_code == 422
    assert rejected.json()["detail"] == (
        "mandatory_reviewer_check_requires_signed_manual_resolution"
    )

    signed = client.post(
        f"/jobs/{job.job_id}/delivery-qc/issues/issue-1/decision",
        headers={"Authorization": f"Bearer {user_token}"},
        json={"decision": "resolved_manual", "reason": "full_video_reviewed"},
    )
    assert signed.status_code == 200, signed.text
    issue = signed.json()["delivery_qc"]["issues"][0]
    assert issue["status"] == "RESOLVED_MANUAL"
    assert issue["operator_decision"]["reviewer_name"]
    assert issue["operator_decision"]["decided_at"]


def test_external_qc_result_is_admin_only_and_persists(
    client, admin_token, user_token, db,
):
    owner = _me(client, user_token)
    job = _job(db, owner)
    path = f"/jobs/{job.job_id}/delivery-qc/external-result"
    payload = {"source": "umg", "report_id": "umg-2026-08-28", "finding_count": 0}

    forbidden = client.post(
        path,
        headers={"Authorization": f"Bearer {user_token}"},
        json=payload,
    )
    assert forbidden.status_code == 403

    response = client.post(
        path,
        headers={"Authorization": f"Bearer {admin_token}"},
        json=payload,
    )
    assert response.status_code == 200, response.text
    assert response.json()["external_result"]["finding_count"] == 0
    db.expire_all()
    stored = db.query(Job).filter(Job.job_id == job.job_id).one()
    assert stored.delivery_qc["external_results"][-1]["report_id"] == "umg-2026-08-28"


def test_external_qc_findings_are_normalized_and_become_a_regression_case(
    client, admin_token, db,
):
    admin = _me(client, admin_token)
    job = _job(db, admin)
    report = dict(job.delivery_qc)
    report["issues"] = [{
        **report["issues"][0],
        "code": "LYRIC_ORTHOGRAPHY_MISMATCH",
        "actual": "JAMAS", "expected": "JAMÁS",
    }]
    job.delivery_qc = report
    db.commit()

    response = client.post(
        f"/jobs/{job.job_id}/delivery-qc/external-result",
        headers={"Authorization": f"Bearer {admin_token}"},
        json={
            "source": "umg", "report_id": "umg-regression-1",
            "finding_count": 1,
            "findings": [{
                "description": 'Misspelled in lyrics, "JAMAS" should be "JAMÁS"',
                "timecode": "00:01:15:24",
            }],
        },
    )

    assert response.status_code == 200, response.text
    result = response.json()["external_result"]
    assert result["schema_version"] == "genly-external-qc-regression-v1"
    assert result["findings"][0]["code"] == "LYRIC_ORTHOGRAPHY_MISMATCH"
    assert result["regression"]["gate_passed"] is True
    assert result["regression"]["recall"] == 1.0


def test_external_qc_rejects_mismatched_finding_count(client, admin_token, db):
    admin = _me(client, admin_token)
    job = _job(db, admin)

    response = client.post(
        f"/jobs/{job.job_id}/delivery-qc/external-result",
        headers={"Authorization": f"Bearer {admin_token}"},
        json={
            "source": "umg", "report_id": "broken-count",
            "finding_count": 2,
            "findings": [{
                "description": 'Misspelled in lyrics, "JAMAS" should be "JAMÁS"',
            }],
        },
    )

    assert response.status_code == 422
    assert response.json()["detail"] == "external_qc_finding_count_mismatch"
