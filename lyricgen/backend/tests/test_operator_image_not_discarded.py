"""La imagen que sube el operador nunca se reemplaza por una generada (P0).

Incidente 2026-07-29. `image_to_video_path` sólo llega con valor cuando el
operador subió un still Y pidió animarlo (`_animate_user_image` en
`run_pipeline`): o con el toggle "Animar con AI", o con el efecto "Foto viva".
Veo es el único proveedor que hace image-to-video. La rama `imagen` de
`_ensure_background` genera un still NUEVO desde texto y nunca lee
`image_to_video_path`, así que rutear ahí **descarta en silencio el arte del
operador** y entrega otra imagen — sin error y con el job marcado OK.

Era alcanzable por dos caminos triviales, los dos con la matriz de movimiento
pisando la intención de animar:

  (a) sticky de localStorage: `movement_style` venía en "foto-parallax" de un
      lote anterior → el `elif` de foto-parallax forzaba `bg_mode="imagen"`.
  (b) elegir "Foto viva": `chooseEffect` (UploadZone.jsx) fuerza
      `movementStyle="foto-parallax"` → mismo elif. O sea el único control
      pensado para "animá MI foto" garantizaba que la foto se descartara.

Para un sello (UMG entrega arte aprobado) es el peor modo de falla posible.

Estos tests son de COMPORTAMIENTO, no de inspección de fuente: mockeamos los
dos generadores y afirmamos a quién se le pasó la imagen del operador. Un test
de `assert "..." in src` pasaría con el bug reintroducido de cualquier otra
forma.
"""
import pytest

import pipeline


@pytest.fixture
def spy_providers(monkeypatch, tmp_path):
    """Reemplaza los dos generadores y el prompt-builder por espías.

    Devuelve un dict con lo que recibió cada proveedor. Los generadores
    escriben un archivo para satisfacer los chequeos de existencia/tamaño que
    hace `_ensure_background` aguas abajo.
    """
    calls = {"veo": [], "imagen": []}

    def fake_get_unique_prompt(*_args, **kwargs):
        return {"style": "video", "prompt": "a fake scene prompt"}

    def fake_veo(prompt, out_path, **kwargs):
        calls["veo"].append({"prompt": prompt, "image_path": kwargs.get("image_path"),
                             "live_photo": kwargs.get("live_photo")})
        with open(out_path, "wb") as fh:
            fh.write(b"\x00" * 2048)  # no vacío: hay guards de getsize()>0

    def fake_imagen(prompt, out_path, **kwargs):
        calls["imagen"].append({"prompt": prompt})
        with open(out_path, "wb") as fh:
            fh.write(b"\x00" * 2048)

    def fake_still_to_mp4(image_path, out_path, **kwargs):
        with open(out_path, "wb") as fh:
            fh.write(b"\x00" * 2048)

    monkeypatch.setattr(pipeline, "_get_unique_prompt", fake_get_unique_prompt)
    monkeypatch.setattr(pipeline, "_generate_veo_video", fake_veo)
    monkeypatch.setattr(pipeline, "_generate_imagen_image", fake_imagen)
    # Neutralizar los pasos de calidad/post-proceso que sí tocan ffmpeg/red.
    # `_static_image_to_mp4` es obligatorio mockearlo: es el paso siguiente de la
    # rama `imagen`, así que SIN mock un test que entra ahí (= con el bug puesto)
    # se cuelga invocando ffmpeg de verdad en vez de fallar limpio en el assert.
    monkeypatch.setattr(pipeline, "_static_image_to_mp4", fake_still_to_mp4)
    monkeypatch.setattr(pipeline, "_score_video_relevance", lambda *a, **k: 10)
    monkeypatch.setattr(pipeline, "_bg_scene_discontinuity", lambda *a, **k: False)
    return calls


def _operator_photo(tmp_path):
    """El arte que sube el operador (lo que NO se puede perder)."""
    p = tmp_path / "arte_aprobado_del_sello.jpg"
    p.write_bytes(b"\xff\xd8\xff" + b"\x00" * 1024)  # cabecera JPEG
    return str(p)


# movement_style que la matriz rutea a Imagen. "foto-parallax" es el caso real
# del incidente (sticky + chooseEffect). El resto entra por el legacy env.
@pytest.mark.parametrize("movement_style", ["foto-parallax", "estatico", "sutil"])
def test_operator_image_pinned_to_veo_not_imagen(spy_providers, tmp_path,
                                                 monkeypatch, movement_style):
    """Con imagen del operador, el proveedor SIEMPRE es Veo (image-to-video).

    Con el bug: movement_style="foto-parallax" forzaba bg_mode="imagen", Imagen-4
    generaba una foto nueva desde la letra y la del operador nunca se usaba.
    """
    # Prende el camino legacy para que estatico/sutil también intenten Imagen:
    # así el invariante queda probado en todos los registers que rutean ahí.
    monkeypatch.setenv("STATIC_SUTIL_VIA_IMAGEN", "1")
    photo = _operator_photo(tmp_path)

    pipeline._ensure_background(
        "auto", str(tmp_path),
        lyrics_text="una letra cualquiera",
        artist="Artista", song_title="Tema",
        movement_style=movement_style,
        image_to_video_path=photo,
    )

    calls = spy_providers
    assert not calls["imagen"], (
        f"movement_style={movement_style!r} ruteó a Imagen teniendo una imagen "
        "del operador para animar. Imagen genera un still NUEVO desde texto y "
        "nunca lee image_to_video_path → el arte subido se descarta en silencio."
    )
    assert len(calls["veo"]) == 1, "Veo debe ser el proveedor cuando hay imagen del operador"
    assert calls["veo"][0]["image_path"] == photo, (
        "Veo tiene que recibir EXACTAMENTE la imagen del operador como semilla "
        f"de image-to-video, recibió {calls['veo'][0]['image_path']!r}"
    )


def test_foto_viva_with_upload_animates_the_uploaded_photo(spy_providers, tmp_path):
    """Foto viva + subida: se anima la foto SUBIDA, con el contrato live_photo.

    Es el camino (b) del incidente y el más grave: `chooseEffect` fuerza
    movementStyle="foto-parallax", y con el bug la rama imagen animaba
    `bg_imagen.jpg` (la generada) en vez de la del operador.
    """
    photo = _operator_photo(tmp_path)

    pipeline._ensure_background(
        "auto", str(tmp_path),
        lyrics_text="una letra cualquiera",
        artist="Artista", song_title="Tema",
        movement_style="foto-parallax",   # lo que fuerza chooseEffect
        effect="foto_viva",
        image_to_video_path=photo,
    )

    calls = spy_providers
    assert not calls["imagen"], (
        "Foto viva CON imagen subida no debe generar un still nuevo: hay que "
        "animar el que subió el operador."
    )
    assert calls["veo"] and calls["veo"][0]["image_path"] == photo, (
        "Foto viva debe animar la foto del operador, no otra"
    )
    assert calls["veo"][0]["live_photo"] is True, (
        "Foto viva tiene que propagar live_photo=True: es el prompt que "
        "preserva composición/identidad y mueve un solo sujeto con cámara fija"
    )


def test_foto_viva_without_upload_still_generates_its_own_still(spy_providers, tmp_path):
    """Caso legítimo inverso — NO romper con el fix.

    Foto viva SIN subida es un contrato foto-primero: genera su propio still con
    Imagen y después lo anima. El invariante sólo debe activarse cuando hay
    imagen del operador.
    """
    pipeline._ensure_background(
        "auto", str(tmp_path),
        lyrics_text="una letra cualquiera",
        artist="Artista", song_title="Tema",
        effect="foto_viva",
        image_to_video_path=None,   # sin subida
    )

    calls = spy_providers
    assert calls["imagen"], (
        "Foto viva sin subida debe seguir generando su propio still con Imagen "
        "(el fix no puede desactivar este camino)"
    )
