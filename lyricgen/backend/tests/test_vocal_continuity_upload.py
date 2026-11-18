"""New-upload integration with local storage/ASR doubles and real acoustics.

No network, remote jobs, or ASR/separation providers. The HTTP upload contract,
queued worker, final quality gate, editor snapshot and polling are real.
"""

import copy
import io
from pathlib import Path

import numpy as np
import soundfile as sf
import pytest


def _audio():
    sr = 16000
    t = np.arange(14 * sr) / sr
    rng = np.random.default_rng(42)
    frequency = (
        190 + 18 * np.sin(2 * np.pi * 3.7 * t) + 4 * np.sin(2 * np.pi * 1.23 * t)
    )
    phase = 2 * np.pi * np.cumsum(frequency) / sr
    voice = (np.sin(phase) + 0.35 * np.sin(2 * phase) + 0.15 * np.sin(3 * phase)) * 0.20
    mod = np.interp(t, np.arange(0, 14, 0.02), rng.uniform(0.65, 1.0, 700))
    gate = np.zeros(len(t))
    for start, end in [(2.0, 5.0), (6.0, 8.0), (10.0, 12.5)]:
        gate = np.maximum(
            gate, np.clip((t - start) / 0.04, 0, 1) * np.clip((end - t) / 0.06, 0, 1)
        )
    voice = voice * mod * gate
    mix = voice + rng.normal(0, 0.015, len(t))
    data = io.BytesIO()
    sf.write(data, mix, sr, format="WAV", subtype="PCM_16")
    return data.getvalue(), voice, sr


@pytest.mark.parametrize("execution", ["worker", "inline"])
def test_new_upload_applies_and_persists_verified_tail(
    client, user_token, monkeypatch, tmp_path, execution
):
    import main
    import pipeline
    import queue_jobs
    import storage
    import vocal_sep
    from database import SessionLocal, Job, EditorDocument
    from transcription_worker import run_transcription_job
    from editor import require_machine_snapshot

    data, voice, sr = _audio()
    outputs = tmp_path / "outputs"
    objects = {}
    enqueued = []
    calls = []
    monkeypatch.setenv("VOCAL_CONTINUITY_MODE", "apply")
    # Application gate uses the actual staging value; the DB remains the
    # isolated test SQLite and all external services below are local doubles.
    monkeypatch.setenv("ENVIRONMENT", "staging")
    monkeypatch.setenv(
        "ASYNC_TRANSCRIBE_ENABLED", "1" if execution == "worker" else "0"
    )
    monkeypatch.setenv("LYRICS_FORMAT_ENABLED", "0")
    for name in [
        "CTC_ALIGN_ENABLED",
        "ADLIB_CONSENSUS_ENABLED",
        "CHORUS_SNAP_ENABLED",
        "APPROVED_TEXT_REUSE_ENABLED",
        "TRANSCRIPTION_QUALITY_INLINE_RETRY",
        "GAP_RESCUE_ENABLED",
        "LORA_FAMILY_ENABLED",
        "WORD_VOTE_ENABLED",
    ]:
        monkeypatch.setenv(name, "0")
    monkeypatch.setattr(main, "OUTPUTS_DIR", str(outputs))
    monkeypatch.setattr(pipeline, "OUTPUTS_DIR", str(outputs))
    monkeypatch.setattr(storage, "is_enabled", lambda: True)

    def presign(tenant, jid, filename, **kw):
        return {
            "url": "https://local.invalid/upload",
            "key": f"inputs/{jid}/{filename}",
            "expires_in": 900,
        }

    monkeypatch.setattr(storage, "presign_put_url", presign)
    monkeypatch.setattr(
        storage,
        "head_object_size",
        lambda key: len(objects[key]) if key in objects else None,
    )
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
    monkeypatch.setattr(
        main,
        "_dispatch_outbox_after_response",
        lambda event_id: enqueued.append(event_id),
    )
    monkeypatch.setattr(
        queue_jobs, "enqueue_transcription_quality", lambda *a, **kw: None
    )

    # Simulate the source's existing cached separation. Acoustic extraction,
    # clock verification and periodicity are not stubbed.
    def cached(path, *, cache_only):
        assert cache_only
        assert Path(path).read_bytes() == data
        stem = tmp_path / "cached-stem.wav"
        sf.write(stem, voice, sr, subtype="PCM_16")
        return str(stem)

    monkeypatch.setattr(vocal_sep, "separate_vocals", cached)
    segments = [
        {
            "start": 2.0,
            "end": 4.0,
            "text": "amor",
            "words": [{"word": "amor", "start": 2.05, "end": 3.75, "score": 0.9}],
        },
        {
            "start": 6.0,
            "end": 8.0,
            "text": "canto",
            "words": [{"word": "canto", "start": 6.05, "end": 7.75, "score": 0.9}],
        },
        {
            "start": 10.0,
            "end": 12.5,
            "text": "final",
            "words": [{"word": "final", "start": 10.05, "end": 12.25, "score": 0.9}],
        },
    ]

    async def provider(*args, **kw):
        calls.append(args[2])
        from display_segmentation_policy import capture_source
        source = capture_source(args[2], args[3])
        return {
            "_transcription_source": source,
            "segments": copy.deepcopy(segments),
            "reference_lyrics": "",
            "timing_source": "whisper_align",
            "_primary_asr_family": "integration-fixture",
            "_recognition_hypotheses": [
                {
                    "family": "integration-fixture",
                    "kind": "segments",
                    "attempt_id": 0,
                    "events": copy.deepcopy(segments),
                    "view": "mix",
                    "transformation": "local_test_fixture",
                }
            ],
            "_recognition_attempt_count": 1,
            "_asr_words": [w for s in copy.deepcopy(segments) for w in s["words"]],
        }

    monkeypatch.setattr(main, "_run_transcription_for_job", provider)

    headers = {"Authorization": f"Bearer {user_token}"}
    response = client.post(
        "/upload-url",
        json={
            "filename": "continuity-new-upload.wav",
            "size_bytes": len(data),
            "artist": "Integration fixture",
            "song_title": "Sustained endpoint",
        },
        headers=headers,
    )
    assert response.status_code == 200, response.text
    jid = response.json()["job_id"]
    with SessionLocal() as db:
        row = db.query(Job).filter_by(job_id=jid).one()
        objects[row.input_r2_key] = data
    queued = client.post(
        "/transcribe-uploaded", json={"job_id": jid, "language": "es"}, headers=headers
    )
    audio = outputs / jid / "continuity-new-upload.wav"
    if execution == "worker":
        assert queued.status_code == 202, queued.text
        assert len(enqueued) == 1
        output = run_transcription_job(
            jid, str(audio), language="es", filename=audio.name
        )
    else:
        assert queued.status_code == 200, queued.text
        assert enqueued == []
        output = queued.json()
    assert calls == [jid]
    assert output.get("status") != "transcription_failed", output
    audit = output["transcription_quality"]["vocal_continuity"]
    assert audit["status"] == "applied", audit
    assert audit["applied_count"] == 1, audit
    assert 4.8 <= output["segments"][0]["end"] <= 5.1
    assert output["segments"][0]["words"] == segments[0]["words"]
    assert output["segments"][0]["provider_evidence"]["words"][0]["end"] == 3.75
    assert not (tmp_path / "cached-stem.wav").exists()
    with SessionLocal() as db:
        row = db.query(Job).filter_by(job_id=jid).one()
        document = db.query(EditorDocument).filter_by(job_id=jid).one()
        assert row.status == "transcribed_pending"
        assert row.segments_json[0]["end"] == output["segments"][0]["end"]
        assert row.transcription_quality["vocal_continuity"]["applied_count"] == 1
        assert document.original_segments[0]["end"] == output["segments"][0]["end"]
        require_machine_snapshot(row, document)
    polled = client.get(f"/transcription-status/{jid}", headers=headers)
    assert polled.status_code == 200, polled.text
    assert polled.json()["segments"][0]["end"] == output["segments"][0]["end"]
