"""POST /edit with edit_type="metadata" — corrects artist/song_title typos
without consuming an edit slot.

PR C 2026-05-26 (feat/edit-metadata): operator subió un job y el title
card del MP4 tiene un typo (ej. "Sin Gamulan" debió ser "Sín Gamulán").
Antes no había forma de corregirlo sin re-subir el audio desde cero —
incluso si quedaban slots de edit, los 3 disponibles (typography /
background / lyrics) no aceptaban metadata. Este flow lo expone.

Tests pinean el contrato API + el comportamiento del edit-slot:

  - artist / song_title se aceptan en EditJobRequest (3 strings)
  - status gate: done/pending_review/rejected aceptan; el resto rechaza
  - empty-or-trimmed-empty falla 400
  - max_length cap (255/500) → Pydantic 422
  - YouTube drift guard (mismo que lyrics)
  - bg_r2_key_cached gate (necesita bg cacheado para el re-render)
  - **edit_count NO incrementa** (decisión del operador 2026-05-26)
  - los nuevos valores se persisten en DB ANTES del enqueue
  - los nuevos valores también van en edit_params como belt-and-suspenders
  - el AuditLog lleva `metadata_only=True`
"""
import uuid
from datetime import datetime, timezone

from database import Job as JobModel, User as UserModel, AuditLog


def _create_pending_review_job(db, tenant_id, user_id, **overrides):
    """Insert a Job in pending_review that passes request_edit's
    pre-checks: bg_r2_key_cached + segments_json + edit_count=0."""
    job_id = uuid.uuid4().hex[:12]
    defaults = dict(
        job_id=job_id,
        user_id=user_id,
        tenant_id=tenant_id,
        artist="Sin Gamulan",                # typo: faltan tildes
        song_title="Los Abuelos De La Nada",
        filename="test.mp3",
        status="pending_review",
        delivery_profile="youtube",
        progress=100,
        bg_r2_key_cached="fake/bg.mp4",
        segments_json=[{"start": 0.0, "end": 1.0, "text": "hola"}],
        edit_count=0,
    )
    defaults.update(overrides)
    db.add(JobModel(**defaults))
    db.commit()
    return job_id


def _admin_identity(db):
    admin = db.query(UserModel).filter(UserModel.username == "admin").first()
    assert admin is not None
    return admin.id, admin.tenant_id


def _capture_enqueue_calls(monkeypatch):
    """Replace enqueue_edit with a capturing no-op."""
    import main
    captured: list[dict] = []
    monkeypatch.setattr(
        main, "enqueue_edit",
        lambda **kwargs: (captured.append(kwargs), "test:noop")[1],
    )
    return captured


def test_metadata_updates_db_and_enqueues(client, admin_token, db, monkeypatch):
    """POST con artist + song_title corregidos → DB actualizada antes del
    enqueue, edit_params lleva los nuevos valores, response 200."""
    captured = _capture_enqueue_calls(monkeypatch)
    user_id, tenant_id = _admin_identity(db)
    job_id = _create_pending_review_job(db, tenant_id, user_id)

    res = client.post(
        f"/edit/{job_id}",
        headers={"Authorization": f"Bearer {admin_token}"},
        json={
            "edit_type": "metadata",
            "artist": "Sín Gamulán",
            "song_title": "Los Abuelos De La Nada",
        },
    )
    assert res.status_code == 200, res.text

    # DB row reflects the new values immediately (handler wrote before commit).
    row = db.query(JobModel).filter(JobModel.job_id == job_id).first()
    db.refresh(row)
    assert row.artist == "Sín Gamulán"
    assert row.song_title == "Los Abuelos De La Nada"

    # edit_params for the worker.
    assert len(captured) == 1
    edit_params = captured[0]["edit_params"]
    assert edit_params.get("artist") == "Sín Gamulán"
    assert edit_params.get("song_title") == "Los Abuelos De La Nada"
    assert captured[0]["edit_type"] == "metadata"


def test_edit_rejected_clears_failed_completion_timestamp(
    client, admin_token, db, monkeypatch,
):
    """A rescued rejection must be stamped when it is actually delivered.

    Keeping the rejection timestamp assigns a July rescue to June's delivery
    denominator because ``update_job`` only stamps terminal transitions when
    ``completed_at`` is null.
    """
    _capture_enqueue_calls(monkeypatch)
    user_id, tenant_id = _admin_identity(db)
    rejected_at = datetime(2026, 6, 30, tzinfo=timezone.utc)
    job_id = _create_pending_review_job(
        db, tenant_id, user_id, status="rejected", completed_at=rejected_at)

    res = client.post(
        f"/edit/{job_id}",
        headers={"Authorization": f"Bearer {admin_token}"},
        json={"edit_type": "metadata", "song_title": "Título corregido"},
    )
    assert res.status_code == 200, res.text
    db.expire_all()
    row = db.query(JobModel).filter(JobModel.job_id == job_id).one()
    assert row.status == "editing"
    assert row.completed_at is None

    from jobs import update_job
    update_job(job_id, status="pending_review")
    db.expire_all()
    row = db.query(JobModel).filter(JobModel.job_id == job_id).one()
    assert row.completed_at is not None
    completed_at = row.completed_at
    if completed_at.tzinfo is None:  # SQLite drops timezone information.
        completed_at = completed_at.replace(tzinfo=timezone.utc)
    assert completed_at > rejected_at


def test_metadata_does_not_consume_edit_slot(client, admin_token, db, monkeypatch):
    """Fire 5 metadata edits on a job with edit_count=2 — edit_count
    must stay at 2 every time. Regular edits would have capped at 3."""
    _capture_enqueue_calls(monkeypatch)
    user_id, tenant_id = _admin_identity(db)
    job_id = _create_pending_review_job(db, tenant_id, user_id, edit_count=2)

    for i in range(5):
        res = client.post(
            f"/edit/{job_id}",
            headers={"Authorization": f"Bearer {admin_token}"},
            json={"edit_type": "metadata", "song_title": f"Tit{i}"},
        )
        assert res.status_code == 200, (
            f"Iter {i} should succeed regardless of edit_count cap; got {res.status_code}: {res.text}"
        )
        # Flip back to pending_review so the next iter passes the status gate
        # (the handler flipped it to 'editing'). expire_all so the test's
        # session re-reads the row state the handler just committed (without
        # this, SQLAlchemy serves the cached pre-handler row and our writes
        # below are based on stale data → the handler's commit wins).
        db.expire_all()
        row = db.query(JobModel).filter(JobModel.job_id == job_id).first()
        row.status = "pending_review"
        db.commit()

    row = db.query(JobModel).filter(JobModel.job_id == job_id).first()
    db.refresh(row)
    assert row.edit_count == 2, f"edit_count must stay at 2, got {row.edit_count}"


def test_metadata_requires_at_least_one_field(client, admin_token, db, monkeypatch):
    """POST con body vacío de metadata → 400."""
    _capture_enqueue_calls(monkeypatch)
    user_id, tenant_id = _admin_identity(db)
    job_id = _create_pending_review_job(db, tenant_id, user_id)

    res = client.post(
        f"/edit/{job_id}",
        headers={"Authorization": f"Bearer {admin_token}"},
        json={"edit_type": "metadata"},
    )
    assert res.status_code == 400
    assert "at least one" in res.text.lower()


def test_metadata_rejects_empty_trimmed(client, admin_token, db, monkeypatch):
    """artist='   ' (sólo espacios) → 400."""
    _capture_enqueue_calls(monkeypatch)
    user_id, tenant_id = _admin_identity(db)
    job_id = _create_pending_review_job(db, tenant_id, user_id)

    res = client.post(
        f"/edit/{job_id}",
        headers={"Authorization": f"Bearer {admin_token}"},
        json={"edit_type": "metadata", "artist": "   "},
    )
    assert res.status_code == 400
    assert "non-empty" in res.text.lower() or "empty" in res.text.lower()


def test_metadata_max_length_artist(client, admin_token, db, monkeypatch):
    """artist con 256 chars → Pydantic 422."""
    _capture_enqueue_calls(monkeypatch)
    user_id, tenant_id = _admin_identity(db)
    job_id = _create_pending_review_job(db, tenant_id, user_id)

    res = client.post(
        f"/edit/{job_id}",
        headers={"Authorization": f"Bearer {admin_token}"},
        json={"edit_type": "metadata", "artist": "x" * 256},
    )
    assert res.status_code == 422


def test_metadata_max_length_song_title(client, admin_token, db, monkeypatch):
    """song_title con 501 chars → Pydantic 422."""
    _capture_enqueue_calls(monkeypatch)
    user_id, tenant_id = _admin_identity(db)
    job_id = _create_pending_review_job(db, tenant_id, user_id)

    res = client.post(
        f"/edit/{job_id}",
        headers={"Authorization": f"Bearer {admin_token}"},
        json={"edit_type": "metadata", "song_title": "x" * 501},
    )
    assert res.status_code == 422


def test_metadata_youtube_drift_409(client, admin_token, db, monkeypatch):
    """Job ya publicado en YouTube + sin allow_youtube_drift → 409 con
    código `youtube_already_published`. Mismo gate que lyrics."""
    _capture_enqueue_calls(monkeypatch)
    user_id, tenant_id = _admin_identity(db)
    job_id = _create_pending_review_job(
        db, tenant_id, user_id,
        youtube_data={"url": "https://youtu.be/abc123", "video_id": "abc123"},
    )

    res = client.post(
        f"/edit/{job_id}",
        headers={"Authorization": f"Bearer {admin_token}"},
        json={"edit_type": "metadata", "song_title": "Nuevo título"},
    )
    assert res.status_code == 409, res.text
    body = res.json()
    assert body["detail"]["code"] == "youtube_already_published"


def test_metadata_youtube_drift_explicit_opt_in_passes(client, admin_token, db, monkeypatch):
    """Mismo que el anterior, pero con allow_youtube_drift=True → 200."""
    _capture_enqueue_calls(monkeypatch)
    user_id, tenant_id = _admin_identity(db)
    job_id = _create_pending_review_job(
        db, tenant_id, user_id,
        youtube_data={"url": "https://youtu.be/abc123"},
    )

    res = client.post(
        f"/edit/{job_id}",
        headers={"Authorization": f"Bearer {admin_token}"},
        json={
            "edit_type": "metadata",
            "song_title": "Nuevo título",
            "allow_youtube_drift": True,
        },
    )
    assert res.status_code == 200, res.text


def test_metadata_status_gate_rejects_transcribing(client, admin_token, db, monkeypatch):
    """Job en `transcribing` no acepta metadata edit (mismo gate que lyrics)."""
    _capture_enqueue_calls(monkeypatch)
    user_id, tenant_id = _admin_identity(db)
    job_id = _create_pending_review_job(
        db, tenant_id, user_id,
        status="transcribing",
    )

    res = client.post(
        f"/edit/{job_id}",
        headers={"Authorization": f"Bearer {admin_token}"},
        json={"edit_type": "metadata", "artist": "Nuevo"},
    )
    assert res.status_code == 400


def test_metadata_editing_returns_structured_conflict_without_enqueue(
    client, admin_token, db, monkeypatch,
):
    """Un CTA duplicado sobre un edit en curso no es un 400 genérico.

    El frontend usa `edit_in_progress` para cerrar el wizard tardío y dejar
    visible el progreso del primer request, sin afirmar que ese edit falló.
    """
    captured = _capture_enqueue_calls(monkeypatch)
    user_id, tenant_id = _admin_identity(db)
    job_id = _create_pending_review_job(
        db,
        tenant_id,
        user_id,
        status="editing",
        current_step="short",
        progress=75,
    )

    res = client.post(
        f"/edit/{job_id}",
        headers={"Authorization": f"Bearer {admin_token}"},
        json={"edit_type": "metadata", "song_title": "Nuevo título"},
    )

    assert res.status_code == 409, res.text
    assert res.json()["detail"] == {
        "code": "edit_in_progress",
        "message": "An edit is already being rendered for this video.",
        "current_step": "short",
        "progress": 75,
    }
    assert captured == []


def test_metadata_requires_cached_bg(client, admin_token, db, monkeypatch):
    """Sin bg_r2_key_cached el re-render no puede reusar el fondo → 400."""
    _capture_enqueue_calls(monkeypatch)
    user_id, tenant_id = _admin_identity(db)
    job_id = _create_pending_review_job(
        db, tenant_id, user_id,
        bg_r2_key_cached=None,
    )

    res = client.post(
        f"/edit/{job_id}",
        headers={"Authorization": f"Bearer {admin_token}"},
        json={"edit_type": "metadata", "song_title": "Nuevo"},
    )
    assert res.status_code == 400
    assert "cached background" in res.text.lower()


def test_metadata_audit_log_flags_metadata_only(client, admin_token, db, monkeypatch):
    """El AuditLog debe llevar `metadata_only=True` para que ops pueda
    filtrar mutaciones de artist/title sin parsear edit_params."""
    _capture_enqueue_calls(monkeypatch)
    user_id, tenant_id = _admin_identity(db)
    job_id = _create_pending_review_job(db, tenant_id, user_id)

    res = client.post(
        f"/edit/{job_id}",
        headers={"Authorization": f"Bearer {admin_token}"},
        json={"edit_type": "metadata", "artist": "Sín Gamulán"},
    )
    assert res.status_code == 200, res.text

    # Find the most recent edit_request audit for this job.
    log = (
        db.query(AuditLog)
        .filter(AuditLog.action == "job.edit_request")
        .order_by(AuditLog.created_at.desc())
        .first()
    )
    assert log is not None
    assert log.detail.get("job_id") == job_id
    assert log.detail.get("metadata_only") is True
    assert log.detail.get("edit_type") == "metadata"


def test_typography_audit_log_metadata_only_false(client, admin_token, db, monkeypatch):
    """Sanity check: para un edit_type normal, metadata_only debe ser False."""
    _capture_enqueue_calls(monkeypatch)
    user_id, tenant_id = _admin_identity(db)
    job_id = _create_pending_review_job(db, tenant_id, user_id)

    res = client.post(
        f"/edit/{job_id}",
        headers={"Authorization": f"Bearer {admin_token}"},
        json={"edit_type": "typography", "font": "jost-bold"},
    )
    assert res.status_code == 200, res.text

    log = (
        db.query(AuditLog)
        .filter(AuditLog.action == "job.edit_request")
        .order_by(AuditLog.created_at.desc())
        .first()
    )
    assert log.detail.get("metadata_only") is False


def test_metadata_rollback_restores_original_values_on_enqueue_failure(
    client, admin_token, db, monkeypatch,
):
    """REGRESSION F1 audit 2026-05-27:

    Previously the handler captured _pre_edit_artist = job.artist AFTER
    mutating job.artist = new_artist (lines 7635-7720). The rollback in
    the enqueue-failure branch then restored the NEW value, not the
    original. UI showed "Guardado" but no actual save happened — silent
    data corruption.

    This test reproduces the failure mode: monkey-patch enqueue_edit to
    raise, POST a metadata edit, and assert the DB still has the original
    artist/song_title.
    """
    import main

    user_id, tenant_id = _admin_identity(db)
    ORIGINAL_ARTIST = "Sin Gamulan"
    ORIGINAL_TITLE = "Los Abuelos De La Nada"
    job_id = _create_pending_review_job(
        db, tenant_id, user_id,
        artist=ORIGINAL_ARTIST, song_title=ORIGINAL_TITLE,
    )

    # Force enqueue_edit to raise — simulates Redis down.
    def _broken_enqueue(**kwargs):
        raise RuntimeError("Redis connection refused (simulated)")
    monkeypatch.setattr(main, "enqueue_edit", _broken_enqueue)

    res = client.post(
        f"/edit/{job_id}",
        headers={"Authorization": f"Bearer {admin_token}"},
        json={
            "edit_type": "metadata",
            "artist": "Should NOT persist",
            "song_title": "Should NOT persist either",
        },
    )
    # Endpoint must signal failure to the client (503 with retry hint).
    assert res.status_code == 503, res.text

    # Crucially: the DB row must hold the ORIGINAL values, not the
    # rejected ones. Before the fix this assertion would fail.
    db.expire_all()
    job_after = db.query(JobModel).filter(JobModel.job_id == job_id).first()
    assert job_after.artist == ORIGINAL_ARTIST, (
        f"Rollback regression: artist should still be {ORIGINAL_ARTIST!r}, "
        f"got {job_after.artist!r}"
    )
    assert job_after.song_title == ORIGINAL_TITLE, (
        f"Rollback regression: song_title should still be {ORIGINAL_TITLE!r}, "
        f"got {job_after.song_title!r}"
    )
    # And status should be back to pending_review (rollback also fixes it).
    assert job_after.status == "pending_review"
