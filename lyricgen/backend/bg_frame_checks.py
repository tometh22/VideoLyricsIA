"""Chequeos determinísticos sobre el fondo entregado: geometría y luz.

POR QUÉ EXISTE
--------------
Dos de las reglas de aceptación del fondo se pedían SÓLO en el prompt y no se
verificaban nunca sobre el archivo resultante:

  · "16:9 full screen, sin franjas negras"
  · "iluminación estable durante toda la canción; si es sunset, mantener el
     sunset; evitar pasar de día a noche"

Los negativos del prompt no alcanzan para ninguna de las dos. El bloque
anti-letterbox de `_generate_veo_video` se agregó justamente porque Veo horneaba
barras 2.39:1 igual, y el comentario de ese incidente (Spinetta, 2026-07-07) dice
que el fallo era ESTOCÁSTICO — "un video sí y otro no". Un negativo que falla a
veces necesita una medición, no más palabras.

Lo mismo del lado de la luz: el fondo único está a salvo por construcción (el
clip de 4-8s se loopea en palíndromo, así que la luz vuelve siempre al punto de
partida y un día→noche es imposible), pero multi-escena genera cada escena por
separado y sólo comparte una `palette` blanda vía la biblia. Nada impedía que el
verso saliera a mediodía y el coro al atardecer.

Este módulo es PURO a propósito (mismo criterio que scenes.py y lyric_anchors.py):
acá viven el parseo y los umbrales, testeables sin ffmpeg ni GPU; `pipeline.py`
pone las llamadas a ffmpeg/PIL. Ninguna de las dos medidas bloquea un render:
son fail-open y sólo alimentan el re-roll que ya existe o un aviso.
"""

from __future__ import annotations

import os
import re
from typing import Any, Iterable, Sequence

# ── Geometría ──────────────────────────────────────────────────────────────
# Fracción del alto (o ancho) que se puede perder antes de llamarlo franja. Veo
# a veces deja 1-2 px de borde por redondeo de escalado; eso no es letterbox.
# 2% de 1080 son ~22 px: por debajo de eso no se ve como barra.
LETTERBOX_TOLERANCE = float(os.environ.get("BG_LETTERBOX_TOLERANCE", "0.02"))

_CROP_RE = re.compile(r"crop=(\d+):(\d+):(-?\d+):(-?\d+)")


def parse_cropdetect(text: str | None) -> tuple[int, int, int, int] | None:
    """Último `crop=W:H:X:Y` que emitió el filtro cropdetect de ffmpeg.

    Se toma el ÚLTIMO y no el primero: cropdetect va refinando su estimación
    a medida que ve frames, y el primero suele salir de un fundido de entrada
    negro que haría ver barras donde no las hay.
    """
    matches = _CROP_RE.findall(text or "")
    if not matches:
        return None
    w, h, x, y = matches[-1]
    return int(w), int(h), int(x), int(y)


def letterbox_report(
    crop: tuple[int, int, int, int] | None,
    width: int,
    height: int,
    *,
    tolerance: float = LETTERBOX_TOLERANCE,
) -> dict[str, Any]:
    """Cuánto del frame se pierde en barras. `has_bars` es la decisión.

    Distingue barras HORIZONTALES (arriba/abajo, el letterbox clásico 2.39:1)
    de VERTICALES (pillarbox), porque son síntomas distintos: la primera es Veo
    imitando cine, la segunda es un aspect ratio mal escalado.
    """
    if not crop or width <= 0 or height <= 0:
        return {"has_bars": False, "reason": "sin medición",
                "top_bottom": 0.0, "left_right": 0.0, "symmetric": False}
    cw, ch, cx, cy = crop
    top_bottom = max(0.0, (height - ch) / height)
    left_right = max(0.0, (width - cw) / width)

    # SIMETRÍA — el guardrail contra el falso positivo que importa. cropdetect
    # recorta lo que ve "negro" (limit=24), y una escena nocturna con cielo
    # oscuro arriba o asfalto en sombra abajo lo dispara igual que un letterbox.
    # Re-rollear ahí quema un Veo por una escena que está perfecta.
    #
    # Un letterbox real es SIMÉTRICO por construcción: la barra de arriba mide
    # lo mismo que la de abajo, porque el encoder centra la imagen. Un cielo
    # oscuro no. Se exige que el offset esté a menos de un cuarto de la barra
    # total de su posición centrada.
    def _is_centered(offset: int, full: int, kept: int) -> bool:
        band = full - kept
        if band <= 0:
            return False
        return abs(offset - band / 2.0) <= max(2.0, band * 0.25)

    tb_symmetric = _is_centered(cy, height, ch)
    lr_symmetric = _is_centered(cx, width, cw)

    has_tb = top_bottom > tolerance and tb_symmetric
    has_lr = left_right > tolerance and lr_symmetric
    parts = []
    if has_tb:
        parts.append(f"barras horizontales {100 * top_bottom:.1f}% del alto")
    if has_lr:
        parts.append(f"barras verticales {100 * left_right:.1f}% del ancho")
    if not parts and (top_bottom > tolerance or left_right > tolerance):
        # Se recortó algo pero no está centrado: es contenido oscuro, no barras.
        parts.append("recorte asimétrico (contenido oscuro, no letterbox)")
    return {
        "has_bars": has_tb or has_lr,
        "reason": " + ".join(parts) or "frame completo",
        "top_bottom": top_bottom,
        "left_right": left_right,
        "symmetric": tb_symmetric or lr_symmetric,
        "crop": crop,
    }


# ── Luz ────────────────────────────────────────────────────────────────────
# Salto de luminancia (0-255) entre dos escenas contiguas que ya se lee como
# "cambió la hora del día". Un día→noche real salta 100+; 45 deja pasar la
# variación normal entre un plano abierto y uno cerrado del mismo momento.
LIGHT_MAX_DELTA = float(os.environ.get("SCENES_LIGHT_MAX_DELTA", "45"))
# Salto de calidez (R−B). Un mediodía frío contra un atardecer ámbar se separan
# bastante más que 40 aunque la luminancia sea parecida (interior iluminado vs
# exterior nublado), así que va como segunda señal independiente.
WARMTH_MAX_DELTA = float(os.environ.get("SCENES_WARMTH_MAX_DELTA", "40"))


def luminance(pixels: Iterable[Sequence[float]]) -> float:
    """Luminancia perceptual media (Rec. 709) de una secuencia de píxeles RGB."""
    total = 0.0
    count = 0
    for px in pixels:
        r, g, b = px[0], px[1], px[2]
        total += 0.2126 * r + 0.7152 * g + 0.0722 * b
        count += 1
    return (total / count) if count else 0.0


def warmth(pixels: Iterable[Sequence[float]]) -> float:
    """Calidez media (R−B). Positivo = ámbar/atardecer, negativo = azul/noche."""
    total = 0.0
    count = 0
    for px in pixels:
        total += px[0] - px[2]
        count += 1
    return (total / count) if count else 0.0


def light_signature(pixels: Iterable[Sequence[float]]) -> dict[str, float]:
    """Firma de luz de un frame. Se guarda por escena y se compara entre pares."""
    pixels = list(pixels)
    return {"luminance": luminance(pixels), "warmth": warmth(pixels)}


def lighting_consistency(
    signatures: Sequence[dict[str, Any]],
    *,
    max_delta: float = LIGHT_MAX_DELTA,
    max_warmth_delta: float = WARMTH_MAX_DELTA,
) -> dict[str, Any]:
    """Compara la luz entre escenas CONTIGUAS en el orden en que se ven.

    Contiguas y no contra el promedio: lo que el ojo registra como error es el
    salto en el corte. Una canción puede oscurecerse de a poco de principio a
    fin sin que se lea mal; lo que rompe es que el coro pegue un salto respecto
    del verso que lo precede.

    Cada firma es {"key": str, "luminance": float, "warmth": float}. Devuelve el
    peor par y la lista de los que cruzan el umbral.
    """
    usable = [s for s in signatures if s and s.get("luminance") is not None]
    if len(usable) < 2:
        return {"consistent": True, "worst_delta": 0.0, "worst_pair": None,
                "offenders": [], "scenes": len(usable)}
    offenders = []
    worst = 0.0
    worst_pair = None
    for prev, cur in zip(usable, usable[1:]):
        d_lum = abs(float(cur["luminance"]) - float(prev["luminance"]))
        d_warm = abs(float(cur.get("warmth") or 0.0) - float(prev.get("warmth") or 0.0))
        pair = (prev.get("key"), cur.get("key"))
        if d_lum > worst:
            worst, worst_pair = d_lum, pair
        if d_lum > max_delta or d_warm > max_warmth_delta:
            offenders.append({
                "pair": pair,
                "luminance_delta": round(d_lum, 1),
                "warmth_delta": round(d_warm, 1),
            })
    return {
        "consistent": not offenders,
        "worst_delta": round(worst, 1),
        "worst_pair": worst_pair,
        "offenders": offenders,
        "scenes": len(usable),
    }


def shared_light_directive(light: str | None) -> str:
    """Línea que TODAS las escenas heredan para no cambiar la hora del día.

    La biblia ya comparte `palette`, pero una paleta no fija un momento: dos
    escenas pueden compartir "azules fríos y ámbar" y una estar al mediodía y la
    otra de noche. Esto lo dice explícito y en cada escena.
    """
    value = (light or "").strip()
    if not value:
        return ""
    return (
        f"LUZ COMPARTIDA POR TODAS LAS ESCENAS (no la cambies): {value}. "
        "Todas las escenas de este video transcurren en EL MISMO momento del día "
        "y con el mismo estado de luz. No pases de día a noche ni de noche a día "
        "entre escenas, y no cambies la dirección ni la temperatura de la luz."
    )
