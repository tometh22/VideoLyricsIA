"""Tests del Canvas de Spotify (`pipeline.generate_canvas`).

El Canvas es un loop de 9:16 que Spotify repite cada 8 segundos en la vista
Now Playing, así que las tres cosas que lo pueden hacer rechazar —o quedar
feo decenas de veces por escucha— son: pasarse de 8s, traer pista de audio, y
que el loop tenga un corte visible. Los tests están escritos alrededor de eso.

Dos capas, igual que test_art_track.py:
  - Contratos de despacho SIN correr ffmpeg (comando capturado por
    monkeypatch): validan la forma del comando y los invariantes de spec.
  - Un test de render REAL marcado `slow`, que produce el archivo y lo mide
    con ffprobe. Es el único que prueba de verdad que el loop cierra.
"""
import os
import shutil
import subprocess

import pytest

import pipeline


# --------------------------------------------------------------------------
# Constantes de spec
# --------------------------------------------------------------------------

def test_spec_matches_spotify_requirements():
    # 9:16 vertical dentro del rango aceptado (720x1280 a 1080x1920).
    assert (pipeline.CANVAS_WIDTH, pipeline.CANVAS_HEIGHT) == (1080, 1920)
    assert pipeline.CANVAS_HEIGHT / pipeline.CANVAS_WIDTH == pytest.approx(16 / 9)
    # Spotify rechaza cualquier cosa por encima de 8s.
    assert pipeline.CANVAS_SECONDS <= 8.0
    # La unidad palindromeada tiene que dar el total exacto.
    assert pipeline.CANVAS_UNIT_SECONDS * 2 == pipeline.CANVAS_SECONDS


def test_canvas_registrado_como_entregable_accesorio():
    # En _DELIVERABLE_FILENAMES para que _upload_deliverables_to_r2 lo suba
    # sin ninguna rama especial...
    assert pipeline._DELIVERABLE_FILENAMES["canvas"] == "canvas.mp4"
    # ...y en _ACCESSORY_ARTIFACTS para que su fallo NUNCA se lleve puesto un
    # master ya renderizado (incidente UMG Chile 2026-08-21).
    assert pipeline._ACCESSORY_ARTIFACTS["canvas"] == "canvas.mp4"
    # Y explícitamente FUERA de los críticos.
    assert "canvas" not in pipeline._CRITICAL_DELIVERABLES


# --------------------------------------------------------------------------
# Despacho de ffmpeg (sin ejecutar ffmpeg)
# --------------------------------------------------------------------------

def _capture(monkeypatch):
    """Reemplaza run_checked y devuelve la lista de comandos despachados."""
    cmds = []

    def fake_run_checked(cmd, **kw):
        cmds.append(cmd)
        out = kw.get("output_path")
        if out:                      # el helper real exige archivo no vacío
            with open(out, "wb") as fh:
                fh.write(b"x")
        return None

    monkeypatch.setattr(pipeline, "run_checked", fake_run_checked)
    monkeypatch.setattr(pipeline, "_ffprobe_duration", lambda p: 8.0)
    return cmds


def test_dos_pasadas_y_el_t_va_en_la_salida(tmp_path, monkeypatch):
    """El bug 2026-08-21: con `-stream_loop -1` el input nunca da EOF, así que
    `reverse` no emite nunca. La unidad tiene que escribirse ACOTADA en disco
    en la pasada 1, y recién ahí palindromearse."""
    cmds = _capture(monkeypatch)
    src = tmp_path / "bg.mp4"
    src.write_bytes(b"fake")
    pipeline.generate_canvas(str(src), str(tmp_path))

    assert len(cmds) == 2, "el render tiene que ser en dos pasadas"
    unidad, palindromo = cmds

    # Pasada 1 acota por conteo de frames, no por -t, para que un redondeo de
    # timestamps no deje el archivo en 8,03s.
    assert "-frames:v" in unidad
    assert unidad[unidad.index("-frames:v") + 1] == str(
        int(pipeline.CANVAS_UNIT_SECONDS * pipeline.CANVAS_FPS))

    # Pasada 2: el palíndromo real (split + reverse + concat).
    graph = palindromo[palindromo.index("-filter_complex") + 1]
    assert "reverse" in graph and "concat=n=2" in graph
    # Y NO puede haber un -stream_loop en la pasada que hace reverse.
    assert "-stream_loop" not in palindromo


def test_nunca_emite_pista_de_audio(tmp_path, monkeypatch):
    """Spotify rechaza un Canvas con audio, y el fondo puede traerlo si vino
    de un clip con sonido."""
    cmds = _capture(monkeypatch)
    src = tmp_path / "bg.mp4"
    src.write_bytes(b"fake")
    pipeline.generate_canvas(str(src), str(tmp_path))
    for cmd in cmds:
        assert "-an" in cmd


def test_salida_final_es_faststart_y_conteo_exacto(tmp_path, monkeypatch):
    cmds = _capture(monkeypatch)
    src = tmp_path / "bg.mp4"
    src.write_bytes(b"fake")
    pipeline.generate_canvas(str(src), str(tmp_path))
    final = cmds[-1]
    assert "+faststart" in final
    total = int(pipeline.CANVAS_SECONDS * pipeline.CANVAS_FPS)
    assert final[final.index("-frames:v") + 1] == str(total)
    assert final[final.index("-pix_fmt") + 1] == "yuv420p"


def test_foto_fija_usa_zoompan_desde_z_igual_1(tmp_path, monkeypatch):
    """Con una foto el push tiene que arrancar en z=1.0: al palindromear, el
    primer frame y el último coinciden y el ciclo cierra sin salto."""
    cmds = _capture(monkeypatch)
    src = tmp_path / "cover.jpg"
    src.write_bytes(b"fake")
    pipeline.generate_canvas(str(src), str(tmp_path))
    graph = cmds[0][cmds[0].index("-filter_complex") + 1]
    assert "zoompan" in graph
    assert "z='1+" in graph, "el zoom tiene que partir de 1.0, no de un valor mayor"
    assert "-loop" in cmds[0], "una imagen fija necesita -loop 1"


def test_video_no_usa_zoompan(tmp_path, monkeypatch):
    """Un fondo Veo ya trae su propio movimiento; agregarle zoom lo ensucia."""
    cmds = _capture(monkeypatch)
    src = tmp_path / "bg.mp4"
    src.write_bytes(b"fake")
    pipeline.generate_canvas(str(src), str(tmp_path))
    graph = cmds[0][cmds[0].index("-filter_complex") + 1]
    assert "zoompan" not in graph
    assert "-stream_loop" in cmds[0], "un clip más corto que la unidad se loopea"


def test_efecto_se_compone_en_screen(tmp_path, monkeypatch):
    cmds = _capture(monkeypatch)
    src = tmp_path / "bg.mp4"
    src.write_bytes(b"fake")
    pipeline.generate_canvas(str(src), str(tmp_path), effect="bokeh")
    graph = cmds[0][cmds[0].index("-filter_complex") + 1]
    assert "blend=all_mode=screen" in graph


def test_efecto_desconocido_no_rompe(tmp_path, monkeypatch):
    cmds = _capture(monkeypatch)
    src = tmp_path / "bg.mp4"
    src.write_bytes(b"fake")
    pipeline.generate_canvas(str(src), str(tmp_path), effect="no-existe")
    graph = cmds[0][cmds[0].index("-filter_complex") + 1]
    assert "blend=" not in graph


def test_bg_inexistente_falla_temprano(tmp_path):
    with pytest.raises(ValueError):
        pipeline.generate_canvas(str(tmp_path / "no-esta.mp4"), str(tmp_path))
    with pytest.raises(ValueError):
        pipeline.generate_canvas(None, str(tmp_path))


def test_rechaza_salida_que_se_pasa_de_8s(tmp_path, monkeypatch):
    """Cinturón: mejor fallar acá que entregar algo que Spotify rechaza sin
    decir por qué."""
    _capture(monkeypatch)
    monkeypatch.setattr(pipeline, "_ffprobe_duration", lambda p: 8.4)
    src = tmp_path / "bg.mp4"
    src.write_bytes(b"fake")
    with pytest.raises(RuntimeError, match="fuera de spec"):
        pipeline.generate_canvas(str(src), str(tmp_path))


def test_borra_la_unidad_intermedia(tmp_path, monkeypatch):
    _capture(monkeypatch)
    src = tmp_path / "bg.mp4"
    src.write_bytes(b"fake")
    pipeline.generate_canvas(str(src), str(tmp_path))
    sobrantes = [p for p in os.listdir(tmp_path) if p.startswith("canvas_unit_")]
    assert sobrantes == [], f"quedó un intermedio sin borrar: {sobrantes}"


# --------------------------------------------------------------------------
# Render real — el único que prueba que el loop cierra de verdad
# --------------------------------------------------------------------------

def _ffprobe(path, *entries):
    out = subprocess.run(
        ["ffprobe", "-v", "error", *entries, "-of", "csv=p=0", path],
        capture_output=True, text=True, check=True).stdout
    return out.strip()


@pytest.mark.slow
@pytest.mark.skipif(not shutil.which("ffmpeg"), reason="ffmpeg no disponible")
def test_render_real_cumple_spec_y_el_loop_cierra(tmp_path):
    # Fondo sintético CON audio, para probar que -an lo saca.
    bg = tmp_path / "bg.mp4"
    subprocess.run(
        ["ffmpeg", "-y", "-f", "lavfi", "-i",
         "testsrc2=size=1280x720:rate=24:duration=4",
         "-f", "lavfi", "-i", "sine=frequency=440:duration=4",
         "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest",
         str(bg), "-loglevel", "error"], check=True)

    out = pipeline.generate_canvas(str(bg), str(tmp_path))

    w, h, pix, nframes = _ffprobe(
        out, "-select_streams", "v", "-show_entries",
        "stream=width,height,pix_fmt,nb_frames").split(",")
    assert (int(w), int(h)) == (1080, 1920)
    assert pix == "yuv420p", "yuvj420p está deprecado y se ve lavado en algunos players"
    assert int(nframes) == int(pipeline.CANVAS_SECONDS * pipeline.CANVAS_FPS)

    dur = float(_ffprobe(out, "-show_entries", "format=duration"))
    assert dur <= 8.0 + 1e-3, f"Spotify rechaza >8s, salió {dur}"

    audio = _ffprobe(out, "-select_streams", "a", "-show_entries", "stream=index")
    assert audio == "", "Spotify rechaza un Canvas con pista de audio"

    # El loop: la diferencia entre el último frame y el primero tiene que ser
    # MENOR que la de dos frames vecinos cualquiera. Si el palíndromo se
    # rompiera (el bug del -t), acá el salto sería enorme.
    from PIL import Image, ImageChops

    def frame(n):
        p = tmp_path / f"f{n}.png"
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", out,
                        "-vf", f"select=eq(n\\,{n})", "-vframes", "1", str(p)],
                       check=True)
        return Image.open(p).convert("L")

    def mad(a, b):
        hist = ImageChops.difference(a, b).histogram()
        return sum(i * c for i, c in enumerate(hist)) / sum(hist)

    ultimo = int(pipeline.CANVAS_SECONDS * pipeline.CANVAS_FPS) - 1
    salto_loop = mad(frame(ultimo), frame(0))
    vecinos = mad(frame(120), frame(121))
    assert salto_loop < max(vecinos, 0.5), (
        f"el loop tiene costura: salto={salto_loop:.2f} vs vecinos={vecinos:.2f}")


def test_cleanup_barre_la_unidad_si_el_worker_muere(tmp_path):
    """El `finally` de generate_canvas cubre el fallo normal, pero un worker
    muerto ENTRE las dos pasadas dejaría el intermedio en disco."""
    huerfano = tmp_path / "canvas_unit_canvas.mp4"
    huerfano.write_bytes(b"x")
    entregable = tmp_path / "canvas.mp4"
    entregable.write_bytes(b"x")
    pipeline._cleanup_local_intermediates(str(tmp_path))
    assert not huerfano.exists(), "el intermedio tiene que barrerse"
    assert entregable.exists(), "el entregable NUNCA se toca"


# --------------------------------------------------------------------------
# Gate admin-only (decisión de producto 27-ago-2026)
# --------------------------------------------------------------------------

class _U:
    def __init__(self, role, tenant_id="universal_music", billing_group="universal_music"):
        self.role = role
        self.tenant_id = tenant_id
        self.billing_group = billing_group


def test_solo_admin_tiene_canvas():
    import auth
    assert auth.has_canvas_access(_U("admin")) is True
    assert auth.has_canvas_access(_U("user")) is False
    assert auth.has_canvas_access(_U("operator")) is False
    assert auth.has_canvas_access(None) is False
    # Forma dict (así viaja current_user en los endpoints).
    assert auth.has_canvas_access({"role": "admin"}) is True
    assert auth.has_canvas_access({"role": "user"}) is False
    assert auth.has_canvas_access({}) is False


def test_ninguna_env_var_abre_el_canvas(monkeypatch):
    """A diferencia de art_track, el Canvas NO tiene válvula de escape: abrirlo
    tiene que ser un cambio de código revisado, no una variable de entorno."""
    import auth
    for var in ("CANVAS_GLOBALLY_ENABLED", "CANVAS_ENABLED_TENANTS",
                "ART_TRACK_GLOBALLY_ENABLED", "ART_TRACK_ENABLED_TENANTS"):
        monkeypatch.setenv(var, "1")
    monkeypatch.setenv("CANVAS_ENABLED_TENANTS", "universal_music")
    assert auth.has_canvas_access(_U("user")) is False
    # ...y art_track SÍ se abre, para probar que el test mide lo que cree.
    assert auth.has_art_track_access(_U("user")) is True


def test_job_owner_is_admin_es_fail_closed(monkeypatch):
    """Si no se puede resolver el rol, NO se genera el Canvas. Perder un
    accesorio no le hace nada a la entrega; filtrarlo sí."""
    def boom():
        raise RuntimeError("db caída")
    monkeypatch.setattr(pipeline, "SessionLocal", boom, raising=False)
    import database
    monkeypatch.setattr(database, "SessionLocal", boom)
    assert pipeline._job_owner_is_admin("cualquiera") is False


def test_el_zip_de_download_all_no_incluye_el_canvas():
    """Un zip se reenvía por mail. El Canvas es admin-only y no puede viajar
    dentro de un bundle que termine en manos del cliente."""
    import main
    assert "canvas" not in main._BUNDLE_TYPES


def test_endpoints_403_para_no_admin():
    import main
    from fastapi import HTTPException
    # No admin pidiendo canvas → 403.
    for user in ({"role": "user"}, {"role": "operator"}, None):
        with pytest.raises(HTTPException) as ei:
            main._require_canvas_access("canvas", user)
        assert ei.value.status_code == 403
    # Admin pasa.
    main._require_canvas_access("canvas", {"role": "admin"})
    # Y el gate NO toca ningún otro entregable.
    for ft in ("video", "short", "thumbnail", "umg_master"):
        main._require_canvas_access(ft, {"role": "user"})


# --------------------------------------------------------------------------
# Variantes (3 encuadres del mismo fondo)
# --------------------------------------------------------------------------

def test_nombres_de_variante_y_compatibilidad_hacia_atras():
    # La variante 1 conserva el nombre pelado: renombrarla dejaría huérfanos
    # los s3_keys de los jobs ya renderizados.
    assert pipeline.canvas_file_type(1) == "canvas"
    assert pipeline.canvas_file_type(2) == "canvas_v2"
    assert pipeline.CANVAS_FILE_TYPES == ("canvas", "canvas_v2", "canvas_v3")
    assert len(pipeline.CANVAS_FILE_TYPES) == pipeline.CANVAS_VARIANTS


def test_todas_las_variantes_estan_registradas():
    import main
    for ft in pipeline.CANVAS_FILE_TYPES:
        assert pipeline._DELIVERABLE_FILENAMES[ft] == f"{ft}.mp4"
        assert pipeline._ACCESSORY_ARTIFACTS[ft] == f"{ft}.mp4"
        assert ft not in pipeline._CRITICAL_DELIVERABLES
        assert main.FILE_MAP[ft] == f"{ft}.mp4"
        assert main.MEDIA_TYPES[ft] == "video/mp4"
        assert ft not in main._BUNDLE_TYPES


def test_todas_las_variantes_estan_gateadas_a_admin():
    """Un 403 que sólo cubriera `canvas` dejaría `canvas_v2` abierto."""
    import main
    from fastapi import HTTPException
    for ft in pipeline.CANVAS_FILE_TYPES:
        with pytest.raises(HTTPException) as ei:
            main._require_canvas_access(ft, {"role": "user"})
        assert ei.value.status_code == 403
        main._require_canvas_access(ft, {"role": "admin"})


@pytest.mark.parametrize("variant,anchor", [(1, "0.5"), (2, "0.15"), (3, "0.85")])
def test_cada_variante_recorta_en_otra_posicion(tmp_path, monkeypatch, variant, anchor):
    cmds = _capture(monkeypatch)
    src = tmp_path / "bg.mp4"
    src.write_bytes(b"fake")
    pipeline.generate_canvas(str(src), str(tmp_path), variant=variant)
    graph = cmds[0][cmds[0].index("-filter_complex") + 1]
    assert f"(in_w-out_w)*{anchor}" in graph


def test_variante_invalida_falla(tmp_path):
    src = tmp_path / "bg.mp4"
    src.write_bytes(b"fake")
    for bad in (0, 4, -1, 99):
        with pytest.raises(ValueError, match="variante"):
            pipeline.generate_canvas(str(src), str(tmp_path), variant=bad)


def test_cada_variante_escribe_su_propio_archivo(tmp_path, monkeypatch):
    """Si dos variantes compartieran nombre, la última pisaría a la anterior."""
    cmds = _capture(monkeypatch)
    src = tmp_path / "bg.mp4"
    src.write_bytes(b"fake")
    salidas = []
    for v in range(1, pipeline.CANVAS_VARIANTS + 1):
        cmds.clear()
        salidas.append(os.path.basename(cmds_out := pipeline.generate_canvas(
            str(src), str(tmp_path), variant=v)))
        assert cmds_out.endswith(f"{pipeline.canvas_file_type(v)}.mp4")
    assert len(set(salidas)) == pipeline.CANVAS_VARIANTS


@pytest.mark.slow
@pytest.mark.skipif(not shutil.which("ffmpeg"), reason="ffmpeg no disponible")
def test_render_real_las_variantes_son_visualmente_distintas(tmp_path):
    """El valor de las variantes es poder ROTAR el Canvas durante la campaña.
    Si las tres se vieran casi igual, la feature no existe."""
    from PIL import Image, ImageChops
    bg = tmp_path / "bg.mp4"
    subprocess.run(
        ["ffmpeg", "-y", "-f", "lavfi", "-i",
         "testsrc2=size=1920x1080:rate=24:duration=4",
         "-c:v", "libx264", "-pix_fmt", "yuv420p", str(bg), "-loglevel", "error"],
        check=True)

    frames = {}
    for v in range(1, pipeline.CANVAS_VARIANTS + 1):
        out = pipeline.generate_canvas(str(bg), str(tmp_path), variant=v)
        png = tmp_path / f"v{v}.png"
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-i", out,
                        "-vf", "select=eq(n\\,60)", "-vframes", "1", str(png)],
                       check=True)
        frames[v] = Image.open(png).convert("L")

    def mad(a, b):
        hist = ImageChops.difference(frames[a], frames[b]).histogram()
        return sum(i * c for i, c in enumerate(hist)) / sum(hist)

    for a, b in ((1, 2), (1, 3), (2, 3)):
        d = mad(a, b)
        assert d > 8, f"v{a} y v{b} son casi el mismo plano (diferencia {d:.1f})"
