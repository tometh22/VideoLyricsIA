"""Medidor de drift de cámara para fondos `movement_style=estatico`.

Contexto: Veo ignora el "locked camera" bastante seguido y no expone un campo
estructurado para forzarlo, así que lo único posible es MEDIRLO. Este medidor
sólo loguea (no re-rollea): primero hay que conocer la tasa real.

Qué fija este test — los tres detalles de implementación sin los cuales la
medición devuelve CERO para paneos reales, verificado a mano contra los fondos
de staging:

1. Ventana de Hanning + resta de la media. Sin ventanear, la fuga espectral de
   los bordes domina y el pico queda en el origen.
2. Interpolación sub-píxel. A 320 px de ancho un drift lento es sub-píxel entre
   frames y el pico entero lo redondea a 0.
3. Comparar contra el PRIMER frame, no contra el anterior. Acumular pasos
   consecutivos pierde los drifts lentos por cuantización.

Y el falso positivo que evita: una escena con movimiento propio que llena el
cuadro (nubes, niebla, agua) NO es un paneo. `estatico` está diseñado para tener
exactamente eso. Una correlación global sin ventanear mide al sujeto y reporta
paneo donde no hay — fue lo que pasó con el fondo 4af8c731051c, que una primera
medición marcó en 6,1% cuando la cámara está clavada y lo que se mueve son las
nubes.
"""
import os
import subprocess
import sys
import tempfile

import numpy as np
import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


def _ffmpeg_available() -> bool:
    try:
        subprocess.run(["ffmpeg", "-version"], capture_output=True, check=True)
        return True
    except Exception:
        return False


pytestmark = pytest.mark.skipif(not _ffmpeg_available(), reason="ffmpeg no disponible")


def _write_clip(frames, path, fps=6):
    """Escribe una lista de arrays HxW (uint8, gris) como mp4."""
    from PIL import Image

    with tempfile.TemporaryDirectory() as d:
        for i, fr in enumerate(frames):
            Image.fromarray(fr).convert("RGB").save(os.path.join(d, "f_%03d.png" % i))
        subprocess.run(
            ["ffmpeg", "-y", "-loglevel", "error", "-framerate", str(fps),
             "-i", os.path.join(d, "f_%03d.png"),
             "-c:v", "libx264", "-pix_fmt", "yuv420p", "-crf", "12", path],
            check=True,
        )


def _texture(h=360, w=640, seed=7):
    """Textura estática con detalle en todo el cuadro (rica en frecuencias)."""
    rng = np.random.default_rng(seed)
    base = rng.integers(0, 255, size=(h, w), dtype=np.uint8)
    # Suavizar un poco para que no sea ruido puro (más parecido a una escena).
    from PIL import Image, ImageFilter
    return np.asarray(
        Image.fromarray(base).filter(ImageFilter.GaussianBlur(2)), dtype=np.uint8
    )


def _pan_clip(total_shift_px, n=18, h=360, w=640):
    """Clip donde la CÁMARA se desplaza `total_shift_px` en horizontal."""
    canvas = _texture(h, w + abs(total_shift_px) + 8)
    frames = []
    for i in range(n):
        off = int(round(total_shift_px * i / (n - 1)))
        frames.append(canvas[:, off:off + w].copy())
    return frames


def _static_with_moving_subject(n=18, h=360, w=640):
    """Cámara CLAVADA y un sujeto grande que se mueve — el falso positivo."""
    bg = _texture(h, w, seed=3)
    frames = []
    for i in range(n):
        fr = bg.copy()
        # Banda brillante que barre 2/3 del cuadro: "nubes" moviéndose.
        y0 = 0
        y1 = int(h * 0.66)
        x = int((w * 0.7) * i / (n - 1))
        band = fr[y0:y1, :]
        shifted = np.roll(band, x, axis=1)
        fr[y0:y1, :] = np.clip(shifted.astype(np.int16) + 40, 0, 255).astype(np.uint8)
        frames.append(fr)
    return frames


@pytest.fixture(scope="module")
def measure():
    from pipeline import _measure_camera_drift
    return _measure_camera_drift


def _measure_frames(measure, frames, tmp_path, name):
    path = str(tmp_path / f"{name}.mp4")
    _write_clip(frames, path)
    out = measure(path)
    assert out is not None, "la medición no debería fallar con un clip válido"
    return out


def test_camara_clavada_mide_casi_cero(measure, tmp_path):
    frames = [_texture()] * 18
    out = _measure_frames(measure, frames, tmp_path, "static")
    assert out["pct_width"] < 1.0, out
    assert out["pct_width_borders"] < 1.0, out


def test_paneo_real_se_detecta(measure, tmp_path):
    # 64 px sobre 640 = 10% del ancho.
    out = _measure_frames(measure, _pan_clip(64), tmp_path, "pan")
    assert out["pct_width"] > 5.0, out


def test_paneo_grande_mide_mas_que_uno_chico(measure, tmp_path):
    chico = _measure_frames(measure, _pan_clip(16), tmp_path, "pan_small")
    grande = _measure_frames(measure, _pan_clip(96), tmp_path, "pan_big")
    assert grande["pct_width"] > chico["pct_width"], (chico, grande)


def test_movimiento_en_escena_confunde_mucho_menos_por_bordes(measure, tmp_path):
    """El falso positivo que importa: `estatico` DEBE tener motion in-scene.

    Este es el modo de falla que hizo que una primera medición (correlación
    global, sin ventanear) marcara el fondo 4af8c731051c en 6,1% cuando la
    cámara está clavada y lo que se mueve son las nubes y la niebla.

    LÍMITE CONOCIDO, documentado a propósito en vez de ajustar el umbral hasta
    que pase: la mediana de franjas ayuda MUCHO pero no es inmune. Acá el sujeto
    sintético cubre 2/3 del alto a lo ancho completo, así que contamina 3 de las
    4 franjas (superior + los dos tercios superiores de las laterales) y la
    mediana lo sigue en parte. En escenas reales el sujeto rara vez tapa 3
    franjas — en el clip de las nubes el suelo texturado ocupaba la mitad
    inferior y la medición por bordes dio 0,0%.

    Por eso el medidor emite las dos variantes y por eso arranca SÓLO LOGUEANDO:
    el umbral se decide con datos de producción, no con un sintético.
    """
    out = _measure_frames(
        measure, _static_with_moving_subject(), tmp_path, "subject"
    )
    # La propiedad que sí se sostiene: por bordes el error es varias veces menor
    # que global. Es la razón de ser de las franjas.
    assert out["pct_width_borders"] < out["pct_width"] / 3.0, out


def test_video_invalido_no_explota(measure, tmp_path):
    bad = tmp_path / "no-es-un-video.mp4"
    bad.write_bytes(b"nope")
    assert measure(str(bad)) is None


def _fade_in_from_black(n=24, h=360, w=640):
    """Cámara CLAVADA pero el clip abre desde negro — muy común en Veo."""
    tex = _texture(h, w, seed=11)
    out = []
    for i in range(n):
        k = min(1.0, i / 6.0)   # negro puro en el primer frame
        out.append((tex * k).astype(np.uint8))
    return out


def _letterboxed(total_shift_px=0, n=24, h=360, w=640):
    """Cámara clavada con franjas negras arriba y abajo (matte/letterbox)."""
    frames = _pan_clip(total_shift_px, n=n, h=h, w=w) if total_shift_px else [_texture(h, w)] * n
    band = h // 5
    out = []
    for fr in frames:
        f = fr.copy()
        f[:band, :] = 0
        f[-band:, :] = 0
        out.append(f)
    return out


def test_clip_que_abre_desde_negro_no_reporta_paneo(measure, tmp_path):
    """Un frame uniforme devolvía el desplazamiento MÁXIMO posible, no cero.

    El frame de referencia es el primero; si es negro puro, la varianza es 0,
    el cross-power queda todo en cero y `argmax` sobre un array plano cae en la
    esquina — que tras el fftshift está a media pantalla del origen. Una cámara
    perfectamente clavada se logueaba como ~57% del ancho.

    Es el caso que más importa: son justo los clips oscuros/desvanecidos los que
    la métrica iba a marcar, y el dataset entero se habría envenenado con ellos.
    """
    out = _measure_frames(measure, _fade_in_from_black(), tmp_path, "fade")
    assert out["pct_width"] < 2.0, out
    assert out["pct_width_borders"] < 2.0, out


def test_letterbox_no_inventa_paneo(measure, tmp_path):
    """Las franjas negras dejan uniformes 2 de las 4 franjas de borde."""
    out = _measure_frames(measure, _letterboxed(), tmp_path, "matte")
    assert out["pct_width_borders"] < 2.0, out


def test_letterbox_no_esconde_un_paneo_real(measure, tmp_path):
    """Y el guard no puede volverse ciego: con franjas Y paneo, se detecta."""
    out = _measure_frames(measure, _letterboxed(total_shift_px=80), tmp_path, "matte_pan")
    assert out["pct_width"] > 4.0, out


def test_se_mide_el_clip_ENTERO_no_solo_el_arranque(measure, tmp_path):
    """El paneo ocurre en la segunda mitad del clip.

    Con el tope viejo (12 frames a 3 fps = 4s de un clip de 8s) esto reportaba
    ~0: la métrica declaraba "excursión acumulada máxima" pero sólo miraba el
    arranque.
    """
    h, w, n = 360, 640, 36
    canvas = _texture(h, w + 96)
    frames = []
    for i in range(n):
        off = 0 if i < n // 2 else int(round(88 * (i - n // 2) / (n // 2 - 1)))
        frames.append(canvas[:, off:off + w].copy())
    out = _measure_frames(measure, frames, tmp_path, "late_pan")
    assert out["pct_width"] > 5.0, out
