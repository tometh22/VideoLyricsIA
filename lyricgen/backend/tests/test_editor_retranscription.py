"""Re-transcribe a reviewed song in a reviewer-chosen language.

Real case: staging campaign job 7d45ea03941b "Gran Ídolo De Asia". WhisperX
auto-detected English and translated half a Spanish song; the job already had
saved revisions, so no product path could re-hear it. These tests run the real
endpoint, outbox payload and worker persistence (ASR is a local double) and
check that history and learning data survive.
"""

import copy
import io
import uuid
from pathlib import Path

import numpy as np
import pytest
import soundfile as sf

from tests.conftest import auth


ENGLISH = [
    {"start": 1.0, "end": 2.5, "text": "great idol of asia",
     "words": [{"word": "great", "start": 1.0, "end": 1.4, "score": 0.9}]},
    {"start": 3.0, "end": 4.5, "text": "you never come back",
     "words": [{"word": "you", "start": 3.0, "end": 3.3, "score": 0.9}]},
]
SPANISH = [
    {"start": 1.0, "end": 2.5, "text": "gran ídolo de asia",
     "words": [{"word": "gran", "start": 1.0, "end": 1.4, "score": 0.9}]},
    {"start": 3.0, "end": 4.5, "text": "nunca vas a volver",
     "words": [{"word": "nunca", "start": 3.0, "end": 3.4, "score": 0.9}]},
]


def _wav_bytes():
    sr = 16000
    t = np.arange(6 * sr) / sr
    tone = 0.2 * np.sin(2 * np.pi * 220 * t)
    data = io.BytesIO()
    sf.write(data, tone, sr, format="WAV", subtype="PCM_16")
    return data.getvalue()


@pytest.fixture
def harness(client, user_token, monkeypatch, tmp_path):
    """First transcription through the real worker, then a human edit."""
    import main
    import pipeline
    import queue_jobs
    import storage
    import vocal_sep
    from database import Job, SessionLocal

    data = _wav_bytes()
    outputs = tmp_path / "outputs"
    objects = {}
    dispatched = []
    calls = []
    monkeypatch.setenv("ASYNC_TRANSCRIBE_ENABLED", "1")
    # No Redis: the transcription runs in this process, so the fleet gate
    # (worker_fleet_ready) reports the same code. Tested separately below.
    monkeypatch.delenv("REDIS_URL", raising=False)
    for name in [
        "CTC_ALIGN_ENABLED", "ADLIB_CONSENSUS_ENABLED", "CHORUS_SNAP_ENABLED",
        "APPROVED_TEXT_REUSE_ENABLED", "TRANSCRIPTION_QUALITY_INLINE_RETRY",
        "GAP_RESCUE_ENABLED", "LORA_FAMILY_ENABLED", "WORD_VOTE_ENABLED",
        "LYRICS_FORMAT_ENABLED",
    ]:
        monkeypatch.setenv(name, "0")
    monkeypatch.setenv("VOCAL_CONTINUITY_MODE", "off")
    monkeypatch.setattr(main, "OUTPUTS_DIR", str(outputs))
    monkeypatch.setattr(pipeline, "OUTPUTS_DIR", str(outputs))
    monkeypatch.setattr(storage, "is_enabled", lambda: True)
    monkeypatch.setattr(storage, "presign_put_url", lambda tenant, jid, filename, **kw: {
        "url": "https://local.invalid/upload", "key": f"inputs/{jid}/{filename}",
        "expires_in": 900,
    })
    monkeypatch.setattr(storage, "head_object_size",
                        lambda key: len(objects[key]) if key in objects else None)
    monkeypatch.setattr(storage, "object_etag", lambda key: "local-test-etag")

    def download(key, path):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        Path(path).write_bytes(objects[key])
        return True

    def upload(path, key, *a, **kw):
        objects[key] = Path(path).read_bytes()
        return key

    monkeypatch.setattr(storage, "download_object", download)
    monkeypatch.setattr(storage, "upload_file", upload)
    monkeypatch.setattr(main, "_dispatch_outbox_after_response",
                        lambda event_id, **kw: dispatched.append(event_id))
    monkeypatch.setattr(queue_jobs, "enqueue_transcription_quality", lambda *a, **kw: None)
    monkeypatch.setattr(vocal_sep, "separate_vocals", lambda *a, **kw: None)

    state = {"segments": ENGLISH}

    async def provider(*args, **kw):
        calls.append({"job_id": args[2], **kw})
        from display_segmentation_policy import capture_source
        source = capture_source(args[2], args[3])
        segments = copy.deepcopy(state["segments"])
        return {
            "_transcription_source": source,
            "segments": segments,
            "reference_lyrics": "",
            "timing_source": "whisper_align",
            "_primary_asr_family": "integration-fixture",
            "_recognition_hypotheses": [{
                "family": "integration-fixture", "kind": "segments",
                "attempt_id": 0, "events": copy.deepcopy(segments),
                "view": "mix", "transformation": "local_test_fixture",
            }],
            "_recognition_attempt_count": 1,
            "_asr_words": [w for s in copy.deepcopy(segments) for w in s["words"]],
        }

    monkeypatch.setattr(main, "_run_transcription_for_job", provider)

    headers = auth(user_token)
    res = client.post("/upload-url", json={
        "filename": "gran-idolo.wav", "size_bytes": len(data),
        "artist": "Fito", "song_title": "Gran Ídolo De Asia",
    }, headers=headers)
    assert res.status_code == 200, res.text
    jid = res.json()["job_id"]
    with SessionLocal() as db:
        objects[db.query(Job).filter_by(job_id=jid).one().input_r2_key] = data
    queued = client.post("/transcribe-uploaded", json={"job_id": jid, "language": ""},
                         headers=headers)
    assert queued.status_code == 202, queued.text
    audio = outputs / jid / "gran-idolo.wav"
    from transcription_worker import run_transcription_job
    out = run_transcription_job(jid, str(audio), language="", filename=audio.name)
    assert out.get("status") not in {"transcription_failed", "discarded"}, out

    # A reviewer saved two corrections on top of the English text.
    from database import EditorDocument
    from editor import save_document
    with SessionLocal() as db:
        job = db.query(Job).filter_by(job_id=jid).one()
        document = db.query(EditorDocument).filter_by(job_id=jid).one()
        edited = copy.deepcopy(job.segments_json)
        edited[0]["text"] = "gran ídolo of asia"
        save_document(db, job, document, job.user_id, int(document.revision), edited, "manual")
        db.commit()
        document = db.query(EditorDocument).filter_by(job_id=jid).one()
        edited[1]["text"] = "you nunca come back"
        save_document(db, job, document, job.user_id, int(document.revision), edited, "manual")
        db.commit()
    dispatched.clear()  # the first transcription's own outbox event
    return {
        "client": client, "headers": headers, "job_id": jid, "audio": audio,
        "state": state, "calls": calls, "dispatched": dispatched,
    }


def _snapshot(jid):
    from database import CorrectionObservation, EditorDocument, EditorVersion, Job, SessionLocal
    with SessionLocal() as db:
        job = db.query(Job).filter_by(job_id=jid).one()
        document = db.query(EditorDocument).filter_by(job_id=jid).one()
        versions = {
            v.id: (v.revision, v.reason, copy.deepcopy(v.segments))
            for v in db.query(EditorVersion).filter_by(job_id=jid).all()
        }
        observations = {
            o.id: o.approved_version_id
            for o in db.query(CorrectionObservation).filter_by(job_id=jid).all()
        }
        return {
            "revision": int(job.segments_revision or 0),
            "status": job.status,
            "segments": copy.deepcopy(job.segments_json),
            "original": copy.deepcopy(document.original_segments),
            "machine_evidence": copy.deepcopy(document.machine_evidence),
            "quality": copy.deepcopy(job.transcription_quality),
            "versions": versions,
            "observations": observations,
            "active_attempt": job.active_transcription_attempt_id,
        }


def _add_observation(jid):
    from database import CorrectionObservation, EditorVersion, Job, SessionLocal
    with SessionLocal() as db:
        job = db.query(Job).filter_by(job_id=jid).one()
        version = db.query(EditorVersion).filter_by(job_id=jid).order_by(
            EditorVersion.revision.desc()).first()
        db.add(CorrectionObservation(
            id=str(uuid.uuid4()), identity_hash=uuid.uuid4().hex + uuid.uuid4().hex,
            job_id=jid, tenant_id=job.tenant_id, original_revision=0,
            approved_revision=version.revision, approved_version_id=version.id,
            original_hash="a" * 64, approved_hash="b" * 64,
            pipeline_release="release", pipeline_config_fingerprint="config",
            timing_source="whisper_align", label_tier="observed",
            source_confidence="exact", hmac_key_id="test-v1",
            categories={}, features={}, metrics={},
        ))
        db.commit()


def _set_language_resolution(jid):
    from database import Job, SessionLocal
    with SessionLocal() as db:
        job = db.query(Job).filter_by(job_id=jid).one()
        quality = dict(job.transcription_quality or {})
        quality["language_resolution"] = {"revision": job.segments_revision, "segments_hash": "x"}
        job.transcription_quality = quality
        db.commit()


def _run_worker_for(h, event_id):
    """Exactly what run_outbox_transcription does for this event."""
    from database import JobOutboxEvent, SessionLocal
    from jobs import bind_job_attempt
    from transcription_worker import run_transcription_job
    with SessionLocal() as db:
        payload = dict(db.query(JobOutboxEvent).filter_by(id=event_id).one().payload)
    kwargs = dict(payload["transcription_kwargs"])
    kwargs.pop("tenant_id", None)  # dropped by queue_jobs.enqueue_transcription
    with bind_job_attempt("transcription", event_id):
        return run_transcription_job(h["job_id"], payload["audio_path"], **kwargs)


def test_retranscribe_preserves_history_and_installs_new_baseline(harness):
    h = harness
    jid = h["job_id"]
    _add_observation(jid)
    _set_language_resolution(jid)
    before = _snapshot(jid)
    assert before["revision"] >= 2
    assert before["machine_evidence"], "first transcription froze a machine snapshot"

    res = h["client"].post(f"/jobs/{jid}/retranscribe-language", headers=h["headers"],
                           json={"language": "es", "base_revision": before["revision"]})
    assert res.status_code == 202, res.text
    body = res.json()
    assert body["status"] == "queued" and body["language"] == "es"
    assert len(h["dispatched"]) == 1
    event_id = h["dispatched"][0]

    from database import AuditLog, EditorRetranscription, JobOutboxEvent, SessionLocal
    with SessionLocal() as db:
        event = db.query(JobOutboxEvent).filter_by(id=event_id).one()
        assert event.payload["transcription_kwargs"]["language"] == "es"
        row = db.query(EditorRetranscription).filter_by(job_id=jid).one()
        assert row.attempt_id == event_id and row.status == "pending"
        assert row.base_revision == before["revision"]
        assert row.archived_machine_evidence == before["machine_evidence"]
        assert row.archived_original_segments == before["original"]
        assert row.archived_transcription_quality["language_resolution"]
        assert row.archived_version_id in before["versions"]
        audit = [log for log in db.query(AuditLog).filter_by(
            action="lyrics.language_retranscription_requested").all()
            if (log.detail or {}).get("job_id") == jid]
        assert len(audit) == 1
        assert audit[0].detail["requested_language"] == "es"
        assert audit[0].detail["base_revision"] == before["revision"]

    # Still running: a second request is rejected.
    again = h["client"].post(f"/jobs/{jid}/retranscribe-language", headers=h["headers"],
                             json={"language": "es", "base_revision": before["revision"]})
    assert again.status_code == 409
    assert again.json()["code"] == "transcription_in_progress"

    h["state"]["segments"] = SPANISH
    out = _run_worker_for(h, event_id)
    assert out.get("status") not in {"transcription_failed", "discarded"}, out
    assert h["calls"][-1]["language"] == "es"
    assert h["calls"][-1]["force_language"] is True

    after = _snapshot(jid)
    assert after["status"] == "transcribed_pending"
    assert after["revision"] > before["revision"]
    assert [s["text"] for s in after["segments"]] == [s["text"] for s in SPANISH]
    assert [s["text"] for s in after["original"]] == [s["text"] for s in SPANISH]
    # Nothing in history was deleted or rewritten; learning rows intact.
    for version_id, value in before["versions"].items():
        assert after["versions"][version_id] == value
    assert after["observations"] == before["observations"]
    new_versions = set(after["versions"]) - set(before["versions"])
    assert [after["versions"][v][1] for v in new_versions] == ["retranscription"]
    # A new, valid machine snapshot for the new attempt.
    assert after["machine_evidence"] and after["machine_evidence"] != before["machine_evidence"]
    from database import EditorDocument, Job
    from editor import require_machine_snapshot
    with SessionLocal() as db:
        require_machine_snapshot(db.query(Job).filter_by(job_id=jid).one(),
                                 db.query(EditorDocument).filter_by(job_id=jid).one())
        row = db.query(EditorRetranscription).filter_by(job_id=jid).one()
        assert row.status == "applied" and row.result_revision == after["revision"]
    assert "language_resolution" not in (after["quality"] or {})
    assert after["quality"]["forced_language"]["language"] == "es"
    assert after["quality"]["evaluated_revision"] == after["revision"]

    status = h["client"].get(f"/jobs/{jid}/retranscribe-language", headers=h["headers"])
    assert status.status_code == 200
    assert status.json()["status"] == "applied"
    assert status.json()["result_revision"] == after["revision"]
    # The editor loads the new version.
    polled = h["client"].get(f"/transcription-status/{jid}", headers=h["headers"])
    assert [s["text"] for s in polled.json()["segments"]] == [s["text"] for s in SPANISH]


def test_retranscribe_rejects_stale_revision_and_bad_language(harness):
    h = harness
    jid = h["job_id"]
    before = _snapshot(jid)
    stale = h["client"].post(f"/jobs/{jid}/retranscribe-language", headers=h["headers"],
                             json={"language": "es", "base_revision": before["revision"] - 1})
    assert stale.status_code == 409
    assert stale.json()["code"] == "stale_revision"
    assert stale.json()["current_revision"] == before["revision"]
    bad = h["client"].post(f"/jobs/{jid}/retranscribe-language", headers=h["headers"],
                           json={"language": "xx", "base_revision": before["revision"]})
    assert bad.status_code == 422
    missing = h["client"].post(f"/jobs/{jid}/retranscribe-language", headers=h["headers"],
                               json={"base_revision": before["revision"]})
    assert missing.status_code == 422
    assert h["dispatched"] == []
    assert _snapshot(jid) == before


def test_retranscribe_is_scoped_like_language_resolution(harness, client):
    jid = harness["job_id"]
    username = f"other_{uuid.uuid4().hex[:6]}"
    res = client.post("/auth/register", json={
        "username": username, "password": "testpass12345",
        "email": f"{username}@test.com",
    })
    assert res.status_code == 200, res.text
    other = auth(res.json()["token"])
    denied = client.post(f"/jobs/{jid}/retranscribe-language", headers=other,
                         json={"language": "es", "base_revision": 0})
    assert denied.status_code == 404
    assert client.get(f"/jobs/{jid}/retranscribe-language", headers=other).status_code == 404
    assert client.post("/jobs/doesnotexist/retranscribe-language", headers=harness["headers"],
                       json={"language": "es", "base_revision": 0}).status_code == 404


def test_human_edit_during_retranscription_wins(harness):
    h = harness
    jid = h["job_id"]
    before = _snapshot(jid)
    res = h["client"].post(f"/jobs/{jid}/retranscribe-language", headers=h["headers"],
                           json={"language": "es", "base_revision": before["revision"]})
    assert res.status_code == 202, res.text
    event_id = h["dispatched"][0]
    from database import EditorDocument, EditorRetranscription, Job, SessionLocal
    from editor import save_document
    with SessionLocal() as db:
        job = db.query(Job).filter_by(job_id=jid).one()
        document = db.query(EditorDocument).filter_by(job_id=jid).one()
        edited = copy.deepcopy(document.current_segments)
        edited[0]["text"] = "edición humana"
        save_document(db, job, document, job.user_id, int(document.revision), edited, "manual")
        db.commit()
    h["state"]["segments"] = SPANISH
    out = _run_worker_for(h, event_id)
    assert out == {"job_id": jid, "status": "discarded",
                   "reason": "edited_during_retranscription"}
    after = _snapshot(jid)
    assert after["segments"][0]["text"] == "edición humana"
    assert after["machine_evidence"] == before["machine_evidence"]
    assert after["status"] == "transcribed_pending"
    with SessionLocal() as db:
        row = db.query(EditorRetranscription).filter_by(job_id=jid).one()
        assert row.status == "discarded"


def test_failed_retranscription_returns_previous_text_to_review(harness):
    h = harness
    jid = h["job_id"]
    before = _snapshot(jid)
    res = h["client"].post(f"/jobs/{jid}/retranscribe-language", headers=h["headers"],
                           json={"language": "pt", "base_revision": before["revision"]})
    assert res.status_code == 202, res.text
    event_id = h["dispatched"][0]

    import main

    async def broken(*args, **kw):
        raise RuntimeError("provider down")

    import pytest as _pytest
    mp = _pytest.MonkeyPatch()
    mp.setattr(main, "_run_transcription_for_job", broken)
    try:
        out = _run_worker_for(h, event_id)
    finally:
        mp.undo()
    assert out["status"] == "transcription_failed"
    after = _snapshot(jid)
    assert after["status"] == "transcribed_pending"
    assert after["segments"] == before["segments"]
    assert after["machine_evidence"] == before["machine_evidence"]
    status = h["client"].get(f"/jobs/{jid}/retranscribe-language", headers=h["headers"])
    assert status.json()["status"] == "failed"
    assert status.json()["error"]
    # The reviewer can try again from the same revision.
    retry = h["client"].post(f"/jobs/{jid}/retranscribe-language", headers=h["headers"],
                             json={"language": "es", "base_revision": before["revision"]})
    assert retry.status_code == 202, retry.text


def test_stale_attempt_cannot_install_after_a_newer_request(harness):
    """A late RQ retry of a superseded attempt never replaces the baseline."""
    h = harness
    jid = h["job_id"]
    before = _snapshot(jid)
    first = h["client"].post(f"/jobs/{jid}/retranscribe-language", headers=h["headers"],
                             json={"language": "en", "base_revision": before["revision"]})
    assert first.status_code == 202
    first_event = h["dispatched"][0]
    from database import Job, SessionLocal
    with SessionLocal() as db:
        job = db.query(Job).filter_by(job_id=jid).one()
        job.status = "transcription_failed"
        job.error = "worker killed"
        db.commit()
    second = h["client"].post(f"/jobs/{jid}/retranscribe-language", headers=h["headers"],
                              json={"language": "es", "base_revision": before["revision"]})
    assert second.status_code == 202, second.text
    h["state"]["segments"] = SPANISH
    out = _run_worker_for(h, first_event)
    assert out["status"] == "discarded"
    assert out["reason"] in {"retranscription_superseded"}
    assert _snapshot(jid)["machine_evidence"] == before["machine_evidence"]
    out = _run_worker_for(h, h["dispatched"][1])
    assert out.get("status") not in {"transcription_failed", "discarded"}, out
    assert [s["text"] for s in _snapshot(jid)["segments"]] == [s["text"] for s in SPANISH]


def test_reviewer_forced_language_survives_a_mixed_audio_reference():
    """Only a reviewer-forced language may outrank a mixed machine reference.

    In Auto (and for the wizard's explicit choice) a mixed reference still
    returns the decode to provider-auto, as before.
    """
    import inspect
    import main
    params = inspect.signature(main._run_transcription_for_job).parameters
    assert params["force_language"].default is False
    src = inspect.getsource(main._run_transcription_for_job)
    forced = src.index("if len(_reference_languages) > 1 and force_language and lang:")
    demote = src.index("elif len(_reference_languages) > 1:", forced)
    assert "lang = None" not in src[forced:demote]
    assert "lang = None" in src[demote:src.index("_detected_lang = (", demote)]


def test_campaign_reviewer_retranscribes_through_the_campaign_worker_contract(
    client, user_token, admin_token, monkeypatch,
):
    """Cross-tenant campaign reviewer; same batch kwargs, language forced."""
    import main
    from database import (
        BatchCampaign, BatchCampaignItem, BatchCampaignReviewer, EditorRetranscription,
        JobOutboxEvent, Job, SessionLocal,
    )
    dispatched = []
    monkeypatch.setattr(main, "_dispatch_outbox_after_response",
                        lambda event_id, **kw: dispatched.append(event_id))
    reviewer = client.get("/auth/me", headers=auth(user_token)).json()
    owner = client.get("/auth/me", headers=auth(admin_token)).json()
    assert reviewer["tenant_id"] != owner["tenant_id"]
    campaign_id, job_id = uuid.uuid4().hex[:12], uuid.uuid4().hex[:12]
    item_id = str(uuid.uuid4())
    with SessionLocal() as db:
        db.add(BatchCampaign(id=campaign_id, tenant_id=owner["tenant_id"],
                             created_by=owner["id"], name="Chile", kind="lyric_video"))
        db.flush()
        db.add(BatchCampaignReviewer(campaign_id=campaign_id, user_id=reviewer["id"]))
        db.add(BatchCampaignItem(id=item_id, campaign_id=campaign_id,
                                 tenant_id=owner["tenant_id"], ordinal=1,
                                 filename="idolo.wav", title="Gran Ídolo De Asia",
                                 artist="Fito", sha256=uuid.uuid4().hex * 2))
        db.flush()
        db.add(Job(job_id=job_id, user_id=owner["id"], tenant_id=owner["tenant_id"],
                   campaign_id=campaign_id, campaign_item_id=item_id,
                   filename="idolo.wav", artist="Fito", song_title="Gran Ídolo De Asia",
                   style="auto", status="transcribed_pending", current_step="editing",
                   progress=100, delivery_profile="youtube", workload_class="batch",
                   input_r2_key=f"inputs/{job_id}/idolo.wav",
                   segments_json=copy.deepcopy(ENGLISH), segments_revision=3,
                   transcription_quality={"metrics": {"language": "en"}}))
        db.commit()
    res = client.post(f"/jobs/{job_id}/retranscribe-language", headers=auth(user_token),
                      json={"language": "es", "base_revision": 3})
    assert res.status_code == 202, res.text
    assert res.json()["previous_language"] == "en"
    with SessionLocal() as db:
        event = db.query(JobOutboxEvent).filter_by(id=dispatched[0]).one()
        kwargs = event.payload["transcription_kwargs"]
        assert kwargs["language"] == "es"
        assert kwargs["workload_class"] == "batch"
        assert kwargs["reference_required"] is True
        job = db.query(Job).filter_by(job_id=job_id).one()
        assert job.status == "transcribing_queued"
        assert job.active_transcription_attempt_id == event.id
        row = db.query(EditorRetranscription).filter_by(job_id=job_id).one()
        assert row.archived_version_id is not None
        assert row.requested_by == reviewer["id"]


# ---------------------------------------------------------------------------
# Rolling deploys: an API on this code may coexist with workers on older code.
# ---------------------------------------------------------------------------

def _fake_fleet(monkeypatch, rows, *, fingerprint="fp-new"):
    import json
    import fakeredis
    import observability
    import queue_jobs
    redis = fakeredis.FakeRedis()
    for index, row in enumerate(rows):
        redis.set(f"genly:worker:release:w{index}", json.dumps(row))
    monkeypatch.setenv("REDIS_URL", "redis://fake")
    monkeypatch.setattr(queue_jobs, "_init_redis", lambda: (redis, None, None))
    monkeypatch.setattr(observability, "backend_code_fingerprint", lambda: fingerprint)


def test_fleet_gate_requires_every_worker_on_the_queue_to_run_this_code(monkeypatch):
    from editor_retranscription import worker_fleet_ready
    new = {"queues": ["transcription_batch", "campaign_control"], "code_fingerprint": "fp-new"}
    old = {"queues": ["transcription_batch"], "code_fingerprint": "fp-old"}
    other = {"queues": ["enterprise"], "code_fingerprint": "fp-old"}
    _fake_fleet(monkeypatch, [new, other])
    assert worker_fleet_ready("transcription_batch") == (True, "match")
    _fake_fleet(monkeypatch, [new, old])
    assert worker_fleet_ready("transcription_batch") == (False, "worker_release_mismatch")
    _fake_fleet(monkeypatch, [new, {"queues": ["transcription_batch"]}])
    assert worker_fleet_ready("transcription_batch")[0] is False  # old worker, no fingerprint
    _fake_fleet(monkeypatch, [other])
    assert worker_fleet_ready("transcription_batch") == (False, "no_worker_for_queue")
    _fake_fleet(monkeypatch, [new], fingerprint="")
    assert worker_fleet_ready("transcription_batch") == (False, "api_fingerprint_unknown")


def test_fleet_gate_fails_closed_without_queue_in_managed_env(monkeypatch):
    from editor_retranscription import worker_fleet_ready
    monkeypatch.delenv("REDIS_URL", raising=False)
    monkeypatch.setenv("ENVIRONMENT", "staging")
    assert worker_fleet_ready("transcription") == (False, "queue_unavailable")
    monkeypatch.setenv("ENVIRONMENT", "development")
    assert worker_fleet_ready("transcription") == (True, "in_process")
    import queue_jobs
    monkeypatch.setenv("REDIS_URL", "redis://down")
    def broken():
        raise ConnectionError("down")
    monkeypatch.setattr(queue_jobs, "_init_redis", broken)
    assert worker_fleet_ready("transcription") == (False, "fleet_unverifiable")


def test_endpoint_refuses_while_workers_run_other_code(harness, monkeypatch):
    h = harness
    jid = h["job_id"]
    before = _snapshot(jid)
    _fake_fleet(monkeypatch, [{"queues": ["transcription"], "code_fingerprint": "fp-old"}])
    res = h["client"].post(f"/jobs/{jid}/retranscribe-language", headers=h["headers"],
                           json={"language": "es", "base_revision": before["revision"]})
    assert res.status_code == 503
    assert res.json()["code"] == "fleet_updating"
    assert res.json()["message"] == "Genly se está actualizando, probá en unos minutos."
    assert res.headers.get("retry-after") == "120"
    assert h["dispatched"] == []
    assert _snapshot(jid) == before
    from database import EditorRetranscription, SessionLocal
    with SessionLocal() as db:
        assert db.query(EditorRetranscription).filter_by(job_id=jid).count() == 0


def _run_as_old_worker(h, event_id, monkeypatch):
    """The real outbox consumer, with the worker blind to the archive row —
    exactly what a worker on the previous release does."""
    import editor_retranscription
    from database import JobOutboxEvent, SessionLocal
    from transactional_outbox import run_outbox_transcription
    with SessionLocal() as db:
        event = db.query(JobOutboxEvent).filter_by(id=event_id).one()
        payload, dedupe_key = dict(event.payload), event.dedupe_key
    kwargs = dict(payload["transcription_kwargs"])
    kwargs.pop("tenant_id", None)
    mp = pytest.MonkeyPatch()
    mp.setattr(editor_retranscription, "for_attempt", lambda *a, **kw: None)
    try:
        return run_outbox_transcription(
            h["job_id"], event_id, dedupe_key, payload["audio_path"], kwargs,
        )
    finally:
        mp.undo()


@pytest.mark.parametrize("healer", ["status_poll", "reaper_sweep"])
def test_old_worker_discard_returns_the_song_to_review(harness, monkeypatch, healer):
    h = harness
    jid = h["job_id"]
    before = _snapshot(jid)
    res = h["client"].post(f"/jobs/{jid}/retranscribe-language", headers=h["headers"],
                           json={"language": "es", "base_revision": before["revision"]})
    assert res.status_code == 202, res.text
    event_id = h["dispatched"][0]
    h["state"]["segments"] = SPANISH
    _run_as_old_worker(h, event_id, monkeypatch)
    stuck = _snapshot(jid)
    assert stuck["status"] == "transcribing"          # the old behaviour
    assert stuck["segments"] == before["segments"]    # result was discarded

    if healer == "status_poll":
        status = h["client"].get(f"/jobs/{jid}/retranscribe-language", headers=h["headers"])
        assert status.status_code == 200
        assert status.json()["status"] == "failed"
        assert status.json()["error"] == "attempt_ended_without_result"
    else:
        from database import SessionLocal
        from editor_retranscription import settle_stalled_jobs
        with SessionLocal() as db:
            assert settle_stalled_jobs(db) == 1
            db.commit()
    after = _snapshot(jid)
    assert after["status"] == "transcribed_pending"
    assert after["segments"] == before["segments"]
    assert after["machine_evidence"] == before["machine_evidence"]
    for version_id, value in before["versions"].items():
        assert after["versions"][version_id] == value
    # And the reviewer can ask again once the fleet is updated.
    again = h["client"].post(f"/jobs/{jid}/retranscribe-language", headers=h["headers"],
                             json={"language": "es", "base_revision": after["revision"]})
    assert again.status_code == 202, again.text


def test_reaper_timeout_returns_a_stuck_retranscription_to_review(harness):
    """Worker died without a trace: the stuck-transcription reaper heals it."""
    h = harness
    jid = h["job_id"]
    before = _snapshot(jid)
    res = h["client"].post(f"/jobs/{jid}/retranscribe-language", headers=h["headers"],
                           json={"language": "es", "base_revision": before["revision"]})
    assert res.status_code == 202, res.text
    from datetime import datetime, timedelta, timezone
    from database import EditorRetranscription, Job, SessionLocal
    import reaper
    with SessionLocal() as db:
        job = db.query(Job).filter_by(job_id=jid).one()
        job.status = "transcribing"
        job.last_progress_at = datetime.now(timezone.utc) - timedelta(hours=5)
        db.commit()
    with SessionLocal() as db:
        stuck = [job for job in reaper.find_stuck_transcriptions(db) if job.job_id == jid]
        assert stuck
        reaper.reap_stuck_transcription(db, stuck[0])
        db.commit()
    after = _snapshot(jid)
    assert after["status"] == "transcribed_pending"
    assert after["segments"] == before["segments"]
    with SessionLocal() as db:
        row = db.query(EditorRetranscription).filter_by(job_id=jid).one()
        assert row.status == "failed" and row.error

