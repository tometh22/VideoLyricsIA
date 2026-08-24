"""Un entregable secundario no puede destruir un master ya renderizado.

Incidente UMG Chile 2026-08-21, job d6fdeb72088e ("Papi, Dónde Está El
Funk?", Los Tetas). Los logs de prod:

    [ASS] lyric video rendered: 352s audio, 519.5 MB (libass fast path, validated)
    ...
    [SHORT] libass text burn failed ([vost#0:0/libx264] ... -22) — fallback moviepy
    OSError: failed to read the first frame of ... short_bg_only.mp4
    [PIPELINE] cleaned up failed job dir (freed disk): .../d6fdeb72088e

El master estaba listo y validado. Falló el clip vertical de 30 s, la
excepción subió sin atrapar hasta el except global, el job quedó 'error' y
_cleanup_job_dir_on_failure borró el directorio con los 519 MB adentro. El
cliente perdió el video dos veces seguidas porque el fallo es determinístico.

Estos tests fijan el contrato que lo impide. Son de comportamiento, no de
texto fuente: importan pipeline de verdad y ejercitan los helpers.
"""
import os
import subprocess
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import pipeline  # noqa: E402


# --------------------------------------------------------------------------
# _accessory_failed — dar por perdido el accesorio, nunca el job
# --------------------------------------------------------------------------

def test_accessory_failed_borra_el_artefacto_a_medio_escribir(tmp_path):
    """generate_short escribe DIRECTO sobre short.mp4 (sin .tmp + rename),
    así que un fallo a mitad deja un MP4 truncado. _cleanup_local_intermediates
    sólo barre bg_*, o sea que sobrevive — y entonces enqueue_prores_prewarm
    lo transcodea y publica un .mov corrupto en R2, porque prores.py sólo
    chequea os.path.exists del source y nunca lo valida."""
    short = tmp_path / "short.mp4"
    short.write_bytes(b"\x00\x00\x00\x18ftypmp42" + b"basura truncada")

    pipeline._accessory_failed("short", "job123", str(tmp_path), RuntimeError("boom"))

    assert not short.exists(), "el short truncado tiene que desaparecer"


def test_accessory_failed_no_explota_si_no_hay_artefacto(tmp_path):
    """El fallo puede ocurrir ANTES de que se escriba nada. El handler corre
    en un camino de error: si él mismo levanta, volvemos al bug original."""
    pipeline._accessory_failed("thumbnail", "job123", str(tmp_path), ValueError("x"))


def test_accessory_failed_relanza_el_death_penalty_de_rq():
    """`_raise_if_job_timeout` primero, siempre. Si nos tragamos el death
    penalty, el worker sigue al thumbnail y a subir cientos de MB pasado el
    deadline — la resiliencia jamás puede neutralizar a RQ."""
    with pytest.raises(pipeline.RQJobTimeoutException):
        pipeline._accessory_failed(
            "short", "job123", "/tmp", pipeline.RQJobTimeoutException("death penalty"),
        )


def test_run_pipeline_atrapa_los_cuatro_accesorios():
    """Los 4 accesorios (short/art-track short/thumbnail/art-track thumbnail)
    van dentro de try/except en AMBOS pipelines. En run_edit_pipeline pesa
    todavía más: /edit ya puso video_url/short_url/thumbnail_url y s3_keys en
    NULL antes de arrancar, así que un fallo dejaba el job en 'error' con los
    entregables ANTERIORES ya borrados de la fila."""
    import inspect
    for fn in (pipeline.run_pipeline, pipeline.run_edit_pipeline):
        src = inspect.getsource(fn)
        assert "_accessory_failed(\"short\"" in src, fn.__name__
        assert "_accessory_failed(\"thumbnail\"" in src, fn.__name__
        # El prewarm de ProRes vertical se saltea si no hay short que derivar.
        assert 'if "short" not in missing_deliverables:' in src, fn.__name__


# --------------------------------------------------------------------------
# _decodes_ok — la única verificación que distingue header lindo de archivo útil
# --------------------------------------------------------------------------

def _ffmpeg_disponible():
    try:
        subprocess.run(["ffmpeg", "-version"], capture_output=True, timeout=30)
        return True
    except Exception:
        return False


needs_ffmpeg = pytest.mark.skipif(not _ffmpeg_disponible(), reason="ffmpeg no instalado")


@needs_ffmpeg
def test_decodes_ok_acepta_un_mp4_sano(tmp_path):
    out = tmp_path / "sano.mp4"
    subprocess.run(
        ["ffmpeg", "-y", "-v", "error", "-f", "lavfi",
         "-i", "testsrc2=size=320x240:duration=1:rate=24",
         "-pix_fmt", "yuv420p", str(out)],
        check=True, capture_output=True, timeout=120,
    )
    assert pipeline._decodes_ok(str(out)) is True


@needs_ffmpeg
def test_decodes_ok_rechaza_header_sano_sin_packets(tmp_path):
    """LA firma del incidente. short_bg_only.mp4 reportaba un moov perfecto
    (1080x1920, 24 fps, 30,00 s, 721 frames) y no tenía un solo packet de
    video decodificable. ffprobe lo daba por bueno; ffmpeg y moviepy
    reventaban. Se reproduce truncando un mp4 faststart en el borde del mdat.
    """
    src = tmp_path / "src.mp4"
    subprocess.run(
        ["ffmpeg", "-y", "-v", "error", "-f", "lavfi",
         "-i", "testsrc2=size=320x240:duration=2:rate=24",
         "-pix_fmt", "yuv420p", "-movflags", "+faststart", str(src)],
        check=True, capture_output=True, timeout=120,
    )
    data = src.read_bytes()
    mdat = data.find(b"mdat")
    assert mdat != -1, "el fixture necesita un mdat para truncar"
    roto = tmp_path / "header_sano_sin_packets.mp4"
    roto.write_bytes(data[: mdat + 4])

    # ffprobe sigue leyendo el header: por eso NO alcanza como validación.
    probe = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration",
         "-of", "default=nw=1:nk=1", str(roto)],
        capture_output=True, text=True, timeout=60,
    )
    assert probe.stdout.strip(), "ffprobe debería seguir reportando duración"

    assert pipeline._decodes_ok(str(roto)) is False


def test_decodes_ok_rechaza_inexistente_y_vacio(tmp_path):
    vacio = tmp_path / "vacio.mp4"
    vacio.write_bytes(b"")
    assert pipeline._decodes_ok(str(tmp_path / "no-existe.mp4")) is False
    assert pipeline._decodes_ok(str(vacio)) is False


# --------------------------------------------------------------------------
# _prepare_short_bg — fondo del short 100% ffmpeg, un solo lector
# --------------------------------------------------------------------------

@needs_ffmpeg
def test_loop_del_fondo_del_short_produce_un_mp4_usable(tmp_path):
    """Un clip de 4 s (lo que da Veo con VEO_CLIP_SECONDS=4) tiene que
    rellenar los 30 s del short a 1080x1920, decodificable."""
    src = tmp_path / "bg4s.mp4"
    subprocess.run(
        ["ffmpeg", "-y", "-v", "error", "-f", "lavfi",
         "-i", "testsrc2=size=1280x720:duration=4:rate=24",
         "-pix_fmt", "yuv420p", str(src)],
        check=True, capture_output=True, timeout=120,
    )

    out = pipeline._prepare_short_bg(str(src), 0.0, 30.0, str(tmp_path))

    assert pipeline._decodes_ok(out)
    assert pipeline._video_dims(out) == (1080, 1920)
    dur = pipeline._ffprobe_duration(out)
    assert dur is not None and dur >= 30.0, f"el loop quedó corto: {dur}"


def test_el_intermedio_del_loop_se_limpia_solo():
    """El nombre TIENE que empezar con bg_looped_: _cleanup_local_intermediates
    barre por ese glob. Con cualquier otro nombre el archivo queda huérfano en
    el job_dir de cada render exitoso — decenas de MB por job, que es la
    cascada de disco que el propio módulo documenta."""
    import inspect
    src = inspect.getsource(pipeline._prepare_short_bg)
    assert "bg_looped_short_1080x1920.mp4" in src

    limpieza = inspect.getsource(pipeline._cleanup_local_intermediates)
    assert 'startswith("bg_looped_")' in limpieza


def test_el_loop_del_short_no_usa_prerender_looped_bg():
    """_prerender_looped_bg pone el -t DESPUÉS del -i (opción de output), así
    que con -stream_loop -1 el input nunca da EOF y el filtro `reverse` —que
    necesita EOF— bufferea sin límite: 2,24 GB de RSS a 1080x1920 contra 662
    MB del loop plano, y framemd5 IDÉNTICO (o sea el palíndromo no se produce
    nunca). Meterlo en el short pondría ese pico justo en progress=75, que es
    donde ya hay documentados workers SIGKILL-eados por OOM."""
    import inspect
    src = inspect.getsource(pipeline._prepare_short_bg)
    # Con paréntesis: el docstring lo nombra para explicar por qué NO se usa.
    assert "_prerender_looped_bg(" not in src
    # Sin filter_complex no hay grafo de `reverse` que bufferear, y sin
    # palíndromo un timeline multi-escena corto no se reproduce al revés.
    assert "-filter_complex" not in src
    assert "-stream_loop" in src


# --------------------------------------------------------------------------
# _rescue_master_before_cleanup — la red para lo que no anticipamos
# --------------------------------------------------------------------------

def test_rescue_no_sube_un_master_invalido(tmp_path, monkeypatch):
    """Subir un master truncado sería PEOR que perderlo: quedaría publicado
    como entregable bueno."""
    (tmp_path / "lyric_video.mp4").write_bytes(b"no soy un mp4")
    monkeypatch.setattr(pipeline.storage, "is_enabled", lambda: True)
    subido = []
    monkeypatch.setattr(
        pipeline.storage, "upload_master",
        lambda *a, **k: subido.append(a) or "k",
    )
    assert pipeline._rescue_master_before_cleanup("job123", str(tmp_path)) is False
    assert subido == []


def test_rescue_es_noop_sin_master(tmp_path):
    assert pipeline._rescue_master_before_cleanup("job123", str(tmp_path)) is False


def test_cleanup_recibe_job_id_para_poder_rescatar():
    """El rescate es la única barrera que cubre las bombas NO anticipadas en
    la ventana entre "el master existe en disco" y "el master está en R2".
    Si algún call site deja de pasar job_id, la red desaparece en silencio."""
    import inspect
    for fn in (pipeline.run_pipeline, pipeline.run_edit_pipeline):
        src = inspect.getsource(fn)
        for linea in src.splitlines():
            if "_cleanup_job_dir_on_failure(" in linea:
                assert "job_id" in linea, f"{fn.__name__}: {linea.strip()}"
