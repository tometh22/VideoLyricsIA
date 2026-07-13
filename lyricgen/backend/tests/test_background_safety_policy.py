"""Regression tests for the no-implicit-people background policy."""

import uuid
import inspect

from database import Job, User
import pipeline
from main import _merge_content_validation_choice


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
    assert pipeline._compute_allow_people(
        job_id, "Una mujer sentada junto a una ventana"
    ) is True


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


def test_human_subject_with_unrelated_negative_constraints_still_opts_in(db):
    job_id = _job(db, render_params={"bypass_content_validation": True})
    assert pipeline._compute_allow_people(
        job_id, "A woman by the window, no text and no logos"
    ) is True
    assert pipeline._compute_allow_people(
        job_id, "Una mujer sentada, sin texto ni marcas"
    ) is True
    assert pipeline._compute_allow_people(
        job_id, "Uma mulher na janela, sem texto ou logotipos"
    ) is True


def test_subject_first_exclusion_still_fails_closed(db):
    job_id = _job(db, render_params={"bypass_content_validation": True})
    assert pipeline._compute_allow_people(job_id, "A woman must not appear") is False
    assert pipeline._compute_allow_people(job_id, "Una mujer no debe aparecer") is False
    assert pipeline._compute_allow_people(job_id, "Uma mulher não deve aparecer") is False


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


def test_missing_job_fails_closed():
    policy = pipeline._background_safety_policy("doesnotexist", "a woman")
    assert policy["is_umg"] is True
    assert policy["allow_people"] is False
    assert policy["should_validate"] is True


def test_all_delivery_profiles_and_edit_paths_are_wired_to_validation():
    initial_source = inspect.getsource(pipeline.run_pipeline)
    edit_source = inspect.getsource(pipeline.run_edit_pipeline)
    scene_source = inspect.getsource(pipeline._generate_scene_clips)
    assert "if wants_youtube or wants_umg" in initial_source
    assert "_validate_background_asset_for_job" in edit_source
    assert "_validate_scene_video" in scene_source


def test_policy_choice_default_is_safe_and_preserves_unrelated_params():
    merged = _merge_content_validation_choice(
        {"style": "neon", "bypass_content_validation": True},
    )
    assert merged == {"style": "neon", "force_content_validation": True}


def test_policy_choice_force_wins_when_legacy_client_sends_both():
    merged = _merge_content_validation_choice(
        {"bypass_content_validation": True}, bypass=True, force=True,
    )
    assert merged["force_content_validation"] is True
    assert "bypass_content_validation" not in merged


def test_policy_choice_explicit_bypass_clears_stale_force():
    merged = _merge_content_validation_choice(
        {"force_content_validation": True}, bypass=True,
    )
    assert merged["bypass_content_validation"] is True
    assert "force_content_validation" not in merged


def test_all_policy_write_paths_use_the_single_merge_helper():
    import main

    for handler in (main.request_edit, main.retry_job, main.create_variant):
        assert "_merge_content_validation_choice(" in inspect.getsource(handler)
