from __future__ import annotations

import uuid

from database import AuditLog, Job
from transcription_quality import evaluate


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def test_quality_ack_requires_exact_windows_and_is_idempotent(
    client, user_token, db, monkeypatch,
):
    monkeypatch.setenv("TRANSCRIPTION_QUALITY_MODE", "enforce")
    monkeypatch.setenv("TRANSCRIPTION_QUALITY_ENFORCE_PERCENT", "100")
    me = client.get("/auth/me", headers=_auth(user_token)).json()
    job_id = f"qv5{uuid.uuid4().hex[:8]}"
    segments = [{"start": 43.0, "end": 52.0, "text": "review me"}]
    windows = [{
        "id": "qw_exact", "start": 43.0, "end": 52.0,
        "reasons": ["text_mismatch"], "segment_indices": [0],
    }]
    quality = evaluate(
        segments,
        {"audio_coverage": .8, "text_mismatches": 1,
         "voiced_gap_s": 0, "uncovered_seconds": 0},
        unsafe_windows=windows,
    )
    quality["evaluated_revision"] = 3
    db.add(Job(
        job_id=job_id, user_id=me["id"], tenant_id=me["tenant_id"],
        filename="quality.wav", artist="Quality", status="transcribed_pending",
        segments_json=segments, segments_revision=3,
        transcription_quality=quality,
    ))
    db.commit()
    try:
        wrong = client.post(
            f"/jobs/{job_id}/transcription-quality/acknowledge",
            headers=_auth(user_token),
            json={"base_revision": 3, "confirmed_window_ids": []},
        )
        assert wrong.status_code == 422

        body = {"base_revision": 3, "confirmed_window_ids": ["qw_exact"]}
        first = client.post(
            f"/jobs/{job_id}/transcription-quality/acknowledge",
            headers=_auth(user_token), json=body,
        )
        assert first.status_code == 200, first.text
        second = client.post(
            f"/jobs/{job_id}/transcription-quality/acknowledge",
            headers=_auth(user_token), json=body,
        )
        assert second.status_code == 200
        assert second.json()["idempotent"] is True

        db.expire_all()
        logs = db.query(AuditLog).filter(
            AuditLog.action == "lyrics.quality_acknowledged",
            AuditLog.detail["job_id"].as_string() == job_id,
        ).all()
        assert len(logs) == 1
    finally:
        db.query(AuditLog).filter(
            AuditLog.action == "lyrics.quality_acknowledged",
            AuditLog.detail["job_id"].as_string() == job_id,
        ).delete(synchronize_session=False)
        db.query(Job).filter(Job.job_id == job_id).delete(
            synchronize_session=False,
        )
        db.commit()
