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


# ---------------------------------------------------------------------------
# Letra oficial pegada desde el editor (2026-09-13)
# ---------------------------------------------------------------------------


def _mock_align_from_lyrics(monkeypatch, seen=None):
    """Motor genérico: devuelve una línea alineada por cada línea del ancla,
    así el test puede pegar cualquier cantidad de líneas."""
    async def _fake(result, audio_path, job_id, anchor_lyrics):
        if seen is not None:
            seen["anchor_lyrics"] = anchor_lyrics
            seen["calls"] = seen.get("calls", 0) + 1
        lines = [ln for ln in anchor_lyrics.splitlines() if ln.strip()]
        out = dict(result)
        out["segments"] = [
            {"start": i * 2 + 0.5, "end": i * 2 + 2.0, "text": line, "words": []}
            for i, line in enumerate(lines)
        ]
        out["timing_source"] = "anchor_ctc"
        return out
    monkeypatch.setattr(main_mod, "_maybe_anchor_align", _fake)


PASTED_SAME_SHAPE = "\n".join([
    "primera linea corregida",
    "Segunda linea corregida",          # igual salvo mayúscula → bloque igual
    "tercera linea OFICIAL distinta",   # cambia → reemplazo + review
    "cuarta linea corregida",
])


def test_reanchor_pasted_merges_by_block_keeps_locked_and_flags_replaced(client, monkeypatch):
    monkeypatch.setenv("ANCHOR_LYRICS_ENABLED", "1")
    seen = {}
    _mock_align_from_lyrics(monkeypatch, seen)
    token, user_id, tenant_id = _make_user(client)
    job_id = _seed_job(user_id, tenant_id, segments=list(SEGS))

    res = client.post(f"/jobs/{job_id}/reanchor", headers=auth(token),
                      json={"lyrics_text": PASTED_SAME_SHAPE})
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["ok"] is True
    assert body["content_source"] == "operator_pasted"
    assert body["lines_kept"] == 3
    assert body["lines_replaced"] == 1
    assert body["locked_kept"] == 1
    assert body["locked_dropped"] == 0
    assert body["structure"]["supported"] is True
    assert seen["anchor_lyrics"] == PASTED_SAME_SHAPE

    persisted = _db_segments(job_id)
    assert len(persisted) == 4
    # La línea locked conserva timing manual, identidad y texto tal cual.
    locked = next(s for s in persisted if s.get("locked"))
    assert (locked["start"], locked["end"], locked["_id"]) == (2.0, 4.0, 1)
    assert locked["text"] == "segunda linea corregida"
    # Bloque igual no locked: conserva _id, toma timing nuevo.
    first = next(s for s in persisted if s.get("_id") == 0)
    assert (first["start"], first["end"]) == (0.5, 2.0)
    # Bloque distinto: entra la línea oficial, marcada para revisar.
    replaced = next(s for s in persisted if s["text"] == "tercera linea OFICIAL distinta")
    assert replaced.get("review") is True
    assert "_id" not in replaced


def test_reanchor_pasted_structure_divergent_409_then_confirm(client, monkeypatch):
    """Color Esperanza (d323e1bc378c): letra íntegra de otra versión sobre
    un audio más corto. Sin confirmación → 409 y NO se gasta alineación."""
    monkeypatch.setenv("ANCHOR_LYRICS_ENABLED", "1")
    seen = {}
    _mock_align_from_lyrics(monkeypatch, seen)
    token, user_id, tenant_id = _make_user(client)
    job_id = _seed_job(user_id, tenant_id, segments=list(SEGS))
    other_version = "\n".join(f"estrofa de otra version numero {i}" for i in range(12))

    res = client.post(f"/jobs/{job_id}/reanchor", headers=auth(token),
                      json={"lyrics_text": other_version})
    assert res.status_code == 409, res.text
    body = res.json()
    assert body["code"] == "reference_structure_unconfirmed"
    assert "line_count_divergent" in body["structure"]["reasons"]
    assert body["structure"]["pasted_line_count"] == 12
    assert body["structure"]["current_line_count"] == 4
    assert seen.get("calls", 0) == 0
    assert [s["text"] for s in _db_segments(job_id)] == [s["text"] for s in SEGS]

    res = client.post(f"/jobs/{job_id}/reanchor", headers=auth(token),
                      json={"lyrics_text": other_version, "confirm_structure": True})
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["count"] == 12
    assert body["lines_replaced"] == 12
    assert body["review_count"] == 12
    assert body["structure"]["supported"] is False
    assert len(_db_segments(job_id)) == 12


def test_reanchor_pasted_too_few_lines_422(client, monkeypatch):
    monkeypatch.setenv("ANCHOR_LYRICS_ENABLED", "1")
    _mock_align_from_lyrics(monkeypatch)
    token, user_id, tenant_id = _make_user(client)
    job_id = _seed_job(user_id, tenant_id, segments=list(SEGS))
    res = client.post(f"/jobs/{job_id}/reanchor", headers=auth(token),
                      json={"lyrics_text": "una sola linea\n\n"})
    assert res.status_code == 422


def test_reanchor_legacy_path_reports_editor_text_source(client, monkeypatch):
    monkeypatch.setenv("ANCHOR_LYRICS_ENABLED", "1")
    _mock_align_ok(monkeypatch)
    token, user_id, tenant_id = _make_user(client)
    job_id = _seed_job(user_id, tenant_id, segments=list(SEGS))
    res = client.post(f"/jobs/{job_id}/reanchor", headers=auth(token), json={})
    assert res.status_code == 200, res.text
    assert res.json()["content_source"] == "editor_text"
    assert res.json()["structure"] is None


# ---------------------------------------------------------------------------
# Guardrails del incidente 2026-09-13 (bots pisaron 185 borradores y el
# re-anclado forzó letras de otra versión sobre el audio)
# ---------------------------------------------------------------------------


def _crammed_retimed():
    """Motor sobre una letra con estrofas que el audio no canta: las 4
    líneas vuelven apretadas en <1 s con score ≈ 0 (forced_align sin
    skip arcs). Es exactamente la forma de Color Esperanza rev 2."""
    out = []
    for i, text in enumerate(s["text"] for s in SEGS):
        out.append({
            "start": 10.0 + i * 0.5, "end": 10.0 + i * 0.5 + 0.4, "text": text,
            "words": [{"word": w, "start": 10.0 + i * 0.5, "end": 10.0 + i * 0.5 + 0.1,
                       "score": 0.01} for w in text.split()],
        })
    return out


def _mock_align_crammed(monkeypatch):
    async def _fake(result, audio_path, job_id, anchor_lyrics):
        out = dict(result)
        out["segments"] = _crammed_retimed()
        out["timing_source"] = "anchor_ctc"
        return out
    monkeypatch.setattr(main_mod, "_maybe_anchor_align", _fake)


def test_reanchor_declines_crammed_alignment_and_keeps_segments(client, monkeypatch):
    monkeypatch.setenv("ANCHOR_LYRICS_ENABLED", "1")
    _mock_align_crammed(monkeypatch)
    token, user_id, tenant_id = _make_user(client)
    job_id = _seed_job(user_id, tenant_id, segments=list(SEGS))

    res = client.post(f"/jobs/{job_id}/reanchor", headers=auth(token))
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["ok"] is False
    assert body["reason"] == "structural_mismatch"
    assert body["structural"]["crammed_run"] == 4
    assert body["structural"]["crammed_lines"] == 4
    assert _db_segments(job_id) == list(SEGS), "los segments del operador quedan intactos"

    from database import AuditLog, SessionLocal
    s = SessionLocal()
    try:
        row = (s.query(AuditLog).filter(AuditLog.action == "lyrics.reanchor_declined")
               .order_by(AuditLog.id.desc()).first())
        assert row is not None and row.detail["job_id"] == job_id
        assert row.detail["reason"] == "structural_mismatch"
    finally:
        s.close()


def test_reanchor_crammed_guard_can_be_disabled(client, monkeypatch):
    monkeypatch.setenv("ANCHOR_LYRICS_ENABLED", "1")
    monkeypatch.setenv("REANCHOR_CRAMMED_GUARD", "0")
    _mock_align_crammed(monkeypatch)
    token, user_id, tenant_id = _make_user(client)
    job_id = _seed_job(user_id, tenant_id, segments=list(SEGS))
    res = client.post(f"/jobs/{job_id}/reanchor", headers=auth(token))
    assert res.status_code == 200, res.text
    assert res.json()["ok"] is True


def test_reanchor_edited_text_divergent_from_machine_snapshot_409_then_confirm(client, monkeypatch):
    """Color Esperanza (d323e1bc378c) por el camino REAL del incidente: la
    letra íntegra de otra versión entró por /save-segments (no por el modal
    de pegar) y el operador apretó "Re-sincronizar con IA". El snapshot de
    máquina del editor tiene 4 líneas; el texto editado, 12."""
    monkeypatch.setenv("ANCHOR_LYRICS_ENABLED", "1")
    seen = {}
    _mock_align_from_lyrics(monkeypatch, seen)
    token, user_id, tenant_id = _make_user(client)
    machine = [{k: v for k, v in s.items() if k != "locked"} for s in SEGS]
    edited = [
        {"start": i * 2.0, "end": i * 2.0 + 1.5, "text": f"estrofa de otra version numero {i}", "_id": i}
        for i in range(12)
    ]
    job_id = _seed_job(user_id, tenant_id, segments=edited)
    _seed_editor_document(job_id, tenant_id, machine, revision=3)

    res = client.post(f"/jobs/{job_id}/reanchor", headers=auth(token), json={"base_revision": 3})
    assert res.status_code == 409, res.text
    body = res.json()
    assert body["code"] == "reference_structure_unconfirmed"
    assert body["structure"]["reference"] == "machine_snapshot"
    assert "line_count_divergent" in body["structure"]["reasons"]
    assert body["structure"]["pasted_line_count"] == 12
    assert body["structure"]["current_line_count"] == 4
    assert seen.get("calls", 0) == 0, "no se gasta CTC sin confirmación"
    assert [s["text"] for s in _db_segments(job_id)] == [s["text"] for s in edited]

    res = client.post(f"/jobs/{job_id}/reanchor", headers=auth(token),
                      json={"base_revision": 3, "confirm_structure": True})
    assert res.status_code == 200, res.text
    assert res.json()["ok"] is True
    assert seen["calls"] == 1


def test_reanchor_edited_text_same_as_machine_snapshot_skips_gate(client, monkeypatch):
    """Correcciones chicas sobre el snapshot (misma estructura) no piden
    confirmación — es el flujo diario del revisor."""
    monkeypatch.setenv("ANCHOR_LYRICS_ENABLED", "1")
    seen = {}
    _mock_align_ok(monkeypatch, seen)
    token, user_id, tenant_id = _make_user(client)
    seeded = [{k: v for k, v in s.items() if k != "locked"} for s in SEGS]
    edited = [dict(s) for s in seeded]
    edited[1]["text"] = "segunda linea CORREGIDA por el revisor"
    job_id = _seed_job(user_id, tenant_id, segments=edited)
    _seed_editor_document(job_id, tenant_id, seeded, revision=2)
    res = client.post(f"/jobs/{job_id}/reanchor", headers=auth(token), json={"base_revision": 2})
    assert res.status_code == 200, res.text
    assert res.json()["ok"] is True


def test_reanchor_surfaces_helper_structural_decline(client, monkeypatch):
    """El veredicto ahora vive en _maybe_anchor_align (todos los motores y
    flujos). El endpoint tiene que devolver el motivo, no un 'declined'
    opaco, y auditar la decisión."""
    monkeypatch.setenv("ANCHOR_LYRICS_ENABLED", "1")

    async def _fake(result, audio_path, job_id, anchor_lyrics):
        out = dict(result)
        out["anchor_alignment"] = {
            "status": "declined", "reason": "structural_mismatch",
            "timing_source": "whisper_align",
            "structural": {"crammed_lines": 5, "crammed_run": 5, "crammed_fraction": 0.29},
        }
        return out
    monkeypatch.setattr(main_mod, "_maybe_anchor_align", _fake)
    token, user_id, tenant_id = _make_user(client)
    job_id = _seed_job(user_id, tenant_id, segments=list(SEGS))
    res = client.post(f"/jobs/{job_id}/reanchor", headers=auth(token))
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["ok"] is False
    assert body["reason"] == "structural_mismatch"
    assert body["structural"]["crammed_run"] == 5
    assert _db_segments(job_id) == list(SEGS)
    from database import AuditLog, SessionLocal
    s = SessionLocal()
    try:
        row = (s.query(AuditLog).filter(AuditLog.action == "lyrics.reanchor_declined")
               .order_by(AuditLog.id.desc()).first())
        assert row.detail["job_id"] == job_id and row.detail["timing_source"] == "whisper_align"
    finally:
        s.close()
