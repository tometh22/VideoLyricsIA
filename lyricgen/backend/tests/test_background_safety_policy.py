"""Regression tests for the no-implicit-people background policy."""

import uuid
import inspect

from database import Job, User
import pipeline


def _job(db, *, tenant="genly", billing_group=None, render_params=None):
    suffix = uuid.uuid4().hex[:10]
    user = User(
        username=f"safety_{suffix}", email=f"safety_{suffix}@example.com",
        hashed_password="x", tenant_id=tenant,
        billing_group=billing_group, is_active=True,
    )
    db.add(user)
    db.flush()
    job = Job(
        job_id=suffix[:12], user_id=user.id, tenant_id=tenant,
        artist="Safety", song_title="Policy", filename="policy.mp3",
        status="processing", delivery_profile="youtube",
        render_params=render_params or {},
    )
    db.add(job)
    db.commit()
    return job.job_id


def test_genly_auto_prompt_never_allows_people(db):
    job_id = _job(db)
    policy = pipeline._background_safety_policy(job_id)
    assert policy["is_umg"] is False
    assert policy["allow_people"] is False


def test_non_umg_explicit_operator_request_allows_people(db):
    job_id = _job(db, render_params={"bypass_content_validation": True})
    prompt = "Una mujer sentada junto a una ventana"
    assert pipeline._compute_allow_people(job_id, prompt) is True
    policy = pipeline._background_safety_policy(job_id, prompt)
    assert policy["validate_people"] is False
    assert policy["validate_brand"] is True
    assert policy["should_validate"] is True


def test_negative_operator_prompt_does_not_opt_in(db):
    job_id = _job(db, render_params={"bypass_content_validation": True})
    assert pipeline._compute_allow_people(job_id, "Bedroom at dawn, no people") is False
    assert pipeline._compute_allow_people(job_id, "Dormitorio sin personas") is False
    assert pipeline._compute_allow_people(job_id, "No quiero gente en la escena") is False
    assert pipeline._compute_allow_people(job_id, "Evitar personas y manos") is False
    assert pipeline._compute_allow_people(job_id, "Não mostrar pessoas") is False
    assert pipeline._compute_allow_people(job_id, "Escena libre de personas") is False
    assert pipeline._compute_allow_people(job_id, "People-free bedroom") is False
    assert pipeline._compute_allow_people(job_id, "Devoid of people") is False
    assert pipeline._compute_allow_people(job_id, "Cero personas") is False
    assert pipeline._compute_allow_people(job_id, "Una mujer no debe aparecer") is False
    assert pipeline._compute_allow_people(job_id, "Personas: ninguna") is False


def test_human_opt_in_requires_visual_intent_not_an_ambiguous_name(db):
    job_id = _job(db, render_params={"bypass_content_validation": True})
    for prompt in (
        "Man Ray inspired empty room",
        "persona non grata neon sign",
        "Men at Work inspired warehouse",
        "Talking Heads inspired empty room",
        "Faces album cover palette",
        "Girls' Generation inspired stage",
        "human rights mural",
        "clock hands at midnight",
        "arms of a chair",
        "face of a clock",
        "a clock with hands moving",
        "a chair with arms in an empty room",
        "a clock with a face glowing",
        "an armchair with arms",
        "a watch with hands at midnight",
        "una silla con brazos en una habitación vacía",
        "un reloj con manos moviéndose",
        "um relógio com mãos em movimento",
        "a clock featuring hands moving",
        "show a clock's hands moving",
        "depict a chair's arms in close-up",
        "a chair featuring arms",
        "mostrar un reloj cuyas manos se mueven",
        "mostrar los brazos de una silla",
        "a watch displaying its face",
    ):
        assert pipeline._compute_allow_people(job_id, prompt) is False


def test_explicit_faces_hands_and_contrast_are_supported_for_non_umg(db):
    job_id = _job(db, render_params={"bypass_content_validation": True})
    assert pipeline._compute_allow_people(job_id, "hands playing guitar") is True
    assert pipeline._compute_allow_people(job_id, "hands clapping") is True
    assert pipeline._compute_allow_people(job_id, "close-up face") is True
    assert pipeline._compute_allow_people(job_id, "portrait of hands") is True
    assert pipeline._compute_allow_people(job_id, "a human face glowing") is True
    assert pipeline._compute_allow_people(job_id, "a woman with hands waving") is True
    assert pipeline._compute_allow_people(
        job_id, "no people, but a woman singing"
    ) is True
    assert pipeline._compute_allow_people(
        job_id, "No logos, a woman singing with no readable text"
    ) is True
    assert pipeline._compute_allow_people(job_id, "A free woman running") is True
    assert pipeline._compute_allow_people(job_id, "No people, a woman singing") is False
    assert pipeline._compute_allow_people(job_id, "a woman without readable text") is True
    assert pipeline._compute_allow_people(job_id, "a woman, no logos") is True
    assert pipeline._compute_allow_people(job_id, "a woman and no brands") is True
    assert pipeline._compute_allow_people(job_id, "no people, only a woman singing") is True


def test_prompt_alone_is_not_enough_without_free_background_opt_in(db):
    job_id = _job(db)
    policy = pipeline._background_safety_policy(job_id, "A woman by the window")
    assert policy["allow_people"] is False
    assert policy["should_validate"] is True


def test_force_wins_over_stale_bypass(db):
    job_id = _job(
        db,
        render_params={"bypass_content_validation": True, "force_content_validation": True},
    )
    policy = pipeline._background_safety_policy(job_id, "A woman by the window")
    assert policy["allow_people"] is False
    assert policy["should_validate"] is True


def test_universal_tenant_ignores_explicit_people_and_bypass(db):
    job_id = _job(
        db, tenant="universal_argentina",
        render_params={"bypass_content_validation": True},
    )
    policy = pipeline._background_safety_policy(job_id, "A singer facing camera")
    assert policy["is_umg"] is True
    assert policy["allow_people"] is False
    assert policy["should_validate"] is True


def test_universal_billing_group_is_authoritative(db):
    job_id = _job(
        db, tenant="country_team", billing_group=" Universal_Music ",
        render_params={"bypass_content_validation": True},
    )
    policy = pipeline._background_safety_policy(job_id, "crowd dancing")
    assert policy["is_umg"] is True
    assert policy["allow_people"] is False
    assert policy["should_validate"] is True


def test_future_universal_country_tenant_is_protected(db):
    job_id = _job(
        db, tenant=" Universal_Mexico ",
        render_params={"bypass_content_validation": True},
    )
    policy = pipeline._background_safety_policy(job_id, "A singer on stage")
    assert policy["is_umg"] is True
    assert policy["allow_people"] is False
    assert policy["should_validate"] is True


def test_universal_exact_and_hyphenated_tenants_are_protected(db):
    for tenant in ("universal", "universal-mexico"):
        job_id = _job(
            db,
            tenant=tenant,
            render_params={"bypass_content_validation": True},
        )
        policy = pipeline._background_safety_policy(job_id, "A singer on stage")
        assert policy["is_umg"] is True
        assert policy["allow_people"] is False


def test_missing_job_fails_closed():
    policy = pipeline._background_safety_policy("doesnotexist", "a woman")
    assert policy["is_umg"] is True
    assert policy["allow_people"] is False
    assert policy["should_validate"] is True


def test_scene_validation_does_not_trust_stored_people_allow_after_umg_change(db):
    job_id = _job(
        db,
        tenant="universal_argentina",
        render_params={"bypass_content_validation": True},
    )
    plan = {
        "scenes": [{
            "recurrence_key": "verse_1",
            "operator_prompt": "A singer facing camera",
            "allow_people": True,
            "atmospherics_policy": {
                "policy_version": "background-v4",
                "policy_mode": "off",
                "allow_atmospherics": False,
                "explicit_atmospherics": [],
                "authorization_source": "default_deny",
            },
            "validation": {
                "passed": True,
                "policy_fingerprint": "background-v4:off:deny|people:allow",
            },
        }]
    }

    assert pipeline._scene_plan_has_current_clip_validation(
        plan, job_id=job_id
    ) is False


def test_all_delivery_profiles_and_edit_paths_are_wired_to_validation():
    initial_source = inspect.getsource(pipeline.run_pipeline)
    edit_source = inspect.getsource(pipeline.run_edit_pipeline)
    scene_source = inspect.getsource(pipeline._generate_scene_clips)
    assert "if wants_youtube or wants_umg" in initial_source
    assert "_validate_background_asset_for_job" in edit_source
    assert "_validate_scene_video" in scene_source
