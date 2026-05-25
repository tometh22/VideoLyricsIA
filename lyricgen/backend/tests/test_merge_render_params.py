"""U5 (audit Sprint 2 2026-05-25) — merge_render_params helper tests.

Resuelve race entre worker pipeline (pipeline.py:703) y /edit endpoint:
ambos leían render_params (dict JSONB), mutaban local, escribían back.
Sin row lock + atomic merge, last-writer-wins.

merge_render_params hace SELECT FOR UPDATE + read + write en UNA tx.
"""

import pytest

from database import Job, User
from jobs import merge_render_params, get_job_model


def _seed_job(db, *, job_id: str = "rpmerge12345", initial: dict | None = None) -> str:
    u = User(
        username=f"u-{job_id}",
        email=f"{job_id}@test.com",
        hashed_password="x",
        tenant_id="t",
        role="operator",
    )
    db.add(u)
    db.commit()
    j = Job(
        job_id=job_id, user_id=u.id, tenant_id="t",
        artist="x", filename="x.mp3", style="auto",
        status="pending_review",
        render_params=dict(initial or {}),
    )
    db.add(j)
    db.commit()
    return job_id


def test_merge_preserves_existing_keys_and_adds_new(db):
    """Caso happy: merge no pisa keys que no estaban en new_params."""
    jid = _seed_job(db, initial={"background_hint": "ocean", "font": "Arial"})
    ok = merge_render_params(jid, {"effect": "stars", "bg_verbatim": True})
    assert ok is True

    fresh = get_job_model(db, jid)
    db.refresh(fresh)
    rp = fresh.render_params
    assert rp["background_hint"] == "ocean", "lost existing background_hint"
    assert rp["font"] == "Arial", "lost existing font"
    assert rp["effect"] == "stars", "new effect not merged"
    assert rp["bg_verbatim"] is True, "new bg_verbatim not merged"


def test_merge_overrides_existing_key(db):
    """Si new_params trae key existente, override (no append/concat)."""
    jid = _seed_job(db, job_id="rpoverride12", initial={"font": "Arial"})
    merge_render_params(jid, {"font": "Helvetica"})
    fresh = get_job_model(db, jid)
    db.refresh(fresh)
    assert fresh.render_params["font"] == "Helvetica"


def test_merge_none_values_skipped(db):
    """Keys con value=None NO se mergean — permite caller usar dict con
    keys opcionales sin pisar valores existentes con None."""
    jid = _seed_job(db, job_id="rpnone1234567", initial={"concept": "vintage"})
    merge_render_params(jid, {"concept": None, "genre": "rock"})
    fresh = get_job_model(db, jid)
    db.refresh(fresh)
    assert fresh.render_params["concept"] == "vintage", "None should NOT override"
    assert fresh.render_params["genre"] == "rock", "non-None value should merge"


def test_merge_empty_dict_is_noop(db):
    """Empty dict → no-op, no rompe, retorna True."""
    jid = _seed_job(db, job_id="rpempty123456", initial={"font": "Arial"})
    ok = merge_render_params(jid, {})
    assert ok is True
    fresh = get_job_model(db, jid)
    db.refresh(fresh)
    assert fresh.render_params == {"font": "Arial"}


def test_merge_missing_job_returns_false(db):
    """Job inexistente → False (no raise)."""
    ok = merge_render_params("nonexistxxxx", {"font": "Arial"})
    assert ok is False


def test_merge_null_initial_render_params(db):
    """Job con render_params=None (column NULL) — merge crea el dict."""
    jid = "rpnullinit12"
    u = User(
        username=f"u-{jid}", email=f"{jid}@test.com",
        hashed_password="x", tenant_id="t", role="operator",
    )
    db.add(u)
    db.commit()
    j = Job(
        job_id=jid, user_id=u.id, tenant_id="t",
        artist="x", filename="x.mp3", style="auto",
        status="pending_review",
        render_params=None,  # NULL en DB
    )
    db.add(j)
    db.commit()

    merge_render_params(jid, {"effect": "snow"})
    fresh = get_job_model(db, jid)
    db.refresh(fresh)
    assert fresh.render_params == {"effect": "snow"}
