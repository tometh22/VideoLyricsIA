"""Voto por palabra: el audio corrige a la referencia, la referencia
aporta la ortografía — el consenso que ninguna fuente sola puede dar.

EL PROBLEMA (medido contra Rotor, "Rodando Por Ahí", 29-07-2026)
----------------------------------------------------------------
Nuestros errores de texto restantes vienen de copiar FIEL la letra de
referencia: "Las estaciones que hice" (se canta "canciones"), "me embragué"
(se canta "embriagué"), y la línea 1 sin el "Cuando…mi" que sí se canta.
Los de Rotor vienen de lo contrario — oír sin referencia que lo corrija:
"Cuando vino tinto" (es "¿Cuánto"), "Mucha gente conocido" (falta "he").

Cada fuente falla hacia su lado. El voto tiene ambas bien: donde el testigo
acústico coincide (normalizado) con la referencia, gana la ortografía
curada de la referencia ("¿Cuánto…tomé?"); donde el testigo contradice a la
referencia con anclas firmes, gana el audio ("canciones").

EL TESTIGO TIENE QUE SER INDEPENDIENTE
--------------------------------------
El ASR principal de la cascada se ceba con la letra de referencia como
prompt (sesga hacia SUS errores: el mix-ASR primed oyó "estaciones"). El
testigo válido es el ASR del STEM SIN prompt — verificado: oye "canciones",
"embriague" y "Cuando pienso cuánto tiempo de mi vida" correctamente.

REGLAS (validadas contra el job real: 3/3 correcciones objetivo, 0 falsos)
--------------------------------------------------------------------------
- SUSTITUCIÓN 1:1 sólo con ANCLAS: el token vecino anterior Y el posterior
  deben coincidir (normalizados) entre referencia y testigo. "las [X] que"
  con vecinos firmes y X distinto → gana el testigo.
- INSERCIÓN de ≤2 tokens sólo con anclas a ambos lados (el inicio de línea
  cuenta como ancla izquierda). Marca `review=True`: es contenido nuevo.
- GUARD ANTI-DUPLICACIÓN: un token del testigo cuyo punto medio cae dentro
  de OTRO cartel no puede insertarse acá — una palabra en el borde entre
  dos carteles aparecería en las ventanas de ambos y se pintaría dos veces
  (detectado en el prototipo sobre el outro del job real).
- ORTOGRAFÍA: tokens normalizados iguales nunca se tocan (se preserva
  "tomé" de la referencia aunque el ASR diga "tome"). En sustituciones se
  transfiere el acento final de la referencia ("embriague"→"embriagué") y
  la mayúscula inicial.
- Duda → no tocar. Kill switch WORD_VOTE_ENABLED (default off). Puro.
"""
from __future__ import annotations

import logging
import os
import unicodedata

logger = logging.getLogger("genly.word_vote")

_TRUE = ("1", "true", "yes", "on")

# Slop temporal al juntar los tokens del testigo para una línea: los bordes
# de cartel traen lead/hold, la palabra real puede arrancar un pelo antes.
_PAD_S = 0.4
# Máximo de tokens por bloque de inserción (más que esto = otra línea, no
# una palabra comida).
_MAX_INSERT = 2

_ACCENT = {"a": "á", "e": "é", "i": "í", "o": "ó", "u": "ú"}


def is_enabled() -> bool:
    return os.environ.get("WORD_VOTE_ENABLED", "0").strip().lower() in _TRUE


def _f(v, default: float = 0.0) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def _norm(t: str) -> str:
    s = unicodedata.normalize("NFD", (t or "").lower())
    return "".join(c for c in s if c.isalnum())


def _transfer_accent(ref_tok: str, heard_tok: str) -> str:
    """La referencia acentuaba la vocal final y el testigo no → transferir
    ('embriague' + ref 'embragué' → 'embriagué')."""
    if (ref_tok and heard_tok and ref_tok[-1] in "áéíóú"
            and heard_tok[-1] in _ACCENT
            and unicodedata.normalize("NFD", ref_tok[-1])[0] == heard_tok[-1]):
        return heard_tok[:-1] + _ACCENT[heard_tok[-1]]
    return heard_tok


def vote(segments: list[dict], witness_words: list[dict], *,
         pad: float = _PAD_S) -> tuple[list[dict], dict]:
    """Aplica el voto por palabra. Devuelve (segmentos nuevos, stats).
    Nunca levanta; ante cualquier problema devuelve la entrada intacta."""
    stats = {"substitutions": 0, "insertions": 0, "lines_changed": 0,
             "declined": []}
    if not segments or not isinstance(witness_words, list) \
            or len(witness_words) < 8:
        stats["declined"].append("sin_testigo")
        return list(segments or []), stats
    try:
        return _vote_inner(segments, witness_words, pad, stats)
    except Exception as e:  # pragma: no cover — nunca romper la cascada
        logger.warning("[WORD-VOTE] declinó por excepción: %r", e)
        stats["declined"].append(f"exc:{type(e).__name__}")
        return list(segments), stats


def _vote_inner(segments, witness_words, pad, stats):
    from difflib import SequenceMatcher

    W = sorted((w for w in witness_words if isinstance(w, dict)),
               key=lambda w: _f(w.get("start")))
    intervals = [(_f(s.get("start")), _f(s.get("end")))
                 for s in segments if isinstance(s, dict)]

    def _mid(w) -> float:
        a, b = _f(w.get("start")), _f(w.get("end"))
        return (a + b) / 2 if b > a else a

    def _dentro_de_otro(w, idx) -> bool:
        m = _mid(w)
        return any(k != idx and a - 0.05 <= m <= b + 0.05
                   for k, (a, b) in enumerate(intervals))

    out: list[dict] = []
    for idx, seg in enumerate(segments):
        if not isinstance(seg, dict):
            out.append(seg)
            continue
        a, b = _f(seg.get("start")), _f(seg.get("end"))
        heard = [w for w in W if a - pad <= _mid(w) <= b + pad]
        ref_toks = (seg.get("text") or "").split()
        if not ref_toks or len(heard) < 2:
            out.append(seg)
            continue

        rn = [_norm(t) for t in ref_toks]
        hn = [_norm(str(w.get("word", ""))) for w in heard]
        sm = SequenceMatcher(None, rn, hn)

        nuevos = list(ref_toks)
        delta = 0
        subs = ins = 0
        for op, i1, i2, j1, j2 in sm.get_opcodes():
            if op == "replace" and (i2 - i1) == 1 and (j2 - j1) == 1:
                left_ok = ((i1 == 0 and j1 == 0)
                           or (i1 > 0 and j1 > 0 and rn[i1 - 1] == hn[j1 - 1]))
                right_ok = ((i2 == len(rn) and j2 == len(hn))
                            or (i2 < len(rn) and j2 < len(hn)
                                and rn[i2] == hn[j2]))
                if not (left_ok and right_ok):
                    continue
                nuevo = _transfer_accent(ref_toks[i1],
                                         str(heard[j1].get("word", "")).strip())
                if not nuevo or _norm(nuevo) == rn[i1]:
                    continue
                if ref_toks[i1][:1].isupper():
                    nuevo = nuevo[:1].upper() + nuevo[1:]
                nuevos[i1 + delta] = nuevo
                subs += 1
            elif op == "insert" and (j2 - j1) <= _MAX_INSERT:
                left_ok = ((i1 == 0 and j1 == 0)
                           or (i1 > 0 and j1 > 0 and rn[i1 - 1] == hn[j1 - 1]))
                right_ok = (i1 < len(rn) and j2 < len(hn)
                            and rn[i1] == hn[j2])
                if not (left_ok and right_ok):
                    continue
                bloque = [heard[j] for j in range(j1, j2)]
                # Guard anti-duplicación: si algún token pertenece a OTRO
                # cartel (borde compartido), no se inserta acá.
                if any(_dentro_de_otro(w, idx) for w in bloque):
                    continue
                toks = [str(w.get("word", "")).strip() for w in bloque]
                if not all(toks):
                    continue
                if i1 == 0:
                    toks[0] = toks[0][:1].upper() + toks[0][1:]
                    viejo = nuevos[delta]
                    if viejo[:1].isupper() and not viejo.isupper():
                        nuevos[delta] = viejo[:1].lower() + viejo[1:]
                nuevos[i1 + delta:i1 + delta] = toks
                delta += len(toks)
                ins += 1

        if subs or ins:
            new = dict(seg)
            new["text"] = " ".join(nuevos)
            new["word_voted"] = True
            if ins:
                new["review"] = True      # contenido nuevo: que lo vea el operador
            out.append(new)
            stats["substitutions"] += subs
            stats["insertions"] += ins
            stats["lines_changed"] += 1
            logger.info("[WORD-VOTE] línea %d: %r -> %r", idx,
                        seg.get("text"), new["text"])
        else:
            out.append(seg)
    return out, stats
