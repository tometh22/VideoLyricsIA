"""Tests for POST /jobs/{job_id}/reanchor — Versión B, parte 2.

Contract under test: auth + status gate idénticos a /save-segments, gate
por ANCHOR_LYRICS_ENABLED, y el merge del re-anclado:

  - usa el TEXTO de segments_json como letra ancla (líneas no vacías),
  - respeta `locked: true` (timing manual del operador no se pisa),
  - marca `review: true` según el gate por línea del motor,
  - persiste el resultado en segments_json en orden monotónico,
  - decline del motor → 200 {ok: false} y segments intactos.

`_maybe_anchor_align` (el motor CTC) se mockea a nivel de main — su propio
contrato ya está cubierto por tests/test_anchor_lyrics.py.
"""

import os
import uuid

import main as main_mod
from tests.conftest import auth


SEGS = [
    {"start": 0.0, "end": 2.0, "text": "primera linea corregida", "_id": 0},
    {"start": 2.0, "end": 4.0, "text": "segunda linea corregida", "_id": 1,
     "locked": True},
    {"start": 4.0, "end": 6.0, "text": "tercera linea corregida", "_id": 2},
    {"start": 6.0, "end": 8.0, "text": "cuarta linea corregida", "_id": 3},
]


def _make_user(client):
    """Register a user and return (token, user_id, tenant_id)."""
    from database import SessionLocal, User

    username = f"reanchor_{uuid.uuid4().hex[:6]}"
    res = client.post("/auth/register", json={
        "username": username,
        "password": "testpass12345",
        "email": f"{username}@test.com",
    })
    assert res.status_code == 200, res.text
    token = res.json()["token"]
    s = SessionLocal()
    try:
        u = s.query(User).filter(User.username == username).first()
        u.ai_authorized = True
        s.commit()
        return token, u.id, u.tenant_id
    finally:
        s.close()


def _seed_job(user_id, tenant_id, *, status="transcribed_pending",
              segments=None, r2_key="uploads/x/song.wav",
              filename="song.wav", with_audio=True):
    """Seed a Job row + (optionally) its audio file on local disk so the
    endpoint skips the R2 download."""
    from database import Job, SessionLocal

    job_id = uuid.uuid4().hex[:12]
    db = SessionLocal()
    try:
        db.add(Job(
            job_id=job_id,
            user_id=user_id,
            tenant_id=tenant_id,
            artist="Intoxicados",
            song_title="Está Saliendo el Sol",
            style="oscuro",
            filename=filename,
            status=status,
            current_step="editing",
            progress=0,
            delivery_profile="youtube",
            segments_json=segments,
            input_r2_key=r2_key,
        ))
        db.commit()
    finally:
        db.close()
    if with_audio and filename:
        job_dir = os.path.join(main_mod.OUTPUTS_DIR, job_id)
        os.makedirs(job_dir, exist_ok=True)
        with open(os.path.join(job_dir, filename), "wb") as f:
            f.write(b"fake-audio")
    return job_id


def _retimed():
    """Engine output for the 4 non-empty lines of SEGS. Line 3 (index 2)
    comes back flagged review. Timings deliberately shifted."""
    return [
        {"start": 0.5, "end": 2.1, "text": "primera linea corregida",
         "words": [{"word": "primera", "start": 0.5, "end": 1.0, "score": 0.9}]},
        {"start": 2.6, "end": 4.1, "text": "segunda linea corregida",
         "words": [{"word": "segunda", "start": 2.6, "end": 3.0, "score": 0.9}]},
        {"start": 4.7, "end": 6.2, "text": "tercera linea corregida",
         "words": [], "review": True},
        {"start": 6.9, "end": 8.4, "text": "cuarta linea corregida",
         "words": []},
    ]


def _mock_align_ok(monkeypatch, seen=None):
    async def _fake(result, audio_path, job_id, anchor_lyrics):
        if seen is not None:
            seen["anchor_lyrics"] = anchor_lyrics
            seen["audio_path"] = audio_path
        out = dict(result)
        out["segments"] = _retimed()
        out["timing_source"] = "anchor_ctc"
        return out
    monkeypatch.setattr(main_mod, "_maybe_anchor_align", _fake)


def _mock_align_decline(monkeypatch):
    async def _fake(result, audio_path, job_id, anchor_lyrics):
        return result  # contrato del helper: decline = input sin tocar
    monkeypatch.setattr(main_mod, "_maybe_anchor_align", _fake)


def _db_segments(job_id):
    from database import Job, SessionLocal
    s = SessionLocal()
    try:
        return s.query(Job).filter(Job.job_id == job_id).first().segments_json
    finally:
        s.close()


# ---------------------------------------------------------------------------
# Gates
# ---------------------------------------------------------------------------


def test_reanchor_requires_auth(client):
    res = client.post("/jobs/anything12345/reanchor")
    assert res.status_code in (401, 403)


def test_reanchor_flag_off_409(client, monkeypatch):
    monkeypatch.delenv("ANCHOR_LYRICS_ENABLED", raising=False)
    token, user_id, tenant_id = _make_user(client)
    job_id = _seed_job(user_id, tenant_id, segments=list(SEGS))

    res = client.post(f"/jobs/{job_id}/reanchor", headers=auth(token))
    assert res.status_code == 409
    assert "habilitado" in res.json()["detail"]


def test_reanchor_unknown_job_404(client, monkeypatch):
    monkeypatch.setenv("ANCHOR_LYRICS_ENABLED", "1")
    token, _, _ = _make_user(client)
    res = client.post("/jobs/deadbeefdead/reanchor", headers=auth(token))
    assert res.status_code == 404


def test_reanchor_other_users_job_404(client, monkeypatch):
    """Ownership opaco igual que /save-segments: ajeno → 404, no 403.
    El 404 gana incluso con el flag off (no filtrar existencia)."""
    monkeypatch.delenv("ANCHOR_LYRICS_ENABLED", raising=False)
    _, a_user_id, a_tenant_id = _make_user(client)
    token_b, _, _ = _make_user(client)
    job_id = _seed_job(a_user_id, a_tenant_id, segments=list(SEGS))

    res = client.post(f"/jobs/{job_id}/reanchor", headers=auth(token_b))
    assert res.status_code == 404


def test_reanchor_platform_admin_can_open_cross_tenant_job(client, admin_token, monkeypatch):
    monkeypatch.delenv("ANCHOR_LYRICS_ENABLED", raising=False)
    _, user_id, tenant_id = _make_user(client)
    job_id = _seed_job(user_id, tenant_id, segments=list(SEGS))
    res = client.post(f"/jobs/{job_id}/reanchor", headers=auth(admin_token))
    # Feature gate proves authorization passed; this used to be an opaque 404.
    assert res.status_code == 409
    assert "habilitado" in res.json()["detail"]


def test_reanchor_wrong_status_409(client, monkeypatch):
    monkeypatch.setenv("ANCHOR_LYRICS_ENABLED", "1")
    token, user_id, tenant_id = _make_user(client)
    job_id = _seed_job(user_id, tenant_id, status="queued", segments=list(SEGS))

    res = client.post(f"/jobs/{job_id}/reanchor", headers=auth(token))
    assert res.status_code == 409
    assert "transcribed_pending" in res.json()["detail"]


def test_reanchor_too_few_lines_422(client, monkeypatch):
    monkeypatch.setenv("ANCHOR_LYRICS_ENABLED", "1")
    token, user_id, tenant_id = _make_user(client)
    job_id = _seed_job(user_id, tenant_id, segments=[
        {"start": 0.0, "end": 1.0, "text": "una"},
        {"start": 1.0, "end": 2.0, "text": "   "},   # vacía no cuenta
        {"start": 2.0, "end": 3.0, "text": "dos"},
    ])
    res = client.post(f"/jobs/{job_id}/reanchor", headers=auth(token))
    assert res.status_code == 422


def test_reanchor_missing_audio_409(client, monkeypatch):
    """Sin archivo local NI input_r2_key no hay contra qué alinear."""
    monkeypatch.setenv("ANCHOR_LYRICS_ENABLED", "1")
    token, user_id, tenant_id = _make_user(client)
    job_id = _seed_job(user_id, tenant_id, segments=list(SEGS),
                       r2_key=None, with_audio=False)
    res = client.post(f"/jobs/{job_id}/reanchor", headers=auth(token))
    assert res.status_code == 409


# ---------------------------------------------------------------------------
# Happy path + decline
# ---------------------------------------------------------------------------


def test_reanchor_happy_path_persists_and_respects_locked(client, monkeypatch):
    monkeypatch.setenv("ANCHOR_LYRICS_ENABLED", "1")
    token, user_id, tenant_id = _make_user(client)
    job_id = _seed_job(user_id, tenant_id, segments=[dict(s) for s in SEGS])
    seen = {}
    _mock_align_ok(monkeypatch, seen)

    res = client.post(f"/jobs/{job_id}/reanchor", headers=auth(token))
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["ok"] is True
    assert body["count"] == 4
    assert body["review_count"] == 1
    assert body["locked_kept"] == 1

    # El ancla que recibió el motor es el TEXTO corregido, en orden.
    assert seen["anchor_lyrics"].splitlines() == [
        "primera linea corregida", "segunda linea corregida",
        "tercera linea corregida", "cuarta linea corregida",
    ]

    persisted = _db_segments(job_id)
    by_text = {s["text"]: s for s in persisted}
    # Línea 1 (no locked): timing nuevo del motor + words.
    assert by_text["primera linea corregida"]["start"] == 0.5
    assert by_text["primera linea corregida"]["end"] == 2.1
    assert by_text["primera linea corregida"]["words"]
    # Línea 2 (locked): timing del operador intacto, sin review.
    assert by_text["segunda linea corregida"]["start"] == 2.0
    assert by_text["segunda linea corregida"]["end"] == 4.0
    assert by_text["segunda linea corregida"]["locked"] is True
    # Línea 3: retimed + flag review del gate por línea.
    assert by_text["tercera linea corregida"]["start"] == 4.7
    assert by_text["tercera linea corregida"]["review"] is True
    # Keys extra del original (p. ej. _id) sobreviven al merge.
    assert by_text["cuarta linea corregida"]["_id"] == 3
    # Orden monotónico por start (contrato /save-segments).
    starts = [s["start"] for s in persisted]
    assert starts == sorted(starts)
    # La respuesta trae los segments mergeados para refrescar el editor.
    assert res.json()["segments"] == persisted


def test_reanchor_decline_keeps_segments_intact(client, monkeypatch):
    monkeypatch.setenv("ANCHOR_LYRICS_ENABLED", "1")
    token, user_id, tenant_id = _make_user(client)
    before = [dict(s) for s in SEGS]
    job_id = _seed_job(user_id, tenant_id, segments=[dict(s) for s in SEGS])
    _mock_align_decline(monkeypatch)

    res = client.post(f"/jobs/{job_id}/reanchor", headers=auth(token))
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["ok"] is False
    assert body["reason"] == "declined"
    assert _db_segments(job_id) == before


def test_reanchor_line_count_mismatch_declines(client, monkeypatch):
    """Si el motor devuelve otra cantidad de líneas que las ancladas
    (nunca debería, pero es el guard del merge 1:1), decline seguro."""
    monkeypatch.setenv("ANCHOR_LYRICS_ENABLED", "1")
    token, user_id, tenant_id = _make_user(client)
    before = [dict(s) for s in SEGS]
    job_id = _seed_job(user_id, tenant_id, segments=[dict(s) for s in SEGS])

    async def _fake(result, audio_path, job_id_, anchor_lyrics):
        out = dict(result)
        out["segments"] = _retimed()[:2]   # 2 líneas para 4 ancladas
        out["timing_source"] = "anchor_ctc"
        return out
    monkeypatch.setattr(main_mod, "_maybe_anchor_align", _fake)

    res = client.post(f"/jobs/{job_id}/reanchor", headers=auth(token))
    assert res.status_code == 200, res.text
    assert res.json()["ok"] is False
    assert _db_segments(job_id) == before


def test_reanchor_empty_text_lines_pass_through(client, monkeypatch):
    """Segments con texto vacío no van al ancla pero sobreviven el merge
    (instrumentales / placeholders del editor)."""
    monkeypatch.setenv("ANCHOR_LYRICS_ENABLED", "1")
    token, user_id, tenant_id = _make_user(client)
    segs = [dict(s) for s in SEGS]
    segs.insert(2, {"start": 3.0, "end": 3.5, "text": "  ", "_id": 99})
    job_id = _seed_job(user_id, tenant_id, segments=segs)
    seen = {}
    _mock_align_ok(monkeypatch, seen)

    res = client.post(f"/jobs/{job_id}/reanchor", headers=auth(token))
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["ok"] is True
    assert body["count"] == 5
    # El ancla sigue siendo solo las 4 líneas con texto.
    assert len(seen["anchor_lyrics"].splitlines()) == 4
    persisted = _db_segments(job_id)
    assert any(s.get("_id") == 99 for s in persisted)


def test_reanchor_rejects_stale_revision_before_alignment(client, monkeypatch):
    monkeypatch.setenv("ANCHOR_LYRICS_ENABLED", "1")
    token, user_id, tenant_id = _make_user(client)
    job_id = _seed_job(user_id, tenant_id, segments=[dict(s) for s in SEGS])
    from database import Job, SessionLocal
    db = SessionLocal()
    try:
        row = db.query(Job).filter(Job.job_id == job_id).first()
        row.segments_revision = 3
        db.commit()
    finally:
        db.close()

    res = client.post(
        f"/jobs/{job_id}/reanchor",
        json={"base_revision": 2},
        headers=auth(token),
    )
    assert res.status_code == 409
    assert res.json()["code"] == "stale_revision"
    assert res.json()["current_revision"] == 3


def _seed_editor_document(job_id, tenant_id, segments, revision):
    """Give the job a durable editor document at a known revision, and set
    job.segments_revision to match — the shape every real job that has been
    opened in the LyricsEditor has."""
    from database import EditorDocument, Job, SessionLocal

    s = SessionLocal()
    try:
        s.query(Job).filter(Job.job_id == job_id).update(
            {"segments_revision": revision}, synchronize_session=False,
        )
        s.add(EditorDocument(
            job_id=job_id, tenant_id=tenant_id,
            current_segments=segments, original_segments=segments,
            revision=revision,
        ))
        s.commit()
    finally:
        s.close()


def _db_document(job_id):
    from database import EditorDocument, SessionLocal
    s = SessionLocal()
    try:
        d = s.query(EditorDocument).filter(EditorDocument.job_id == job_id).first()
        return (d.current_segments, d.revision) if d else (None, None)
    finally:
        s.close()


def test_reanchor_persists_retimed_values_when_job_has_editor_document(client, monkeypatch):
    """Regression (found live in prod 2026-08-13, same day the editor bridge
    shipped): the bridge calls get_or_create_document(), which re-queries the
    Job with .populate_existing(). SessionLocal runs autoflush=False, so the
    pending `row.segments_json = merged` assignment was silently reverted by
    that refresh before it ever reached the DB — the endpoint burned 40-130s
    of real CTC compute and persisted NOTHING (observed: editor revisions
    N and N+1 byte-identical while the worker logged "[CTC] retimed 50 lines").

    The pre-existing happy-path test did not catch it because its job has no
    editor_document and no base_revision, which takes a different branch.
    This one mirrors the real shape: durable document at a known revision +
    explicit base_revision.
    """
    monkeypatch.setenv("ANCHOR_LYRICS_ENABLED", "1")
    token, user_id, tenant_id = _make_user(client)
    # All 4 lines, none locked, so the engine retimes every one of them
    # (line count must match _retimed() or the engine declines).
    seeded = [{k: v for k, v in s.items() if k != "locked"} for s in SEGS]
    job_id = _seed_job(user_id, tenant_id, segments=seeded)
    _seed_editor_document(job_id, tenant_id, seeded, revision=7)
    _mock_align_ok(monkeypatch)

    res = client.post(
        f"/jobs/{job_id}/reanchor", headers=auth(token), json={"base_revision": 7},
    )
    assert res.status_code == 200, res.text
    assert res.json()["ok"] is True

    persisted = {s["text"]: s for s in _db_segments(job_id)}
    assert persisted["primera linea corregida"]["start"] == 0.5, (
        "the retimed values must actually reach the DB — a populate_existing() "
        "refresh in the editor bridge must not silently revert them"
    )
    assert persisted["primera linea corregida"]["end"] == 2.1

    doc_segments, doc_revision = _db_document(job_id)
    assert doc_revision == 8, f"document must advance with the job, got {doc_revision}"
    doc_by_text = {s["text"]: s for s in doc_segments}
    assert doc_by_text["primera linea corregida"]["start"] == 0.5, (
        "editor_documents must carry the retimed values too — otherwise the "
        "editor shows stale timings and the next reconcile stomps one side"
    )
