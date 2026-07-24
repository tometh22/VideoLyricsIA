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
