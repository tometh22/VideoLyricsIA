"""Regression tests for the no-implicit-people background policy."""

import uuid
import inspect

import pytest

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
    assert pipeline._compute_allow_people(job_id, "a singer must not appear") is False
    assert pipeline._compute_allow_people(job_id, "la cantante no debe aparecer") is False
    assert pipeline._compute_allow_people(job_id, "a cantora não deve aparecer") is False


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
    assert policy["requires_secondary_validator"] is True


def test_common_account_does_not_pay_for_universal_secondary_detector(db):
    job_id = _job(db)
    assert pipeline._background_safety_policy(job_id)[
        "requires_secondary_validator"
    ] is False


def test_universal_rewrite_removes_artist_but_preserves_creative_intent(
    db, monkeypatch,
):
    import provenance

    job_id = _job(db, tenant="universal_argentina")
    original = (
        "Safety canta en un estudio de grabación de Buenos Aires de 1996, "
        "con consola analógica, luces azules, cámara fija, lluvia en las "
        "ventanas y movimiento ambiental lento"
    )
    rewritten = (
        "Estudio de grabación vacío de Buenos Aires de 1996, con consola "
        "analógica, luces azules, cámara fija, lluvia en las ventanas y "
        "movimiento ambiental lento, sin personas ni rostros"
    )

    class _Recorder:
        def finish(self, **_kwargs):
            return None

    class _Models:
        @staticmethod
        def generate_content(**_kwargs):
            return type("Response", (), {"text": rewritten})()

    fake_client = type("Client", (), {"models": _Models()})()
    monkeypatch.setattr(provenance, "record_ai_call", lambda **_kwargs: _Recorder())
    monkeypatch.setattr(pipeline, "_get_genai_client", lambda: fake_client)

    safe = pipeline._sanitize_people_at_provider_boundary(
        original,
        allow_people=False,
        job_id=job_id,
    )

    assert safe == rewritten
    assert "Safety" not in safe
    for detail in ("Buenos Aires", "1996", "consola", "luces azules", "cámara fija"):
        assert detail in safe


def test_static_camera_constraint_survives_safety_rewrite_failure(db, monkeypatch):
    job_id = _job(db, tenant="universal_argentina")
    monkeypatch.setattr(
        pipeline,
        "_get_genai_client",
        lambda: (_ for _ in ()).throw(RuntimeError("rewrite unavailable")),
    )

    safe = pipeline._sanitize_people_at_provider_boundary(
        "Una cantante en estudio, cámara fija y sin paneos",
        allow_people=False,
        job_id=job_id,
    )

    assert "locked-off fixed tripod camera" in safe
    assert "no pan" in safe


def test_restricted_provider_boundary_replaces_positive_human_subject():
    original = "A singer facing camera with a crowd behind her"
    safe = pipeline._sanitize_people_at_provider_boundary(
        original,
        allow_people=False,
    )
    assert safe != original
    assert "singer" not in safe.lower()
    assert "crowd" not in safe.lower()
    assert "unoccupied" in safe.lower()


def test_authorized_common_prompt_is_preserved_at_provider_boundary():
    original = "A singer facing camera"
    assert pipeline._sanitize_people_at_provider_boundary(
        original,
        allow_people=True,
    ) == original


def test_restricted_provider_boundary_uses_high_recall_human_detector():
    prompts = (
        "a monk meditating beside a river",
        "a nurse walking through a corridor",
        "a firefighter in a red-lit warehouse",
        "a chef and waitress at night",
        "the protagonist watches the sunrise",
        "a queen inside a glass palace",
        "she walks through the rain",
        "ella bailando bajo luces azules",
        "eles caminhando na praia",
        "a plain silhouette dancing",
        "a humanlike creature in the forest",
        "no people foreground, include a distant crowd",
        "without people nearby, a singer",
        "no crowd except one singer",
        "no people other than a guitarist",
        "sin personas cerca, mostrar una fotógrafa",
        "sem pessoas próximas, incluir um surfista",
        "no faces visible, silhouettes dancing",
        "a police officer running",
        "a waitress serving",
        "a princess dancing",
        "a hero standing",
        "un pintor pintando",
        "un rey de pie",
        "uma policial correndo",
        "uma soldada marchando",
        "um pintor pintando",
        "sem pessoas na frente, mostrar uma cantora",
        "nenhuma multidão exceto um cantor",
        "a teacher walking through a school",
        "a student studying",
        "a pilot standing by an aircraft",
        "an athlete running",
        "an engineer working",
        "a priest praying",
        "a coach shouting",
        "un profesor enseñando",
        "un estudiante caminando",
        "un sacerdote rezando",
        "um ator trabalhando",
        "um trabalhador caminhando",
        "um garçom servindo",
    )
    for prompt in prompts:
        safe = pipeline._sanitize_people_at_provider_boundary(
            prompt, allow_people=False,
        )
        assert safe != prompt, prompt
        assert "unoccupied" in safe.lower(), prompt


def test_high_recall_detector_does_not_treat_equipment_as_a_person():
    prompts = (
        "mechanical arm holding a camera",
        "robotic arm holding a light",
        "clock hands at midnight",
        "a chair's arms in close-up",
        "a 3D model of an empty futuristic city",
        "an Android phone on a table",
        "an electric fan in an empty room",
        "a rubber band on a white surface",
        "no people, no faces, no silhouettes",
        "manos de reloj a medianoche",
        "brazos de silla en primer plano",
        "brazo mecánico sosteniendo una cámara",
        "modelo 3d de una ciudad vacía",
        "mãos do relógio à meia-noite",
        "braços da cadeira em primeiro plano",
        "braço robótico segurando uma câmera",
        "modelo 3d de uma cidade vazia",
        "banda elástica sobre una mesa",
    )
    for prompt in prompts:
        assert pipeline._sanitize_people_at_provider_boundary(
            prompt, allow_people=False,
        ) == prompt, prompt


def test_human_shaped_figures_require_common_user_opt_in(db):
    job_id = _job(db, render_params={"bypass_content_validation": True})
    for prompt in (
        "a human-shaped mannequin in a shop window",
        "a humanoid statue in an empty plaza",
        "un maniquí humanoide bajo una luz roja",
    ):
        assert pipeline._compute_allow_people(job_id, prompt) is True


def test_visual_human_roles_require_common_user_opt_in_and_are_sanitized(db):
    job_id = _job(db, render_params={"bypass_content_validation": True})
    prompts = (
        "a lone guitarist playing on stage",
        "a drummer performing",
        "a vocalist at a microphone",
        "a silhouetted performer dancing",
        "a solitary human figure walking",
        "two friends dancing",
        "a pedestrian",
        "a driver",
        "lovers embracing",
        "an audience cheering",
        "fans dancing",
        "a DJ performing",
        "a rapper performing",
        "a band performing",
        "a worker walking",
        "an actor performing",
        "a model posing",
        "a camera operator filming",
        "a film crew working",
        "un guitarrista tocando en un escenario",
        "una operadora de cámara filmando",
        "uma baterista tocando",
        "uma equipe de filmagem trabalhando",
    )
    for prompt in prompts:
        assert pipeline._compute_allow_people(job_id, prompt) is True, prompt
        safe = pipeline._sanitize_people_at_provider_boundary(
            prompt,
            allow_people=False,
        )
        assert safe != prompt, prompt
        assert "unoccupied" in safe.lower(), prompt


def test_mechanical_camera_equipment_does_not_authorize_people(db):
    job_id = _job(db, render_params={"bypass_content_validation": True})
    for prompt in (
        "mechanical arm holding a camera",
        "robotic arm holding a light",
        "crane arm reaching across frame",
        "boom arm holding a microphone",
        "brazo mecánico sosteniendo una cámara",
        "braço robótico segurando uma luz",
    ):
        assert pipeline._compute_allow_people(job_id, prompt) is False, prompt


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
                "policy_version": "background-v6",
                "policy_mode": "off",
                "allow_atmospherics": False,
                "explicit_atmospherics": [],
                "authorization_source": "default_deny",
            },
            "validation": {
                "passed": True,
                "policy_fingerprint": "background-v6:off:deny|people:allow",
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


def test_universal_delivery_gate_runs_before_upload_and_success_state():
    for handler, success_marker in (
        (pipeline.run_pipeline, "final_status ="),
        (pipeline.run_edit_pipeline, 'status="pending_review", progress=100'),
    ):
        source = inspect.getsource(handler)
        seal_at = source.index("_seal_universal_delivery(")
        upload_at = source.index("_upload_deliverables_to_r2(")
        success_at = source.index(success_marker)
        assert seal_at < upload_at < success_at


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


def test_scene_fallback_provenance_commits_only_with_verified_cached_asset():
    source = inspect.getsource(pipeline.run_edit_pipeline)
    verify_at = source.index("_verify_deliverables(job_dir, files, audio_dur)")
    cache_commit_at = source.index(
        '_final_state_updates["bg_r2_key_cached"] = _new_background_cache_key'
    )
    assert verify_at < cache_commit_at
    assert '_final_state_updates["scene_plan"] = None' in source
    assert "_stored_background_ai_generated" in source[cache_commit_at:]
    assert "update_job(job_id, scene_plan=None" not in source


def test_initial_generation_has_provider_error_fallback_without_swallowing_timeout():
    source = inspect.getsource(pipeline.run_pipeline)
    assert "except RQJobTimeoutException" in source
    assert 'filename="bg_initial_policy_fallback.mp4"' in source
    assert "_background_is_deterministic_fallback = True" in source
    assert "_raise_if_job_timeout(_recovery_error)" in source


def test_rq_timeout_is_never_treated_as_a_recoverable_generation_error():
    timeout = pipeline.RQJobTimeoutException("worker deadline")
    with pytest.raises(pipeline.RQJobTimeoutException):
        pipeline._raise_if_job_timeout(timeout)
    pipeline._raise_if_job_timeout(RuntimeError("ordinary provider error"))

    scene_source = inspect.getsource(pipeline._generate_scene_clips)
    edit_source = inspect.getsource(pipeline.run_edit_pipeline)
    assert "_raise_if_job_timeout(e)" in scene_source
    for error_name in (
        "_background_generation_error",
        "_scene_generation_error",
        "_dense_error",
        "_edit_recovery_error",
        "exc",
    ):
        assert f"_raise_if_job_timeout({error_name})" in edit_source
