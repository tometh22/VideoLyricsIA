"""Endpoint POST /jobs/{id}/edit-art-track — editar un Art Track ya generado.

Los art tracks ("official audio": cover + waveform, sin letra) no pasan por el
wizard de letra, pero sí se pueden re-renderizar con otra portada / efecto /
título / línea legal. El endpoint mergea los ejes en render_params, actualiza
las columnas de título/artista y re-encola vía enqueue_pipeline(art_track=True)
—el mismo camino que /retry, gratis (no consume cuota)—.

Estos tests pinean el contrato: solo art tracks, solo estados editables, y el
enqueue lleva art_track=True + effect + label_line + el cover cacheado.
"""
from __future__ import annotations

import uuid
from unittest.mock import patch

import pytest


def _admin_identity(client, admin_token):
    me = client.get(
        "/auth/me", headers={"Authorization": f"Bearer {admin_token}"}
    ).json()
    return me["id"], me["tenant_id"]


def _seed_art_track_job(user_id, tenant_id, *, status="pending_review",
                        art_track=True, effect="", label_line=""):
    from database import SessionLocal, Job
    db = SessionLocal()
    try:
        job_id = uuid.uuid4().hex[:12]
        render_params = {}
        if art_track:
            render_params["art_track"] = True
        if effect:
            render_params["effect"] = effect
        if label_line:
            render_params["label_line"] = label_line
        db.add(Job(
            job_id=job_id,
            user_id=user_id,
            tenant_id=tenant_id,
            artist="Amanda Pujó",
            song_title="Hacia el Espacio",
            style="oscuro",
            filename="hacia_el_espacio.wav",
            status=status,
            current_step="done",
            progress=100,
            delivery_profile="youtube",
            input_r2_key=f"inputs/{tenant_id}/{job_id}/hacia_el_espacio.wav",
            bg_r2_key_cached=f"inputs/{tenant_id}/{job_id}/bg_custom.jpg",
            render_params=render_params,
        ))
        db.commit()
        return job_id
    finally:
        db.close()


def _read_job(job_id):
    from database import SessionLocal, Job
    db = SessionLocal()
    try:
        return db.query(Job).filter(Job.job_id == job_id).first()
    finally:
        db.close()


def test_edit_art_track_reenqueues_with_new_params(client, admin_token):
    """Happy path: cambia efecto + título + línea legal (sin cover nuevo) →
    200, status editing, y enqueue_pipeline recibe art_track=True + el nuevo
    effect/label_line + el cover cacheado."""
    uid, tid = _admin_identity(client, admin_token)
    job_id = _seed_art_track_job(uid, tid, effect="", label_line="")

    with patch("main.enqueue_pipeline") as enq:
        res = client.post(
            f"/jobs/{job_id}/edit-art-track",
            headers={"Authorization": f"Bearer {admin_token}"},
            data={
                "effect": "bokeh",
                "song_title": "Hacia el Espacio (Remaster)",
                "artist": "Amanda Pujó",
                "label_line": "℗ 2026 Universal Music Chile",
            },
        )

    assert res.status_code == 200, res.text
    body = res.json()
    assert body["status"] == "editing"
    assert body["cover_replaced"] is False

    # enqueue con los kwargs correctos
    assert enq.called
    kwargs = enq.call_args.kwargs
    assert kwargs["art_track"] is True
    assert kwargs["effect"] == "bokeh"
    assert kwargs["label_line"] == "℗ 2026 Universal Music Chile"
    # reusa el cover cacheado (no se subió uno nuevo)
    assert kwargs["bg_r2_key"].endswith("bg_custom.jpg")

    # persistencia: render_params + columnas + status
    row = _read_job(job_id)
    assert row.status == "editing"
    assert row.render_params["effect"] == "bokeh"
    assert row.render_params["label_line"] == "℗ 2026 Universal Music Chile"
    assert row.render_params["art_track"] is True
    assert row.song_title == "Hacia el Espacio (Remaster)"


def test_edit_art_track_clears_effect_and_label(client, admin_token):
    """Valores vacíos = limpiar: un art track con efecto/línea legal se puede
    dejar sin efecto y sin línea legal enviando strings vacíos."""
    uid, tid = _admin_identity(client, admin_token)
    job_id = _seed_art_track_job(
        uid, tid, effect="snow", label_line="℗ 2025 Sello")

    with patch("main.enqueue_pipeline") as enq:
        res = client.post(
            f"/jobs/{job_id}/edit-art-track",
            headers={"Authorization": f"Bearer {admin_token}"},
            data={"effect": "", "label_line": ""},
        )

    assert res.status_code == 200, res.text
    kwargs = enq.call_args.kwargs
    assert kwargs["effect"] == ""
    assert kwargs["label_line"] == ""
    row = _read_job(job_id)
    assert row.render_params["effect"] == ""
    assert row.render_params["label_line"] == ""


def test_edit_rejects_non_art_track(client, admin_token):
    """Un job de lyric video normal no se puede editar por este endpoint
    (se re-rendería sin letra)."""
    uid, tid = _admin_identity(client, admin_token)
    job_id = _seed_art_track_job(uid, tid, art_track=False)

    with patch("main.enqueue_pipeline") as enq:
        res = client.post(
            f"/jobs/{job_id}/edit-art-track",
            headers={"Authorization": f"Bearer {admin_token}"},
            data={"effect": "bokeh"},
        )

    assert res.status_code == 400
    assert not enq.called


def test_edit_rejects_while_processing(client, admin_token):
    """Solo estados editables (pending_review/done/rejected). Mientras procesa
    no se edita (para eso está /retry si falló)."""
    uid, tid = _admin_identity(client, admin_token)
    job_id = _seed_art_track_job(uid, tid, status="processing")

    with patch("main.enqueue_pipeline") as enq:
        res = client.post(
            f"/jobs/{job_id}/edit-art-track",
            headers={"Authorization": f"Bearer {admin_token}"},
            data={"effect": "bokeh"},
        )

    assert res.status_code == 400
    assert not enq.called


def test_edit_art_track_404_other_tenant(client, admin_token):
    """No se puede editar un job de otro tenant (el endpoint filtra por tenant,
    sin bypass de admin). user_id válido (FK a users) pero tenant ajeno."""
    uid, _tid = _admin_identity(client, admin_token)
    job_id = _seed_art_track_job(uid, "some-other-tenant")

    with patch("main.enqueue_pipeline") as enq:
        res = client.post(
            f"/jobs/{job_id}/edit-art-track",
            headers={"Authorization": f"Bearer {admin_token}"},
            data={"effect": "bokeh"},
        )

    assert res.status_code == 404
    assert not enq.called
