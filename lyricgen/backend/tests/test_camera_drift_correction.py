"""Corrector de deriva de cámara: `_estimate_camera_track` + `_correct_camera_drift`.

Contexto. El prompt de Veo pide "camera completely LOCKED" DOS veces —char 199 y
char 2201 de 2324, o sea en las dos posiciones de máxima atención— y Veo lo
ignora igual: el fondo de "Tu Cárcel" (Universal, 2026-08-26) salió con un
push-in del 3,3%. No hay parámetro de API para forzarlo (`veo_params` sólo lleva
aspectRatio, sampleCount, generateAudio, durationSeconds), así que en vez de
seguir reescribiendo el prompt se mide lo que devolvió y se deshace.

Lo que fijan estos tests son las tres cosas que se rompieron al construirlo, en
orden de gravedad:

1. EL SIGNO. `_estimate_shift` devuelve la transformación INVERSA. La primera
   versión no lo invertía y reportaba 0,9751 para un zoom-IN real, o sea que el
   corrector concluía "cámara clavada" justo en el caso que venía a arreglar.
   `test_zoom_in_se_estima_con_el_signo_correcto` es la calibración contra
   verdad conocida que ancla eso.

2. LA DIRECCIÓN DEL WARP. Alinear el frame i de vuelta al 0 exige píxeles de
   afuera del cuadro —los que el zoom-in se comió, que Veo nunca generó—; el
   box se clampea al borde y la imagen queda intacta, así que el residuo vuelve
   a medir la deriva entera y la iteración DIVERGE (1,04 real → 1,15 estimado).
   Sólo se puede predecir el frame i DESDE el 0.

3. ESTIMAR SOBRE EL FRAME COMPLETO, no sobre las franjas de borde. Despejar la
   escala de las franjas es más barato pero SUBESTIMA sobre contenido real: 1,0260
   contra una deriva real de 1,0325 en el clip de Veo, y corregido con ese valor
   la mesa y la guitarra seguían marcadas en el mapa de diferencias. El
   movimiento propio de la escena contamina las franjas.

Y el falso positivo que NO se debe tocar: un fondo con movimiento propio
(follaje, nubes, agua) y la cámara clavada. Es exactamente lo que un cinemagraph
debe ser, y recortarlo sería un bug.
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
        subprocess.run(["ffprobe", "-version"], capture_output=True, check=True)
        return True
    except Exception:
        return False


pytestmark = pytest.mark.skipif(
    not _ffmpeg_available(), reason="ffmpeg/ffprobe no disponibles"
)

W, H, N = 640, 360, 48
FPS = 12


@pytest.fixture(scope="module")
def track():
    from pipeline import _estimate_camera_track
    return _estimate_camera_track


@pytest.fixture(scope="module")
def correct():
    from pipeline import _correct_camera_drift
    return _correct_camera_drift


def _texture(h=H, w=W, seed=7):
    """Textura estática rica en frecuencias — sin detalle no hay qué correlacionar."""
    from PIL import Image, ImageFilter

    rng = np.random.default_rng(seed)
    base = rng.integers(0, 255, size=(h, w), dtype=np.uint8)
    return np.asarray(
        Image.fromarray(base).filter(ImageFilter.GaussianBlur(2)), dtype=np.uint8
    )


def _write(frames, path, fps=FPS):
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
    return path


def _zoom_clip(total_scale, n=N, seed=7):
    """Clip con un zoom-in de cámara conocido: 1.0 → `total_scale`, lineal."""
    from PIL import Image

    base = Image.fromarray(_texture(seed=seed))
    out = []
    for i in range(n):
        z = 1.0 + (total_scale - 1.0) * (i / (n - 1))
        cw, ch = W / z, H / z
        x0, y0 = (W - cw) / 2, (H - ch) / 2
        out.append(np.asarray(
            base.resize((W, H), Image.LANCZOS, box=(x0, y0, x0 + cw, y0 + ch)),
            dtype=np.uint8,
        ))
    return out


def _static_with_moving_subject(n=N):
    """Cámara CLAVADA, contenido que se mueve. Un cinemagraph correcto."""
    bg = _texture(seed=3)
    out = []
    for i in range(n):
        fr = bg.copy()
        band = fr[: int(H * 0.5), :]
        fr[: int(H * 0.5), :] = np.roll(band, int((W * 0.5) * i / (n - 1)), axis=1)
        out.append(fr)
    return out


# ---------------------------------------------------------------- estimación

def test_zoom_in_se_estima_con_el_signo_correcto(track, tmp_path):
    """Calibración contra verdad conocida. Ancla el signo y la magnitud.

    La primera versión devolvía 0,9751 acá —la transformación inversa sin dar
    vuelta— y con eso el corrector veía "no se movió" en el caso exacto que
    tiene que arreglar. Un test que sólo mirara `abs(s-1)` habría pasado igual,
    por eso se afirma el LADO además del valor.
    """
    tr = track(_write(_zoom_clip(1.04), str(tmp_path / "zoom.mp4")))
    assert tr is not None and len(tr) >= 3
    s = tr[-1][1]
    assert s > 1.0, f"un zoom-IN debe dar escala > 1, dio {s:.4f} (¿signo invertido?)"
    assert abs(s - 1.04) < 0.008, f"esperado ~1.040, estimado {s:.4f}"


def test_la_estimacion_es_monotona_y_arranca_en_identidad(track, tmp_path):
    tr = track(_write(_zoom_clip(1.06), str(tmp_path / "zoom6.mp4")))
    assert tr[0] == (0.0, 1.0, 0.0, 0.0)
    scales = [s for _, s, _, _ in tr]
    # Un zoom lineal tiene que crecer; se tolera ruido de estimación entre
    # muestras contiguas, pero la tendencia global no es negociable.
    assert scales[-1] > scales[len(scales) // 2] > scales[1]


def test_zoom_mas_grande_estima_mas_grande(track, tmp_path):
    chico = track(_write(_zoom_clip(1.02), str(tmp_path / "z2.mp4")))[-1][1]
    grande = track(_write(_zoom_clip(1.08), str(tmp_path / "z8.mp4")))[-1][1]
    assert grande > chico + 0.02, (chico, grande)


def test_camara_clavada_estima_identidad(track, tmp_path):
    tr = track(_write([_texture()] * N, str(tmp_path / "static.mp4")))
    assert abs(tr[-1][1] - 1.0) < 0.006, tr[-1]


# ---------------------------------------------------------------- corrección

def test_correccion_deja_la_camara_clavada(correct, track, tmp_path):
    """El test que importa: después de corregir, la deriva se fue."""
    path = _write(_zoom_clip(1.05), str(tmp_path / "fix.mp4"))
    antes = track(path)[-1][1]
    assert antes > 1.03, f"el clip de prueba debía tener deriva, dio {antes:.4f}"

    res = correct(path, job_id="TEST")
    assert res is not None and res["corrected"] is True, res
    assert res["reason"] == "stabilized"

    despues = track(path)[-1][1]
    assert abs(despues - 1.0) < 0.01, (
        f"tras corregir la cámara debe quedar clavada: {antes:.4f} → {despues:.4f}"
    )
    assert despues < antes


def test_el_recorte_reportado_es_del_orden_de_la_deriva(correct, tmp_path):
    """`crop_pct` es lo que el operador PIERDE de encuadre: no puede mentir."""
    res = correct(_write(_zoom_clip(1.05), str(tmp_path / "crop.mp4")), job_id="T")
    assert res["corrected"] is True
    assert 2.0 < res["crop_pct"] < 8.0, res


def test_clip_ya_clavado_no_se_toca(correct, tmp_path):
    """Sin deriva no hay nada que arreglar — y reencodear degradaría gratis."""
    path = _write([_texture()] * N, str(tmp_path / "nodrift.mp4"))
    antes = open(path, "rb").read()
    res = correct(path, job_id="T")
    assert res is not None and res["corrected"] is False, res
    assert res["reason"] == "already_locked"
    assert open(path, "rb").read() == antes, "no debe reescribir un clip clavado"


def test_movimiento_de_escena_no_se_confunde_con_la_camara(correct, tmp_path):
    """El falso positivo caro: un cinemagraph correcto NO se recorta.

    `estatico` y el i2v están DISEÑADOS para tener movimiento dentro del cuadro
    (follaje, nubes, agua). Tratarlo como paneo recortaría un fondo que estaba
    perfecto.
    """
    path = _write(_static_with_moving_subject(), str(tmp_path / "subject.mp4"))
    antes = open(path, "rb").read()
    res = correct(path, job_id="T")
    assert res is not None and res["corrected"] is False, res
    assert open(path, "rb").read() == antes


def test_deriva_enorme_no_se_corrige(correct, tmp_path):
    """A 20% el recorte destruye más que el paneo: se deja pasar y se loguea.

    Los dos peores casos medidos en staging fueron 26,8% y 29,7%. Ahí la
    respuesta correcta es re-rollear el clip, no mutilarlo.
    """
    path = _write(_zoom_clip(1.25), str(tmp_path / "huge.mp4"))
    antes = open(path, "rb").read()
    res = correct(path, job_id="T")
    assert res is not None and res["corrected"] is False, res
    assert res["reason"] == "drift_too_large"
    assert open(path, "rb").read() == antes


def test_fail_open_con_entrada_invalida(correct, track, tmp_path):
    """Esto JAMÁS debe tumbar un render: ante cualquier problema, no toca nada."""
    roto = str(tmp_path / "roto.mp4")
    with open(roto, "wb") as f:
        f.write(b"no soy un mp4")
    assert correct(roto, job_id="T") is None
    assert track(roto) is None
    assert correct(str(tmp_path / "no_existe.mp4"), job_id="T") is None
