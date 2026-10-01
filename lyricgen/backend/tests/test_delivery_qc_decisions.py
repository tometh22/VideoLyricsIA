"""Reviewer decisions and external label results must persist safely."""

import uuid

from database import AuditLog, Job, ProductEvent


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
            "report_id": "synthetic-viewed-report",
            "status": status,
            "mode": "observe",
            "segments_revision": 0,
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
    db.refresh(job)
    from delivery_qc_runtime import delivery_qc_source_fingerprint
    input_fingerprint = delivery_qc_source_fingerprint(job)
    job.delivery_qc = {
        **job.delivery_qc,
        "source_fingerprint": input_fingerprint,
        "job_input_fingerprint": input_fingerprint,
    }
    db.commit()
    return job


def test_reviewer_decision_persists_and_closes_issue(client, user_token, db):
    owner = _me(client, user_token)
    job = _job(db, owner)

    response = client.post(
        f"/jobs/{job.job_id}/delivery-qc/issues/issue-1/decision",
        headers={"Authorization": f"Bearer {user_token}"},
        json={"decision": "acknowledged", "reason": "audio_preview_checked", "expected_report_id": "synthetic-viewed-report"},
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
        json={"decision": "acknowledged", "expected_report_id": "synthetic-viewed-report"},
    )
    assert rejected.status_code == 422
    assert rejected.json()["detail"] == (
        "mandatory_reviewer_check_requires_signed_manual_resolution"
    )

    signed = client.post(
        f"/jobs/{job.job_id}/delivery-qc/issues/issue-1/decision",
        headers={"Authorization": f"Bearer {user_token}"},
        json={"decision": "resolved_manual", "reason": "full_video_reviewed", "expected_report_id": "synthetic-viewed-report"},
    )
    assert signed.status_code == 200, signed.text
    issue = signed.json()["delivery_qc"]["issues"][0]
    assert issue["status"] == "RESOLVED_MANUAL"
    assert issue["operator_decision"]["reviewer_name"]
    assert issue["operator_decision"]["decided_at"]


def test_full_video_attestation_resolves_checklist_once_for_current_render(
    client, user_token, db,
):
    from delivery_qc_runtime import MANDATORY_REVIEW_CHECKS, qc_input_fingerprint

    owner = _me(client, user_token)
    job = _job(db, owner)
    job.delivery_profile = "umg"
    job.umg_spec = {"frame_size": "HD", "fps": 29.97}
    report = dict(job.delivery_qc)
    report["issues"] = [*report["issues"], *[
        {
            "issue_id": f"manual-{code}", "code": code,
            "summary": label, "status": "OPEN", "severity": "FAIL",
            "result_status": "REVIEW", "manual_verification_required": True,
            "blocking": False,
        }
        for code, label, _description in MANDATORY_REVIEW_CHECKS
    ]]
    fingerprint = qc_input_fingerprint(job)
    report.update({
        "job_input_fingerprint": fingerprint,
        "source_fingerprint": fingerprint,
        "report_id": "current-render-1",
    })
    job.delivery_qc = report
    db.commit()

    response = client.post(
        f"/jobs/{job.job_id}/delivery-qc/review-attestation",
        headers={"Authorization": f"Bearer {user_token}"},
        json={"confirmed": True, "expected_report_id": "current-render-1"},
    )

    assert response.status_code == 200, response.text
    updated = response.json()["delivery_qc"]
    manual_rows = [row for row in updated["issues"] if row.get("manual_verification_required")]
    assert len(manual_rows) == len(MANDATORY_REVIEW_CHECKS)
    assert all(row["status"] == "RESOLVED_MANUAL" for row in manual_rows)
    assert all(row["operator_decision"]["user_id"] == owner["id"] for row in manual_rows)
    assert updated["approval"]["can_approve"] is True
    event = db.query(ProductEvent).filter(
        ProductEvent.job_id == job.job_id,
        ProductEvent.name == "delivery_qc_review_attestation",
    ).one()
    assert event.properties["issue_count"] == len(MANDATORY_REVIEW_CHECKS)
    audit = db.query(AuditLog).filter(
        AuditLog.user_id == owner["id"],
        AuditLog.action == "delivery_qc.review_attestation",
    ).one()
    assert audit.detail["report_id"] == "current-render-1"


def test_full_video_attestation_rejects_stale_render_and_does_not_sign(
    client, user_token, db,
):
    owner = _me(client, user_token)
    job = _job(db, owner)
    response = client.post(
        f"/jobs/{job.job_id}/delivery-qc/review-attestation",
        headers={"Authorization": f"Bearer {user_token}"},
        json={"confirmed": True, "expected_report_id": "old-render"},
    )
    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "delivery_qc_preview_changed"
    assert not any(row.get("manual_verification_required") for row in job.delivery_qc["issues"])


def test_full_video_attestation_cannot_override_an_objective_failure(
    client, user_token, db,
):
    from delivery_qc_runtime import MANDATORY_REVIEW_CHECKS, qc_input_fingerprint

    owner = _me(client, user_token)
    job = _job(db, owner)
    job.delivery_profile = "umg"
    job.umg_spec = {"frame_size": "HD"}
    report = dict(job.delivery_qc)
    report["issues"] = [*report["issues"], {
        "issue_id": "objective-fail", "code": "MEDIA_DURATION_MISMATCH",
        "summary": "Duración distinta a la especificación", "status": "OPEN",
        "severity": "FAIL", "result_status": "FAIL", "blocking": True,
    }, *[
        {"issue_id": f"manual-{code}", "code": code, "status": "OPEN",
         "severity": "FAIL", "result_status": "REVIEW", "blocking": False,
         "manual_verification_required": True}
        for code, _label, _description in MANDATORY_REVIEW_CHECKS
    ]]
    fingerprint = qc_input_fingerprint(job)
    report.update({"job_input_fingerprint": fingerprint, "source_fingerprint": fingerprint,
                   "report_id": "render-with-failure"})
    job.delivery_qc = report
    db.commit()

    response = client.post(
        f"/jobs/{job.job_id}/delivery-qc/review-attestation",
        headers={"Authorization": f"Bearer {user_token}"},
        json={"confirmed": True, "expected_report_id": "render-with-failure"},
    )

    assert response.status_code == 422
    assert response.json()["detail"] == "blocking_fail_requires_correction_and_new_preflight"
    assert all(row["status"] == "OPEN" for row in job.delivery_qc["issues"] if row.get("manual_verification_required"))


def test_external_qc_result_is_admin_only_and_persists(
    client, admin_token, user_token, db,
):
    owner = _me(client, user_token)
    job = _job(db, owner)
    path = f"/jobs/{job.job_id}/delivery-qc/external-result"
    payload = {"source": "umg", "report_id": "umg-2026-08-28", "finding_count": 0,
               "expected_report_id": "synthetic-viewed-report"}

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
            "expected_report_id": "synthetic-viewed-report",
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
