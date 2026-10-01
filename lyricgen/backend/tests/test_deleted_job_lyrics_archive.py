"""deleted_job_lyrics_archive — survives delete_job/bulk_delete_jobs cascade.

Incident (audited 2026-08-24): 137 jobs carrying real operator lyric
corrections were hard-deleted via delete_job/bulk_delete_jobs with no
recoverable trace of which song they belonged to. editor_documents/
editor_versions cascade with the Job row (ON DELETE CASCADE), and the
Job's own artist/song_title go with it. DeletedJobLyricsArchive is a
best-effort copy taken just before that cascade, with no FK back to
jobs.job_id, so it survives the delete it was taken ahead of.
"""

import uuid
from datetime import datetime, timezone

import jobs as jobs_module
from database import DeletedJobLyricsArchive, EditorDocument, EditorVersion, Job, User
from tests.conftest import auth


def _make_user(db, tenant):
    user = User(
        username=f"archive-test-{uuid.uuid4().hex[:8]}",
        hashed_password="unused",
        tenant_id=tenant,
    )
    db.add(user)
    db.flush()
    return user


def _make_job(db, user, tenant, job_id, status="error", artist="Artist X", song_title="Song Y"):
    job = Job(
        job_id=job_id, user_id=user.id, tenant_id=tenant,
        artist=artist, song_title=song_title, filename="f.mp3",
        style="oscuro", status=status, delivery_profile="youtube",
    )
    db.add(job)
    db.flush()
    return job


def _cleanup_archive(db, job_ids):
    db.query(DeletedJobLyricsArchive).filter(
        DeletedJobLyricsArchive.job_id.in_(job_ids),
    ).delete(synchronize_session=False)
    db.commit()


# ---------------------------------------------------------------------------
# (a) End-to-end: a real hand-correction survives delete_job
# ---------------------------------------------------------------------------

def test_delete_job_archives_editor_document_segments(db):
    tenant = f"archive-e2e-{uuid.uuid4().hex[:8]}"
    user = _make_user(db, tenant)
    job_id = f"arch{uuid.uuid4().hex[:8]}"
    _make_job(db, user, tenant, job_id, artist="Los Bunkers", song_title="Ya No Estoy Aqui")

    corrected_segments = [{"start": 0.0, "end": 1.2, "text": "letra corregida a mano"}]
    db.add(EditorDocument(
        job_id=job_id, tenant_id=tenant,
        current_segments=corrected_segments,
        original_segments=[{"start": 0.0, "end": 1.2, "text": "letra original"}],
        revision=1, updated_at=datetime.now(timezone.utc),
    ))
    db.commit()

    ok, reason = jobs_module.delete_job(db, job_id, tenant)
    assert (ok, reason) == (True, "ok")

    # The job (and its editor_documents row, via ON DELETE CASCADE) is gone...
    assert db.query(Job).filter_by(job_id=job_id).first() is None
    assert db.query(EditorDocument).filter_by(job_id=job_id).first() is None

    # ...but the archive row survives and is independently queryable, with
    # the artist/song_title context that the 137 lost rows were missing.
    archived = db.query(DeletedJobLyricsArchive).filter_by(job_id=job_id).one()
    assert archived.tenant_id == tenant
    assert archived.artist == "Los Bunkers"
    assert archived.song_title == "Ya No Estoy Aqui"
    assert archived.job_status_at_deletion == "error"
    assert archived.source == "editor_documents"
    assert archived.segments == corrected_segments

    _cleanup_archive(db, [job_id])


def test_delete_job_falls_back_to_latest_editor_version(db):
    """No editor_documents row, but two editor_versions checkpoints exist —
    the most recent one (by created_at) must be the one archived."""
    tenant = f"archive-version-{uuid.uuid4().hex[:8]}"
    user = _make_user(db, tenant)
    job_id = f"arch{uuid.uuid4().hex[:8]}"
    _make_job(db, user, tenant, job_id)

    older = [{"start": 0, "end": 1, "text": "v1"}]
    newer = [{"start": 0, "end": 1, "text": "v2 (mas reciente)"}]
    db.add(EditorVersion(
        id=str(uuid.uuid4()), job_id=job_id, tenant_id=tenant, revision=1,
        segments=older, created_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
        reason="autosave",
    ))
    db.add(EditorVersion(
        id=str(uuid.uuid4()), job_id=job_id, tenant_id=tenant, revision=2,
        segments=newer, created_at=datetime(2026, 1, 2, tzinfo=timezone.utc),
        reason="autosave",
    ))
    db.commit()

    ok, _ = jobs_module.delete_job(db, job_id, tenant)
    assert ok

    archived = db.query(DeletedJobLyricsArchive).filter_by(job_id=job_id).one()
    assert archived.source == "editor_versions"
    assert archived.segments == newer

    _cleanup_archive(db, [job_id])


# ---------------------------------------------------------------------------
# (b) No correction ever made -> no archive row (no noise)
# ---------------------------------------------------------------------------

def test_delete_job_without_any_correction_does_not_archive(db):
    tenant = f"archive-none-{uuid.uuid4().hex[:8]}"
    user = _make_user(db, tenant)
    job_id = f"arch{uuid.uuid4().hex[:8]}"
    _make_job(db, user, tenant, job_id)
    db.commit()

    ok, _ = jobs_module.delete_job(db, job_id, tenant)
    assert ok
    assert db.query(DeletedJobLyricsArchive).filter_by(job_id=job_id).count() == 0


def test_delete_job_with_empty_editor_document_does_not_archive(db):
    """An editor_documents row with empty current_segments (created but
    never actually typed into) must not be treated as a real correction."""
    tenant = f"archive-empty-{uuid.uuid4().hex[:8]}"
    user = _make_user(db, tenant)
    job_id = f"arch{uuid.uuid4().hex[:8]}"
    _make_job(db, user, tenant, job_id)
    db.add(EditorDocument(
        job_id=job_id, tenant_id=tenant, current_segments=[],
        original_segments=[], revision=0, updated_at=datetime.now(timezone.utc),
    ))
    db.commit()

    ok, _ = jobs_module.delete_job(db, job_id, tenant)
    assert ok
    assert db.query(DeletedJobLyricsArchive).filter_by(job_id=job_id).count() == 0


# ---------------------------------------------------------------------------
# (c) bulk_delete_jobs archives every applicable job in the batch
# ---------------------------------------------------------------------------

def test_bulk_delete_jobs_archives_every_applicable_job_in_the_batch(db):
    tenant = f"archive-bulk-{uuid.uuid4().hex[:8]}"
    user = _make_user(db, tenant)
    job_with_doc = f"arch{uuid.uuid4().hex[:8]}"
    job_untouched = f"arch{uuid.uuid4().hex[:8]}"
    job_protected = f"arch{uuid.uuid4().hex[:8]}"

    _make_job(db, user, tenant, job_with_doc, artist="A1", song_title="S1")
    _make_job(db, user, tenant, job_untouched, artist="A2", song_title="S2")
    _make_job(db, user, tenant, job_protected, status="done", artist="A3", song_title="S3")

    segs = [{"start": 0, "end": 1, "text": "correccion bulk"}]
    db.add(EditorDocument(
        job_id=job_with_doc, tenant_id=tenant, current_segments=segs,
        original_segments=[{"start": 0, "end": 1, "text": "orig"}],
        revision=1, updated_at=datetime.now(timezone.utc),
    ))
    db.commit()

    try:
        result = jobs_module.bulk_delete_jobs(
            db, [job_with_doc, job_untouched, job_protected], tenant,
        )
        assert set(result["deleted"]) == {job_with_doc, job_untouched}
        assert job_protected in result["skipped"]

        archived_ids = {
            row.job_id for row in
            db.query(DeletedJobLyricsArchive).filter(
                DeletedJobLyricsArchive.job_id.in_(
                    [job_with_doc, job_untouched, job_protected],
                ),
            ).all()
        }
        # Only the touched job gets an archive row — not the untouched
        # sibling that was deleted in the SAME bulk call, and not the
        # protected (status=done) job that was never even deleted.
        assert archived_ids == {job_with_doc}

        archived = db.query(DeletedJobLyricsArchive).filter_by(job_id=job_with_doc).one()
        assert archived.artist == "A1"
        assert archived.song_title == "S1"
        assert archived.segments == segs
    finally:
        _cleanup_archive(db, [job_with_doc, job_untouched, job_protected])
        db.query(Job).filter_by(job_id=job_protected).delete(synchronize_session=False)
        db.commit()


def test_delete_job_records_deleted_by_user_id(db):
    tenant = f"archive-who-{uuid.uuid4().hex[:8]}"
    user = _make_user(db, tenant)
    admin_user = _make_user(db, tenant)
    job_id = f"arch{uuid.uuid4().hex[:8]}"
    _make_job(db, user, tenant, job_id)
    db.add(EditorDocument(
        job_id=job_id, tenant_id=tenant,
        current_segments=[{"start": 0, "end": 1, "text": "x"}],
        original_segments=[{"start": 0, "end": 1, "text": "y"}],
        revision=1, updated_at=datetime.now(timezone.utc),
    ))
    db.commit()

    ok, _ = jobs_module.delete_job(db, job_id, tenant, deleted_by_user_id=admin_user.id)
    assert ok
    archived = db.query(DeletedJobLyricsArchive).filter_by(job_id=job_id).one()
    assert archived.deleted_by_user_id == admin_user.id

    _cleanup_archive(db, [job_id])


# ---------------------------------------------------------------------------
# (e) Admin-only read endpoint
# ---------------------------------------------------------------------------

def test_admin_deleted_job_lyrics_archive_requires_admin(client, user_token):
    res = client.get(
        "/admin/deleted-job-lyrics-archive", headers=auth(user_token),
    )
    assert res.status_code == 403


def test_admin_deleted_job_lyrics_archive_lists_entries(client, admin_token, db):
    tenant = f"archive-admin-ep-{uuid.uuid4().hex[:8]}"
    user = _make_user(db, tenant)
    job_id = f"arch{uuid.uuid4().hex[:8]}"
    _make_job(db, user, tenant, job_id, artist="Admin EP Artist", song_title="Admin EP Song")
    db.add(EditorDocument(
        job_id=job_id, tenant_id=tenant,
        current_segments=[{"start": 0, "end": 1, "text": "hola"}],
        original_segments=[{"start": 0, "end": 1, "text": "hola orig"}],
        revision=1, updated_at=datetime.now(timezone.utc),
    ))
    db.commit()

    ok, _ = jobs_module.delete_job(db, job_id, tenant)
    assert ok

    res = client.get(
        f"/admin/deleted-job-lyrics-archive?tenant_id={tenant}",
        headers=auth(admin_token),
    )
    assert res.status_code == 200
    data = res.json()
    assert data["total"] == 1
    assert data["entries"][0]["job_id"] == job_id
    assert data["entries"][0]["artist"] == "Admin EP Artist"
    assert data["entries"][0]["song_title"] == "Admin EP Song"
    assert data["entries"][0]["source"] == "editor_documents"

    _cleanup_archive(db, [job_id])
