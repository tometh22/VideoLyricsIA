"""El arte que sube el operador no se degrada en silencio.

Tres protecciones, todas del mismo incidente (auditoría 30-jul-2026 del flujo
"subo mi propia foto"):

1. **No sustituir por gradiente.** `run_pipeline` ya declaraba la intención en un
   comentario —"intentionally uploaded common-user assets keep the explicit
   validation failure behavior"— pero la implementación la contradecía:
   `_background_source_is_ai` devuelve True cuando `animate_image` está prendido,
   así que animar la foto del operador la reclasificaba como "generated model
   output = disposable". Un arte aprobado que no pasaba validación se
   reemplazaba por un GRADIENTE y el job quedaba `passed: True`.

2. **Registrar la degradación de la animación.** Si Veo falla, se entregaba un
   zoom lento con sólo un `logger.warning`: ninguna señal en el job, ni en la
   ficha del video.

3. **"Quieta de verdad".** Un fondo-imagen recibía SIEMPRE zoom 15%
   (`_prerender_kenburns_bg`), así que "sin movimiento" no existía para una foto
   subida — mientras el still generado por Imagen sí quedaba quieto.

Nota de método: los tests de (1) sobre la función pura son de COMPORTAMIENTO. Los
de cableado son estructurales (inspección de fuente), siguiendo la convención que
este directorio ya documenta en `test_bg_mode_dispatch.py`: `run_pipeline` y
`generate_lyric_video` son orquestadores enormes y ligados a ffmpeg/red, no
ejercitables de punta a punta en un unit test. Los estructurales no prueban el
comportamiento — sólo evitan que el cableado se borre sin que nadie se entere.
"""
import inspect

import pipeline


# ---------------------------------------------------------------- (1) provenance

def test_operator_upload_recognised_even_when_animated():
    """El caso del incidente: subida + animar seguía siendo material del operador.

    `_background_source_is_ai` dice True acá (correcto: los píxeles finales los
    generó Veo). La pregunta de `_background_is_operator_upload` es otra —el
    INPUT es del operador— y tiene que decir True igual.
    """
    assert pipeline._background_source_is_ai(
        "/jobs/x/bg_custom.jpg", "inputs/umusic/x/bg_custom.jpg",
        animate_image=True, variation_source_path=None,
    ) is True, "la provenance del OUTPUT sigue siendo AI (no cambiar esto)"

    assert pipeline._background_is_operator_upload(
        "/jobs/x/bg_custom.jpg", "inputs/umusic/x/bg_custom.jpg",
    ) is True, (
        "una foto SUBIDA por el operador sigue siendo suya aunque la animemos: "
        "si esto da False, un arte aprobado que falla validación se reemplaza "
        "por un gradiente y el job se entrega como OK"
    )


def test_operator_upload_recognised_without_animation():
    assert pipeline._background_is_operator_upload(
        "/jobs/x/bg_custom.png", "inputs/t/x/bg_custom.png",
    ) is True


def test_edit_path_upload_recognised():
    """El path de edición nombra el archivo `bg_custom_edit*`."""
    assert pipeline._background_is_operator_upload(
        "/jobs/x/bg_custom_edit.jpg", None,
    ) is True


def test_library_and_variation_are_not_operator_uploads():
    """Catálogo y derivados generados NO son arte del operador.

    Importa para no volver no-descartable a un asset que sí conviene regenerar:
    una variación es output de modelo, no el arte aprobado del sello.
    """
    assert pipeline._background_is_operator_upload(
        "/jobs/x/bg_library_12.mp4", "library/12.mp4",
    ) is False
    assert pipeline._background_is_operator_upload(
        "/jobs/x/bg_custom.jpg", "inputs/t/x/bg_custom.jpg",
        library_asset_id=12,
    ) is False, "un asset de biblioteca no es upload del operador"
    assert pipeline._background_is_operator_upload(
        "/jobs/x/bg_custom.jpg", "inputs/t/x/bg_custom.jpg",
        variation_source_path="/jobs/x/seed.jpg",
    ) is False, "una variación es output de modelo, sí es descartable"


def test_pure_ai_background_is_not_operator_upload():
    """Sin archivo del operador no hay nada que proteger (y ahí el gradiente
    de recuperación es el comportamiento deseado)."""
    assert pipeline._background_is_operator_upload(None, None) is False
    assert pipeline._background_is_operator_upload(
        "/jobs/x/bg_cached.mp4", "backgrounds/x/bg_cached.mp4",
    ) is False


# ------------------------------------------------------------------- (1) cableado

def test_recovery_gate_excludes_operator_uploads():
    """El gate de recuperación tiene que consultar la provenance del INPUT.

    Sin esto se vuelve al bug: `_recover_background` era
    `bool(_is_umg or _background_is_ai_generated)`, y como animar marca
    ai_generated=True, el arte del operador entraba en "disposable".
    """
    src = inspect.getsource(pipeline.run_pipeline)
    assert "_background_is_operator_upload(" in src, (
        "run_pipeline debe clasificar el INPUT con _background_is_operator_upload"
    )
    idx = src.find("_recover_background = bool(")
    assert idx > 0, "el gate de recuperación debe existir"
    gate = src[idx:idx + 260]
    assert "not _operator_upload_bg" in gate, (
        "el gate de recuperación DEBE excluir los uploads del operador. Sin esa "
        "condición, un arte aprobado que no pasa validación se reemplaza por un "
        "gradiente y el job se marca passed=True — entrega silenciosa equivocada."
    )


# --------------------------------------------------------- (2) degradación visible

def test_animation_degradation_is_persisted():
    """Que la degradación quede en el job, no sólo en un log."""
    src = inspect.getsource(pipeline.run_pipeline)
    assert "_bg_animation_degraded = True" in src, (
        "el fallback de image-to-video debe marcar la degradación"
    )
    assert '"bg_animation_degraded"' in src, (
        "la degradación debe persistirse en render_params para que la ficha del "
        "video pueda decir que la animación no se pudo hacer"
    )
    # Se escribe siempre que se intentó animar, así un re-render exitoso limpia
    # un True viejo en vez de dejarnos avisando de algo que ya no pasa.
    idx = src.find('"bg_animation_degraded"')
    ctx = src[max(0, idx - 200):idx + 120]
    assert "if _animate_user_image" in ctx, (
        "debe persistirse condicionado a que se haya intentado animar (True o "
        "False), no sólo cuando falla: si no, un re-render exitoso no limpia el "
        "aviso viejo"
    )


# -------------------------------------------------------------- (3) quieta de verdad

def test_still_background_uses_static_not_kenburns():
    """Con still_background el render no debe meter el zoom del 15%."""
    sig = inspect.signature(pipeline.generate_lyric_video)
    assert "still_background" in sig.parameters, (
        "generate_lyric_video debe aceptar still_background"
    )
    assert sig.parameters["still_background"].default is False, (
        "default False = comportamiento histórico intacto"
    )

    src = inspect.getsource(pipeline.generate_lyric_video)
    idx = src.find("if still_background:")
    assert idx > 0, "debe haber una rama para el fondo quieto"
    branch = src[idx:idx + 400]
    assert "_static_image_to_mp4(" in branch, (
        "la rama quieta debe usar _static_image_to_mp4 — el mismo helper que la "
        "rama Imagen, así 'quieta' significa lo mismo venga de IA o de un archivo"
    )
    assert "_prerender_kenburns_bg(" not in branch.split("else:")[0], (
        "la rama quieta NO debe llamar al Ken Burns"
    )


def test_still_background_wired_from_both_pipelines():
    """Creación y edición deben pasar la señal, o un edit pierde el 'quieta'."""
    for fn in (pipeline.run_pipeline, pipeline.run_edit_pipeline):
        src = inspect.getsource(fn)
        assert "still_background=" in src, (
            f"{fn.__name__} debe pasar still_background a generate_lyric_video"
        )
        idx = src.find("still_background=")
        assert 'estatico' in src[idx:idx + 200], (
            f"{fn.__name__} debe derivar still_background del movimiento "
            "'estatico' elegido por el operador"
        )
