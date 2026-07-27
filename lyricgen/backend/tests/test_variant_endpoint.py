"""Tests del endpoint POST /jobs/{parent_job_id}/variant.

La variante crea un job nuevo (cuenta como video pago del plan) que hereda
audio + segments_json del padre y re-genera solo el background Veo. Mismo
billing y review flow que un upload nuevo, pero ahorra el costo de lyrics
fetch + transcribe.

Cobertura:
- Happy path: padre done crea variante con parent_job_id seteado
- Padre no-done → 400
- Padre inexistente → 404
- Padre de otro tenant → 404 (IDOR-safe vía filter)
- Padre sin segments_json (no debería existir pero defensivo) → 422
- Padre sin input_r2_key → 422
- background_hint llega a enqueue_pipeline kwargs
- concept override mergea con render_params del padre
- Variante de variante (chain de 2) permitida
- AuditLog entry creado con metadata correcta
- Contrato espejado con /edit (2026-07-24): cada eje del wizard (fondo,
  escena, FX, tipografía, portada) overridea y llega al render; None
  hereda del padre sin pisar; background_hint "" = clear explícito;
  background_id resuelve la Biblioteca con el resolver de /generate
"""
from __future__ import annotations

import os
import sys
import uuid
from datetime import datetime, timezone

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from database import Job, AuditLog


def _decode_user(client, token: str):
    return client.get("/auth/me", headers={"Authorization": f"Bearer {token}"}).json()


def _seed_done_job(
    db,
    *,
    owner_id: int,
    tenant_id: str,
    segments_json=None,
    input_r2_key: str = "inputs/synth/track.wav",
    render_params: dict | None = None,
    parent_job_id: str | None = None,
) -> str:
    jid = f"var_{uuid.uuid4().hex[:6]}"
    db.add(Job(
        job_id=jid,
        user_id=owner_id,
        tenant_id=tenant_id,
        artist="Test Artist",
        song_title="Test Song",
        filename="track.wav",
        style="oscuro",
        status="done",
        current_step="thumbnail",
        progress=100,
        delivery_profile="youtube",
        segments_json=segments_json or [
            {"start": 0.0, "end": 2.0, "text": "Line one"},
            {"start": 2.0, "end": 4.0, "text": "Line two"},
        ],
        input_r2_key=input_r2_key,
        bg_r2_key_cached="backgrounds/synth/bg.mp4",
        render_params=render_params,
        parent_job_id=parent_job_id,
        created_at=datetime.now(timezone.utc),
        completed_at=datetime.now(timezone.utc),
    ))
    db.commit()
    return jid


def _cleanup(db, prefix="var_"):
    jids = [j.job_id for j in db.query(Job).filter(Job.job_id.like(f"{prefix}%")).all()]
    if jids:
        from database import AIProvenance
        db.query(AIProvenance).filter(AIProvenance.job_id.in_(jids)).delete(synchronize_session=False)
        db.query(Job).filter(Job.job_id.in_(jids)).delete(synchronize_session=False)
    db.query(AuditLog).filter(AuditLog.action == "job.variant_created").delete(synchronize_session=False)
    db.commit()


@pytest.fixture(autouse=True)
def _auto_cleanup(db):
    _cleanup(db)
    yield
    _cleanup(db)


# ─── Happy path ─────────────────────────────────────────────────────

def test_variant_creates_new_job_with_parent_link(client, user_token, db, monkeypatch):
    """Variante exitosa: status=processing, parent_job_id seteado, hereda
    audio + segments_json, AuditLog creado."""
    me = _decode_user(client, user_token)
    parent_segments = [
        {"start": 0.0, "end": 2.5, "text": "Approved lyric line"},
    ]
    parent_id = _seed_done_job(
        db, owner_id=me["id"], tenant_id=me["tenant_id"],
        segments_json=parent_segments,
        input_r2_key="inputs/abc/track.wav",
    )

    captured = {}
    monkeypatch.setattr(
        "main.enqueue_pipeline",
        lambda **kw: captured.update(kw) or "fake_rq_id",
    )

    r = client.post(
        f"/jobs/{parent_id}/variant",
        json={"background_hint": "interior cálido al atardecer"},
        headers={"Authorization": f"Bearer {user_token}"},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["parent_job_id"] == parent_id
    assert body["status"] == "processing"
    new_id = body["job_id"]
    assert new_id != parent_id

    # Verify the DB row
    db.expire_all()
    new_job = db.query(Job).filter(Job.job_id == new_id).first()
    assert new_job is not None
    assert new_job.parent_job_id == parent_id
    assert new_job.input_r2_key == "inputs/abc/track.wav"
    assert new_job.segments_json == parent_segments
    assert new_job.status == "processing"
    assert new_job.current_step == "background"  # salta Whisper
    assert new_job.edit_count == 0  # arranca limpio

    # Verify enqueue_pipeline kwargs
    assert captured.get("segments_override") == parent_segments
    assert captured.get("input_r2_key") == "inputs/abc/track.wav"
    assert captured.get("background_hint") == "interior cálido al atardecer"

    # AuditLog
    log = db.query(AuditLog).filter(AuditLog.action == "job.variant_created").first()
    assert log is not None
    assert log.detail["parent_job_id"] == parent_id
    assert log.detail["new_job_id"] == new_id
    assert log.detail["background_hint"] == "interior cálido al atardecer"


def test_variant_without_overrides_inherits_everything(client, user_token, db, monkeypatch):
    """Empty body crea variante usando solo defaults del padre — el use
    case 'probar otra estética' donde el operador deja que Gemini elija
    libre con el system prompt desbiaseado del PR #116."""
    me = _decode_user(client, user_token)
    parent_id = _seed_done_job(
        db, owner_id=me["id"], tenant_id=me["tenant_id"],
        render_params={"font": "montserrat-bold", "text_case": "upper", "concept": "atardecer"},
    )

    captured = {}
    monkeypatch.setattr(
        "main.enqueue_pipeline",
        lambda **kw: captured.update(kw) or "fake_rq_id",
    )

    r = client.post(
        f"/jobs/{parent_id}/variant",
        json={},
        headers={"Authorization": f"Bearer {user_token}"},
    )
    assert r.status_code == 200
    # background_hint NO se pasa cuando el body no lo trae
    assert "background_hint" not in captured
    # concept se hereda del padre's render_params
    assert captured.get("concept") == "atardecer"
    # typography se hereda
    assert captured.get("font") == "montserrat-bold"
    assert captured.get("text_case") == "upper"


def test_variant_concept_override_replaces_parent(client, user_token, db, monkeypatch):
    """Si el body trae concept, ese pisa el del padre."""
    me = _decode_user(client, user_token)
    parent_id = _seed_done_job(
        db, owner_id=me["id"], tenant_id=me["tenant_id"],
        render_params={"concept": "neón urbano"},
    )

    captured = {}
    monkeypatch.setattr(
        "main.enqueue_pipeline",
        lambda **kw: captured.update(kw) or "fake_rq_id",
    )

    r = client.post(
        f"/jobs/{parent_id}/variant",
        json={"concept": "balada romántica acústica"},
        headers={"Authorization": f"Bearer {user_token}"},
    )
    assert r.status_code == 200
    assert captured.get("concept") == "balada romántica acústica"

    # Y en la DB, el render_params del nuevo job tiene el concept overrideado
    db.expire_all()
    new_job = db.query(Job).filter(Job.job_id == r.json()["job_id"]).first()
    assert new_job.render_params["concept"] == "balada romántica acústica"


def test_variant_style_override(client, user_token, db, monkeypatch):
    """Override de style preset."""
    me = _decode_user(client, user_token)
    parent_id = _seed_done_job(db, owner_id=me["id"], tenant_id=me["tenant_id"])
    # Verify parent style is "oscuro" default
    monkeypatch.setattr("main.enqueue_pipeline", lambda **kw: "fake")

    r = client.post(
        f"/jobs/{parent_id}/variant",
        json={"style": "neon"},
        headers={"Authorization": f"Bearer {user_token}"},
    )
    assert r.status_code == 200
    db.expire_all()
    new_job = db.query(Job).filter(Job.job_id == r.json()["job_id"]).first()
    assert new_job.style == "neon"


# ─── Validaciones ───────────────────────────────────────────────────

def test_parent_not_done_rejected(client, user_token, db, monkeypatch):
    """No se puede crear variante de un job que no terminó (puede estar
    processing, pending_review, error, etc). 400 con mensaje claro."""
    me = _decode_user(client, user_token)
    parent_id = _seed_done_job(db, owner_id=me["id"], tenant_id=me["tenant_id"])
    # Override the status to non-done
    db.query(Job).filter(Job.job_id == parent_id).update({"status": "pending_review"})
    db.commit()

    monkeypatch.setattr("main.enqueue_pipeline", lambda **kw: "fake")
    r = client.post(
        f"/jobs/{parent_id}/variant",
        json={},
        headers={"Authorization": f"Bearer {user_token}"},
    )
    assert r.status_code == 400
    assert "done" in r.json()["detail"].lower()


def test_parent_not_found(client, user_token, monkeypatch):
    monkeypatch.setattr("main.enqueue_pipeline", lambda **kw: "fake")
    r = client.post(
        "/jobs/nonexistent_id/variant",
        json={},
        headers={"Authorization": f"Bearer {user_token}"},
    )
    assert r.status_code == 404


def test_parent_no_segments_rejected(client, user_token, db, monkeypatch):
    """Padre done pero sin segments_json (caso defensivo — no debería
    pasar post-PR #106 pero igual lo guardamos). 422."""
    me = _decode_user(client, user_token)
    parent_id = _seed_done_job(
        db, owner_id=me["id"], tenant_id=me["tenant_id"],
        segments_json=None,
    )
    # Override after seed (the seed defaults to non-empty)
    db.query(Job).filter(Job.job_id == parent_id).update({"segments_json": None})
    db.commit()

    monkeypatch.setattr("main.enqueue_pipeline", lambda **kw: "fake")
    r = client.post(
        f"/jobs/{parent_id}/variant",
        json={},
        headers={"Authorization": f"Bearer {user_token}"},
    )
    assert r.status_code == 422


def test_parent_no_input_r2_key_rejected(client, user_token, db, monkeypatch):
    """Padre done pero el audio ya no está en R2 (cleanup viejo).
    No podemos crear variante — la pipeline necesita el audio. 422."""
    me = _decode_user(client, user_token)
    parent_id = _seed_done_job(
        db, owner_id=me["id"], tenant_id=me["tenant_id"],
        input_r2_key=None,
    )
    db.query(Job).filter(Job.job_id == parent_id).update({"input_r2_key": None})
    db.commit()

    monkeypatch.setattr("main.enqueue_pipeline", lambda **kw: "fake")
    r = client.post(
        f"/jobs/{parent_id}/variant",
        json={},
        headers={"Authorization": f"Bearer {user_token}"},
    )
    assert r.status_code == 422


# ─── Chains de variantes ────────────────────────────────────────────

def test_variant_of_variant_allowed(client, user_token, db, monkeypatch):
    """Una variante puede a su vez ser padre de otra variante.
    Permitido sin límite hoy — si vemos abuso, agregamos max_depth.
    El campo parent_job_id queda apuntando al hijo intermediario."""
    me = _decode_user(client, user_token)
    grand_parent_id = _seed_done_job(db, owner_id=me["id"], tenant_id=me["tenant_id"])

    monkeypatch.setattr("main.enqueue_pipeline", lambda **kw: "fake")

    # Variant 1
    r1 = client.post(
        f"/jobs/{grand_parent_id}/variant",
        json={"background_hint": "warm"},
        headers={"Authorization": f"Bearer {user_token}"},
    )
    assert r1.status_code == 200
    middle_id = r1.json()["job_id"]

    # Mark middle as done so it can be a parent
    db.query(Job).filter(Job.job_id == middle_id).update({"status": "done"})
    db.commit()

    # Variant 2 (of variant 1)
    r2 = client.post(
        f"/jobs/{middle_id}/variant",
        json={"background_hint": "cool"},
        headers={"Authorization": f"Bearer {user_token}"},
    )
    assert r2.status_code == 200
    grand_child_id = r2.json()["job_id"]

    db.expire_all()
    grand_child = db.query(Job).filter(Job.job_id == grand_child_id).first()
    assert grand_child.parent_job_id == middle_id


# ─── Aislamiento entre tenants (IDOR-safe) ──────────────────────────

def test_cannot_create_variant_of_other_tenant_job(client, db, monkeypatch):
    """Un user de tenant A no debe poder ver/usar jobs de tenant B
    aunque conozca el job_id. El filter por tenant_id en el query
    devuelve None → 404 (no leakea que el job existe)."""
    # Crear usuario B con su propio tenant
    res = client.post("/auth/register", json={
        "username": f"tenantB_{uuid.uuid4().hex[:6]}",
        "password": "testpass12345",
        "email": f"b_{uuid.uuid4().hex[:6]}@test.com",
    })
    tokenB = res.json()["token"]
    meB = _decode_user(client, tokenB)

    # Seed un job done en tenant B
    parent_id = _seed_done_job(db, owner_id=meB["id"], tenant_id=meB["tenant_id"])

    # User A intenta crear variante de B's job
    resA = client.post("/auth/register", json={
        "username": f"tenantA_{uuid.uuid4().hex[:6]}",
        "password": "testpass12345",
        "email": f"a_{uuid.uuid4().hex[:6]}@test.com",
    })
    tokenA = resA.json()["token"]

    monkeypatch.setattr("main.enqueue_pipeline", lambda **kw: "fake")
    r = client.post(
        f"/jobs/{parent_id}/variant",
        json={},
        headers={"Authorization": f"Bearer {tokenA}"},
    )
    # 404 — no leakeamos info de que el job existe en otro tenant
    assert r.status_code == 404


# ───────────────────────────────────────────────────────────────────
# Variant cap (PR feat/variant-cap, 2026-05-29)
# Each plan includes 3 renders of the same song (original + 2 variants).
# The 4th onward costs $0.90 USD passthrough (Veo background generation).
# Tests here lock that contract.
# ───────────────────────────────────────────────────────────────────


def _seed_done_job_for_song(db, *, owner_id, tenant_id, artist, song_title, parent_job_id=None):
    """Same as `_seed_done_job` but lets the test pin the song identity
    so we can build a multi-variant scenario for the same (artist, title).
    Otherwise identical fixture shape."""
    jid = f"var_{uuid.uuid4().hex[:6]}"
    db.add(Job(
        job_id=jid,
        user_id=owner_id,
        tenant_id=tenant_id,
        artist=artist,
        song_title=song_title,
        filename="track.wav",
        style="oscuro",
        status="done",
        current_step="thumbnail",
        progress=100,
        delivery_profile="youtube",
        segments_json=[{"start": 0.0, "end": 1.0, "text": "x"}],
        input_r2_key="inputs/synth/track.wav",
        bg_r2_key_cached="backgrounds/synth/bg.mp4",
        parent_job_id=parent_job_id,
        created_at=datetime.now(timezone.utc),
        completed_at=datetime.now(timezone.utc),
    ))
    db.commit()
    return jid


def test_variant_third_render_is_free(client, user_token, db, monkeypatch):
    """The 3rd render (original + 2 variants) is the LAST included one.
    No acknowledge_variant_overage required."""
    me = _decode_user(client, user_token)
    # Two existing renders of the same song (the "original" + one variant).
    parent_id = _seed_done_job_for_song(
        db, owner_id=me["id"], tenant_id=me["tenant_id"],
        artist="Los Abuelos de la Nada", song_title="Cosas Mías",
    )
    _seed_done_job_for_song(
        db, owner_id=me["id"], tenant_id=me["tenant_id"],
        artist="Los Abuelos de la Nada", song_title="Cosas Mías",
        parent_job_id=parent_id,
    )

    monkeypatch.setattr("main.enqueue_pipeline", lambda **kw: "fake")
    r = client.post(
        f"/jobs/{parent_id}/variant",
        json={},
        headers={"Authorization": f"Bearer {user_token}"},
    )
    # 3rd render total → still included, no extra cost, no ack required.
    assert r.status_code == 200, r.text


def test_variant_fourth_render_requires_acknowledgement(client, user_token, db, monkeypatch):
    """The 4th render of the same song must be acknowledged. Without the
    flag, the endpoint returns 402 with `variant_overage_unconfirmed` +
    a structured body the frontend can read to show a confirm modal."""
    me = _decode_user(client, user_token)
    parent_id = _seed_done_job_for_song(
        db, owner_id=me["id"], tenant_id=me["tenant_id"],
        artist="Bersuit Vergarabat", song_title="Vuelos",
    )
    # Two prior variants → with the parent that's 3 renders existing,
    # so a new variant would be the 4th.
    _seed_done_job_for_song(
        db, owner_id=me["id"], tenant_id=me["tenant_id"],
        artist="Bersuit Vergarabat", song_title="Vuelos",
        parent_job_id=parent_id,
    )
    _seed_done_job_for_song(
        db, owner_id=me["id"], tenant_id=me["tenant_id"],
        artist="Bersuit Vergarabat", song_title="Vuelos",
        parent_job_id=parent_id,
    )

    monkeypatch.setattr("main.enqueue_pipeline", lambda **kw: "fake")
    r = client.post(
        f"/jobs/{parent_id}/variant",
        json={},  # NOT acknowledged
        headers={"Authorization": f"Bearer {user_token}"},
    )
    assert r.status_code == 402, r.text
    body = r.json()
    # FastAPI wraps `detail` around our structured payload.
    detail = body["detail"]
    assert detail["code"] == "variant_overage_unconfirmed"
    assert detail["existing_renders"] == 3
    assert detail["included_per_song"] == 3
    assert detail["cost_extra_usd"] == 0.90
    assert "Bersuit Vergarabat" in detail.get("artist", "")
    assert detail.get("song_title") == "Vuelos"


def test_variant_fourth_render_proceeds_when_acknowledged(client, user_token, db, monkeypatch):
    """When `acknowledge_variant_overage: true`, the endpoint creates the
    variant AND writes an AuditLog row capturing the charge."""
    me = _decode_user(client, user_token)
    parent_id = _seed_done_job_for_song(
        db, owner_id=me["id"], tenant_id=me["tenant_id"],
        artist="Intoxicados", song_title="Don Electrón",
    )
    _seed_done_job_for_song(
        db, owner_id=me["id"], tenant_id=me["tenant_id"],
        artist="Intoxicados", song_title="Don Electrón",
        parent_job_id=parent_id,
    )
    _seed_done_job_for_song(
        db, owner_id=me["id"], tenant_id=me["tenant_id"],
        artist="Intoxicados", song_title="Don Electrón",
        parent_job_id=parent_id,
    )

    monkeypatch.setattr("main.enqueue_pipeline", lambda **kw: "fake")
    r = client.post(
        f"/jobs/{parent_id}/variant",
        json={"acknowledge_variant_overage": True},
        headers={"Authorization": f"Bearer {user_token}"},
    )
    assert r.status_code == 200, r.text

    # AuditLog row recorded the overage charge with the right metadata.
    db.expire_all()
    charge = (
        db.query(AuditLog)
        .filter(AuditLog.action == "variant.overage_charge")
        .order_by(AuditLog.id.desc())
        .first()
    )
    assert charge is not None
    assert charge.detail["parent_job_id"] == parent_id
    assert charge.detail["existing_renders_before_this_one"] == 3
    assert charge.detail["would_be_render_number"] == 4
    assert charge.detail["cost_usd"] == 0.90
    assert charge.detail["artist"] == "Intoxicados"


def test_variant_cap_is_per_song_not_per_tenant(client, user_token, db, monkeypatch):
    """A different song in the same tenant must have its OWN counter.
    Without this isolation, an operator who hit 3 variants on song A
    couldn't make a single variant of song B without acknowledging an
    overage that doesn't actually apply."""
    me = _decode_user(client, user_token)
    # 3 prior renders of song A — that song is at the cap.
    parent_a = _seed_done_job_for_song(
        db, owner_id=me["id"], tenant_id=me["tenant_id"],
        artist="Bersuit Vergarabat", song_title="El Baile De La Gambeta",
    )
    _seed_done_job_for_song(
        db, owner_id=me["id"], tenant_id=me["tenant_id"],
        artist="Bersuit Vergarabat", song_title="El Baile De La Gambeta",
        parent_job_id=parent_a,
    )
    _seed_done_job_for_song(
        db, owner_id=me["id"], tenant_id=me["tenant_id"],
        artist="Bersuit Vergarabat", song_title="El Baile De La Gambeta",
        parent_job_id=parent_a,
    )
    # Different song — only 1 render so far. First variant should be FREE.
    parent_b = _seed_done_job_for_song(
        db, owner_id=me["id"], tenant_id=me["tenant_id"],
        artist="Rata Blanca", song_title="Mujer Amante",
    )

    monkeypatch.setattr("main.enqueue_pipeline", lambda **kw: "fake")
    r = client.post(
        f"/jobs/{parent_b}/variant",
        json={},  # NOT acknowledged
        headers={"Authorization": f"Bearer {user_token}"},
    )
    assert r.status_code == 200, (
        f"first variant of song B was incorrectly blocked because song A "
        f"is at the cap (response: {r.text})"
    )


def test_variant_cap_case_insensitive_song_match(client, user_token, db, monkeypatch):
    """Casing/whitespace drift in artist or title still groups variants
    of the same song. `Cosas mías` and `COSAS MÍAS` are the same song,
    same as in get_plan_usage."""
    me = _decode_user(client, user_token)
    parent_id = _seed_done_job_for_song(
        db, owner_id=me["id"], tenant_id=me["tenant_id"],
        artist="los abuelos de la nada", song_title="cosas mias",  # lower
    )
    _seed_done_job_for_song(
        db, owner_id=me["id"], tenant_id=me["tenant_id"],
        artist="LOS ABUELOS DE LA NADA", song_title="COSAS MIAS",  # upper
        parent_job_id=parent_id,
    )
    _seed_done_job_for_song(
        db, owner_id=me["id"], tenant_id=me["tenant_id"],
        artist="  Los Abuelos De La Nada  ", song_title="Cosas Mias",  # mixed+padding
        parent_job_id=parent_id,
    )
    # 3 existing renders (all the "same song" after case-folding).
    # 4th must trigger the overage check.
    monkeypatch.setattr("main.enqueue_pipeline", lambda **kw: "fake")
    r = client.post(
        f"/jobs/{parent_id}/variant",
        json={},
        headers={"Authorization": f"Bearer {user_token}"},
    )
    assert r.status_code == 402, (
        f"casing drift broke variant cap dedup — 3 renders of same song "
        f"weren't recognised as siblings (response: {r.text})"
    )
    assert r.json()["detail"]["code"] == "variant_overage_unconfirmed"


# ───────────────────────────────────────────────────────────────────
# Contrato espejado con /edit (2026-07-24, "Crear variante abre el
# wizard completo"). El wizard de variante muestra fondo + tipografía +
# portada + Biblioteca; nada de eso puede mostrarse editable si el
# backend lo ignora (principio de honestidad del PR #977). Estos tests
# fijan que CADA eje viaja de punta a punta.
# ───────────────────────────────────────────────────────────────────

# Estado ABSOLUTO que manda el wizard (no un diff): un valor por cada eje
# overridable, todos distintos de los del padre para que un "no llegó" sea
# inequívoco.
_ALL_AXES_BODY = {
    "background_hint": "  catedral abandonada al amanecer  ",  # se trimea
    "concept": "ruinas y niebla",
    "genre": "post-rock",
    "match_lyrics": False,
    "bg_verbatim": True,
    "movement_style": "estatico",
    "effect": "snow",
    "lyrics_animation": "karaoke",
    "line_transition": "slide_up",
    "font": "bebas-neue",
    "font_scale": 1.25,
    "text_case": "title",
    "text_contrast": "strong",
    "frame_format": "cine",
    "custom_colors": "#101820,#F2AA4C",
    "title_template": "lower_third",
    "title_size": 1.4,
    "title_artist_font": "montserrat-bold",
    "title_song_font": "playfair",
    "title_song_break": "Donde Estan\nCorazón",
}

# Lo que esperamos ver en render_params Y en los kwargs de run_pipeline.
_ALL_AXES_EXPECTED = {**_ALL_AXES_BODY, "background_hint": "catedral abandonada al amanecer"}


def test_variant_overrides_every_wizard_axis(client, user_token, db, monkeypatch):
    """Cada campo nuevo del body pisa el del padre, se persiste en
    render_params y llega a los kwargs de run_pipeline. Si un eje se cae
    en el camino, el wizard estaría mostrando un control que el backend
    ignora."""
    me = _decode_user(client, user_token)
    parent_id = _seed_done_job(
        db, owner_id=me["id"], tenant_id=me["tenant_id"],
        render_params={
            "background_hint": "callejón neón",
            "concept": "ciudad",
            "genre": "trap",
            "match_lyrics": True,
            "bg_verbatim": False,
            "movement_style": "animado",
            "effect": "",
            "lyrics_animation": "none",
            "line_transition": "none",
            "font": "montserrat-bold",
            "font_scale": 1.0,
            "text_case": "upper",
            "text_contrast": "medium",
            "frame_format": "full",
            "custom_colors": "",
            "title_template": "auto",
            "title_size": 1.0,
            "title_artist_font": "",
            "title_song_font": "",
            "title_song_break": "",
        },
    )

    captured = {}
    monkeypatch.setattr(
        "main.enqueue_pipeline",
        lambda **kw: captured.update(kw) or "fake_rq_id",
    )

    r = client.post(
        f"/jobs/{parent_id}/variant",
        json=dict(_ALL_AXES_BODY),
        headers={"Authorization": f"Bearer {user_token}"},
    )
    assert r.status_code == 200, r.text

    db.expire_all()
    new_job = db.query(Job).filter(Job.job_id == r.json()["job_id"]).first()
    for field, expected in _ALL_AXES_EXPECTED.items():
        assert new_job.render_params[field] == expected, (
            f"render_params[{field!r}] no recibió el override del wizard"
        )
        assert captured.get(field) == expected, (
            f"{field!r} no llegó a run_pipeline — el control sería "
            f"editable-e-ignorado"
        )

    # El audit deja rastro de qué ejes pisó el operador.
    log = db.query(AuditLog).filter(AuditLog.action == "job.variant_created").first()
    assert set(log.detail["overridden_fields"]) == set(_ALL_AXES_BODY.keys())


def test_variant_none_inherits_parent_axes_without_clobber(client, user_token, db, monkeypatch):
    """None = heredar. Un body vacío no puede pisar con defaults ninguno
    de los ejes persistidos del padre (el BUG-5 que /edit ya cerró para
    bg_verbatim: un `bool` default False borraba un True persistido)."""
    me = _decode_user(client, user_token)
    parent_params = {
        "background_hint": "catedral gótica en penumbra",
        "concept": "ruinas",
        "genre": "post-rock",
        "match_lyrics": False,
        "bg_verbatim": True,
        "movement_style": "estatico",
        "effect": "rain",
        "lyrics_animation": "word_reveal",
        "line_transition": "wipe",
        "font": "bebas-neue",
        "font_scale": 1.2,
        "text_case": "title",
        "text_contrast": "strong",
        "frame_format": "cine",
        "custom_colors": "#101820",
        "title_template": "badge",
        "title_size": 1.3,
        "title_artist_font": "montserrat-bold",
        "title_song_font": "playfair",
        "title_song_break": "Una\nDos",
        # Sólo heredables (el wizard de variante no los expone): igual
        # tienen que sobrevivir al salto padre→variante.
        "lyric_color": "#FF0055",
        "lyric_sung_color": "#FFFFFF",
    }
    parent_id = _seed_done_job(
        db, owner_id=me["id"], tenant_id=me["tenant_id"],
        render_params=dict(parent_params),
    )

    captured = {}
    monkeypatch.setattr(
        "main.enqueue_pipeline",
        lambda **kw: captured.update(kw) or "fake_rq_id",
    )

    r = client.post(
        f"/jobs/{parent_id}/variant",
        json={},
        headers={"Authorization": f"Bearer {user_token}"},
    )
    assert r.status_code == 200, r.text

    db.expire_all()
    new_job = db.query(Job).filter(Job.job_id == r.json()["job_id"]).first()
    for field, expected in parent_params.items():
        assert new_job.render_params[field] == expected, (
            f"render_params[{field!r}] se perdió al heredar del padre"
        )
        assert captured.get(field) == expected, (
            f"{field!r} no llegó a run_pipeline al heredar del padre"
        )

    # Nada se marcó como overrideado: fue herencia pura.
    log = db.query(AuditLog).filter(AuditLog.action == "job.variant_created").first()
    assert log.detail["overridden_fields"] == []


def test_variant_background_hint_empty_string_is_explicit_clear(client, user_token, db, monkeypatch):
    """`background_hint: ""` = borrar el prompt del operador (mismo
    contrato que /edit). El "" se PERSISTE en render_params — así el
    prompt viejo no revive en un retry — y NO viaja a run_pipeline, que
    vuelve al flow default de Gemini."""
    me = _decode_user(client, user_token)
    parent_id = _seed_done_job(
        db, owner_id=me["id"], tenant_id=me["tenant_id"],
        render_params={"background_hint": "callejón con grafitis de noche"},
    )

    captured = {}
    monkeypatch.setattr(
        "main.enqueue_pipeline",
        lambda **kw: captured.update(kw) or "fake_rq_id",
    )

    r = client.post(
        f"/jobs/{parent_id}/variant",
        json={"background_hint": ""},
        headers={"Authorization": f"Bearer {user_token}"},
    )
    assert r.status_code == 200, r.text

    db.expire_all()
    new_job = db.query(Job).filter(Job.job_id == r.json()["job_id"]).first()
    assert new_job.render_params["background_hint"] == "", (
        "el clear explícito tiene que persistir como \"\" (no revivir el viejo)"
    )
    assert not captured.get("background_hint"), (
        "un hint vacío no puede viajar a run_pipeline como texto"
    )


def test_variant_inherits_parent_background_hint_when_not_sent(client, user_token, db, monkeypatch):
    """None ≠ "" — si el body no trae hint, el del padre se hereda Y se
    usa en el render. Antes se persistía en render_params pero NUNCA
    llegaba a run_pipeline: la row decía una cosa y el video mostraba
    otra."""
    me = _decode_user(client, user_token)
    parent_id = _seed_done_job(
        db, owner_id=me["id"], tenant_id=me["tenant_id"],
        render_params={"background_hint": "faro en la tormenta"},
    )

    captured = {}
    monkeypatch.setattr(
        "main.enqueue_pipeline",
        lambda **kw: captured.update(kw) or "fake_rq_id",
    )

    r = client.post(
        f"/jobs/{parent_id}/variant",
        json={},
        headers={"Authorization": f"Bearer {user_token}"},
    )
    assert r.status_code == 200, r.text
    assert captured.get("background_hint") == "faro en la tormenta"


# ─── Biblioteca de fondos ───────────────────────────────────────────

def test_variant_with_library_background_passes_bg_keys(client, user_token, db, monkeypatch):
    """`background_id` resuelve la Biblioteca con el MISMO resolver que
    /generate y pasa bg_path/bg_r2_key a enqueue_pipeline — ese camino
    reemplaza la generación IA."""
    me = _decode_user(client, user_token)
    parent_id = _seed_done_job(db, owner_id=me["id"], tenant_id=me["tenant_id"])

    resolver_calls = []

    def _fake_resolver(background_id, background_mode, current_user, db_, job_dir, job_id):
        resolver_calls.append({
            "background_id": background_id,
            "background_mode": background_mode,
            "job_id": job_id,
            "job_dir": job_dir,
        })
        return (f"{job_dir}/bg_library.mp4", "library/abc.mp4", None, None, background_id)

    monkeypatch.setattr("main._resolve_library_background", _fake_resolver)
    captured = {}
    monkeypatch.setattr(
        "main.enqueue_pipeline",
        lambda **kw: captured.update(kw) or "fake_rq_id",
    )

    r = client.post(
        f"/jobs/{parent_id}/variant",
        json={"background_id": 42, "background_mode": "as_is"},
        headers={"Authorization": f"Bearer {user_token}"},
    )
    assert r.status_code == 200, r.text
    new_id = r.json()["job_id"]

    assert len(resolver_calls) == 1
    call = resolver_calls[0]
    assert call["background_id"] == 42
    assert call["background_mode"] == "as_is"
    # El uso del asset se registra contra el job NUEVO, no el padre.
    assert call["job_id"] == new_id
    assert new_id in call["job_dir"]

    assert captured.get("background_path") == f"{call['job_dir']}/bg_library.mp4"
    assert captured.get("bg_r2_key") == "library/abc.mp4"
    assert captured.get("variation_parent_asset_id") == 42


def test_variant_library_variation_mode_forwards_variation_source(client, user_token, db, monkeypatch):
    """En modo `variation` el asset viaja como fuente image-to-video
    (variation_source_*) y bg_path queda None — mismo shape que
    /generate."""
    me = _decode_user(client, user_token)
    parent_id = _seed_done_job(db, owner_id=me["id"], tenant_id=me["tenant_id"])

    monkeypatch.setattr(
        "main._resolve_library_background",
        lambda bid, mode, u, d, job_dir, jid: (None, None, f"{job_dir}/src.mp4", "library/src.mp4", bid),
    )
    captured = {}
    monkeypatch.setattr(
        "main.enqueue_pipeline",
        lambda **kw: captured.update(kw) or "fake_rq_id",
    )

    r = client.post(
        f"/jobs/{parent_id}/variant",
        json={"background_id": 7, "background_mode": "variation"},
        headers={"Authorization": f"Bearer {user_token}"},
    )
    assert r.status_code == 200, r.text
    assert captured.get("background_path") is None
    assert captured.get("variation_source_r2_key") == "library/src.mp4"
    assert captured.get("variation_parent_asset_id") == 7


def test_variant_without_library_sends_no_background_path(client, user_token, db, monkeypatch):
    """Sin `background_id` el fondo se genera con IA: nada de bg_path ni
    variation_source_* (que harían saltear la generación)."""
    me = _decode_user(client, user_token)
    parent_id = _seed_done_job(db, owner_id=me["id"], tenant_id=me["tenant_id"])

    captured = {}
    monkeypatch.setattr(
        "main.enqueue_pipeline",
        lambda **kw: captured.update(kw) or "fake_rq_id",
    )

    r = client.post(
        f"/jobs/{parent_id}/variant",
        json={},
        headers={"Authorization": f"Bearer {user_token}"},
    )
    assert r.status_code == 200
    assert captured.get("background_path") is None
    assert captured.get("bg_r2_key") is None
    assert captured.get("variation_source_path") is None
    assert captured.get("variation_source_r2_key") is None


def test_variant_background_mode_rejects_engine_values(client, user_token, db, monkeypatch):
    """`background_mode` en /variant es el modo de BIBLIOTECA
    (as_is|variation), NO el motor Veo/Imagen de /edit. Mandar "veo"
    tiene que ser un 422, no un silencioso no-op: el motor lo deriva
    movement_style."""
    me = _decode_user(client, user_token)
    parent_id = _seed_done_job(db, owner_id=me["id"], tenant_id=me["tenant_id"])
    monkeypatch.setattr("main.enqueue_pipeline", lambda **kw: "fake")

    r = client.post(
        f"/jobs/{parent_id}/variant",
        json={"background_mode": "veo"},
        headers={"Authorization": f"Bearer {user_token}"},
    )
    assert r.status_code == 422


# ─── Art track ──────────────────────────────────────────────────────

def test_variant_of_art_track_rejected(client, user_token, db, monkeypatch):
    """Un art track no tiene fondo generado — "variante de art track" no
    aplica. 400 explícito en vez de heredar art_track=True y renderear
    un lyric vacío."""
    me = _decode_user(client, user_token)
    parent_id = _seed_done_job(
        db, owner_id=me["id"], tenant_id=me["tenant_id"],
        render_params={"art_track": True},
    )
    monkeypatch.setattr("main.enqueue_pipeline", lambda **kw: "fake")

    r = client.post(
        f"/jobs/{parent_id}/variant",
        json={},
        headers={"Authorization": f"Bearer {user_token}"},
    )
    assert r.status_code == 400
    assert "Art Track" in r.json()["detail"]


# ─── Guardrail del contrato ─────────────────────────────────────────

def test_every_overridable_field_exists_in_run_pipeline(client):
    """Cada campo overridable tiene que existir como kwarg de
    run_pipeline. Si alguien agrega uno que la firma no acepta, el
    enqueue explota en runtime (TypeError) en vez de fallar acá."""
    import inspect as _inspect

    import main
    import pipeline

    sig = _inspect.signature(pipeline.run_pipeline).parameters
    missing = [f for f in main._VARIANT_OVERRIDABLE_FIELDS if f not in sig]
    assert not missing, (
        f"campos overridables que run_pipeline no acepta: {missing}"
    )


def test_every_overridable_field_exists_in_request_model(client):
    """Y al revés: la tupla no puede nombrar un campo que el body no
    declara (sería un override muerto que nunca se puede mandar)."""
    import main

    fields = set(main.VariantJobRequest.model_fields)
    missing = [f for f in main._VARIANT_OVERRIDABLE_FIELDS if f not in fields]
    assert not missing, (
        f"campos en _VARIANT_OVERRIDABLE_FIELDS que VariantJobRequest no "
        f"declara: {missing}"
    )
