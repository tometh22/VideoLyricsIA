"""Filtro de líneas fantasma por consenso acústico.

PROBLEMA
--------
En canciones con secciones largas de ad-lib ("uh uh uh…"), el ASR alucina
texto donde no lo hay: "¿Para qué?" o "Tus santos de papel" incrustados en
medio del "uh" que sí se canta. La letra de referencia no distingue ese
texto (es texto real de la canción, en el momento equivocado), y ninguna
regla estructural local lo separa de una línea legítima (medido 04/07:
"Tus santos de papel" @85s fantasma vs "Tomás del miedo tu don" @102s real
son estructuralmente idénticas — ambas contenido, tras un bloque de uh).

CONSENSO (la señal que sí separa, validada 24/24 en Amanda Pujó)
----------------------------------------------------------------
Para cada línea SOSPECHOSA (adyacente a una zona de ad-lib), balancear:
  1. ACÚSTICA — transcribir su ventana sobre el stem de VOZ aislada.
     El fantasma cae en silencio/uh → whisper devuelve vacío o basura
     no relacionada ("Subtítulos realizados por…"); la línea real cae
     sobre canto → whisper devuelve algo del texto.
  2. FONÉTICA — comparar lo oído vs el texto asignado por similitud de
     caracteres (no de palabras): "Más que el miedo tu don" ~ "Tomás del
     miedo tu don" (0.74, real) aunque whisper no clave las palabras.
  3. ESTRUCTURA — una línea cuyo texto se repite como coro en la canción
     está protegida (whisper alucina en clips cortos de coro; la
     repetición es la evidencia de que es real).

Una línea es fantasma sii falla la acústica+fonética Y no tiene respaldo
estructural. SOLO se evalúan candidatas (junto a ad-libs): una canción
sin ad-libs no tiene candidatas → el filtro es un no-op → cero regresión
por construcción.

CONTRATO
--------
- `find_candidates(segs)` / `is_adlib_text` / `is_phantom` / `is_chorus`:
  puras, unit-testeables.
- `filter_and_collapse(segs, transcribe_window)`: orquesta. `transcribe_window(
  start, end) -> str` se inyecta (el caller la ata al stem) para que la
  lógica sea testeable sin I/O. Colapsa runs de ad-lib en una línea y
  descarta las fantasmas confirmadas. Devuelve lista nueva; never raises
  (ante error de transcripción, conserva la línea — el filtro nunca borra
  por las dudas).
"""

from __future__ import annotations

import logging
import re
import unicodedata
from collections import Counter
from difflib import SequenceMatcher

logger = logging.getLogger("genly.adlib_consensus")

# Vocalizaciones no-léxicas que las letras omiten y el ASR fragmenta.
_ADLIB = {"uh", "eh", "ah", "oh", "mmm", "mm", "na", "la", "oo", "uu",
          "ooh", "ay", "ha", "ho", "wo", "woh", "uuh", "ahh", "ohh"}

# Umbrales (validados en Amanda Pujó, 04/07). Env-tuneables desde el caller.
PHON_MIN = 0.35      # similitud fonética por debajo de la cual el audio NO confirma
HEARD_MIN_CHARS = 4  # menos que esto = whisper no oyó nada sustancial


def _fold(t: str) -> str:
    t = unicodedata.normalize("NFD", (t or "").lower())
    return "".join(c for c in t if unicodedata.category(c) != "Mn")


def _words(t: str) -> list[str]:
    return [w for w in re.sub(r"[^a-z0-9ñ ]", " ", _fold(t)).split() if w]


def _compact(t: str) -> str:
    return re.sub(r"[^a-z0-9ñ]", "", _fold(t))


def _norm_key(t: str) -> str:
    return " ".join(_words(t))


def is_adlib_text(text: str) -> bool:
    """La línea es puro ad-lib (solo 'uh'/'ah'/… repetido)."""
    w = _words(text)
    return bool(w) and all(x in _ADLIB for x in w)


def phonetic(heard: str, assigned: str) -> float:
    """Similitud de caracteres entre lo oído y el texto asignado (0..1)."""
    h, a = _compact(heard), _compact(assigned)
    if not h or not a:
        return 0.0
    return SequenceMatcher(None, h, a).ratio()


def chorus_keys(segs: list[dict], min_reps: int = 2) -> set:
    """Textos (normalizados) que se repiten >= min_reps veces = coros."""
    c = Counter(_norm_key(s.get("text", "")) for s in segs
                if not is_adlib_text(s.get("text", "")))
    return {k for k, n in c.items() if k and n >= min_reps}


def is_phantom(assigned_text: str, heard_text: str, protected: bool,
               *, phon_min: float = PHON_MIN,
               heard_min: int = HEARD_MIN_CHARS) -> bool:
    """¿La línea es una alucinación incrustada? Pura.

    protected: la línea tiene respaldo estructural (es un coro repetido)
               → nunca se descarta, aunque el clip de audio no la confirme
               (whisper alucina en clips cortos de coro).
    """
    if protected:
        return False
    if not _compact(assigned_text):
        return False
    h = _compact(heard_text)
    if len(h) < heard_min:
        return True                       # el audio no dice nada sustancial
    return phonetic(heard_text, assigned_text) < phon_min


def find_candidates(segs: list[dict]) -> list[int]:
    """Índices de líneas de CONTENIDO adyacentes a un ad-lib.

    Solo estas se someten al chequeo acústico (costo whisper). Una canción
    sin ad-libs → sin candidatas → filtro no-op → cero regresión."""
    tags = [is_adlib_text(s.get("text", "")) for s in segs]
    out = []
    for i in range(len(segs)):
        if tags[i]:
            continue
        prev_ad = i > 0 and tags[i - 1]
        next_ad = i < len(segs) - 1 and tags[i + 1]
        if prev_ad or next_ad:
            out.append(i)
    return out


def _collapse_runs(segs: list[dict], drop: set) -> list[dict]:
    """Salta las fantasmas y funde runs de ad-lib consecutivos en una línea."""
    tags = [is_adlib_text(s.get("text", "")) for s in segs]
    kept = [i for i in range(len(segs)) if i not in drop]
    out: list[dict] = []
    j = 0
    while j < len(kept):
        k = kept[j]
        if tags[k]:
            run = [k]
            j += 1
            while j < len(kept) and tags[kept[j]]:
                run.append(kept[j])
                j += 1
            a, b = segs[run[0]], segs[run[-1]]
            out.append({**a, "end": b.get("end", a.get("end")), "text": "Uh, uh, uh…"})
        else:
            out.append(segs[k])
            j += 1
    return out


def filter_and_collapse(segs: list[dict], transcribe_window,
                        *, phon_min: float = PHON_MIN) -> list[dict]:
    """Descarta líneas fantasma (consenso acústico) y colapsa ad-libs.

    `transcribe_window(start, end) -> str`: transcribe esa ventana del stem
    de voz. Inyectada para testeo. Si devuelve None / lanza, la candidata
    se conserva (nunca borrar por las dudas). Never raises."""
    if not segs:
        return segs
    try:
        cands = find_candidates(segs)
        if not cands:
            # sin ad-libs: solo colapsar (no hay nada que colapsar tampoco) y
            # devolver tal cual — no-op garantizado.
            return list(segs)
        choruses = chorus_keys(segs)
        drop = set()
        for i in cands:
            s = segs[i]
            protected = _norm_key(s.get("text", "")) in choruses
            if protected:
                continue
            try:
                heard = transcribe_window(float(s.get("start", 0)),
                                          float(s.get("end", 0)))
            except Exception as e:  # pragma: no cover
                logger.warning("[ADLIB] transcribe falló en %.1fs: %s — conservo",
                               s.get("start"), e)
                continue
            if heard is None:
                continue
            if is_phantom(s.get("text", ""), heard, False, phon_min=phon_min):
                drop.add(i)
                logger.info("[ADLIB] fantasma descartado @%.1fs: %r (oído: %r)",
                            s.get("start"), s.get("text", "")[:40], heard[:40])
        return _collapse_runs(segs, drop)
    except Exception as e:  # pragma: no cover — el filtro nunca rompe el render
        logger.warning("[ADLIB] filtro declinó (%s) — segmentos originales", e)
        return list(segs)
