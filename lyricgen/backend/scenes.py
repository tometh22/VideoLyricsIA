"""Multi-escena ("Escenas") — add-on premium.

Hoy un video lyric usa UN solo fondo: el pipeline genera un clip Veo y lo
repite con loop palíndromo para toda la canción (mismo plano 3-4 min). Esta
feature lo convierte en un CONJUNTO DE ESCENAS con arco narrativo.

El riesgo es que se vea random/barato (N fondos cortados al azar). Todo este
módulo está diseñado para lo contrario — tres principios anti-random:

  1. UNA biblia visual única → toda escena la hereda → parecen el mismo film.
  2. Cortes en límites MUSICALES (entra el coro, hueco instrumental, puente),
     nunca a mitad de frase. Snap al inicio de línea.
  3. Rima estructural: el coro vuelve SIEMPRE a la misma escena (se genera 1
     vez y se reusa) → estructura + costo más bajo. Los versos progresan.

Diseño de aislamiento: este módulo NO importa los internals de Gemini de
pipeline.py. La detección de secciones es 100% DETERMINISTA (testeable sin
LLM ni GPU). La construcción de prompts de escena se hace vía una callable
`prompt_fn` inyectada por el caller (pipeline pasa su `_get_unique_prompt`),
y el loop por sección reusa `_prerender_looped_bg` con import perezoso para
evitar import circular. Eso mantiene `scenes.py` unit-testeable offline.
"""

from __future__ import annotations

import logging
import os
import re
import subprocess
from dataclasses import dataclass, field, asdict
from typing import Callable, Optional

logger = logging.getLogger(__name__)

# ── Guardarraíles anti-random (el corazón de que quede BUENO) ──────────────
# Ningún corte más corto que esto: cuts rápidos = videoclip random, no premium.
MIN_SCENE_DURATION = float(os.environ.get("SCENES_MIN_DURATION", "14.0"))
# Tope de escenas ÚNICAS por canción (costo Veo + coherencia visual). Los
# coros recurrentes comparten escena, así que esto acota los versos.
MAX_UNIQUE_SCENES = int(os.environ.get("SCENES_MAX_UNIQUE", "6"))
# Hueco sin letra (segundos) que cuenta como sección instrumental (intro/
# puente/breakdown/outro) — buen punto de corte de escena.
GAP_THRESHOLD = float(os.environ.get("SCENES_GAP_THRESHOLD", "4.0"))
# Duración del crossfade entre escenas (segundos). Set chico de transiciones:
# xfade default + fadeblack al entrar al puente.
XFADE_DURATION = float(os.environ.get("SCENES_XFADE", "0.8"))

# Energía heurística por tipo de sección (0..1). Maneja cámara/movimiento de
# la escena. Un LLM puede refinar esto luego; el default es musicalmente sano.
_ENERGY_BY_TYPE = {
    "intro": 0.25,
    "verso": 0.50,
    "pre": 0.65,
    "coro": 0.85,
    "puente": 0.40,
    "outro": 0.30,
}


@dataclass
class Section:
    """Un tramo temporal de la canción con su tipo musical."""
    type: str            # intro | verso | pre | coro | puente | outro
    start: float
    end: float
    energy: float = 0.5
    # Escenas con la misma recurrence_key comparten el MISMO clip (rima
    # estructural + se genera 1 sola vez). Los coros idénticos comparten key.
    recurrence_key: str = ""
    text: str = ""       # letra del tramo (vacío si es instrumental)

    @property
    def duration(self) -> float:
        return max(0.0, self.end - self.start)

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "Section":
        """Reconstruye una Section desde su forma serializada en scene_plan
        (ignora claves extra como `duration`, que es property, no campo).

        Tolera planes malformados/truncados (audit LOW): aplica defaults para
        campos faltantes en vez de TypeError — coherente con el resto del módulo
        que es best-effort y nunca tumba el job."""
        d = d or {}
        return cls(
            type=str(d.get("type") or "verso"),
            start=float(d.get("start") or 0.0),
            end=float(d.get("end") or 0.0),
            energy=float(d.get("energy") if d.get("energy") is not None else 0.5),
            recurrence_key=str(d.get("recurrence_key") or ""),
            text=str(d.get("text") or ""),
        )


def sections_from_plan(plan: dict) -> list["Section"]:
    """Lista de Section a partir de plan["sections"] (para re-stitch en edits)."""
    return [Section.from_dict(s) for s in (plan or {}).get("sections", [])]


# ── Normalización de texto para detectar repeticiones de coro ──────────────
_WORD_RE = re.compile(r"[^\wáéíóúñü\s]", re.UNICODE)


def _norm_line(text: str) -> str:
    """Normaliza una línea para comparar repeticiones (case/puntuación-insensible)."""
    t = (text or "").strip().lower()
    t = _WORD_RE.sub("", t)
    t = re.sub(r"\s+", " ", t)
    return t.strip()


def detect_sections(segments: list[dict], audio_duration: float) -> list[Section]:
    """Detecta secciones (verso/coro/puente/intro/outro) de forma DETERMINISTA.

    Señales (las que ya tenemos, sin audio analysis):
      - letra repetida  → coro (líneas cuyo texto normalizado aparece ≥2 veces)
      - hueco sin letra → intro/puente/breakdown/outro (gap > GAP_THRESHOLD)
      - timing de líneas → límites de corte (snap al inicio de línea)

    Devuelve secciones contiguas que cubren [0, audio_duration], ya fusionadas
    para respetar MIN_SCENE_DURATION y con recurrence_key asignada (coros
    idénticos comparten key → comparten escena).
    """
    segs = [s for s in (segments or []) if (s.get("text") or "").strip()]
    if not segs:
        # Sin letra timeada no hay nada que segmentar — una sola escena.
        return [Section(type="verso", start=0.0, end=float(audio_duration or 0.0),
                        energy=0.5, recurrence_key="scene_0")]
    segs = sorted(segs, key=lambda s: float(s["start"]))

    # 1) Contar repeticiones de cada línea normalizada → marca coros.
    counts: dict[str, int] = {}
    for s in segs:
        n = _norm_line(s["text"])
        if n:
            counts[n] = counts.get(n, 0) + 1

    # 2) Construir bloques vocales: agrupar líneas consecutivas por "recurrente"
    #    (texto que se repite) vs "única". Un cambio recurrente↔único es un
    #    límite de sección (entra/sale el coro).
    @dataclass
    class _Block:
        start: float
        end: float
        recurrent: bool
        norm_text: str  # texto normalizado concatenado (clave de recurrencia del coro)

    blocks: list[_Block] = []
    for s in segs:
        n = _norm_line(s["text"])
        is_rec = counts.get(n, 0) >= 2 and len(n) > 0
        st, en = float(s["start"]), float(s["end"])
        if blocks and blocks[-1].recurrent == is_rec and (st - blocks[-1].end) <= GAP_THRESHOLD:
            blocks[-1].end = en
            blocks[-1].norm_text += " | " + n
        else:
            blocks.append(_Block(start=st, end=en, recurrent=is_rec, norm_text=n))

    # 3) Materializar secciones cubriendo huecos:
    #    - hueco inicial → intro ; hueco final → outro ; hueco intermedio →
    #      puente (si es largo) o se absorbe.
    sections: list[Section] = []
    verse_idx = 0
    chorus_keys: dict[str, str] = {}   # norm_text del coro → key estable

    # Intro instrumental antes de la primera línea.
    first_start = blocks[0].start
    if first_start >= GAP_THRESHOLD:
        sections.append(Section(type="intro", start=0.0, end=first_start))
    else:
        # Adelantar el primer bloque al 0 para no dejar un tramo huérfano corto.
        blocks[0].start = 0.0

    for i, b in enumerate(blocks):
        # ¿Hueco instrumental ANTES de este bloque (y después del anterior)?
        if sections and b.start - sections[-1].end >= GAP_THRESHOLD:
            gap_start = sections[-1].end
            # Hueco intermedio largo = puente/breakdown.
            sections.append(Section(type="puente", start=gap_start, end=b.start))

        if b.recurrent:
            key = chorus_keys.get(b.norm_text)
            if key is None:
                key = f"coro_{len(chorus_keys) + 1}"
                chorus_keys[b.norm_text] = key
            sections.append(Section(type="coro", start=b.start, end=b.end,
                                    recurrence_key=key, text=b.norm_text))
        else:
            verse_idx += 1
            sections.append(Section(type="verso", start=b.start, end=b.end,
                                    recurrence_key=f"verso_{verse_idx}", text=b.norm_text))

    # Outro instrumental después de la última línea.
    last_end = sections[-1].end if sections else 0.0
    if audio_duration and (audio_duration - last_end) >= GAP_THRESHOLD:
        sections.append(Section(type="outro", start=last_end, end=float(audio_duration)))
    elif sections:
        # Estirar la última sección hasta el final (sin dejar cola).
        sections[-1].end = float(audio_duration or sections[-1].end)

    # 4) Energía + recurrence_key para instrumentales (intro/puente/outro
    #    comparten escena por tipo → menos generaciones).
    for sec in sections:
        sec.energy = _ENERGY_BY_TYPE.get(sec.type, 0.5)
        if not sec.recurrence_key:
            sec.recurrence_key = sec.type  # intro/puente/outro: 1 escena por tipo

    # 5) Fusionar secciones más cortas que MIN_SCENE_DURATION (anti-corte-rápido).
    sections = _merge_short_sections(sections)

    # 6) Cap de escenas únicas (costo + coherencia): si hay más de
    #    MAX_UNIQUE_SCENES claves distintas, colapsar versos para compartir.
    sections = _cap_unique_scenes(sections)

    return sections


# Precedencia de identidad al fusionar: el coro SIEMPRE gana (su escena
# recurrente es lo que da estructura), luego pre/verso, luego instrumentales.
# Cuando dos secciones se funden, la de mayor rango impone type + recurrence_key.
_IDENTITY_RANK = {"coro": 3, "pre": 2, "verso": 2, "puente": 1, "intro": 0, "outro": 0}


# Tipos vocales: tienen letra propia y su escena ES contenido (vs los
# instrumentales, que son bookends). Preservar su identidad es lo que mantiene
# la progresión de versos y evita que el coro "suene" sobre un verso.
_VOCAL_TYPES = {"verso", "pre", "coro"}


def _soft_min(sec: Section) -> float:
    """Umbral BLANDO: el objetivo anti-corte-frecuente. Por debajo CONVIENE
    fusionar, pero (para secciones vocales) sólo si no hay que borrar su
    identidad. Los instrumentales son bookends y toleran ser más cortos."""
    if sec.type in ("intro", "outro", "puente"):
        return max(6.0, MIN_SCENE_DURATION / 2.0)
    return MIN_SCENE_DURATION


def _hard_min(sec: Section) -> float:
    """Piso DURO: por debajo, cortar ahí se ve frenético y la fusión es
    OBLIGATORIA (un fragmento de pocos segundos). Escala con MIN_SCENE_DURATION
    para que el cap de escenas (que monkeypatchea MIN) siga funcionando.

    Un verso por ENCIMA de este piso pero por debajo del objetivo blando se
    respeta como su propia escena (no lo absorbe el coro) — ése es el corazón
    del fix: 4/7·MIN ≈ 8s con el default 14s. `min(soft, …)` garantiza que el
    piso duro nunca supere al blando aunque MIN sea muy chico."""
    if sec.type in ("intro", "outro", "puente"):
        return min(_soft_min(sec), max(4.0, MIN_SCENE_DURATION / 4.0))
    return min(_soft_min(sec), MIN_SCENE_DURATION * 4.0 / 7.0)


def _effective_min(sec: Section) -> float:
    """Mínimo EFECTIVO garantizado tras la fusión = el piso duro. Una sección
    puede quedar por debajo del objetivo blando (un verso corto que preservamos
    como su propia escena), pero nunca por debajo del piso duro."""
    return _hard_min(sec)


def _merge_into(winner: Section, loser: Section) -> Section:
    """Funde `loser` dentro de `winner` (unión de límites). La identidad
    (type, recurrence_key, energy) la impone la sección de mayor rango — así
    un coro que absorbe un huequito sigue siendo el coro recurrente, y no se
    pierde la rima estructural."""
    keep_winner = _IDENTITY_RANK.get(winner.type, 1) >= _IDENTITY_RANK.get(loser.type, 1)
    idn = winner if keep_winner else loser
    start = min(winner.start, loser.start)
    end = max(winner.end, loser.end)
    winner.start, winner.end = start, end
    winner.type = idn.type
    winner.recurrence_key = idn.recurrence_key
    winner.energy = idn.energy
    winner.text = (winner.text + " " + loser.text).strip()
    return winner


def _rank_energy_target(sections: list[Section], i: int) -> Optional[int]:
    """Vecino de fusión 'clásico': mayor rango de identidad; a igualdad, el de
    energía más parecida. Usado para fusiones duras y para instrumentales."""
    sec = sections[i]
    prev = sections[i - 1] if i > 0 else None
    nxt = sections[i + 1] if i < len(sections) - 1 else None
    if prev and nxt:
        rp, rn = _IDENTITY_RANK.get(prev.type, 1), _IDENTITY_RANK.get(nxt.type, 1)
        if rp != rn:
            return i - 1 if rp > rn else i + 1
        return i - 1 if abs(prev.energy - sec.energy) <= abs(nxt.energy - sec.energy) else i + 1
    if prev:
        return i - 1
    if nxt:
        return i + 1
    return None


def _choose_merge_target(sections: list[Section], i: int, preserve_vocal: bool) -> Optional[int]:
    """Elige el vecino al que fundir la sección i, o None si NO conviene fundir.

    `preserve_vocal=False` (fusión dura): comportamiento clásico — al vecino de
    mayor rango. La sección es tan corta que cortar ahí sería frenético.

    `preserve_vocal=True` (fusión blanda): si la sección es vocal y distinta,
    evitamos borrar su identidad. Orden de preferencia:
      1) vecino con la MISMA recurrence_key (es un fragmento del mismo tramo);
      2) absorber un INSTRUMENTAL adyacente hacia el verso (el verso gana
         identidad por rango → la escena del verso se preserva y crece);
      3) si ambos vecinos son vocales de otra identidad (p.ej. coros), NO
         fusionar — mejor un verso un poco corto que el coro tapándolo.
    """
    sec = sections[i]
    if not preserve_vocal or sec.type not in _VOCAL_TYPES:
        return _rank_energy_target(sections, i)

    prev = sections[i - 1] if i > 0 else None
    nxt = sections[i + 1] if i < len(sections) - 1 else None
    # 1) Mismo tramo partido en dos → fundir sin perder nada.
    if prev and prev.recurrence_key == sec.recurrence_key:
        return i - 1
    if nxt and nxt.recurrence_key == sec.recurrence_key:
        return i + 1
    # 2) Absorber un instrumental adyacente (el verso conserva su identidad).
    _INSTR = ("intro", "puente", "outro")
    prev_instr = prev is not None and prev.type in _INSTR
    nxt_instr = nxt is not None and nxt.type in _INSTR
    if prev_instr and nxt_instr:
        # el instrumental más corto, para no comerse un puente largo a propósito
        return i - 1 if prev.duration <= nxt.duration else i + 1
    if prev_instr:
        return i - 1
    if nxt_instr:
        return i + 1
    # 3) Rodeado de vocales de otra identidad → dejar el verso como su escena.
    return None


def _run_merges(sections: list[Section], threshold_fn, *, preserve_vocal: bool) -> list[Section]:
    """Fusiona tramos por debajo de `threshold_fn`, del más corto hacia arriba.

    Con `preserve_vocal`, una sección vocal que no se puede fundir sin borrar su
    identidad se marca y se deja como está (no entra en loop infinito)."""
    skip: set[int] = set()
    changed = True
    while changed and len(sections) > 1:
        changed = False
        shortest = None
        for i, sec in enumerate(sections):
            if id(sec) in skip:
                continue
            if sec.duration < threshold_fn(sec):
                if shortest is None or sec.duration < sections[shortest].duration:
                    shortest = i
        if shortest is None:
            break
        i = shortest
        target_i = _choose_merge_target(sections, i, preserve_vocal)
        if target_i is None:
            # No conviene fundir (verso entre coros): se queda como su escena.
            skip.add(id(sections[i]))
            continue
        winner_i = min(i, target_i)
        loser_i = max(i, target_i)
        _merge_into(sections[winner_i], sections[loser_i])
        sections.pop(loser_i)
        changed = True
    return sections


def _merge_short_sections(sections: list[Section]) -> list[Section]:
    """Funde tramos demasiado cortos, en dos pasadas (anti-corte-frenético sin
    aplastar la progresión de versos):

      1) DURA: todo lo que baje del piso duro se fusiona sí o sí (un fragmento
         de pocos segundos es frenético; ahí no protegemos identidad).
      2) BLANDA: lo que está entre el piso duro y el objetivo blando se acerca
         al objetivo SÓLO si no hay que borrar la identidad de un verso —
         si no, se respeta como su propia escena.
    """
    if len(sections) <= 1:
        return sections
    sections = _run_merges(sections, _hard_min, preserve_vocal=False)
    sections = _run_merges(sections, _soft_min, preserve_vocal=True)
    return sections


def _cap_unique_scenes(sections: list[Section]) -> list[Section]:
    """Limita el nº de escenas ÚNICAS a MAX_UNIQUE_SCENES.

    Los coros ya comparten escena. Si los versos hacen pasar el tope, los
    versos extra reusan claves de versos previos (round-robin) — siguen
    progresando dentro del mismo mundo (la biblia), pero acotamos costo Veo.
    """
    order: list[str] = []
    for sec in sections:
        if sec.recurrence_key not in order:
            order.append(sec.recurrence_key)
    if len(order) <= MAX_UNIQUE_SCENES:
        return sections
    # Versos son los candidatos a colapsar (coros/instrumentales se preservan).
    verse_keys = [k for k in order if k.startswith("verso_")]
    keep_verses = max(1, MAX_UNIQUE_SCENES - (len(order) - len(verse_keys)))
    if keep_verses >= len(verse_keys):
        return sections
    canonical = verse_keys[:keep_verses]
    remap = {}
    for idx, k in enumerate(verse_keys):
        remap[k] = canonical[idx % keep_verses]
    for sec in sections:
        if sec.recurrence_key in remap:
            sec.recurrence_key = remap[sec.recurrence_key]
    logger.info("[SCENES] cap escenas únicas: %d→%d (versos colapsados a %d)",
                len(order), MAX_UNIQUE_SCENES, keep_verses)
    return sections


def energy_to_movement(energy: float) -> str:
    """Mapea energía de sección al movement_style existente del pipeline.

    Coro (alta energía) → cámara con movimiento; verso → drift sutil;
    intro/outro (baja) → estático. Reusa el vocabulario que Veo ya entiende.
    """
    if energy >= 0.75:
        return "dinamico"
    if energy >= 0.45:
        return "sutil"
    return "estatico"


def build_scene_plan(
    sections: list[Section],
    bible: dict,
    prompt_fn: Callable[..., dict],
    *,
    artist: str = "",
    song_title: str = "",
    style: str = "",
) -> dict:
    """Construye el storyboard: una escena por recurrence_key única.

    `prompt_fn` es inyectada por el caller (pipeline pasa un wrapper de su
    `_get_unique_prompt`). Recibe el contexto de la escena + la biblia como
    `background_hint` y devuelve {"style","prompt"}. Así toda escena hereda
    la biblia (mundo/paleta/textura/cámara/motivo) → coherencia "mismo film".
    """
    bible_text = _bible_to_prompt_fragment(bible)
    scenes: list[dict] = []
    seen: dict[str, dict] = {}
    for sec in sections:
        key = sec.recurrence_key
        if key in seen:
            continue
        movement = energy_to_movement(sec.energy)
        hint = _scene_hint(bible_text, sec)
        try:
            result = prompt_fn(
                background_hint=hint,
                movement_style=movement,
                section_type=sec.type,
                energy=sec.energy,
            )
            prompt = (result or {}).get("prompt", "") or hint
        except Exception as e:  # noqa: BLE001 — un prompt que falla no debe tumbar el plan
            logger.warning("[SCENES] prompt_fn falló para %s (%s); uso hint", key, e)
            prompt = hint
        scene = {
            "id": key,
            "recurrence_key": key,
            "section_type": sec.type,
            "energy": sec.energy,
            "movement_style": movement,
            "prompt": prompt,
            # cache_token: bust de caché por regen (lo setea el regen).
            "cache_token": "",
            # clip_cache_key: key R2 del clip generado (para GC en regen).
            "clip_cache_key": None,
            "status": "planned",
        }
        seen[key] = scene
        scenes.append(scene)

    return {
        "bible": bible,
        "sections": [s.to_dict() for s in sections],
        "scenes": scenes,
        "params": {
            "min_scene_duration": MIN_SCENE_DURATION,
            "max_unique_scenes": MAX_UNIQUE_SCENES,
            "xfade": XFADE_DURATION,
        },
    }


def _bible_to_prompt_fragment(bible: dict) -> str:
    """Aplana la biblia visual a un fragmento de prompt que toda escena hereda."""
    if not bible:
        return ""
    parts = []
    for k in ("world", "palette", "texture", "camera", "motif"):
        v = (bible.get(k) or "").strip()
        if v:
            parts.append(v)
    return ", ".join(parts)


def _scene_hint(bible_text: str, sec: Section) -> str:
    """Combina la biblia con la variación de esta sección (energía/tipo)."""
    beat = {
        "intro": "opening establishing shot, calm and restrained",
        "verso": "intimate, grounded framing",
        "pre": "building tension, rising energy",
        "coro": "expansive, the emotional peak, more movement and light",
        "puente": "a visual departure, the contrast moment, still within the palette",
        "outro": "winding down, the closing bookend",
    }.get(sec.type, "")
    bits = [b for b in (bible_text, beat) if b]
    return ". ".join(bits)


def stitch_timeline(
    sections: list[Section],
    clip_for_key: dict[str, str],
    audio_duration: float,
    job_dir: str,
    *,
    target_w: int = 1920,
    target_h: int = 1080,
    out_name: str = "bg_scenes_timeline.mp4",
    loop_fn: Optional[Callable[..., str]] = None,
) -> str:
    """Arma un único mp4 del largo de la canción: cada sección loopea su clip a
    su duración y se concatenan con xfade en los límites.

    - `clip_for_key`: recurrence_key → path del clip Veo (los coros comparten).
    - `loop_fn`: por defecto `_prerender_looped_bg` de pipeline (import perezoso).
      Inyectable para tests.

    Transiciones (set chico, anti-random): xfade "fade" por defecto; "fadeblack"
    al ENTRAR a un puente (marca el contraste sin ser abrupto).
    """
    if loop_fn is None:
        from pipeline import _prerender_looped_bg as loop_fn  # import perezoso

    multi = len(sections) > 1
    n = len(sections)
    # xfade SOLAPA clips: cada transición se come `xfade` segundos de la
    # duración total. La cadena de N escenas con xfade rinde naturalmente
    # `Σdur − (N−1)·xfade` → si looperáramos cada escena a su duración exacta
    # el timeline terminaría (N−1)·xfade ANTES que el audio (cola negra/freeze
    # al final). Las escenas intermedias ya tienen el largo justo para su
    # transición; sólo la ÚLTIMA hay que estirarla `(N−1)·xfade` para recuperar
    # todo el déficit acumulado → el total queda EXACTO en Σdur = audio_dur.
    # El `-t audio_dur` de abajo recorta el sobrante por redondeo. Una sola
    # escena no cruza nada → xfade=0.
    xfade = min(XFADE_DURATION, max(0.1, MIN_SCENE_DURATION / 4.0)) if multi else 0.0

    # 1) Loop por sección (reusa el palíndromo seamless). El pad de xfade va
    #    sólo en la última escena (ver nota de arriba).
    section_files: list[str] = []
    for i, sec in enumerate(sections):
        clip = clip_for_key.get(sec.recurrence_key)
        if not clip or not os.path.exists(clip):
            raise FileNotFoundError(
                f"[SCENES] sin clip para recurrence_key={sec.recurrence_key!r} (sección {i})")
        pad = (n - 1) * xfade if (multi and i == n - 1) else 0.0
        seg_path = loop_fn(
            clip, sec.duration + pad, job_dir,
            target_w=target_w, target_h=target_h,
            out_name=f"bg_scene_{i:02d}.mp4",
        )
        section_files.append(seg_path)

    if not multi:
        # Una sola escena: el loop ya cubre toda la canción.
        return section_files[0]

    # 2) Concatenar con xfade. xfade SOLAPA clips: el offset de cada transición
    #    es (suma de duraciones previas) − (xfade acumulado). Construimos la
    #    cadena progresivamente: v0 ⨯ v1 → vx1 ; vx1 ⨯ v2 → vx2 ; ...
    out_path = os.path.join(job_dir, out_name)
    inputs: list[str] = []
    for f in section_files:
        inputs += ["-i", f]

    filters: list[str] = []
    # Normalizar todas las entradas al mismo SAR/tamaño/fps para que xfade no falle.
    labels = []
    for i in range(len(section_files)):
        lbl = f"v{i}"
        filters.append(
            f"[{i}:v]scale={target_w}:{target_h}:force_original_aspect_ratio=increase,"
            f"crop={target_w}:{target_h},setsar=1,format=yuv420p,settb=AVTB[{lbl}]"
        )
        labels.append(lbl)

    prev = labels[0]
    # Clamp ≥0 (audit LOW): si la primera escena fuera más corta que el xfade
    # (no debería tras el merge, pero stitch_timeline es público y testeable),
    # un offset negativo rompe el filter graph de ffmpeg.
    offset = max(0.0, sections[0].duration - xfade)
    for i in range(1, len(section_files)):
        nxt = labels[i]
        out_lbl = f"x{i}" if i < len(section_files) - 1 else "vout"
        # fadeblack al entrar a un puente; fade normal en el resto.
        trans = "fadeblack" if sections[i].type == "puente" else "fade"
        filters.append(
            f"[{prev}][{nxt}]xfade=transition={trans}:duration={xfade:.3f}:"
            f"offset={offset:.3f}[{out_lbl}]"
        )
        prev = out_lbl
        # Cada xfade consume `xfade` segundos de solape.
        offset += sections[i].duration - xfade

    filter_complex = ";".join(filters)
    cmd = [
        "ffmpeg", "-y", "-loglevel", "error",
        *inputs,
        "-filter_complex", filter_complex,
        "-map", "[vout]",
        "-t", str(audio_duration),
        "-c:v", "libx264", "-preset", "fast", "-crf", "20",
        "-pix_fmt", "yuv420p", "-an",
        out_path,
    ]
    try:
        from subprocess_utils import run_checked
        run_checked(cmd, label="ffmpeg-scenes-xfade", timeout=1800, output_path=out_path)
    except ImportError:
        subprocess.run(cmd, check=True, timeout=1800)
    size_mb = os.path.getsize(out_path) / 1024 / 1024 if os.path.exists(out_path) else 0
    logger.info("[SCENES] timeline armado: %d escenas, %.0fs, %.1f MB",
                len(section_files), audio_duration, size_mb)
    return out_path


def extract_thumbnail(video_path: str, out_path: str, at_seconds: float = 1.0,
                      width: int = 320) -> Optional[str]:
    """Extrae un frame del clip de una escena como póster para el filmstrip.

    Best-effort: si falla (clip corto/corrupto), devuelve None y el caller cae a
    un placeholder — un thumb faltante no debe tumbar la generación del video.
    `at_seconds` se clampa para no pedir un frame más allá del fin del clip.
    """
    if not video_path or not os.path.exists(video_path):
        return None
    ss = max(0.0, float(at_seconds))
    cmd = [
        "ffmpeg", "-y", "-loglevel", "error",
        "-ss", f"{ss:.2f}", "-i", video_path,
        "-frames:v", "1",
        "-vf", f"scale={int(width)}:-2",
        "-q:v", "4",
        out_path,
    ]
    try:
        from subprocess_utils import run_checked
        run_checked(cmd, label="ffmpeg-scene-thumb", timeout=60, output_path=out_path)
    except ImportError:
        try:
            subprocess.run(cmd, check=True, timeout=60)
        except Exception:
            return None
    except Exception:
        return None
    return out_path if os.path.exists(out_path) and os.path.getsize(out_path) > 0 else None
