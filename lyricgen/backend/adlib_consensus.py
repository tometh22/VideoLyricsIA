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


# Duración máxima de una línea de ad-lib fundida. Un run largo (p.ej. 38 s
# de "uh" fragmentado en 12 líneas) se parte en bloques de a lo sumo esto —
# un subtítulo único de 38 s es impresentable. Estilo ROTOR: la sección de
# uh queda en 3-5 bloques, no en 1 gigante ni en 12 fragmentos.
MAX_ADLIB_LINE_S = 9.0


def _merge(segs: list[dict], run: list[int]) -> dict:
    """Funde un sub-run de ad-libs en una línea. El texto es el MÁS LARGO
    del sub-run (no un 'Uh…' hardcodeado): 'uh uh uh' queda 'uh uh uh' y un
    'na na na' legítimo queda 'na na na' — une fragmentos, no reescribe."""
    a, b = segs[run[0]], segs[run[-1]]
    merged = {**a, "end": b.get("end", a.get("end"))}
    if len(run) > 1:
        merged["text"] = max((segs[r].get("text", "") for r in run), key=len)
    return merged


def _collapse_runs(segs: list[dict], drop: set) -> list[dict]:
    """Salta las fantasmas y funde runs de ad-lib consecutivos, con tope de
    duración por línea (MAX_ADLIB_LINE_S)."""
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
            # partir el run en sub-bloques de <= MAX_ADLIB_LINE_S, en los
            # límites de las líneas originales.
            sub = [run[0]]
            for r in run[1:]:
                if segs[r].get("end", 0) - segs[sub[0]].get("start", 0) > MAX_ADLIB_LINE_S:
                    out.append(_merge(segs, sub))
                    sub = [r]
                else:
                    sub.append(r)
            out.append(_merge(segs, sub))
        else:
            out.append(segs[k])
            j += 1
    return out


# Margen tras el fin de la última región de voz: una línea que ARRANCA
# después de esto está en la cola muda (el lead-in mueve starts ~0.4s hacia
# antes; 3s lo absorbe con aire de sobra).
TAIL_MARGIN_S = 3.0


def tail_candidates(segs: list[dict], tail_after: float | None) -> set:
    """Índices de líneas en la COLA MUDA: arrancan después del fin del
    último canto detectado (VAD de energía sobre el stem) + margen.

    Caso real (El Riesgo, 05/07): lrclib entregó la letra de OTRA edición
    de la canción, cuyo outro cantado ("Este es el plan / De la, de la
    mariposa") no existe en este audio. El scaffold sincronizado colocó
    esas líneas sobre 76s de música instrumental — whisperX no oyó NADA
    ahí. A diferencia de las candidatas por adyacencia (find_candidates),
    acá se chequea TODO lo que caiga en la cola, ad-libs incluidos, y sin
    protección de coro: el canto fantasma se repite solo DENTRO de la
    zona muda, así que repetirse no es evidencia de nada."""
    if tail_after is None:
        return set()
    out = set()
    for i, s in enumerate(segs):
        try:
            if float(s.get("start", 0)) > tail_after + TAIL_MARGIN_S:
                out.add(i)
        except (TypeError, ValueError):
            continue
    return out


def filter_and_collapse(segs: list[dict], transcribe_window,
                        *, phon_min: float = PHON_MIN,
                        tail_after: float | None = None) -> list[dict]:
    """Descarta líneas fantasma (consenso acústico) y colapsa ad-libs.

    `transcribe_window(start, end) -> str`: transcribe esa ventana del stem
    de voz. Inyectada para testeo. Si devuelve None / lanza, la candidata
    se conserva (nunca borrar por las dudas). Never raises.

    `tail_after`: fin del último canto real (segundos) según el VAD del
    stem, o None. Con un valor, las líneas de la cola muda (start >
    tail_after + TAIL_MARGIN_S) también se verifican acústicamente, SIN
    protección de coro (ver tail_candidates). None = comportamiento
    exacto pre-cola."""
    if not segs:
        return segs
    try:
        tail_idx = tail_candidates(segs, tail_after)
        # No-op verdadero si la canción no tiene NINGÚN ad-lib NI cola muda:
        # nada que colapsar ni candidatas que filtrar → cero regresión.
        if not tail_idx and not any(is_adlib_text(s.get("text", "")) for s in segs):
            return list(segs)
        cands = sorted(set(find_candidates(segs)) | tail_idx)
        # La protección de coro solo se gana en la zona OÍDA: un texto que
        # se repite únicamente dentro de la cola muda no tiene respaldo.
        choruses = chorus_keys([s for i, s in enumerate(segs) if i not in tail_idx])
        drop = set()
        for i in cands:
            s = segs[i]
            protected = (i not in tail_idx
                         and _norm_key(s.get("text", "")) in choruses)
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
                logger.info("[ADLIB] fantasma descartado @%.1fs%s: %r (oído: %r)",
                            s.get("start"),
                            " [cola]" if i in tail_idx else "",
                            s.get("text", "")[:40], heard[:40])
        return _collapse_runs(segs, drop)
    except Exception as e:  # pragma: no cover — el filtro nunca rompe el render
        logger.warning("[ADLIB] filtro declinó (%s) — segmentos originales", e)
        return list(segs)
