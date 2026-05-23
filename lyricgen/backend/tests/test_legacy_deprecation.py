"""Regresión 2026-05-23 — `text_motion` + `lyric_transition` deprecados.

Estos dos campos legacy de tipografía vivieron junto a los nuevos
`lyrics_animation` + `line_transition` durante un par de releases. Dos
problemas reales obligaron a deprecarlos:

1. **Bug silencioso**: `text_motion != "none"` apagaba el gate del path ASS en
   `pipeline.py` y forzaba moviepy → moviepy NO implementa
   `lyrics_animation` ni `line_transition`, así que las animaciones
   karaoke/reveal/slide_up se evaporaban sin warning visible al operador.

2. **Colisión semántica**: "Transición" en el editor (legacy
   `lyric_transition` = duración de `\\fad`) y "Transición de línea" en el
   wizard (nuevo `line_transition` = slide/wipe/dissolve_blur) usaban la
   misma palabra para conceptos distintos.

Los tests acá fijan que:
  - El gate del ASS engine no consulta `text_motion`.
  - Las whitelists de `/retry` y `/variant` ya no propagan los campos legacy.
  - `run_pipeline` coerce a defaults y loguea WARN una vez por job cuando
    recibe valores no-default (telemetría de jobs viejos en cola).
"""
from __future__ import annotations

import inspect

import pipeline


def test_ass_gate_does_not_consult_text_motion():
    """El gate del path ASS es libre de `text_motion`. Antes, jobs viejos con
    `render_params.text_motion = "subtle"` se rendereaban via moviepy y
    perdían el lyrics_animation/line_transition silenciosamente."""
    src = inspect.getsource(pipeline)
    # Buscamos la sentencia EXACTA del gate viejo. Si reaparece, el bug
    # silencioso vuelve.
    assert 'text_motion == "none"' not in src, (
        "El gate del ASS engine no debe condicionarse por text_motion — el "
        "campo está deprecado y siempre se coerce a 'none' upstream."
    )


def test_retry_whitelist_excludes_legacy_typography():
    """La whitelist de /retry no debe propagar text_motion ni lyric_transition.
    Cualquier valor que quede en render_params viejos queda como dato muerto
    y NO viaja al re-render."""
    import main
    src = inspect.getsource(main)
    # Sección del /retry — heurística: buscamos el bloque que arma
    # retry_pipeline_kwargs y verificamos que no nombre los legacy.
    retry_block_start = src.index("retry_pipeline_kwargs = {}")
    retry_block_end = src.index("enqueue_pipeline(", retry_block_start)
    retry_block = src[retry_block_start:retry_block_end]
    assert '"text_motion"' not in retry_block, (
        "/retry whitelist no debe incluir text_motion."
    )
    assert '"lyric_transition"' not in retry_block, (
        "/retry whitelist no debe incluir lyric_transition."
    )


def test_variant_whitelist_excludes_legacy_typography():
    """Idem /variant — ningún variante hereda los campos deprecados del padre."""
    import main
    src = inspect.getsource(main)
    # Buscamos el bloque de /variant pipeline_kwargs (el del segundo
    # `for k in (...)` cerca del create_variant handler).
    # Heurística simple: ambos campos legacy NO aparecen en ningún whitelist.
    # Si aparece en algún whitelist del archivo, falla.
    assert src.count('"text_motion"') == 0, (
        "Ningún whitelist debe nombrar text_motion (deprecado 2026-05-23)."
    )
    assert src.count('"lyric_transition"') == 0, (
        "Ningún whitelist debe nombrar lyric_transition (deprecado 2026-05-23)."
    )


def test_run_pipeline_coerces_legacy_to_defaults():
    """`run_pipeline` debe loguear WARN y reset a defaults cuando recibe
    valores no-default — son jobs viejos en cola antes del deploy."""
    src = inspect.getsource(pipeline.run_pipeline)
    # Hay una rama explícita que coerce text_motion → "none" y otra para
    # lyric_transition → "cut" en el cuerpo. Verificamos por sustring.
    assert "text_motion = \"none\"" in src or "text_motion = 'none'" in src, (
        "run_pipeline debe forzar text_motion='none' (deprecado 2026-05-23)."
    )
    assert "lyric_transition = \"cut\"" in src or "lyric_transition = 'cut'" in src, (
        "run_pipeline debe forzar lyric_transition='cut' (deprecado)."
    )
    assert "[DEPRECATED]" in src, (
        "run_pipeline debe loguear WARN [DEPRECATED] para telemetría."
    )


def test_edit_job_request_drops_legacy_fields():
    """`EditJobRequest` no debe declarar los campos legacy. Pydantic ignora
    extras del cliente, así que viejos requests siguen pasando — pero
    nuevos requests NO pueden setear los campos deprecados."""
    from main import EditJobRequest
    # Pydantic v2 fields() attribute. Si el campo está declarado, está acá.
    field_names = set(EditJobRequest.model_fields.keys())
    assert "text_motion" not in field_names, (
        "EditJobRequest no debe declarar text_motion (deprecado)."
    )
    assert "lyric_transition" not in field_names, (
        "EditJobRequest no debe declarar lyric_transition (deprecado)."
    )
    # Sanity — los nuevos sí están.
    assert "lyrics_animation" in field_names
    assert "line_transition" in field_names


def test_generate_form_coerces_legacy_inputs():
    """El handler de /upload + /generate sigue aceptando los Form params
    (back-compat con clientes en tránsito) pero coerce a default antes
    de pasarlos a run_pipeline."""
    import main
    src = inspect.getsource(main)
    # Buscamos que después de los Form params haya una coerción explícita.
    # Hay DOS endpoints (/upload + /generate), cada uno debe coercionar.
    coerce_count_text = src.count('text_motion="none"')
    coerce_count_lyric = src.count('lyric_transition="cut"')
    # Al menos 2 (uno por endpoint). El audit cubre 2647-2655 (/upload)
    # y 3645-3653 (/generate).
    assert coerce_count_text >= 2, (
        f"Esperaba ≥2 coerciones text_motion='none' en main.py, encontré "
        f"{coerce_count_text}."
    )
    assert coerce_count_lyric >= 2, (
        f"Esperaba ≥2 coerciones lyric_transition='cut' en main.py, encontré "
        f"{coerce_count_lyric}."
    )
