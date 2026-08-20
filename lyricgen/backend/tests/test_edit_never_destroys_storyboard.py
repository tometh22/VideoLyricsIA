"""Un fallo de infraestructura no puede costarle al operador el storyboard
ni el fondo que ya tenía.

Continuación del incidente 2026-08-20 (UMG Chile, "La Funa"): el fix de prod
(PR #1180) cubrió el fondo único, pero la rama multi-escena tenía el mismo
patrón destructivo por otros tres caminos:

  1. `_generate_scene_clips` marcaba la escena FALLIDA cuando el re-chequeo
     del clip cacheado no daba veredicto (429) → la evidencia quedaba
     incompleta → la revalidación densa tiraba excepción;
  2. esa excepción rendereaba el degradé Y borraba el `scene_plan` — con los
     prompts de cada escena adentro;
  3. un "Otra toma" que fallaba en el proveedor borraba el storyboard entero
     por UNA escena, y un regen de fondo que fallaba en el proveedor pisaba el
     fondo anterior con el degradé.

Regla que fijan estos tests: **degradar al último asset certificado, nunca a
la destrucción**. Un hallazgo decisivo de política sigue mandando al degradé.
"""
import inspect

import pipeline


FP = "background-v5:enforce:no_people"


# ------------------------------------------- veredicto heredado por clip


def _scene(validation=None):
    scene = {"recurrence_key": "verse", "prompt": "una mesa con una billetera"}
    if validation is not None:
        scene["validation"] = validation
    return scene


def test_inconclusive_recheck_inherits_the_clips_prior_verdict():
    scene = _scene()
    inherited = pipeline._inherit_inconclusive_scene_verdict(
        scene,
        verdict={"passed": False, "inconclusive": True, "issues": [{"frame": -1}]},
        prior_validation={"passed": True, "policy_fingerprint": FP},
        policy_fingerprint=FP,
        cache_only=True,
    )
    assert inherited is True
    assert scene["validation"]["passed"] is True
    assert scene["validation"]["revalidation_inconclusive"] is True


def test_decisive_rejection_is_never_inherited():
    scene = _scene()
    assert pipeline._inherit_inconclusive_scene_verdict(
        scene,
        verdict={
            "passed": False, "inconclusive": False,
            "issues": [{"frame": 2, "type": "people: recognizable face"}],
        },
        prior_validation={"passed": True, "policy_fingerprint": FP},
        policy_fingerprint=FP,
        cache_only=True,
    ) is False


def test_freshly_generated_clip_cannot_inherit():
    """Un regen escribe bytes NUEVOS: el veredicto viejo no dice nada de ellos."""
    scene = _scene()
    assert pipeline._inherit_inconclusive_scene_verdict(
        scene,
        verdict={"passed": False, "inconclusive": True, "issues": []},
        prior_validation={"passed": True, "policy_fingerprint": FP},
        policy_fingerprint=FP,
        cache_only=False,
    ) is False


def test_policy_change_invalidates_the_prior_verdict():
    scene = _scene()
    assert pipeline._inherit_inconclusive_scene_verdict(
        scene,
        verdict={"passed": False, "inconclusive": True, "issues": []},
        prior_validation={"passed": True, "policy_fingerprint": "background-v4:enforce"},
        policy_fingerprint=FP,
        cache_only=True,
    ) is False


def test_clip_without_prior_certification_is_not_inherited():
    scene = _scene()
    assert pipeline._inherit_inconclusive_scene_verdict(
        scene,
        verdict={"passed": False, "inconclusive": True, "issues": []},
        prior_validation={},
        policy_fingerprint=FP,
        cache_only=True,
    ) is False


# ------------------------------------------- decisivo vs no concluyente


def test_policy_rejection_in_the_plan_is_decisive():
    plan = {"scenes": [
        {"recurrence_key": "a", "validation": {"passed": True}},
        {"recurrence_key": "b", "status": "failed",
         "error": "RuntimeError: scene rejected by no-human policy: [{'frame': 1}]",
         "validation": {"passed": False}},
    ]}
    assert pipeline._scene_plan_failure_is_decisive(plan) is True


def test_detected_person_is_decisive_even_without_the_error_string():
    plan = {"scenes": [
        {"recurrence_key": "a", "validation": {
            "passed": False, "detections": {"people": True, "brand": False},
        }},
    ]}
    assert pipeline._scene_plan_failure_is_decisive(plan) is True


def test_cache_miss_and_breaker_are_not_decisive():
    plan = {"scenes": [
        {"recurrence_key": "a", "validation": {"passed": True}},
        {"recurrence_key": "b", "status": "reused",
         "error": "RuntimeError: veo breaker OPEN — multi-escena no puede generar clips",
         "validation": {"passed": False}},
        {"recurrence_key": "c", "status": "failed",
         "error": "RuntimeError: clip no escrito",
         "validation": {"passed": False}},
    ]}
    assert pipeline._scene_plan_failure_is_decisive(plan) is False


def test_healthy_or_missing_plan_is_not_decisive():
    assert pipeline._scene_plan_failure_is_decisive(None) is False
    assert pipeline._scene_plan_failure_is_decisive({}) is False
    assert pipeline._scene_plan_failure_is_decisive(
        {"scenes": [{"recurrence_key": "a", "validation": {"passed": True}}]}
    ) is False


# ------------------------------------------- degradar al asset certificado


def test_previous_certified_background_is_downloaded(monkeypatch, tmp_path):
    calls = {}

    def _download(key, dest):
        calls["key"] = key
        with open(dest, "wb") as fh:
            fh.write(b"veo")
        return True

    monkeypatch.setattr(pipeline.storage, "is_enabled", lambda: True)
    monkeypatch.setattr(pipeline.storage, "download_object", _download)

    path = pipeline._download_previously_certified_background(
        "job1", str(tmp_path), "backgrounds/job1/bg_cached.mp4",
        {"passed": True},
    )

    assert path and path.endswith(".mp4")
    assert calls["key"] == "backgrounds/job1/bg_cached.mp4"


def test_no_fallback_when_nothing_was_certified(monkeypatch, tmp_path):
    monkeypatch.setattr(pipeline.storage, "is_enabled", lambda: True)
    monkeypatch.setattr(
        pipeline.storage, "download_object",
        lambda *a: (_ for _ in ()).throw(AssertionError("must not download")),
    )
    for prior in ({}, {"passed": False}):
        assert pipeline._download_previously_certified_background(
            "job1", str(tmp_path), "backgrounds/job1/bg_cached.mp4", prior,
        ) is None


def test_download_failure_degrades_to_none(monkeypatch, tmp_path):
    monkeypatch.setattr(pipeline.storage, "is_enabled", lambda: True)
    monkeypatch.setattr(
        pipeline.storage, "download_object",
        lambda *a: (_ for _ in ()).throw(RuntimeError("R2 500")),
    )
    assert pipeline._download_previously_certified_background(
        "job1", str(tmp_path), "backgrounds/job1/bg_cached.mp4", {"passed": True},
    ) is None


def test_degraded_asset_is_covered_by_the_keep_rule(tmp_path):
    """Un regen fallido deja en mano el asset VIEJO: aunque el edit_type diga
    'background', un re-chequeo no concluyente no puede destruirlo."""
    asset = tmp_path / "bg_prev_certified.mp4"
    asset.write_bytes(b"veo")
    verdict = {"passed": False, "inconclusive": True}
    prior = {"passed": True}
    assert pipeline._should_keep_certified_background(
        "background", verdict=verdict, prior_validation_result=prior,
        asset_path=str(asset), reusing_certified_asset=True,
    ) is True
    # sin el flag, un edit de fondo sigue sin estar cubierto
    assert pipeline._should_keep_certified_background(
        "background", verdict=verdict, prior_validation_result=prior,
        asset_path=str(asset),
    ) is False


# ------------------------------------------- el storyboard nunca se pierde


def test_cleared_storyboard_is_archived_for_recovery():
    merged = {}
    plan = {"scenes": [{"recurrence_key": "a", "prompt": "billetera de cuero"}]}
    assert pipeline._archive_and_clear_scene_plan(
        merged, plan, "job1", reason="scene_validation_error",
    ) is None
    archived = merged["scene_plan_archived"]
    assert archived["reason"] == "scene_validation_error"
    assert archived["plan"]["scenes"][0]["prompt"] == "billetera de cuero"


def test_archiving_an_empty_plan_is_a_noop():
    merged = {}
    assert pipeline._archive_and_clear_scene_plan(merged, None, "job1", reason="x") is None
    assert "scene_plan_archived" not in merged


# ------------------------------------------- cableado en run_edit_pipeline


def test_edit_pipeline_never_drops_a_plan_without_archiving_it():
    src = inspect.getsource(pipeline.run_edit_pipeline)
    # Ninguna asignación cruda `scene_plan = None` puede sobrevivir: toda
    # limpieza pasa por el archivador.
    assert "scene_plan = None" not in src, (
        "clear the storyboard through _archive_and_clear_scene_plan so the "
        "scene prompts survive"
    )
    assert src.count("_archive_and_clear_scene_plan(") == 3


def test_edit_pipeline_prefers_the_certified_asset_over_the_gradient():
    src = inspect.getsource(pipeline.run_edit_pipeline)
    # Fallo de generación de fondo y de escena: primero el asset previo.
    assert "_download_previously_certified_background(" in src
    assert src.count("_kept_previous_background = True") == 2, (
        "both the background-regen and the scene-regen failure paths must "
        "degrade to the previously certified asset"
    )
    assert "_pending_background_recache = not _kept_previous_background" in src, (
        "re-rendering the cached bytes must never recache them"
    )
    assert "reusing_certified_asset=_kept_previous_background" in src


def test_dense_revalidation_keeps_the_plan_when_nothing_was_seen():
    src = inspect.getsource(pipeline.run_edit_pipeline)
    assert "_dense_decisive = _scene_plan_failure_is_decisive(scene_plan)" in src
    keep_at = src.index("_dense_decisive = ")
    gradient_at = src.index("recovered_from_scene_validation_error")
    assert keep_at < gradient_at, (
        "the decisive/inconclusive split must be evaluated before the "
        "gradient branch claims the storyboard"
    )


def test_scene_clip_validation_consults_the_inheritance_rule():
    src = inspect.getsource(pipeline._generate_scene_clips)
    assert "_inherit_inconclusive_scene_verdict(" in src
    inherit_at = src.index("_inherit_inconclusive_scene_verdict(")
    reject_at = src.index("scene rejected by no-human policy")
    assert inherit_at < reject_at, (
        "an inconclusive re-check must be resolved before the clip is "
        "rejected as a policy violation"
    )
