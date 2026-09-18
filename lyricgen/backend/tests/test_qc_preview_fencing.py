"""Adversarial revision fences: a review of A must never authorize B.

The media detectors are stubbed, not the revision checks or DB commits. The
download interleaving exercises the real HTTP endpoint and worker source fence.
"""

from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
import uuid

import pytest

import database
import delivery_qc_runtime as runtime
from database import Job, ProductEvent


def _headers(token):
    return {"Authorization": f"Bearer {token}"}


def _report(report_id="report-A"):
    return {
        "schema_version": "genly-delivery-qc-runtime-v1",
        "report_id": report_id, "status": "COMPLETE", "mode": "observe",
        "decision": "REVIEW",
        "summary": {"open_count": 1, "fail_count": 0, "warn_count": 1},
        "issues": [{"issue_id": "same-issue", "status": "OPEN",
                    "severity": "WARN", "category": "timing",
                    "code": "LYRIC_OVERLAP"}],
    }


def _job(client, token, db):
    owner = client.get("/auth/me", headers=_headers(token)).json()
    job_id = uuid.uuid4().hex[:12]
    db.add(Job(
        job_id=job_id, user_id=owner["id"], tenant_id=owner["tenant_id"],
        artist="QC Fence Artist", song_title="QC Fence Song", filename="qc.wav",
        status="pending_review", progress=100, workload_class="interactive",
        segments_json=[{"start": 0, "end": 2, "text": "Hola"}],
        segments_revision=1, audio_revision=1, input_audio_sha256="a" * 64,
        transcription_quality={}, delivery_qc=_report(),
        s3_keys={"video": f"synthetic/{job_id}/video.mp4"},
    ))
    db.commit()
    return job_id


def _stored(db, job_id):
    db.expire_all()
    value = deepcopy(db.query(Job).filter(Job.job_id == job_id).one().delivery_qc)
    db.rollback()
    return value


@pytest.mark.parametrize("preview", ["report-A", None])
def test_same_issue_id_does_not_authorize_replacement_report(
    client, user_token, db, preview,
):
    job_id = _job(client, user_token, db)
    job = db.query(Job).filter(Job.job_id == job_id).one()
    job.delivery_qc = _report("report-B")
    db.commit()

    response = client.post(
        f"/jobs/{job_id}/delivery-qc/issues/same-issue/decision",
        headers=_headers(user_token),
        json={"decision": "acknowledged", "expected_report_id": preview},
    )

    assert response.status_code == 409, response.text
    assert response.json()["detail"]["code"] == "delivery_qc_preview_changed"
    assert _stored(db, job_id) == _report("report-B")
    assert db.query(ProductEvent).filter(
        ProductEvent.job_id == job_id,
        ProductEvent.name == "delivery_qc_issue_decision",
    ).count() == 0
    db.rollback()

    fresh = client.post(
        f"/jobs/{job_id}/delivery-qc/issues/same-issue/decision",
        headers=_headers(user_token),
        json={"decision": "acknowledged", "expected_report_id": "report-B"},
    )
    assert fresh.status_code == 200, fresh.text
    assert _stored(db, job_id)["issues"][0]["status"] == "ACKNOWLEDGED"
    competing = client.post(
        f"/jobs/{job_id}/delivery-qc/issues/same-issue/decision",
        headers=_headers(user_token),
        json={"decision": "rejected", "expected_report_id": "report-B"},
    )
    assert competing.status_code == 409
    assert _stored(db, job_id)["issues"][0]["status"] == "ACKNOWLEDGED"


def test_external_result_cannot_be_attached_to_unseen_report(client, admin_token, db):
    job_id = _job(client, admin_token, db)
    job = db.query(Job).filter(Job.job_id == job_id).one()
    job.delivery_qc = _report("report-B")
    db.commit()
    response = client.post(
        f"/jobs/{job_id}/delivery-qc/external-result",
        headers=_headers(admin_token),
        json={"source": "umg", "report_id": "external-A", "finding_count": 0,
              "expected_report_id": "report-A"},
    )
    assert response.status_code == 409, response.text
    assert response.json()["detail"]["code"] == "delivery_qc_preview_changed"
    assert _stored(db, job_id) == _report("report-B")


def test_qc_releases_transaction_before_detectors(client, user_token, db, monkeypatch):
    monkeypatch.setenv("DELIVERY_QC_MODE", "observe")
    job_id = _job(client, user_token, db)
    real_factory = database.SessionLocal
    sessions = []

    def tracked_factory():
        session = real_factory()
        sessions.append(session)
        return session

    def build(**kwargs):
        assert sessions and all(not s.in_transaction() for s in sessions)
        assert kwargs["job"].song_title == "QC Fence Song"
        return _report("report-B")

    monkeypatch.setattr(database, "SessionLocal", tracked_factory)
    monkeypatch.setattr(runtime, "build_runtime_report", build)
    assert runtime.run_delivery_qc_for_job(job_id, "unused.mp4") == _report("report-B")
    assert _stored(db, job_id) == _report("report-B")
    assert all(not s.in_transaction() for s in sessions)


@pytest.mark.parametrize("field,value", [
    ("transcription_quality", {"reference_lyrics": "Changed reference"}),
    ("filename", "replacement.wav"),
    ("workload_class", "batch"),
    ("segments_revision", 2),
    ("input_audio_sha256", "b" * 64),
])
def test_detector_input_change_discards_result(
    client, user_token, db, monkeypatch, field, value,
):
    monkeypatch.setenv("DELIVERY_QC_MODE", "observe")
    job_id = _job(client, user_token, db)

    def build(**_kwargs):
        with database.SessionLocal() as concurrent:
            job = concurrent.query(Job).filter(Job.job_id == job_id).one()
            setattr(job, field, value)
            concurrent.commit()
        return _report("must-not-persist")

    monkeypatch.setattr(runtime, "build_runtime_report", build)
    with pytest.raises(RuntimeError, match="^delivery_qc_input_changed$"):
        runtime.run_delivery_qc_for_job(job_id, "unused.mp4")
    assert _stored(db, job_id) == _report()


def test_concurrent_human_review_wins_over_detector_result(
    client, user_token, db, monkeypatch,
):
    monkeypatch.setenv("DELIVERY_QC_MODE", "observe")
    job_id = _job(client, user_token, db)
    reviewed = _report()
    reviewed["issues"][0].update(
        status="ACKNOWLEDGED", operator_decision={"reason": "reviewed-current-video"},
    )

    def build(**_kwargs):
        with database.SessionLocal() as concurrent:
            job = concurrent.query(Job).filter(Job.job_id == job_id).one()
            job.delivery_qc = reviewed
            concurrent.commit()
        return _report("must-not-persist")

    monkeypatch.setattr(runtime, "build_runtime_report", build)
    with pytest.raises(RuntimeError, match="^delivery_qc_review_changed$"):
        runtime.run_delivery_qc_for_job(job_id, "unused.mp4")
    assert _stored(db, job_id) == reviewed


@pytest.mark.parametrize("advance_segments", [True, False], ids=["new-revision", "source-key-only"])
def test_render_advancing_during_download_returns_409_without_running_detectors(
    client, user_token, db, monkeypatch, advance_segments,
):
    import main

    monkeypatch.setenv("DELIVERY_QC_MODE", "observe")
    job_id = _job(client, user_token, db)
    monkeypatch.setattr(main.storage, "is_enabled", lambda: True)

    def download(_key, path):
        Path(path).write_bytes(b"downloaded-render-A")
        with database.SessionLocal() as concurrent:
            job = concurrent.query(Job).filter(Job.job_id == job_id).one()
            if advance_segments:
                job.segments_revision += 1
            job.s3_keys = {"video": "synthetic/new-render-B.mp4"}
            concurrent.commit()
        return True

    def unexpected_build(**_kwargs):
        pytest.fail("Old downloaded render must be rejected before detectors run")

    monkeypatch.setattr(main.storage, "download_object", download)
    monkeypatch.setattr(runtime, "build_runtime_report", unexpected_build)
    response = client.post(
        f"/jobs/{job_id}/delivery-qc/recheck", headers=_headers(user_token),
    )
    assert response.status_code == 409, response.text
    assert response.json()["detail"]["code"] == "delivery_qc_input_changed"
    assert _stored(db, job_id) == _report()


def test_artifact_mutated_mid_analysis_is_not_a_valid_report(tmp_path, monkeypatch):
    monkeypatch.setenv("DELIVERY_QC_MODE", "observe")
    asset = tmp_path / "video.mp4"
    asset.write_bytes(b"render-A")
    monkeypatch.setattr(runtime, "inspect_delivery_media", lambda *args, **kwargs: {
        "probe": {"duration": 2.0, "video": {"fps": 30}, "audio_streams": 1},
        "issues": [], "abstentions": [],
    })

    def ocr(*_args, **_kwargs):
        asset.write_bytes(b"render-B")
        return {"observations": [], "issues": [], "abstentions": []}

    monkeypatch.setattr(runtime, "inspect_rendered_text", ocr)
    job = SimpleNamespace(
        artist="Artist", song_title="Song", filename="song.wav", umg_spec=None,
        segments_revision=1, edit_count=0, transcription_quality={},
        workload_class="interactive", audio_revision=1, input_audio_sha256="a" * 64,
    )
    with pytest.raises(RuntimeError, match="^delivery_qc_artifact_changed$"):
        runtime.build_runtime_report(
            job=job, video_path=str(asset),
            segments=[{"start": 0, "end": 2, "text": "Hola"}],
        )
