"""El writer de moviepy tiene que reportar los fallos de ffmpeg, no tragárselos.

`FFMPEG_VideoWriter.close()` de moviepy 1.0.3 hace `self.proc.wait()` y
descarta el returncode, y `write_frame()` sólo atrapa broken pipe. Si ffmpeg
falla DESPUÉS de aceptar el último frame —la fase de finalización, que con
`+faststart` incluye reubicar el moov— `write_videofile()` devuelve éxito y
deja un archivo roto. Así quedó `short_bg_only.mp4` con moov impecable y cero
packets en el incidente UMG Chile 2026-08-21 (job `d6fdeb72088e`).
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import moviepy_writer_patch as mwp  # noqa: E402


class _FakeProc:
    """Proceso mínimo con la forma que consume close()."""

    def __init__(self, returncode, stderr=b"", stdin=True):
        self.returncode_value = returncode
        self.stdin = _FakeStream() if stdin else None
        self.stderr = _FakeStream(stderr) if stderr is not None else None
        self.waited = False

    def wait(self):
        self.waited = True
        return self.returncode_value


class _FakeStream:
    def __init__(self, data=b""):
        self._data = data
        self.closed = False

    def read(self):
        return self._data

    def close(self):
        self.closed = True


class _FakeWriter:
    def __init__(self, proc, filename="/tmp/x.mp4"):
        self.proc = proc
        self.filename = filename

    close = mwp._patched_close


def test_returncode_cero_no_molesta():
    """El camino feliz no cambia: ni excepción ni ruido."""
    proc = _FakeProc(0, stderr=b"")
    w = _FakeWriter(proc)
    w.close()
    assert proc.waited is True
    assert w.proc is None, "close() debe soltar el proceso igual que el original"


def test_returncode_distinto_de_cero_levanta_con_el_stderr():
    """Lo que antes era silencio ahora es un error que NOMBRA la causa."""
    proc = _FakeProc(1, stderr=b"Error: no space left on device")
    w = _FakeWriter(proc, filename="/tmp/short_bg_only.mp4")
    with pytest.raises(mwp.FFmpegWriterError) as exc:
        w.close()
    msg = str(exc.value)
    assert "1" in msg
    assert "short_bg_only.mp4" in msg
    assert "no space left on device" in msg, "el stderr de ffmpeg es el dato útil"
    assert w.proc is None


def test_lee_el_stderr_ANTES_de_cerrarlo():
    """moviepy cierra el pipe sin leerlo y ahí se pierde el único lugar donde
    ffmpeg explica qué pasó."""
    proc = _FakeProc(2, stderr="algo explotó".encode("utf-8"))
    w = _FakeWriter(proc)
    with pytest.raises(mwp.FFmpegWriterError) as exc:
        w.close()
    assert "algo explotó" in str(exc.value)
    assert proc.stderr.closed is True, "igual hay que cerrarlo después de leer"


def test_no_pisa_una_excepcion_en_vuelo():
    """close() corre en el __exit__ del `with` de ffmpeg_write_video, también
    mientras se propaga OTRA excepción. Levantar ahí cambiaría un diagnóstico
    bueno por uno peor: la original manda."""
    proc = _FakeProc(1, stderr=b"secundario")
    w = _FakeWriter(proc)
    with pytest.raises(ValueError, match="el error de verdad"):
        try:
            raise ValueError("el error de verdad")
        except ValueError:
            w.close()          # no debe levantar FFmpegWriterError
            raise
    assert proc.waited is True, "aun así hay que cosechar el proceso"


def test_sin_stderr_igual_levanta():
    """Con logfile en vez de PIPE, proc.stderr es None. No es excusa."""
    proc = _FakeProc(3, stderr=None)
    w = _FakeWriter(proc)
    with pytest.raises(mwp.FFmpegWriterError):
        w.close()


def test_close_sin_proceso_es_noop():
    w = _FakeWriter(None)
    w.close()  # no debe explotar


def test_apply_patch_es_idempotente_y_no_rompe_con_el_stub():
    """conftest stubea moviepy, así que acá apply_patch tiene que SALTEARSE en
    silencio — ese es el contrato importante: importar el módulo nunca puede
    romper pipeline.py en un entorno sin los internals de moviepy."""
    primera = mwp.apply_patch()
    assert primera is mwp.apply_patch(), "idempotente"
    assert isinstance(primera, bool)


def test_pipeline_importa_el_patch():
    """El patch no sirve de nada si nadie lo importa. Se chequea por fuente
    porque con el stub de moviepy no se puede observar el rebind real."""
    ruta = os.path.join(os.path.dirname(__file__), "..", "pipeline.py")
    with open(ruta, encoding="utf-8") as f:
        src = f.read()
    assert "import moviepy_writer_patch" in src


@pytest.mark.skipif(
    "moviepy.video.io.ffmpeg_writer" not in sys.modules
    and not os.environ.get("GENLY_REAL_MOVIEPY"),
    reason="moviepy está stubeado en conftest; correr con GENLY_REAL_MOVIEPY=1",
)
def test_writer_real_camino_feliz_sigue_intacto(tmp_path):
    """Contra moviepy y ffmpeg de verdad: escribir bien no debe levantar."""
    np = pytest.importorskip("numpy")
    from moviepy.video.io.ffmpeg_writer import FFMPEG_VideoWriter
    out = str(tmp_path / "ok.mp4")
    w = FFMPEG_VideoWriter(out, (64, 64), 24, codec="libx264", preset="ultrafast")
    for _ in range(12):
        w.write_frame(np.zeros((64, 64, 3), dtype="uint8"))
    w.close()
    assert os.path.exists(out) and os.path.getsize(out) > 0
