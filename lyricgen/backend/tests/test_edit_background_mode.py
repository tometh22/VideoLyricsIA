"""POST /edit accepts and forwards background_mode for the bg regen path.

2026-05-16: cabled Imagen-4 as an alternative to Veo for the background
re-generation flow. These tests pin the API layer:

  - Pydantic accepts "veo" and "imagen" via the EditJobRequest enum
  - "midjourney" or other strings are rejected (422)
  - When `background_mode` is in the body, it lands in edit_params (and
    therefore reaches run_edit_pipeline → _ensure_background)
  - When absent, edit_params doesn't carry the key (run_edit_pipeline's
    default "veo" handles it — verified in test_bg_mode_dispatch.py)

Source-level wiring (run_edit_pipeline reads edit_params, _ensure_background
branches on bg_mode) is pinned separately in test_bg_mode_dispatch.py.
"""
import uuid

from database import Job as JobModel, User as UserModel


def _create_pending_review_job(db, tenant_id, user_id):
    """Insert a Job in pending_review status that satisfies request_edit's
    pre-checks: bg_r2_key_cached + segments_json + edit_count=0."""
    job_id = uuid.uuid4().hex[:12]
    db.add(JobModel(
        job_id=job_id,
        user_id=user_id,
        tenant_id=tenant_id,
        artist="Test",
        song_title="BG Mode Test",
        filename="test.mp3",
        status="pending_review",
        delivery_profile="youtube",
        progress=100,
        bg_r2_key_cached="fake/bg.mp4",
        segments_json=[{"start": 0.0, "end": 1.0, "text": "hola"}],
        edit_count=0,
    ))
    db.commit()
    return job_id


def _admin_identity(db):
    admin = db.query(UserModel).filter(UserModel.username == "admin").first()
    assert admin is not None
    return admin.id, admin.tenant_id


def _capture_enqueue_calls(monkeypatch):
    """Replace enqueue_edit with a capturing no-op. Returns the captured
    kwargs list so tests can assert on what would have been enqueued."""
    import main
    captured: list[dict] = []
    monkeypatch.setattr(
        main, "enqueue_edit",
        lambda **kwargs: (captured.append(kwargs), "test:noop")[1],
    )
    return captured


def test_bg_mode_imagen_forwarded_to_edit_params(client, admin_token, db, monkeypatch):
    """Operator picks Imagen → background_mode flows through to
    enqueue_edit's edit_params dict."""
    captured = _capture_enqueue_calls(monkeypatch)
    user_id, tenant_id = _admin_identity(db)
    job_id = _create_pending_review_job(db, tenant_id, user_id)

    res = client.post(
        f"/edit/{job_id}",
        headers={"Authorization": f"Bearer {admin_token}"},
        json={
            "edit_type": "background",
            "background_mode": "imagen",
            "background_hint": "tropical mountain dawn, no people",
        },
    )
    assert res.status_code == 202, res.text
    assert len(captured) == 1
    edit_params = captured[0]["edit_params"]
    assert edit_params.get("background_mode") == "imagen", (
        f"background_mode must land in edit_params; got {edit_params!r}"
    )
    # background_hint also forwards (separate field, pinned for safety)
    assert edit_params.get("background_hint") == "tropical mountain dawn, no people"


def test_bg_mode_veo_explicit_also_forwarded(client, admin_token, db, monkeypatch):
    """Operator picks Veo explicitly (rare but legal) → also lands in
    edit_params. Even though Veo is the runtime default, accepting an
    explicit value is the contract."""
    captured = _capture_enqueue_calls(monkeypatch)
    user_id, tenant_id = _admin_identity(db)
    job_id = _create_pending_review_job(db, tenant_id, user_id)

    res = client.post(
        f"/edit/{job_id}",
        headers={"Authorization": f"Bearer {admin_token}"},
        json={"edit_type": "background", "background_mode": "veo"},
    )
    assert res.status_code == 202, res.text
    assert captured[0]["edit_params"].get("background_mode") == "veo"


def test_bg_mode_absent_leaves_edit_params_clean(client, admin_token, db, monkeypatch):
    """No background_mode in body → key NOT in edit_params. The pipeline's
    own default ("veo") handles the absence; we don't inject a synthetic
    value so the on-wire contract stays minimal."""
    captured = _capture_enqueue_calls(monkeypatch)
    user_id, tenant_id = _admin_identity(db)
    job_id = _create_pending_review_job(db, tenant_id, user_id)

    res = client.post(
        f"/edit/{job_id}",
        headers={"Authorization": f"Bearer {admin_token}"},
        json={"edit_type": "background"},
    )
    assert res.status_code == 202, res.text
    assert "background_mode" not in captured[0]["edit_params"], (
        "When background_mode is absent from the body, it should NOT "
        "appear in edit_params either — let the pipeline's default kick in"
    )


def test_bg_mode_invalid_value_rejected(client, admin_token, db, monkeypatch):
    """Anything outside {veo, imagen} → 422 from Pydantic pattern validation.
    Prevents typos / future-mode pre-announcements from silently going
    through and crashing the worker."""
    _capture_enqueue_calls(monkeypatch)
    user_id, tenant_id = _admin_identity(db)
    job_id = _create_pending_review_job(db, tenant_id, user_id)

    res = client.post(
        f"/edit/{job_id}",
        headers={"Authorization": f"Bearer {admin_token}"},
        json={"edit_type": "background", "background_mode": "midjourney"},
    )
    assert res.status_code == 422, (
        f"invalid background_mode must 422 from Pydantic; got "
        f"{res.status_code} body={res.text!r}"
    )


def test_bg_mode_ignored_for_typography_edit(client, admin_token, db, monkeypatch):
    """background_mode only makes sense for edit_type=background. For
    typography or lyrics edits, the key is accepted in the body (Pydantic
    has no per-edit-type validation) but the handler does NOT propagate
    it to edit_params — typography/lyrics edits reuse the cached bg and
    never invoke _ensure_background."""
    captured = _capture_enqueue_calls(monkeypatch)
    user_id, tenant_id = _admin_identity(db)
    job_id = _create_pending_review_job(db, tenant_id, user_id)

    res = client.post(
        f"/edit/{job_id}",
        headers={"Authorization": f"Bearer {admin_token}"},
        json={
            "edit_type": "typography",
            "background_mode": "imagen",
            "font": "bebas-neue",
        },
    )
    assert res.status_code == 202, res.text
    edit_params = captured[0]["edit_params"]
    assert "background_mode" not in edit_params, (
        "background_mode should be ignored for non-background edit_types"
    )
    # And typography params still propagate
    assert edit_params.get("font") == "bebas-neue"


def test_background_edit_rejected_for_scene_jobs(client, admin_token, db, monkeypatch):
    """Incidente 2026-07-01 (job 53b9513225b1): un edit "background" sobre un
    job multi-escena generaba UN clip Veo de 8 s, pisaba el timeline cacheado
    (bg_r2_key_cached) y re-renderizaba video+short con una sola escena en
    loop. El handler ahora rechaza con 400 y deriva al filmstrip; los demás
    edit types siguen funcionando en jobs de escenas."""
    import uuid as _uuid
    captured = _capture_enqueue_calls(monkeypatch)
    user_id, tenant_id = _admin_identity(db)
    job_id = _uuid.uuid4().hex[:12]
    db.add(JobModel(
        job_id=job_id, user_id=user_id, tenant_id=tenant_id,
        artist="Test", song_title="Scene Guard", filename="t.mp3",
        status="pending_review", delivery_profile="youtube", progress=100,
        bg_r2_key_cached="backgrounds/x/bg_cached.mp4",
        segments_json=[{"start": 0.0, "end": 1.0, "text": "hola"}],
        edit_count=0,
        scene_plan={"scenes": [{"id": "intro", "status": "generated"}]},
    ))
    db.commit()

    res = client.post(
        f"/edit/{job_id}",
        headers={"Authorization": f"Bearer {admin_token}"},
        json={"edit_type": "background"},
    )
    assert res.status_code == 400, res.text
    assert "Escenas" in res.json()["detail"]
    assert captured == []  # nada encolado, cero gasto Veo

    res2 = client.post(
        f"/edit/{job_id}",
        headers={"Authorization": f"Bearer {admin_token}"},
        json={"edit_type": "typography"},
    )
    assert res2.status_code == 202, res2.text
    assert len(captured) == 1


# ── BUG-5: bg_verbatim None-aware (no durable clobber) ──────────────────
# The unified wizard only sends bg_verbatim when the toggle CHANGED. The old
# `bool = False` default silently flipped a persisted True→False on any
# background edit that didn't touch it. bg_verbatim is now `bool | None` and
# request_edit only persists it when the caller actually sent a value.

def _bg_job_with_render_params(db, tenant_id, user_id, render_params):
    job_id = uuid.uuid4().hex[:12]
    db.add(JobModel(
        job_id=job_id, user_id=user_id, tenant_id=tenant_id,
        artist="Test", song_title="Verbatim", filename="t.mp3",
        status="pending_review", delivery_profile="youtube", progress=100,
        bg_r2_key_cached="backgrounds/x/bg_cached.mp4",
        segments_json=[{"start": 0.0, "end": 1.0, "text": "hola"}],
        edit_count=0, render_params=render_params,
    ))
    db.commit()
    return job_id


def test_bg_verbatim_absent_does_not_clobber_persisted_true(client, admin_token, db, monkeypatch):
    """A background edit that omits bg_verbatim must NOT overwrite a persisted
    bg_verbatim=True (the BUG-5 clobber): the write block is skipped entirely."""
    captured = _capture_enqueue_calls(monkeypatch)
    user_id, tenant_id = _admin_identity(db)
    job_id = _bg_job_with_render_params(db, tenant_id, user_id, {"bg_verbatim": True})

    res = client.post(
        f"/edit/{job_id}",
        headers={"Authorization": f"Bearer {admin_token}"},
        json={"edit_type": "background", "movement_style": "animado"},
    )
    assert res.status_code == 202, res.text
    edit_params = captured[0]["edit_params"]
    assert "bg_verbatim" not in edit_params, (
        f"omitted bg_verbatim must not be written; got {edit_params!r}"
    )
    db.expire_all()
    job = db.query(JobModel).filter(JobModel.job_id == job_id).first()
    assert job.render_params.get("bg_verbatim") is True, (
        "persisted bg_verbatim=True must survive a verbatim-untouched bg edit"
    )


def test_bg_verbatim_explicit_false_is_written(client, admin_token, db, monkeypatch):
    """An explicit bg_verbatim=false IS persisted — the None-aware write still
    honours an explicit operator choice."""
    captured = _capture_enqueue_calls(monkeypatch)
    user_id, tenant_id = _admin_identity(db)
    job_id = _bg_job_with_render_params(db, tenant_id, user_id, {"bg_verbatim": True})

    res = client.post(
        f"/edit/{job_id}",
        headers={"Authorization": f"Bearer {admin_token}"},
        json={"edit_type": "background", "bg_verbatim": False},
    )
    assert res.status_code == 202, res.text
    assert captured[0]["edit_params"].get("bg_verbatim") is False


# ── Scene axes editable en un regen: genre / concept / match_lyrics ─────
# Cableados 2026-07-24. None = keep persisted (solo se mandan si cambiaron);
# el pipeline los lee de merged render_params.

def test_scene_axes_forwarded_and_persisted(client, admin_token, db, monkeypatch):
    """genre/concept/match_lyrics enviados → edit_params + render_params."""
    captured = _capture_enqueue_calls(monkeypatch)
    user_id, tenant_id = _admin_identity(db)
    job_id = _bg_job_with_render_params(
        db, tenant_id, user_id,
        {"genre": "rock", "concept": "ciudad", "match_lyrics": True},
    )
    res = client.post(
        f"/edit/{job_id}",
        headers={"Authorization": f"Bearer {admin_token}"},
        json={
            "edit_type": "background",
            "genre": "pop",
            "concept": "naturaleza",
            "match_lyrics": False,
        },
    )
    assert res.status_code == 202, res.text
    ep = captured[0]["edit_params"]
    assert ep.get("genre") == "pop"
    assert ep.get("concept") == "naturaleza"
    assert ep.get("match_lyrics") is False
    db.expire_all()
    job = db.query(JobModel).filter(JobModel.job_id == job_id).first()
    assert job.render_params.get("genre") == "pop"
    assert job.render_params.get("concept") == "naturaleza"
    assert job.render_params.get("match_lyrics") is False


def test_scene_axes_absent_keep_persisted(client, admin_token, db, monkeypatch):
    """Un edit que no toca género/concepto/modo no los pisa (None-aware)."""
    captured = _capture_enqueue_calls(monkeypatch)
    user_id, tenant_id = _admin_identity(db)
    job_id = _bg_job_with_render_params(
        db, tenant_id, user_id,
        {"genre": "rock", "concept": "ciudad", "match_lyrics": False},
    )
    res = client.post(
        f"/edit/{job_id}",
        headers={"Authorization": f"Bearer {admin_token}"},
        json={"edit_type": "background", "movement_style": "animado"},
    )
    assert res.status_code == 202, res.text
    ep = captured[0]["edit_params"]
    assert "genre" not in ep and "concept" not in ep and "match_lyrics" not in ep
    db.expire_all()
    job = db.query(JobModel).filter(JobModel.job_id == job_id).first()
    assert job.render_params.get("genre") == "rock"
    assert job.render_params.get("concept") == "ciudad"
    assert job.render_params.get("match_lyrics") is False


# ── Clear explícito del hint: None = keep, "" = clear, valor = set ──────
# 2026-07-24 (complemento del #979): sin el clear, cambiar el modo de
# escena con un hint persistido era un no-op — resolve_creative_mode
# prioriza operator_prompt y run_edit_pipeline revive el persistido vía
# `background_hint or _persisted_operator_prompt`.

def test_background_hint_empty_string_is_explicit_clear(client, admin_token, db, monkeypatch):
    """background_hint:"" borra el hint persistido (render_params queda "")
    y viaja en edit_params — el caso clave "borro el prompt y elijo
    Inspirado" ({background_hint:"", match_lyrics:true})."""
    captured = _capture_enqueue_calls(monkeypatch)
    user_id, tenant_id = _admin_identity(db)
    job_id = _bg_job_with_render_params(
        db, tenant_id, user_id, {"background_hint": "castillo medieval"})

    res = client.post(
        f"/edit/{job_id}",
        headers={"Authorization": f"Bearer {admin_token}"},
        json={"edit_type": "background", "background_hint": "", "match_lyrics": True},
    )
    assert res.status_code == 202, res.text
    edit_params = captured[0]["edit_params"]
    assert edit_params.get("background_hint") == ""
    assert edit_params.get("match_lyrics") is True
    db.expire_all()
    job = db.query(JobModel).filter(JobModel.job_id == job_id).first()
    assert job.render_params.get("background_hint") == "", (
        "clearing the textarea must persist '' — else the old prompt "
        "resurrects on the next regen and the mode change is a no-op"
    )
    assert job.render_params.get("match_lyrics") is True


def test_background_hint_whitespace_only_is_clear_too(client, admin_token, db, monkeypatch):
    """'   ' se normaliza a "" (strip) → mismo clear explícito."""
    captured = _capture_enqueue_calls(monkeypatch)
    user_id, tenant_id = _admin_identity(db)
    job_id = _bg_job_with_render_params(
        db, tenant_id, user_id, {"background_hint": "castillo medieval"})

    res = client.post(
        f"/edit/{job_id}",
        headers={"Authorization": f"Bearer {admin_token}"},
        json={"edit_type": "background", "background_hint": "   "},
    )
    assert res.status_code == 202, res.text
    assert captured[0]["edit_params"].get("background_hint") == ""
    db.expire_all()
    job = db.query(JobModel).filter(JobModel.job_id == job_id).first()
    assert job.render_params.get("background_hint") == ""


def test_background_hint_absent_keeps_persisted(client, admin_token, db, monkeypatch):
    """Omitir background_hint NO toca el persistido (None = keep)."""
    captured = _capture_enqueue_calls(monkeypatch)
    user_id, tenant_id = _admin_identity(db)
    job_id = _bg_job_with_render_params(
        db, tenant_id, user_id, {"background_hint": "castillo medieval"})

    res = client.post(
        f"/edit/{job_id}",
        headers={"Authorization": f"Bearer {admin_token}"},
        json={"edit_type": "background", "movement_style": "animado"},
    )
    assert res.status_code == 202, res.text
    assert "background_hint" not in captured[0]["edit_params"]
    db.expire_all()
    job = db.query(JobModel).filter(JobModel.job_id == job_id).first()
    assert job.render_params.get("background_hint") == "castillo medieval"


def test_background_hint_ignored_for_non_background(client, admin_token, db, monkeypatch):
    """El clear solo aplica con edit_type=background (mismo gating que
    siempre): un edit de tipografía con background_hint:"" no borra nada."""
    captured = _capture_enqueue_calls(monkeypatch)
    user_id, tenant_id = _admin_identity(db)
    job_id = _bg_job_with_render_params(
        db, tenant_id, user_id, {"background_hint": "castillo medieval"})

    res = client.post(
        f"/edit/{job_id}",
        headers={"Authorization": f"Bearer {admin_token}"},
        json={"edit_type": "typography", "font": "bebas-neue", "background_hint": ""},
    )
    assert res.status_code == 202, res.text
    assert "background_hint" not in captured[0]["edit_params"]
    db.expire_all()
    job = db.query(JobModel).filter(JobModel.job_id == job_id).first()
    assert job.render_params.get("background_hint") == "castillo medieval"


# ── E2E (cadena de datos): "cambiar género + Generar otra versión" ──────
# Maneja el /edit REAL + la persistencia REAL, reconstruye el `merged` del
# worker y ejecuta la lógica REAL del pipeline para verificar qué inputs
# recibe el generador (_ensure_background). Prueba la cadena completa menos el
# render Veo (network-bound, no drivable — pineado en test_bg_mode_dispatch).
def test_e2e_regen_change_genre_keeps_prompt_and_scene(client, admin_token, db, monkeypatch):
    import inspect
    import pipeline

    captured = _capture_enqueue_calls(monkeypatch)
    user_id, tenant_id = _admin_identity(db)
    PROMPT = "mansión surreal de noche, pileta vacía, cámara fija"
    job_id = _bg_job_with_render_params(db, tenant_id, user_id, {
        "background_hint": PROMPT,
        "genre": "rock",
        "concept": "ciudad",
        "match_lyrics": False,        # "Auto"
        "bg_verbatim": True,
        "movement_style": "estatico",
        "style": "oscuro",
    })

    # 1) La llamada EXACTA del wizard para "cambiar género + Generar otra
    #    versión": cambia genre, sin hint fresco (bucket vacío = otra versión).
    # Body EXACTO que arma el wizard (computeFieldDiff + backgroundRegenExtras)
    # para este escenario — ver el handshake en editWizardDiff.test.js.
    res = client.post(
        f"/edit/{job_id}",
        headers={"Authorization": f"Bearer {admin_token}"},
        json={"edit_type": "background", "genre": "pop", "force_content_validation": True},
    )
    assert res.status_code == 202, res.text

    # 2) Persistencia REAL: género nuevo, todo lo demás intacto (sin clobber).
    db.expire_all()
    rp = db.query(JobModel).filter(JobModel.job_id == job_id).first().render_params
    assert rp["genre"] == "pop"              # el edit
    assert rp["background_hint"] == PROMPT    # sin tocar → se mantiene
    assert rp["match_lyrics"] is False        # sin tocar → se mantiene
    assert rp["bg_verbatim"] is True          # sin tocar → se mantiene
    assert rp["movement_style"] == "estatico"

    # 3) La vista `merged` del worker (pipeline.py: merged = render_params ∪ edit_params).
    edit_params = captured[0]["edit_params"]
    merged = {**rp, **edit_params}

    # 4) Inputs del generador, derivados con la MISMA lógica del pipeline.
    background_hint = edit_params.get("background_hint") or None   # None: sin hint fresco
    persisted = merged.get("background_hint") or None
    effective_hint = background_hint or persisted                 # FIX 1
    _ml = merged.get("match_lyrics", True)
    effective_match_lyrics = True if _ml is None else bool(_ml)    # FIX 4
    genre = merged.get("genre") or ""

    assert genre == "pop"                    # el género nuevo llega al regen
    assert effective_hint == PROMPT          # el prompt original se reproduce (no se pierde)
    assert effective_match_lyrics is False   # "Auto" preservado (no flipea a True)

    # El prompt de SEGURIDAD/validación (función REAL) coincide con el de
    # generación → sin split generate-with-intent / validate-without-permission.
    assert pipeline._operator_prompt_for_edit(
        "background", fresh_background_hint=background_hint,
        persisted_operator_prompt=persisted,
    ) == PROMPT

    # Contrato: _ensure_background acepta de verdad estos kwargs.
    sig = inspect.signature(pipeline._ensure_background).parameters
    for p in ("genre", "concept", "background_hint", "match_lyrics",
              "bg_verbatim", "movement_style", "effect"):
        assert p in sig, f"_ensure_background debe aceptar {p}"
