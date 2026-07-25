"""Contrato: el prompt del operador NUNCA se reescribe.

Decisión de producto (2026-07-24): las personas se evitan en la IMAGEN, no
reescribiendo el texto. Hasta esta fecha, un detector de alta recall
(``_sanitize_people_at_provider_boundary``) reemplazaba el prompt del operador
cuando "parecía" tener una persona. Era irrecuperablemente impreciso sobre
texto libre en español porque la lista de pronombres incluía ``[eé]l``, que
matchea el ARTÍCULO "el" además del pronombre "él": 6 de cada 10 prompts
realistas daban falso positivo.

Caso real que motivó el cambio (job d34cef371408, staging): el operador pidió
"la avenida 9 de julio de buenos aires y el obelisco, vista desde arriba" y
recibió una CATEDRAL — el detector marcó "el obelisco", borró la cláusula, y
como sobraban 18 caracteres eligió un arquetipo por hash del prompt.

Las dos capas que SÍ evitan personas y que este cambio no toca:
  1. El riel negativo del prompt ("no people, no faces, no hands").
  2. La validación Vision de los frames de salida (obligatoria para Universal).
"""

import pipeline


# Prompts REALES en español, todos sin ninguna persona. Antes del cambio, los
# que contienen el artículo "el" se reemplazaban por un arquetipo random.
PROMPTS_ES = (
    "la avenida 9 de julio de buenos aires y el obelisco, vista desde arriba",
    "el mar rompiendo contra las rocas",
    "atardecer sobre el desierto de atacama",
    "la cordillera con el sol saliendo detras",
    "el rio de la plata al amanecer",
    "el estadio vacio de noche",
    "una calle de buenos aires bajo la lluvia",
    "bosque de pinos con niebla",
)


def test_removed_sanitizer_is_gone_for_good():
    """El reescritor y su detector de recall no deben volver: eran la causa
    raíz. Si alguien los reintroduce, este test lo frena."""
    assert not hasattr(pipeline, "_sanitize_people_at_provider_boundary")
    assert not hasattr(pipeline, "_prompt_may_contain_human_subject")
    assert not hasattr(pipeline, "_UNOCCUPIED_FALLBACK_SETTINGS")


def test_boundary_observer_never_mutates_and_returns_nothing():
    """La función que quedó en el borde solo observa: no devuelve prompt."""
    for prompt in PROMPTS_ES:
        assert pipeline._note_people_policy_at_provider_boundary(
            prompt, allow_people=False, job_id="test1234abcd",
        ) is None


def test_spanish_article_el_is_not_treated_as_a_person(caplog):
    """Regresión directa del job d34cef371408: "el obelisco" no puede
    disparar la ruta de personas (el artículo NO es el pronombre "él")."""
    import logging

    with caplog.at_level(logging.WARNING):
        for prompt in PROMPTS_ES:
            pipeline._note_people_policy_at_provider_boundary(
                prompt, allow_people=False, job_id="test1234abcd",
            )
    assert "pide personas" not in caplog.text, (
        "un prompt de paisaje en español no puede marcarse como pedido de "
        f"personas: {caplog.text}"
    )


def test_explicit_people_request_is_logged_but_prompt_still_untouched(caplog):
    """Cuando el operador SÍ pide personas y la cuenta no las permite, se
    avisa (observabilidad) pero no se reescribe nada: la salida la deciden el
    riel negativo y la validación Vision."""
    import logging

    with caplog.at_level(logging.WARNING):
        pipeline._note_people_policy_at_provider_boundary(
            "a singer facing camera with a crowd behind her",
            allow_people=False, job_id="test1234abcd",
        )
    assert "pide personas" in caplog.text


def test_no_warning_when_people_are_allowed(caplog):
    import logging

    with caplog.at_level(logging.WARNING):
        pipeline._note_people_policy_at_provider_boundary(
            "a singer facing camera", allow_people=True, job_id="test1234abcd",
        )
    assert "pide personas" not in caplog.text


def test_negative_people_rail_is_still_built_independently():
    """El riel negativo sigue existiendo y NO depende del sanitizador
    eliminado: se arma desde ``allow_people``, en Veo y en Imagen.

    Y su contenido es el criterio nuevo (2026-07-24): caras reconocibles y
    persona-sujeto, NO "no people" — que hacía imposible un plano general
    urbano honesto.
    """
    import inspect

    src = inspect.getsource(pipeline)
    # 4 sitios: prompt de Veo, prompt de Imagen y los dos system prompts del
    # planner de Gemini (lyrics-mode y auto-mode).
    assert src.count('"" if allow_people else') == 4, (
        "el riel debe seguir armándose desde allow_people en los 4 sitios"
    )
    assert "no recognizable faces" in src
    assert "no person as the subject of the shot" in src
    assert " no people, no faces, no hands," not in src, (
        "el riel viejo prohibía cualquier persona: una cenital de una avenida "
        "con gente diminuta se pedía VACÍA"
    )
    assert "Never include people, faces, hands" not in src, (
        "el planner de Gemini tampoco debe tener la regla vieja de 'nunca "
        "incluyas personas' — le impedía escribir un plano urbano honesto"
    )


def test_output_validation_still_gates_universal():
    """La validación Vision de la salida — la capa autoritativa — sigue siendo
    obligatoria para Universal (no se tocó en este cambio)."""
    policy = pipeline._background_safety_policy(None, "cualquier prompt")
    assert policy["is_umg"] is True          # fail-closed sin job
    assert policy["allow_people"] is False
    assert policy["should_validate"] is True
    assert policy["validate_people"] is True
