"""Tests de endpoints de Escenas (multi-escena) vía TestClient.

Cubren la garantía de gating/entitlement que la auditoría marcó:
- /auth/me devuelve features.scenes (audit A6) → el refresh del front desbloquea
  sin re-login.
- features.scenes = admin OR SCENES_ENABLED_TENANTS.
"""


def test_auth_me_returns_features_scenes_admin(client, admin_token):
    r = client.get("/auth/me", headers={"Authorization": f"Bearer {admin_token}"})
    assert r.status_code == 200
    body = r.json()
    assert "features" in body, "/auth/me debe incluir features (audit A6)"
    # Admin siempre elegible (has_scenes_access).
    assert body["features"]["scenes"] is True
    # No rompimos los otros flags.
    assert "prores_export" in body["features"]
    assert "telemetry" in body["features"]


def test_auth_me_features_scenes_false_for_plain_user(client, user_token, monkeypatch):
    # Rollback (SCENES_GLOBALLY_ENABLED=0): un user común sin allowlist NO es
    # elegible. Con el flag en ON (default) Escenas es PÚBLICO — ver
    # test_scenes_credits.py.
    monkeypatch.setenv("SCENES_GLOBALLY_ENABLED", "0")
    r = client.get("/auth/me", headers={"Authorization": f"Bearer {user_token}"})
    assert r.status_code == 200
    assert r.json()["features"]["scenes"] is False


# ── POST /jobs/{id}/scenes/{key}/regenerate ────────────────────────────────
import uuid as _uuid


def _make_scene_job(db, tenant_id, *, user_id=1, status="pending_review", edit_count=0,
                    with_scene=True, input_key="in/x.mp3", youtube=None):
    from database import Job as JobModel
    jid = _uuid.uuid4().hex[:12]
    plan = {
        "bible": {"world": "w"},
        "sections": [{"type": "coro", "start": 0.0, "end": 10.0, "energy": 0.85, "recurrence_key": "coro_1", "text": ""}],
        "scenes": [{"recurrence_key": "coro_1", "section_type": "coro", "energy": 0.85,
                    "movement_style": "dinamico", "prompt": "p", "status": "generated"}],
    } if with_scene else None
    job = JobModel(
        job_id=jid, tenant_id=tenant_id, user_id=user_id, artist="T", song_title="S",
        filename="t.mp3", status=status, delivery_profile="youtube", progress=100,
        segments_json=[{"start": 0.0, "end": 1.0, "text": "x"}],
        edit_count=edit_count, scene_plan=plan, input_r2_key=input_key,
        youtube_data=youtube,
    )
    db.add(job)
    db.commit()
    return jid


def _hdr(tok):
    return {"Authorization": f"Bearer {tok}"}


def test_regen_scene_403_for_non_eligible(client, user_token, db, monkeypatch):
    import main
    monkeypatch.setenv("SCENES_GLOBALLY_ENABLED", "0")  # rollback: gating activo
    monkeypatch.setattr(main, "enqueue_edit", lambda **k: "edit:fake")
    me = client.get("/auth/me", headers=_hdr(user_token)).json()
    jid = _make_scene_job(db, me["tenant_id"])
    r = client.post(f"/jobs/{jid}/scenes/coro_1/regenerate", headers=_hdr(user_token), json={})
    assert r.status_code == 403  # user común no tiene has_scenes_access (modo rollback)


def test_regen_scene_404_unknown_key(client, admin_token, db, monkeypatch):
    import main
    monkeypatch.setattr(main, "enqueue_edit", lambda **k: "edit:fake")
    me = client.get("/auth/me", headers=_hdr(admin_token)).json()
    jid = _make_scene_job(db, me["tenant_id"])
    r = client.post(f"/jobs/{jid}/scenes/no_existe/regenerate", headers=_hdr(admin_token), json={})
    assert r.status_code == 404


def test_regen_scene_400_not_scene_job(client, admin_token, db, monkeypatch):
    import main
    monkeypatch.setattr(main, "enqueue_edit", lambda **k: "edit:fake")
    me = client.get("/auth/me", headers=_hdr(admin_token)).json()
    jid = _make_scene_job(db, me["tenant_id"], with_scene=False)
    r = client.post(f"/jobs/{jid}/scenes/coro_1/regenerate", headers=_hdr(admin_token), json={})
    assert r.status_code == 400


def test_regen_scene_404_wrong_tenant(client, admin_token, db, monkeypatch):
    import main
    monkeypatch.setattr(main, "enqueue_edit", lambda **k: "edit:fake")
    jid = _make_scene_job(db, "otro_tenant_inexistente")
    r = client.post(f"/jobs/{jid}/scenes/coro_1/regenerate", headers=_hdr(admin_token), json={})
    assert r.status_code == 404  # no es del tenant del admin


def test_regen_scene_edit_count_cap_admin_exempt(client, admin_token, db, monkeypatch):
    """Admin está exento del cap; un job al límite igual procesa."""
    import main
    captured = {}
    monkeypatch.setattr(main, "enqueue_edit", lambda **k: captured.update(k) or "edit:fake")
    me = client.get("/auth/me", headers=_hdr(admin_token)).json()
    jid = _make_scene_job(db, me["tenant_id"], edit_count=99)
    r = client.post(f"/jobs/{jid}/scenes/coro_1/regenerate", headers=_hdr(admin_token), json={})
    assert r.status_code == 200
    assert captured.get("edit_type") == "scene"
    assert captured["edit_params"]["scene_key"] == "coro_1"


def test_regen_scene_409_youtube_drift(client, admin_token, db, monkeypatch):
    import main
    monkeypatch.setattr(main, "enqueue_edit", lambda **k: "edit:fake")
    me = client.get("/auth/me", headers=_hdr(admin_token)).json()
    jid = _make_scene_job(db, me["tenant_id"], status="done",
                          youtube={"url": "https://youtu.be/x"})
    r = client.post(f"/jobs/{jid}/scenes/coro_1/regenerate", headers=_hdr(admin_token), json={})
    assert r.status_code == 409
    # con override pasa.
    r2 = client.post(f"/jobs/{jid}/scenes/coro_1/regenerate", headers=_hdr(admin_token),
                     json={"allow_youtube_drift": True})
    assert r2.status_code == 200


def test_regen_scene_happy_does_not_touch_edit_count(client, admin_token, db, monkeypatch):
    """El re-roll de escena NO consume el cupo de ediciones caras (_MAX_EDITS):
    tiene su propio cupo (SCENE_REROLL_MAX). edit_count queda igual; la respuesta
    trae `reroll_count`."""
    from database import Job as JobModel
    import main
    captured = {}
    monkeypatch.setattr(main, "enqueue_edit", lambda **k: captured.update(k) or "edit:fake")
    me = client.get("/auth/me", headers=_hdr(admin_token)).json()
    jid = _make_scene_job(db, me["tenant_id"], edit_count=0)
    r = client.post(f"/jobs/{jid}/scenes/coro_1/regenerate", headers=_hdr(admin_token),
                    json={"hint": "más oscuro", "movement_style": "sutil"})
    assert r.status_code == 200
    body = r.json()
    assert body["edit_count"] == 0          # NO toca el cupo de ediciones caras
    assert body["reroll_count"] == 1        # su propio contador
    assert captured["edit_params"]["scene_hint"] == "más oscuro"
    assert captured["edit_params"]["scene_movement"] == "sutil"
    db.expire_all()
    fresh = db.query(JobModel).filter(JobModel.job_id == jid).first()
    assert fresh.edit_count == 0 and fresh.status == "editing"


def _make_retry_job(db, tenant_id, user_id, *, enable_scenes=True):
    from database import Job as JobModel
    jid = _uuid.uuid4().hex[:12]
    job = JobModel(
        job_id=jid, tenant_id=tenant_id, user_id=user_id, artist="T", song_title="S",
        filename="t.mp3", status="error", delivery_profile="youtube", progress=0,
        segments_json=[{"start": 0.0, "end": 1.0, "text": "x"}],
        input_r2_key="in/x.mp3", render_params={"enable_scenes": enable_scenes},
    )
    db.add(job)
    db.commit()
    return jid


def test_retry_gates_enable_scenes_for_non_eligible(client, user_token, db, monkeypatch):
    """Audit A5: un user sin acceso reintenta un job multi-escena → enable_scenes
    se fuerza a False (no debe seguir generando multi-escena ni su costo).

    Modo rollback (SCENES_GLOBALLY_ENABLED=0): el gating por elegibilidad está
    activo. Con el flag en ON (default) Escenas es público y el costo lo
    gobiernan los créditos."""
    import main
    monkeypatch.setenv("SCENES_GLOBALLY_ENABLED", "0")
    captured = {}
    monkeypatch.setattr(main, "enqueue_pipeline", lambda **k: captured.update(k) or "thread:fake")
    me = client.get("/auth/me", headers=_hdr(user_token)).json()
    jid = _make_retry_job(db, me["tenant_id"], me["id"], enable_scenes=True)
    r = client.post(f"/retry/{jid}", headers=_hdr(user_token), json={})
    assert r.status_code == 200
    # El user común NO es elegible → enable_scenes gateado a False.
    assert captured.get("enable_scenes") is False


def test_retry_keeps_enable_scenes_for_admin(client, admin_token, db, monkeypatch):
    import main
    captured = {}
    monkeypatch.setattr(main, "enqueue_pipeline", lambda **k: captured.update(k) or "thread:fake")
    me = client.get("/auth/me", headers=_hdr(admin_token)).json()
    jid = _make_retry_job(db, me["tenant_id"], me["id"], enable_scenes=True)
    r = client.post(f"/retry/{jid}", headers=_hdr(admin_token), json={})
    assert r.status_code == 200
    # Admin es elegible → se mantiene.
    assert captured.get("enable_scenes") is True


def test_regen_scene_rollback_on_enqueue_failure(client, admin_token, db, monkeypatch):
    """Si el enqueue falla, el job revierte (status + edit_count) y devuelve 503."""
    from database import Job as JobModel
    import main

    def boom(**k):
        raise RuntimeError("redis caído")
    monkeypatch.setattr(main, "enqueue_edit", boom)
    me = client.get("/auth/me", headers=_hdr(admin_token)).json()
    jid = _make_scene_job(db, me["tenant_id"], edit_count=0)
    r = client.post(f"/jobs/{jid}/scenes/coro_1/regenerate", headers=_hdr(admin_token), json={})
    assert r.status_code == 503
    db.expire_all()
    fresh = db.query(JobModel).filter(JobModel.job_id == jid).first()
    # Revertido: no quedó en "editing" ni con el edit consumido.
    assert fresh.status == "pending_review"
    assert fresh.edit_count == 0


def test_scenes_thumbs_ownership_and_shape(client, admin_token, db):
    me = client.get("/auth/me", headers=_hdr(admin_token)).json()
    jid = _make_scene_job(db, me["tenant_id"])
    r = client.get(f"/jobs/{jid}/scenes/thumbs", headers=_hdr(admin_token))
    assert r.status_code == 200
    assert "thumbs" in r.json()  # vacío sin storage, pero la forma es correcta
    # job de otro tenant → 404
    other = _make_scene_job(db, "tenant_ajeno")
    assert client.get(f"/jobs/{other}/scenes/thumbs", headers=_hdr(admin_token)).status_code == 404


def test_jobs_list_includes_scene_plan(client, admin_token, db):
    """La lista /jobs DEBE traer scene_plan: JobDetail recibe el job desde la
    lista (prop) y sin scene_plan la tira de corrección de escenas nunca
    aparece (bug 2026-06-30 — to_list_dict lo omitía)."""
    me = client.get("/auth/me", headers=_hdr(admin_token)).json()
    jid = _make_scene_job(db, me["tenant_id"], user_id=me["id"])
    rows = client.get("/jobs", headers=_hdr(admin_token)).json()
    mine = [j for j in rows if j["job_id"] == jid]
    assert len(mine) == 1
    sp = mine[0].get("scene_plan")
    assert sp and sp.get("scenes"), "el job de escenas debe traer scene_plan.scenes en la lista"


def test_status_includes_scene_plan(client, admin_token, db):
    """Al abrir un video por URL/refresh, JobDetail toma el job de /status/{id}
    (no de la lista). Sin scene_plan acá, la tira de corrección NUNCA aparece por
    link (bug 2026-07-01; #780 solo cubrió el camino desde la lista)."""
    me = client.get("/auth/me", headers=_hdr(admin_token)).json()
    jid = _make_scene_job(db, me["tenant_id"], user_id=me["id"])
    r = client.get(f"/status/{jid}", headers=_hdr(admin_token))
    assert r.status_code == 200, r.text
    sp = r.json().get("scene_plan")
    assert sp and sp.get("scenes"), "/status debe traer scene_plan.scenes en un job de escenas"
