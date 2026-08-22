"""Regression guards for worker-owned R2 audio materialization."""


def test_worker_rejects_invalid_audio_before_transcription(tmp_path, monkeypatch):
    """Moving the R2 download out of API must not remove header validation."""
    import jobs
    import transcription_worker

    audio_path = tmp_path / "corrupt.wav"
    audio_path.write_bytes(b"this is not a RIFF/WAVE file")
    updates = []
    monkeypatch.setattr(
        jobs,
        "update_job",
        lambda job_id, **values: updates.append((job_id, values)),
    )

    result = transcription_worker.run_transcription_job(
        "badwav123456", str(audio_path), filename="corrupt.wav",
    )

    assert result["status"] == "transcription_failed"
    assert "audio inválido" in result["error"]
    assert any(
        values.get("status") == "transcription_failed"
        for _, values in updates
    )
