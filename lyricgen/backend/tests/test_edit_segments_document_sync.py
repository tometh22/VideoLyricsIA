"""Regression tests for a confirmed real incident (UMG Chile, 2026-08-13):
"edité la letra y luego me borró partes en un segundo cambio de fondo".

Root cause: POST /edit persisted segments (when the request body carried
them alongside a non-lyrics edit_type, e.g. "background" — editSubmission.js
bundles all pending buckets into one request) by writing job.segments_json /
job.segments_revision directly, checked only against job.segments_revision.
That let the durable editor_documents row (the LyricsEditor's actual source
of truth) drift out of sync with the job row — any writer through that path
could advance job.segments_revision without editor_documents ever knowing.
The next GET /editor then saw job_revision > document_revision and
reconciled by silently overwriting editor_documents.current_segments with
whatever was in the job row (get_or_create_document's migration branch) —
stomping real edits with a stale snapshot.

Fix: POST /edit's segments write now goes through save_document(), the same
durable path PATCH /editor and /save-segments already use. It checks
base_revision against document.revision (the actual source of truth) under
a row lock, so job and document always move together — no more divergence,
no more stale-snapshot overwrite on the next reconcile.
"""
import uuid

from database import EditorDocument, Job as JobModel, User as UserModel
from editor import get_or_create_document


def _admin_identity(db):
    admin = db.query(UserModel).filter(UserModel.username == "admin").first()
    assert admin is not None
    return admin.id, admin.tenant_id


def _create_job_with_document(db, tenant_id, user_id, *, job_revision, document_revision,
                               job_segments, document_segments):
    """A job whose segments_revision and editor_documents.revision are
    DELIBERATELY out of sync — reproduces whatever state a prior divergent
    write (the bug this file guards against, or any other path that still
    manages to desync them) would leave behind."""
    job_id = uuid.uuid4().hex[:12]
    db.add(JobModel(
        job_id=job_id,
        user_id=user_id,
        tenant_id=tenant_id,
        artist="Test",
        song_title="Segments Sync Test",
        filename="test.mp3",
        status="pending_review",
        delivery_profile="youtube",
        progress=100,
        bg_r2_key_cached="fake/bg.mp4",
        segments_json=job_segments,
        segments_revision=job_revision,
        edit_count=0,
    ))
    db.commit()
    db.add(EditorDocument(
        job_id=job_id,
        tenant_id=tenant_id,
        current_segments=document_segments,
        original_segments=document_segments,
        revision=document_revision,
    ))
    db.commit()
    return job_id


def _capture_enqueue_calls(monkeypatch):
    import main
    captured: list[dict] = []
    monkeypatch.setattr(
        main, "enqueue_edit",
        lambda **kwargs: (captured.append(kwargs), "test:noop")[1],
    )
    return captured


REAL_EDITED_LYRICS = [{"start": 0.0, "end": 1.0, "text": "letra editada de verdad"}]
STALE_WIZARD_SNAPSHOT = [{"start": 0.0, "end": 1.0, "text": "letra vieja del wizard"}]


def test_background_edit_with_stale_wizard_segments_conflicts_not_silently_diverges(
    client, admin_token, db, monkeypatch,
):
    """The exact incident shape: editor_documents already moved ahead
    (revision 7, real edited lyrics) of what job.segments_revision (5) and
    the wizard's stale in-memory snapshot both still think is current. A
    background edit carrying that stale snapshot with base_revision=5 must
    be rejected — NOT silently accepted just because it happens to match
    job.segments_revision alone."""
    _capture_enqueue_calls(monkeypatch)
    user_id, tenant_id = _admin_identity(db)
    job_id = _create_job_with_document(
        db, tenant_id, user_id,
        job_revision=5, document_revision=7,
        job_segments=STALE_WIZARD_SNAPSHOT, document_segments=REAL_EDITED_LYRICS,
    )

    res = client.post(
        f"/edit/{job_id}",
        headers={"Authorization": f"Bearer {admin_token}"},
        json={
            "edit_type": "background",
            "background_hint": "atardecer en la playa",
            "segments": STALE_WIZARD_SNAPSHOT,
            "base_revision": 5,
        },
    )
    assert res.status_code == 409, (
        f"expected a conflict (job.segments_revision alone is not proof the "
        f"submitted content descends from the durable editor's true state), "
        f"got {res.status_code}: {res.text}"
    )

    # Nothing changed — this is the crux of the fix. Before it, this call
    # would have written job.segments_json = STALE_WIZARD_SNAPSHOT and left
    # editor_documents at revision 7, guaranteeing the next reconcile
    # stomps the real edited lyrics with the stale snapshot.
    job = db.query(JobModel).filter(JobModel.job_id == job_id).first()
    assert job.segments_json == STALE_WIZARD_SNAPSHOT
    assert job.segments_revision == 5
    document = db.query(EditorDocument).filter(EditorDocument.job_id == job_id).first()
    assert document.current_segments == REAL_EDITED_LYRICS
    assert document.revision == 7


def test_background_edit_with_current_segments_advances_job_and_document_together(
    client, admin_token, db, monkeypatch,
):
    """The legitimate case (Bersuit incident, 2026-05-15): operator tweaks a
    lyric line inside the same modal as a background change. base_revision
    correctly matches BOTH job and document (no divergence) — the edit must
    succeed, and afterward job.segments_revision and
    editor_documents.revision must be equal, never drift apart."""
    _capture_enqueue_calls(monkeypatch)
    user_id, tenant_id = _admin_identity(db)
    job_id = _create_job_with_document(
        db, tenant_id, user_id,
        job_revision=5, document_revision=5,
        job_segments=REAL_EDITED_LYRICS, document_segments=REAL_EDITED_LYRICS,
    )
    fixed_lyrics = [{"start": 0.0, "end": 1.0, "text": "del amor, no de la amor"}]

    res = client.post(
        f"/edit/{job_id}",
        headers={"Authorization": f"Bearer {admin_token}"},
        json={
            "edit_type": "background",
            "background_hint": "atardecer en la playa",
            "segments": fixed_lyrics,
            "base_revision": 5,
        },
    )
    assert res.status_code == 202, res.text

    job = db.query(JobModel).filter(JobModel.job_id == job_id).first()
    document = db.query(EditorDocument).filter(EditorDocument.job_id == job_id).first()
    assert job.segments_json == fixed_lyrics
    assert document.current_segments == fixed_lyrics
    assert job.segments_revision == document.revision, (
        "job and document must move together — a mismatch here is exactly "
        "the divergence that causes the next GET /editor to silently "
        "overwrite real edits with a stale snapshot"
    )


def test_lyrics_edit_also_keeps_job_and_document_in_sync(client, admin_token, db, monkeypatch):
    """edit_type='lyrics' (the primary path, not the background-carries-
    segments side case) must go through the same durable write too."""
    _capture_enqueue_calls(monkeypatch)
    user_id, tenant_id = _admin_identity(db)
    job_id = _create_job_with_document(
        db, tenant_id, user_id,
        job_revision=2, document_revision=2,
        job_segments=STALE_WIZARD_SNAPSHOT, document_segments=STALE_WIZARD_SNAPSHOT,
    )

    res = client.post(
        f"/edit/{job_id}",
        headers={"Authorization": f"Bearer {admin_token}"},
        json={
            "edit_type": "lyrics",
            "segments": REAL_EDITED_LYRICS,
            "base_revision": 2,
        },
    )
    assert res.status_code == 202, res.text
    job = db.query(JobModel).filter(JobModel.job_id == job_id).first()
    document = db.query(EditorDocument).filter(EditorDocument.job_id == job_id).first()
    assert job.segments_json == REAL_EDITED_LYRICS
    assert document.current_segments == REAL_EDITED_LYRICS
    assert job.segments_revision == document.revision == 3
