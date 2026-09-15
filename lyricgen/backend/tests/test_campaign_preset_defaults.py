"""Preset de la campaña (2026-09-14): GET /creative expone defaults y
POST /creative/defaults MERGEA sin tocar review_queue/stage1_pipeline."""
import pytest


def _campaign(client, admin_token, monkeypatch):
    monkeypatch.setenv("BATCH_CAMPAIGN_ENABLED", "1")
    auth = {"Authorization": f"Bearer {admin_token}"}
    created = client.post("/batch/campaigns", headers=auth, json={
        "name": "Preset fixture", "kind": "lyric_video", "destination_portal": "chile",
    })
    assert created.status_code in (200, 201), created.text
    cid = created.json()["id"]
    from database import BatchCampaign, SessionLocal
    db = SessionLocal()
    try:
        row = db.query(BatchCampaign).filter_by(id=cid).one()
        row.default_render_params = {**(row.default_render_params or {}),
                                     "review_queue": {"calibration_target": 50},
                                     "stage1_pipeline": {"promotion_limit": 39}}
        db.commit()
    finally:
        db.close()
    return cid, auth


def _defaults_row(cid):
    from database import BatchCampaign, SessionLocal
    db = SessionLocal()
    try:
        return dict(db.query(BatchCampaign).filter_by(id=cid).one().default_render_params or {})
    finally:
        db.close()


def test_get_exposes_defaults_and_post_merges_preserving_pipeline_keys(client, admin_token, monkeypatch):
    cid, auth = _campaign(client, admin_token, monkeypatch)
    head = client.get(f"/batch/campaigns/{cid}/creative", headers=auth).json()
    assert head["defaults"]["font"] == "" and head["defaults"]["text_case"] == "upper"
    assert head["defaults_explicit"] == {}

    res = client.post(f"/batch/campaigns/{cid}/creative/defaults", headers=auth, json={
        "revision": 0, "reason": "preset chile",
        "settings": {"font": "poppins-bold", "font_scale": 1.3, "movement_style": "estandar", "line_transition": "dissolve_blur"},
    })
    assert res.status_code == 200, res.text
    assert res.json()["revision"] == 1
    assert res.json()["defaults"]["font"] == "poppins-bold"
    row = _defaults_row(cid)
    assert row["review_queue"] == {"calibration_target": 50}
    assert row["stage1_pipeline"] == {"promotion_limit": 39}
    assert row["font"] == "poppins-bold" and row["movement_style"] == "estandar"
    assert row["creative_plan"]["revision"] == 1

    head = client.get(f"/batch/campaigns/{cid}/creative", headers=auth).json()
    assert head["defaults_explicit"]["line_transition"] == "dissolve_blur"
    assert all(i["settings"]["font"] == "poppins-bold" for i in head["items"]) or head["items"] == []

    # null borra la clave (vuelve al default); las otras quedan.
    res = client.post(f"/batch/campaigns/{cid}/creative/defaults", headers=auth, json={
        "revision": 1, "reason": "quitar fuente", "settings": {"font": None},
    })
    assert res.status_code == 200, res.text
    row = _defaults_row(cid)
    assert "font" not in row and row["movement_style"] == "estandar" and row["review_queue"]
    assert res.json()["defaults"]["font"] == ""


def test_post_defaults_rejects_stale_revision_and_bad_option(client, admin_token, monkeypatch):
    cid, auth = _campaign(client, admin_token, monkeypatch)
    stale = client.post(f"/batch/campaigns/{cid}/creative/defaults", headers=auth, json={
        "revision": 7, "reason": "x y z", "settings": {"font": "poppins-bold"},
    })
    assert stale.status_code == 409
    bad = client.post(f"/batch/campaigns/{cid}/creative/defaults", headers=auth, json={
        "revision": 0, "reason": "x y z", "settings": {"font": "comic-sans"},
    })
    assert bad.status_code == 422
    unknown = client.post(f"/batch/campaigns/{cid}/creative/defaults", headers=auth, json={
        "revision": 0, "reason": "x y z", "settings": {"review_queue": None},
    })
    assert unknown.status_code == 422
    assert "font" not in _defaults_row(cid)


def test_post_defaults_unchanged_is_idempotent(client, admin_token, monkeypatch):
    cid, auth = _campaign(client, admin_token, monkeypatch)
    first = client.post(f"/batch/campaigns/{cid}/creative/defaults", headers=auth, json={
        "revision": 0, "reason": "preset", "settings": {"text_case": "lower"}})
    assert first.status_code == 200 and first.json()["revision"] == 1
    again = client.post(f"/batch/campaigns/{cid}/creative/defaults", headers=auth, json={
        "revision": 1, "reason": "preset", "settings": {"text_case": "lower"}})
    assert again.status_code == 200 and again.json().get("unchanged") is True and again.json()["revision"] == 1
