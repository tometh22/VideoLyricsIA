"""Clasificación de errores de jobs en categorías estables.

El dashboard de actividad del admin necesita responder "¿de qué tipo son
los errores de este usuario?" sin que el operador lea mensajes uno por
uno. Este módulo es la única fuente de verdad de esa taxonomía:

  - Los sinks de error (pipeline.run_*_pipeline, reaper) llaman a
    classify_error() y persisten el resultado en jobs.error_category.
  - /admin/activity usa el mismo clasificador a lectura para las rows
    históricas que no tienen la columna poblada (coalesce).

Mantener las categorías cortas y estables: son una API para el frontend
(chips del tab Actividad) y para queries de operación. Agregar una
categoría nueva = agregar el patrón acá + el label en AdminPanel.jsx.

Categorías:
  validation  — content policy / validación UMG (el video no debería existir)
  upload      — R2 / transferencia de archivos (infra de storage)
  veo         — Veo / Imagen / Vertex (generación de fondos con IA)
  render      — ffmpeg / moviepy / verificación del mp4 final
  timing      — Whisper / alineación / segments (timing de la letra)
  reaper      — el reaper mató un job colgado (worker muerto / OOM / stall)
  timeout     — timeout genérico no atribuible a Veo
  unknown     — todo lo demás (revisar y promover a categoría si se repite)
"""
import re

# Orden = prioridad. First match wins. El orden importa:
#   - "Content policy violation" debe caer en validation aunque mencione a Veo.
#   - "Failed to fetch background from R2" es upload (infra), no veo.
#   - "Veo 3 operation timed out" es veo (más accionable que timeout genérico).
#   - El mensaje del reaper ("se interrumpió por un problema temporal...")
#     va antes que timeout para no caer en la categoría genérica.
_RULES = (
    ("validation", re.compile(
        r"content policy|umg validation|umg delivery requested without|moderation",
        re.IGNORECASE)),
    ("upload", re.compile(
        r"(failed to fetch|could not download).*(from )?r2|r2 upload|upload.*(failed|interrupted)"
        r"|source audio not available",
        re.IGNORECASE)),
    ("veo", re.compile(
        r"\bveo\b|\bimagen 4\b|vertex|no background available",
        re.IGNORECASE)),
    ("render", re.compile(
        r"ffprobe|ffmpeg|moviepy|rendered (mp4|file)|faststart|verify:"
        r"|cannot load background|no (video|audio) stream|truncated",
        re.IGNORECASE)),
    ("timing", re.compile(
        r"whisper|align|transcri|segments|timing|lyric",
        re.IGNORECASE)),
    ("reaper", re.compile(
        r"se interrumpi[oó] por un problema temporal|stuck|stalled|reaped|sigkill"
        r"|out of memory|memoryerror|worker.*(died|killed|gone)",
        re.IGNORECASE)),
    ("timeout", re.compile(
        r"timed? ?out|timeout",
        re.IGNORECASE)),
)

UNKNOWN = "unknown"

# Categorías válidas, exportadas para que tests y frontend validen contra
# la misma lista.
CATEGORIES = tuple(rule[0] for rule in _RULES) + (UNKNOWN,)


def classify_error(error_text, status=None):
    """Clasifica un mensaje de error de job en una categoría estable.

    Args:
        error_text: contenido de jobs.error (puede ser None / vacío).
        status: status del job, hoy no se usa pero queda en la firma para
            poder desambiguar por status en el futuro sin tocar call sites.

    Returns:
        str: una de CATEGORIES. Nunca lanza.
    """
    if not error_text:
        return UNKNOWN
    text = str(error_text)
    for category, pattern in _RULES:
        if pattern.search(text):
            return category
    return UNKNOWN
