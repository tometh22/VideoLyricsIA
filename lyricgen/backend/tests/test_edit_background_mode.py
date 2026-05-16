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
    assert res.status_code == 200, res.text
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
    assert res.status_code == 200, res.text
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
    assert res.status_code == 200, res.text
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
    assert res.status_code == 200, res.text
    edit_params = captured[0]["edit_params"]
    assert "background_mode" not in edit_params, (
        "background_mode should be ignored for non-background edit_types"
    )
    # And typography params still propagate
    assert edit_params.get("font") == "bebas-neue"
