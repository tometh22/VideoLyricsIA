"""El fondo del short lo lee ffmpeg, nunca moviepy.

Incidente UMG Chile 2026-08-21 (job d6fdeb72088e), el detalle que costó caro:
ffmpeg leyó `bg_cached.mp4` perfectamente —el master salió de ahí, 352 s /
548 MB / validado— y moviepy levantó en el PRIMER frame del MISMO archivo.
Los dos usan ffmpeg, pero no el mismo binario: moviepy trae el suyo embebido
(imageio-ffmpeg) y el pipeline llama al del sistema. Resultado: el master con
el fondo Veo y el short con un degradé, en silencio.

Ojo con la trampa: validar el fuente con `_decodes_ok()` NO cubre esto —corre
con el ffmpeg del sistema, así que en ese escenario da OK y moviepy revienta
igual dos líneas después. La única defensa es que moviepy no lea el original.
"""
import ast
import inspect
import os
import subprocess
import sys
import textwrap

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pipeline  # noqa: E402


def _ffmpeg_ok():
    try:
        subprocess.run(["ffmpeg", "-version"], capture_output=True, timeout=30)
        return True
    except Exception:
        return False


needs_ffmpeg = pytest.mark.skipif(not _ffmpeg_ok(), reason="ffmpeg no instalado")


def _clip(path, dur, size="1280x720", rampa=False):
    filtro = ["-vf", f"geq=lum='(T/{dur})*255':cb=128:cr=128"] if rampa else []
    fuente = (f"color=c=black:s={size}:r=24:d={dur}" if rampa
              else f"testsrc2=size={size}:duration={dur}:rate=24")
    subprocess.run(
        ["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i", fuente,
         *filtro, "-pix_fmt", "yuv420p", path],
        check=True, capture_output=True, timeout=180,
    )
    return path


def test_moviepy_nunca_abre_el_fondo_original():
    """LA regresión a evitar. Si alguien vuelve a poner un
    `VideoFileClip(bg_source)` acá, el short vuelve a depender del binario de
    moviepy para leer lo que produjo Veo — y volvemos al degradé silencioso."""
    src = textwrap.dedent(inspect.getsource(pipeline.generate_short))
    arbol = ast.parse(src)
    for nodo in ast.walk(arbol):
        if (isinstance(nodo, ast.Call)
                and isinstance(nodo.func, ast.Name)
                and nodo.func.id == "VideoFileClip"):
            arg = nodo.args[0] if nodo.args else None
            nombre = getattr(arg, "id", None)
            assert nombre != "bg_source", (
                "moviepy no puede leer el fondo original: se normaliza con "
                "ffmpeg primero (ver _prepare_short_bg)"
            )


@needs_ffmpeg
def test_fondo_corto_se_loopea_a_la_duracion_pedida(tmp_path):
    """El caso típico: clip Veo de 4 s cubriendo un short de 30 s."""
    src = _clip(str(tmp_path / "bg4.mp4"), 4)
    out = pipeline._prepare_short_bg(src, 0.0, 30.0, str(tmp_path))
    assert pipeline._video_dims(out) == (1080, 1920)
    assert pipeline._ffprobe_duration(out) >= 30.0
    assert pipeline._decodes_ok(out)


@needs_ffmpeg
def test_fondo_largo_recorta_la_ventana_del_coro(tmp_path):
    """#785: el short usa la MISMA ventana temporal que su audio y su letra,
    no los primeros 30 s. En un timeline multi-escena eso es la diferencia
    entre mostrar las escenas del coro o las de la intro."""
    # Rampa de 120 s: el brillo codifica el timestamp, así que se puede
    # verificar QUÉ ventana se recortó mirando el primer frame.
    src = _clip(str(tmp_path / "largo.mp4"), 120, size="320x180", rampa=True)
    out = pipeline._prepare_short_bg(src, 90.0, 30.0, str(tmp_path))
    assert pipeline._ffprobe_duration(out) >= 29.5

    def _lum(path, ss=None):
        cmd = ["ffmpeg", "-v", "error"]
        if ss is not None:
            cmd += ["-ss", str(ss)]
        cmd += ["-i", path, "-frames:v", "1", "-vf", "scale=1:1",
                "-f", "rawvideo", "-pix_fmt", "gray", "-"]
        return subprocess.run(cmd, capture_output=True, timeout=180).stdout[0]

    # Auto-calibrado a propósito: la rampa de `geq` no es perfectamente lineal
    # y hardcodear el valor esperado hacía fallar el test con el código BIEN
    # (primera versión: esperaba 191 y tanto la fuente como la salida daban
    # 219). Se compara contra la propia fuente en el mismo timestamp.
    esperado = _lum(src, ss=90.0)
    intro = _lum(src, ss=0.0)
    obtenido = _lum(out)
    assert abs(obtenido - esperado) <= 12, (
        f"el primer frame da {obtenido}, la fuente a t=90 da {esperado}: "
        "se recortó la ventana equivocada"
    )
    assert abs(obtenido - intro) > 60, (
        "la ventana pedida tiene que distinguirse de la intro, si no el test "
        "no prueba nada"
    )


@needs_ffmpeg
def test_fondo_ilegible_levanta_en_vez_de_devolver_basura(tmp_path):
    """Con la firma del incidente: moov sano, mdat vacío."""
    src = _clip(str(tmp_path / "ok.mp4"), 2, size="320x240")
    data = open(src, "rb").read()
    roto = str(tmp_path / "roto.mp4")
    with open(roto, "wb") as f:
        f.write(data[: data.find(b"mdat") + 4])
    with pytest.raises(Exception):
        pipeline._prepare_short_bg(roto, 0.0, 30.0, str(tmp_path))


@needs_ffmpeg
def test_no_deja_intermedios_fuera_del_glob_de_limpieza(tmp_path):
    src = _clip(str(tmp_path / "bg4.mp4"), 4)
    pipeline._prepare_short_bg(src, 0.0, 10.0, str(tmp_path))
    sobrantes = [f for f in os.listdir(str(tmp_path))
                 if f.endswith(".mp4") and f not in ("bg4.mp4",)]
    assert all(f.startswith("bg_looped_") for f in sobrantes), sobrantes
    limpieza = inspect.getsource(pipeline._cleanup_local_intermediates)
    assert 'startswith("bg_looped_")' in limpieza


def test_la_escritura_del_intermedio_reintenta_una_vez():
    """La causa de fondo sigue sin determinarse y las hipótesis vivas son
    TRANSITORIAS (el encoder muere en la finalización). Sin reintento, un hipo
    de 30 segundos le cuesta el short entero al cliente."""
    src = inspect.getsource(pipeline.generate_short)
    assert "for _intento in (1, 2):" in src
    assert "short-bg-write-failed" in src
    # Y que valide DENTRO del bucle: reintentar sin revalidar no sirve de nada.
    bloque = src[src.index("for _intento in (1, 2):"):src.index("audio.close()")]
    assert "_decodes_ok(bg_only_path)" in bloque
    assert "write_videofile(" in bloque
