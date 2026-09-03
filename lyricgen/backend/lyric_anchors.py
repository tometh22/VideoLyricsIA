"""Anclas de la letra para el fondo "Inspirado en la letra".

POR QUÉ EXISTE ESTE MÓDULO
--------------------------
Hasta ahora el modo "Inspirado en la letra" resolvía todo en UNA sola llamada a
Gemini: leer ~1800 caracteres de letra transcrita (ruidosa, en español), obedecer
un system prompt de varios miles de palabras (reglas duras, ejemplos few-shot,
guías de género, cláusula de cámara, anti-cliché, negativos) y devolver un prompt
de 80-120 palabras. La letra entraba ÚLTIMA en el mensaje de usuario y perdía
contra todo lo demás.

Medición sobre la base de staging (269 prompts entregados en modo letra, 60 días):

    golden hour / dusk / twilight / sunset ..... 59%  (159/269)
    mist / fog / haze .......................... 29%  ( 77/269)
    callejón (alley/graffiti/wet pavement) ..... 14,5% ( 39/269)
    dust motes ................................. 6,7% ( 18/269)

Ese 59% aparece IGUAL en modo Auto, que ni siquiera mira la letra: el modo que
debería derivar el fondo de la canción producía la misma hora del día que el que
la ignora. Las palabras salen literalmente del bloque de ejemplos del system
prompt (pipeline._EXAMPLES_BLOCK) y de los ejemplos trabajados del anti-cliché.

La respuesta es separar LEER de COMPONER. Este módulo cubre el lado de leer:
una tarea de extracción chica, a temperatura baja, sin estilo ni cámara ni
reglas de seguridad, que devuelve sustantivos concretos que están de verdad en
la letra. Cada ancla viaja con la línea de la que salió, así que se puede
verificar SIN LLM que exista (`verify_anchors`) y se puede medir SIN LLM si el
prompt final la usó (`anchor_coverage`). Esa métrica es la que reemplaza al gate
de calidad viejo, que en 204 mediciones nunca disparó porque comparaba el frame
contra el prompt que el propio sistema había generado.

DISEÑO DE AISLAMIENTO (mismo criterio que scenes.py): acá no se importa nada de
Gemini ni de la pipeline. Todo es puro y testeable offline; `pipeline.py` inyecta
la llamada al proveedor y usa `build_extraction_request` / `parse_anchors`.
"""

from __future__ import annotations

import json
import os
import re
import unicodedata
from typing import Any

# ── Flag de rollout ────────────────────────────────────────────────────────
# Deliberadamente NO cuelga de BACKGROUND_SMOKE_POLICY_MODE. Son dos políticas
# distintas, y acoplarlas es exactamente lo que dejó sin usar la mejora previa:
# la rama `enforce` ya había reemplazado los ejemplos que sangran por un esquema
# sin contenido ("examples were being copied as recurring smoke/alley/bed
# motifs"), pero staging corre en `shadow`, así que esa mejora nunca se aplicó.
ANCHORS_ENV = "BG_LYRIC_ANCHORS"
VALID_ANCHOR_MODES = frozenset({"off", "shadow", "on"})

# Tope de anclas que se le piden al extractor. Más de esto y el compositor
# empieza a hacer una lista de compras en vez de una escena.
MAX_OBJECT_ANCHORS = 10
# Piso de objetos que el prompt final tiene que usar para considerarse anclado.
# 4 sobre ~6-8 extraídas deja aire para que el compositor descarte las que no
# funcionan visualmente, sin permitir que las ignore a todas.
MIN_ANCHOR_COVERAGE = 4


def anchors_mode(env: dict[str, str] | None = None) -> str:
    """Devuelve ``off``, ``shadow`` u ``on``; cualquier otro valor cae a ``off``.

    ``off`` es el default de despliegue para que mergear el código no cambie
    ninguna salida antes de que api/Worker/ShortWorker estén en el mismo commit.
    """
    source = os.environ if env is None else env
    value = str(source.get(ANCHORS_ENV, "off") or "off").strip().lower()
    return value if value in VALID_ANCHOR_MODES else "off"


def anchors_enabled(mode: str | None = None) -> bool:
    """True cuando las anclas deben CAMBIAR la salida."""
    return (mode or anchors_mode()) == "on"


def anchors_observed(mode: str | None = None) -> bool:
    """True cuando hay que calcular y loguear sin tocar la salida."""
    return (mode or anchors_mode()) in {"shadow", "on"}


# ── Normalización compartida ───────────────────────────────────────────────
# Se usa para dos cosas distintas que tienen que coincidir: verificar que un
# ancla existe en la letra, y medir si el prompt final la usó. Si divergieran,
# la cobertura mentiría.
_PUNCT_RE = re.compile(r"[^\w\s]", re.UNICODE)
_SPACE_RE = re.compile(r"\s+")


def normalize(text: str | None) -> str:
    """Minúsculas, sin tildes, sin puntuación, espacios colapsados.

    Sacar las tildes es lo que permite que "corazón" en la letra matchee
    "corazon" en un prompt que el modelo escribió sin acentos, y al revés.
    """
    value = unicodedata.normalize("NFD", str(text or ""))
    value = "".join(ch for ch in value if unicodedata.category(ch) != "Mn")
    value = _PUNCT_RE.sub(" ", value.lower())
    return _SPACE_RE.sub(" ", value).strip()


# ── Extracción: el prompt del paso 1 ───────────────────────────────────────
# Chico y sin estilo A PROPÓSITO. Todo lo que se agregue acá (cámara, paleta,
# género, seguridad) vuelve a ser un prior que compite con la letra, que es el
# problema que este módulo existe para resolver. Si algo suena a dirección de
# arte, va en el compositor, no acá.
EXTRACTION_SYSTEM_PROMPT = """Sos un lector de letras. Tu única tarea es extraer lo que la canción DICE.

No inventás, no interpretás de más, no proponés estética. Si la letra no lo dice
ni lo implica con fuerza, devolvés null o lista vacía. Es mejor devolver poco y
verdadero que mucho y verosímil.

Respondé SOLO con un objeto JSON con exactamente estas claves:

  "lugar":      string o null. Un lugar CONCRETO que la canción nombra o implica
                sin ambigüedad. Si nombra un lugar real (una ciudad, una plaza,
                un barrio, un río), usá su nombre propio. Si sólo implica un tipo
                de lugar ("la cocina", "la ruta"), describilo en pocas palabras.
                null si la letra es abstracta y no ancla en ningún lado.
  "linea_lugar": string o null. La línea EXACTA de la letra de donde sale el lugar.
  "objetos":    array de 5 a 10 objetos. Cada uno {"objeto": "...", "linea": "..."}.
                "objeto" es un sustantivo CONCRETO Y FILMABLE: una silla, una
                botella, un teléfono, la lluvia, una bandera, un colectivo.

                EXCLUÍ SIEMPRE, aunque estén en la letra (la escena final no
                puede mostrarlos y contarlos como ancla sólo ensucia la medición):
                  · personas, nombres propios de personas y partes del cuerpo
                    (piel, ojos, manos, brazos, pupilas, carne, garras)
                  · marcas, apps, logos y todo lo que se vea como texto legible
                    (Instagram, Coca-Cola, un cartel con letras)
                  · abstracciones y cosas sin forma fija (el vacío, la herida,
                    el alma, un ángel, el cielo, las estrellas, el destino)
                Si la letra habla de una persona o de una marca, extraé en su
                lugar el OBJETO o el LUGAR que la rodea —lo que dejó, dónde
                estaba, qué la acompaña— que sí se puede filmar.

                "linea" es la línea exacta de la letra donde aparece. Preferí
                lo que la canción menciona literalmente.
  "situacion":  string. Una frase: qué está pasando, o qué acaba de pasar, en la
                canción. Es la narrativa, no el clima emocional.
  "registro":   string. El tono emocional, usando el vocabulario de la canción
                misma, no categorías genéricas.
  "epoca":      string o null. Época, hora del día o estación SÓLO si la letra la
                da. null si no la da. No la inventes: la mitad de los fondos de
                este sistema terminaban al atardecer porque alguien lo asumía.

Cada "linea" tiene que ser texto que está en la letra que te paso, copiado tal
cual. Las anclas cuyas líneas no aparezcan en la letra se descartan
automáticamente, así que copiar mal es peor que devolver menos."""


def build_extraction_request(
    artist: str = "",
    song_title: str = "",
    lyrics_text: str = "",
    *,
    max_lyrics_chars: int = 4000,
) -> str:
    """Arma el mensaje de usuario del extractor.

    La letra va COMPLETA (hasta 4000 caracteres, más del doble que los 1800 del
    compositor viejo): acá no compite con nada, es lo único que hay que leer.
    """
    parts = []
    if (artist or "").strip():
        parts.append(f"Artista: {artist.strip()}")
    if (song_title or "").strip():
        parts.append(f"Título: {song_title.strip()}")
    head = "\n".join(parts)
    body = (lyrics_text or "").strip()[:max_lyrics_chars]
    if not body:
        body = "[sin letra disponible]"
    return f"{head}\n\nLetra:\n{body}".strip()


def _coerce_object_anchor(item: Any) -> dict[str, str] | None:
    """Acepta tanto {"objeto","linea"} como un string suelto."""
    if isinstance(item, str):
        name = item.strip()
        return {"objeto": name, "linea": ""} if name else None
    if isinstance(item, dict):
        name = str(item.get("objeto") or item.get("object") or "").strip()
        if not name:
            return None
        return {"objeto": name, "linea": str(item.get("linea") or item.get("line") or "").strip()}
    return None


def parse_anchors(text: str | None) -> dict[str, Any] | None:
    """Parsea la respuesta del extractor. Devuelve None si no hay nada usable.

    Tolerante igual que _parse_gemini_bg_response: el modelo a veces envuelve el
    JSON en un fence de markdown aunque se le pida lo contrario.
    """
    raw = (text or "").strip()
    if not raw:
        return None
    if raw.startswith("```"):
        raw = re.sub(r"^```(?:json)?\s*", "", raw)
        raw = re.sub(r"\s*```$", "", raw).strip()
    match = re.search(r"\{.*\}", raw, re.DOTALL)
    if not match:
        return None
    try:
        data = json.loads(match.group())
    except json.JSONDecodeError:
        return None
    if not isinstance(data, dict):
        return None

    objetos: list[dict[str, str]] = []
    seen: set[str] = set()
    for item in (data.get("objetos") or [])[: MAX_OBJECT_ANCHORS * 2]:
        anchor = _coerce_object_anchor(item)
        if not anchor:
            continue
        # Dedup por TOKENS DE CONTENIDO, no por forma normalizada a secas: si se
        # usaran los artículos, "la bandera" y "Bandera" contarían como dos
        # anclas distintas e inflarían el denominador de la cobertura, dejando
        # pasar un prompt que sólo usó una. Es la misma base que usa
        # `anchor_coverage`, y tienen que coincidir o la métrica miente.
        key = " ".join(_content_tokens(anchor["objeto"]))
        if not key or key in seen:
            continue
        seen.add(key)
        objetos.append(anchor)
        if len(objetos) >= MAX_OBJECT_ANCHORS:
            break

    def _s(key: str) -> str:
        value = data.get(key)
        return str(value).strip() if isinstance(value, (str, int, float)) else ""

    parsed = {
        "lugar": _s("lugar") or None,
        "linea_lugar": _s("linea_lugar") or None,
        "objetos": objetos,
        "situacion": _s("situacion"),
        "registro": _s("registro"),
        "epoca": _s("epoca") or None,
    }
    # Sin lugar y sin objetos no hay ancla: que el caller caiga al motor viejo
    # en vez de inyectar un bloque vacío que sólo agrega ruido al prompt.
    if not parsed["lugar"] and not parsed["objetos"]:
        return None
    return parsed


def verify_anchors(anchors: dict[str, Any] | None, lyrics_text: str) -> dict[str, Any] | None:
    """Descarta las anclas cuya línea de origen NO está en la letra.

    Este es el chequeo que hace que el paso 1 sea verificable sin un segundo
    LLM: el extractor tiene que citar, y la cita se comprueba contra el texto.
    Un ancla sin `linea` se conserva (el modelo a veces la omite y el objeto
    igual es correcto), pero una con una `linea` INVENTADA se tira — ése es el
    modo de falla que importa.
    """
    if not anchors:
        return None
    haystack = normalize(lyrics_text)
    if not haystack:
        return anchors

    def _cited(line: str | None) -> bool:
        needle = normalize(line)
        if not needle:
            return True  # sin cita: se conserva, no se puede refutar
        return needle in haystack

    kept = [
        a for a in anchors.get("objetos", [])
        if _cited(a.get("linea")) and is_renderable(a.get("objeto", ""))
    ]
    result = dict(anchors)
    result["objetos"] = kept
    if anchors.get("lugar") and not _cited(anchors.get("linea_lugar")):
        # El lugar es el ancla más fuerte del prompt; si la cita no se verifica,
        # se cae a null antes que arrastrar una alucinación a toda la escena.
        result["lugar"] = None
        result["linea_lugar"] = None
    if not result["lugar"] and not result["objetos"]:
        return None
    return result


# Cinturón local sobre la exclusión que ya pide el prompt del extractor. Medido
# sobre 12 canciones reales de staging: el 24% de las anclas extraídas eran cosas
# que los rieles del propio pipeline prohíben dibujar (partes del cuerpo, personas,
# marcas, abstracciones). Contarlas infla el denominador de la cobertura, hace que
# el compositor "falle" por obedecer compliance, y dispara re-rolls que cuestan
# plata y no arreglan nada. Caso testigo: Luciano Pereyra "Eres Perfecta" quedó
# 1/7 porque 6 de sus 7 anclas eran piel, estrellas, Instagram, perfil, Cristóbal
# Colón y doctor.
_UNRENDERABLE_TERMS = frozenset("""
piel ojos ojo pupilas manos mano brazos brazo carne garras cuerpo cuerpos cara
caras rostro rostros cabeza cabezas dedos labios boca pelo sangre corazon alma
espiritu angel angeles dios diablo fantasma
vacio nada destino tiempo suerte miedo amor odio dolor herida heridas sueno
suenos recuerdo recuerdos silencio verdad mentira vida muerte libertad
cielo estrellas estrella luna sol universo infinito
gente personas persona nadie alguien todos
""".split())


def is_renderable(term: str) -> bool:
    """False para anclas que la escena final tiene prohibido mostrar.

    Se evalúa sobre los tokens de contenido: "las estrellas" y "estrellas" caen
    igual, y "una estrella de mar" NO cae (tiene un token de contenido que no
    está en la lista, así que no es un match total).
    """
    tokens = _content_tokens(term)
    if not tokens:
        return False
    return not all(tok in _UNRENDERABLE_TERMS for tok in tokens)


def anchor_terms(anchors: dict[str, Any] | None) -> list[str]:
    """Términos contra los que se mide la cobertura: el lugar y los objetos."""
    if not anchors:
        return []
    terms = []
    if anchors.get("lugar"):
        terms.append(str(anchors["lugar"]))
    terms.extend(str(a.get("objeto") or "") for a in anchors.get("objetos", []))
    return [t for t in terms if t.strip()]


# Palabras vacías que no deben contar como "match" por sí solas: si un ancla es
# "la ruta", el match tiene que venir de "ruta", no del artículo.
_STOPWORDS = frozenset(
    "el la los las un una unos unas de del al a en con por para y o u lo su sus "
    "mi mis tu tus se le les que the a an of in on at and or to".split()
)


def _content_tokens(term: str) -> list[str]:
    return [t for t in normalize(term).split() if t and t not in _STOPWORDS]


def anchor_coverage(prompt: str | None, anchors: dict[str, Any] | None) -> dict[str, Any]:
    """Cuántas anclas aparecen en el prompt final. Sin LLM, sin costo.

    Un ancla cuenta como usada cuando TODOS sus tokens de contenido aparecen en
    el prompt normalizado. Es deliberadamente laxo con el orden y las palabras
    intercaladas ("botella" matchea "una botella de vidrio vacía") y estricto
    con la ausencia: si el objeto no está, no está.
    """
    terms = anchor_terms(anchors)
    normalized_prompt = normalize(prompt)
    hits: list[str] = []
    misses: list[str] = []
    for term in terms:
        tokens = _content_tokens(term)
        if tokens and all(tok in normalized_prompt for tok in tokens):
            hits.append(term)
        else:
            misses.append(term)
    total = len(terms)
    return {
        "hits": hits,
        "misses": misses,
        "covered": len(hits),
        "total": total,
        "ratio": (len(hits) / total) if total else 0.0,
    }


def coverage_is_sufficient(coverage: dict[str, Any] | None,
                           minimum: int = MIN_ANCHOR_COVERAGE) -> bool:
    """True si el prompt usó suficientes anclas como para aceptarlo.

    Cuando hay MENOS anclas que el mínimo (canción muy abstracta), se exige que
    estén todas: pedir 4 de 2 haría re-rollear para siempre.
    """
    if not coverage:
        return True
    total = int(coverage.get("total") or 0)
    if total == 0:
        return True
    return int(coverage.get("covered") or 0) >= min(minimum, total)


# ── Lugares urbanos: gate del negativo anti-callejón ───────────────────────
# El riel `no_alley` de _generate_veo_video existe porque el modelo caía en el
# callejón noir ~80% de las veces con genre=rock. Pero cuando la LETRA pide una
# calle, ese riel pelea contra la canción. Con anclas verificadas ya se puede
# distinguir un caso del otro.
_URBAN_TERMS = (
    "calle", "avenida", "esquina", "barrio", "vereda", "ciudad", "plaza",
    "puerto", "estacion", "subte", "colectivo", "semaforo", "asfalto",
    "edificio", "balcon", "bar", "boliche", "cancha", "estadio", "obelisco",
    "street", "avenue", "corner", "city", "square", "downtown", "sidewalk",
    "alley", "callejon",
)


def has_urban_anchor(anchors: dict[str, Any] | None) -> bool:
    """True si la letra ancla en un entorno urbano.

    El caller usa esto para NO aplicar el negativo anti-callejón cuando la
    canción efectivamente transcurre en la calle.
    """
    blob = normalize(" ".join(anchor_terms(anchors) + [str((anchors or {}).get("situacion") or "")]))
    if not blob:
        return False
    tokens = set(blob.split())
    return any(term in tokens for term in _URBAN_TERMS)


# ── Bloque de restricción que entra al compositor ──────────────────────────
def anchors_constraint_block(anchors: dict[str, Any] | None,
                             minimum: int = MIN_ANCHOR_COVERAGE) -> str:
    """El bloque que va ARRIBA DE TODO en el mensaje de usuario del compositor.

    Arriba a propósito: en el motor viejo la letra iba última, etiquetada
    "Lyrics (may be incomplete or noisy)", y era la señal más débil del
    contexto. Acá las anclas son lo primero que el modelo lee y están redactadas
    como restricción, no como sugerencia.
    """
    if not anchors:
        return ""
    lines = ["[ANCLAS DE LA LETRA — RESTRICCIÓN, NO SUGERENCIA]"]
    if anchors.get("lugar"):
        lines.append(f"LUGAR (la escena transcurre acá): {anchors['lugar']}")
    objetos = anchors.get("objetos") or []
    if objetos:
        lines.append("OBJETOS que la canción nombra (usá al menos "
                     f"{min(minimum, len(objetos))} de estos, literalmente):")
        for anchor in objetos:
            lines.append(f"  - {anchor['objeto']}")
    if anchors.get("situacion"):
        lines.append(f"SITUACIÓN: {anchors['situacion']}")
    if anchors.get("registro"):
        lines.append(f"REGISTRO: {anchors['registro']}")
    if anchors.get("epoca"):
        lines.append(f"ÉPOCA / MOMENTO: {anchors['epoca']}")
    else:
        # Sin esta línea el modelo pone "golden hour" por default: 59% de los
        # fondos medidos en staging terminaban al atardecer, la misma tasa que
        # en modo Auto (que ni mira la letra).
        lines.append("ÉPOCA / MOMENTO: la letra no la da — elegí la que pida la "
                     "escena y NO uses atardecer/golden hour por descarte.")
    lines.append(
        "Estas anclas mandan sobre género, concepto y sobre cualquier escena "
        "que te resulte familiar. Si una no funciona visualmente, descartala; "
        "no las reemplaces todas por una escena genérica."
    )
    return "\n".join(lines) + "\n\n"
