"""Que moviepy deje de tragarse los fallos de su propio ffmpeg escritor.

`FFMPEG_VideoWriter.close()` (moviepy 1.0.3) hace:

    def close(self):
        if self.proc:
            self.proc.stdin.close()
            if self.proc.stderr is not None:
                self.proc.stderr.close()
            self.proc.wait()          # <-- el returncode se DESCARTA
        self.proc = None

y `write_frame()` sólo atrapa `IOError` (broken pipe). O sea: si ffmpeg muere
o falla DESPUÉS de aceptar el último frame —justo la fase de finalización,
que con `-movflags +faststart` incluye reubicar el moov al principio—
`write_videofile()` retorna de lo más normal y deja un archivo roto en disco.

Eso es lo que rompió el job de UMG Chile el 2026-08-21 (`d6fdeb72088e`):
`short_bg_only.mp4` quedó con un moov impecable (1080x1920, 24 fps, 30,00 s,
721 frames) y CERO packets de video decodificables. ffprobe lo daba por bueno;
la pasada libass murió con `-22` ("Nothing was written into output file,
because at least one of its streams received no packets") y el fallback de
moviepy con `OSError: failed to read the first frame`, que sin atrapar se
llevó puesto el job entero y un master de 519 MB ya validado.

`_decodes_ok()` en pipeline.py ya detecta el SÍNTOMA (el archivo no
decodifica). Este patch ataca la CAUSA: convierte un fallo silencioso del
encoder en un error ruidoso, con el stderr de ffmpeg adjunto, en el momento y
lugar donde ocurre. La diferencia práctica es entre "el intermedio salió mal,
andá a saber por qué" y "ffmpeg salió con código N y dijo esto".

Contrato de la excepción — importa MÁS de lo que parece:

`ffmpeg_write_video` usa `with FFMPEG_VideoWriter(...) as writer:`, así que
`close()` corre también en el `__exit__` mientras se propaga OTRA excepción.
Levantar ahí incondicionalmente enmascararía el error original (que casi
siempre es el más informativo). Por eso sólo levantamos cuando NO hay una
excepción en vuelo; si la hay, logueamos y dejamos ganar a la original.

Mismas convenciones que moviepy_utf8_patch: pineado a moviepy 1.0.3 y
defensivo — si los internals no están (el stub de CI en tests/conftest.py) o
la versión no coincide, `apply_patch()` es un no-op y el import nunca rompe.
"""

import logging
import sys

logger = logging.getLogger("genly.moviepy_patch")

_EXPECTED_MOVIEPY = "1.0.3"
_applied = False
_STDERR_TAIL = 4000


class FFmpegWriterError(RuntimeError):
    """El ffmpeg escritor de moviepy terminó con código distinto de cero."""


def _patched_close(self):
    """close() que MIRA el returncode de ffmpeg antes de darlo por bueno."""
    proc = getattr(self, "proc", None)
    if proc is None:
        return

    stderr_tail = ""
    try:
        if proc.stdin is not None:
            try:
                proc.stdin.close()
            except Exception:
                pass
        if proc.stderr is not None:
            # Leer ANTES de cerrar: moviepy cierra el pipe sin mirarlo y ahí se
            # pierde el único lugar donde ffmpeg explica qué pasó. A esta
            # altura stdin ya está cerrado, así que ffmpeg termina y el read
            # llega a EOF sin riesgo de deadlock (stderr es el pipe que queda).
            try:
                raw = proc.stderr.read() or b""
                if isinstance(raw, bytes):
                    stderr_tail = raw[-_STDERR_TAIL:].decode("utf8", "replace").strip()
                else:
                    stderr_tail = str(raw)[-_STDERR_TAIL:].strip()
            except Exception:
                stderr_tail = ""
            try:
                proc.stderr.close()
            except Exception:
                pass
        returncode = proc.wait()
    finally:
        self.proc = None

    if returncode == 0:
        return

    filename = getattr(self, "filename", "?")
    msg = (
        f"el ffmpeg escritor de moviepy salió con código {returncode} "
        f"escribiendo {filename}"
        + (f" — stderr: {stderr_tail}" if stderr_tail else " (sin stderr)")
    )
    # Siempre se loguea y se alerta, se levante o no: si hay otra excepción en
    # vuelo este dato igual tiene que quedar registrado.
    logger.error("[moviepy-patch] %s", msg)
    try:
        import sentry_sdk
        with sentry_sdk.push_scope() as scope:
            scope.fingerprint = ["moviepy-writer-nonzero-exit"]
            scope.set_tag("ffmpeg_returncode", str(returncode))
            sentry_sdk.capture_message(f"[MOVIEPY-WRITER] {msg}", level="error")
    except Exception:
        pass

    if sys.exc_info()[0] is not None:
        # Estamos dentro del __exit__ de un `with` que ya está propagando algo.
        # La excepción original manda; pisarla acá sería cambiar un diagnóstico
        # bueno por uno peor.
        logger.warning(
            "[moviepy-patch] no levanto: ya hay una excepción en vuelo (%s)",
            sys.exc_info()[0].__name__,
        )
        return
    raise FFmpegWriterError(msg)


def apply_patch():
    """Rebindea FFMPEG_VideoWriter.close a la versión que chequea el returncode.

    Devuelve True si quedó activo, False si se salteó de forma segura.
    """
    global _applied
    if _applied:
        return True

    try:
        import moviepy
        import moviepy.video.io.ffmpeg_writer as ffmpeg_writer
    except Exception as exc:  # stub de CI / instalación parcial
        logger.debug("[moviepy-patch] writer internals no disponibles (%s) — no parcheo", exc)
        return False

    version = getattr(moviepy, "__version__", None)
    if version is None:
        try:
            from moviepy.version import __version__ as version
        except Exception:
            version = None
    if version != _EXPECTED_MOVIEPY:
        logger.warning(
            "[moviepy-patch] moviepy %s != el pineado %s — el chequeo de "
            "returncode del writer NO se aplicó; revisar close() antes de subir.",
            version, _EXPECTED_MOVIEPY,
        )
        return False

    writer_cls = getattr(ffmpeg_writer, "FFMPEG_VideoWriter", None)
    if writer_cls is None:
        logger.debug("[moviepy-patch] FFMPEG_VideoWriter ausente — no parcheo")
        return False

    # Idempotente aunque otro import path ya haya parcheado.
    if getattr(writer_cls.close, "_genly_writer_patched", False):
        _applied = True
        return True

    _patched_close._genly_writer_patched = True
    writer_cls.close = _patched_close
    _applied = True
    logger.info(
        "[moviepy-patch] el writer de moviepy ahora reporta los fallos de ffmpeg "
        "en vez de tragárselos"
    )
    return True


# Auto-aplicar al importar, igual que el hermano de UTF-8: un
# `import moviepy_writer_patch` arriba de pipeline.py alcanza para armarlo
# antes de que se escriba el primer clip.
apply_patch()
