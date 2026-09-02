"""El loop del fondo tiene que palindromear DE VERDAD.

`_prerender_looped_bg` promete desde siempre un loop sin costura (A + reverse(A))
para que no se vea el "pop" al volver del último frame al primero. No lo
cumplía: ponía el `-t` DESPUÉS del `-i`, o sea como opción de OUTPUT, así que
el input con `-stream_loop -1` nunca daba EOF y el filtro `reverse` —que
necesita EOF para emitir— bufferaba sin límite mientras `concat` dejaba pasar
el segmento sin invertir. Entregaba un loop plano pagando 2,24 GB de RSS a
1080x1920 contra 662 MB del loop plano (framemd5 idéntico entre la rama del
"palíndromo" y su propio fallback).

Verificado sobre el video entregado a UMG Chile el 2026-08-21: en una frontera
de loop la diferencia media entre frames contiguos era 60,1 contra 35,2 a mitad
de ciclo — el corte existía y era medible.

Estos tests usan una rampa de luminancia porque hace la diferencia obvia:
un palíndromo real da una onda TRIANGULAR (sube y baja), un loop plano da
diente de sierra.
"""
import os
import subprocess
import sys

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


def _rampa(path, dur, size=128):
    """Clip cuya luminancia sube linealmente de 0 a 255 en `dur` segundos."""
    subprocess.run(
        ["ffmpeg", "-y", "-v", "error", "-f", "lavfi",
         "-i", f"color=c=black:s={size}x{size}:r=24:d={dur}",
         "-vf", f"geq=lum='(T/{dur})*255':cb=128:cr=128",
         "-pix_fmt", "yuv420p", path],
        check=True, capture_output=True, timeout=180,
    )
    return path


def _luminancia(path):
    raw = subprocess.run(
        ["ffmpeg", "-v", "error", "-i", path, "-vf", "scale=1:1",
         "-f", "rawvideo", "-pix_fmt", "gray", "-"],
        capture_output=True, timeout=180,
    ).stdout
    return list(raw)


def _salto_maximo(lum):
    """El pop, medido: el mayor salto de brillo entre frames contiguos.

    Es LA magnitud que importa y la única que discrimina de verdad. Una primera
    versión de estos tests miraba dónde caía el pico del ciclo y era VACUA: con
    el código roto el ciclo dura la mitad, así que el pico caía igual dentro de
    la ventana esperada. El salto no se puede fingir — sobre una rampa, el loop
    plano da 255 y el palíndromo da ~0.
    """
    return max(abs(lum[i + 1] - lum[i]) for i in range(len(lum) - 1))


@needs_ffmpeg
def test_clip_corto_produce_un_palindromo_real(tmp_path):
    src = _rampa(str(tmp_path / "ramp.mp4"), 4)
    out = pipeline._prerender_looped_bg(
        src, 24.0, str(tmp_path), target_w=128, target_h=128,
        out_name="bg_looped_t.mp4",
    )
    lum = _luminancia(out)
    assert len(lum) > 500, "24s @24fps"

    # Sobre una rampa, el código roto daba saltos de 255 en cada vuelta.
    salto = _salto_maximo(lum)
    assert salto < 40, (
        f"salto máximo entre frames = {salto}: eso es el pop de un loop plano, "
        "no un palíndromo (el código roto daba 255)"
    )
    # Y que efectivamente vaya y vuelva, no que sea un clip constante.
    assert max(lum) - min(lum) > 180, "el fondo tiene que recorrer la rampa"
    assert lum.index(max(lum)) > 40, "sube antes de bajar"


@needs_ffmpeg
def test_el_empalme_entre_ciclos_no_salta(tmp_path):
    """El punto entero del palíndromo: que el último frame de una pasada
    coincida con el primero de la siguiente."""
    src = _rampa(str(tmp_path / "ramp.mp4"), 4)
    out = pipeline._prerender_looped_bg(
        src, 24.0, str(tmp_path), target_w=128, target_h=128,
        out_name="bg_looped_t.mp4",
    )
    lum = _luminancia(out)
    # Unidad = 4s ida + 4s vuelta = 8s = 192 frames.
    salto_empalme = abs(lum[192] - lum[191])
    salto_normal = abs(lum[101] - lum[100])
    assert salto_empalme <= max(4, salto_normal), (
        f"salto en el empalme={salto_empalme} vs normal={salto_normal} — "
        "eso es exactamente el pop que este helper existe para eliminar "
        "(el código roto daba 255 acá)"
    )


@needs_ffmpeg
def test_clip_largo_cae_al_loop_plano(tmp_path):
    """`reverse` bufferea el clip ENTERO descomprimido: ~107 MB por segundo de
    clip a 720p. Palindromear un fondo largo se comería el worker, y encima no
    aporta (ya no repite lo suficiente como para que el corte moleste)."""
    src = _rampa(str(tmp_path / "long.mp4"), 20)
    assert 20 > pipeline._PALINDROME_MAX_CLIP_S
    out = pipeline._prerender_looped_bg(
        src, 40.0, str(tmp_path), target_w=128, target_h=128,
        out_name="bg_looped_l.mp4",
    )
    lum = _luminancia(out)
    assert _salto_maximo(lum) > 150, (
        "por encima del tope se espera loop plano (con su corte); si no salta, "
        "el guardrail de memoria no está funcionando"
    )


@needs_ffmpeg
def test_la_salida_decodifica_y_dura_lo_pedido(tmp_path):
    src = _rampa(str(tmp_path / "ramp.mp4"), 4)
    out = pipeline._prerender_looped_bg(
        src, 24.0, str(tmp_path), target_w=128, target_h=128,
        out_name="bg_looped_t.mp4",
    )
    assert pipeline._decodes_ok(out)
    dur = pipeline._ffprobe_duration(out)
    assert dur is not None and dur >= 24.0 - 0.1


@needs_ffmpeg
def test_no_deja_el_intermedio_tirado(tmp_path):
    """El palíndromo unidad es un archivo aparte. Si queda huérfano en cada
    render exitoso son decenas de MB por job — la cascada de disco que el
    propio módulo documenta."""
    src = _rampa(str(tmp_path / "ramp.mp4"), 4)
    pipeline._prerender_looped_bg(
        src, 12.0, str(tmp_path), target_w=128, target_h=128,
        out_name="bg_looped_t.mp4",
    )
    huerfanos = [f for f in os.listdir(str(tmp_path)) if "palindrome_unit" in f]
    assert huerfanos == [], f"quedó basura: {huerfanos}"


def test_el_input_del_reverse_esta_acotado():
    """La regresión concreta: `reverse` NUNCA puede colgar de un
    `-stream_loop -1`, porque sin EOF bufferea hasta quedarse sin RAM."""
    import inspect
    src = inspect.getsource(pipeline._prerender_looped_bg)
    fin = src.index('label="ffmpeg-palindrome-unit"')
    # Acotar al run_checked( que envuelve a esa etapa, no a los 900 chars
    # previos: _flat_loop está definido más arriba y SÍ usa -stream_loop.
    inicio = src.rindex("run_checked(", 0, fin)
    etapa = src[inicio:fin]
    assert "reverse" in etapa, "esta es la etapa que invierte"
    assert "-stream_loop" not in etapa, (
        "la etapa que invierte no puede recibir un input infinito: sin EOF, "
        "`reverse` bufferea hasta quedarse sin RAM y nunca emite"
    )


def test_el_intermedio_lo_barre_la_limpieza():
    """Cinturón: además del unlink explícito, el nombre tiene que matchear el
    glob de _cleanup_local_intermediates por si el worker muere en el medio."""
    import inspect
    src = inspect.getsource(pipeline._prerender_looped_bg)
    assert 'f"bg_looped_palindrome_unit_{out_name}"' in src
    limpieza = inspect.getsource(pipeline._cleanup_local_intermediates)
    assert 'startswith("bg_looped_")' in limpieza
