"""Chorus snap: limpiar los fragmentos del coro a la frase canónica.

EL DEFECTO (medido contra Rotor en "Rodando Por Ahí", job ae6f1165)
-------------------------------------------------------------------
En el outro, donde el estribillo se repite muchas veces con la voz enterrada,
el ASR devuelve fragmentos ("por ahí", "rotando") que gap_rescue corta por
silencio y quedan mal:

    nuestro: "Rodando por ahí, estuve rodando" | "Por ahí"   (cortado a la mitad)
    Rotor:   "Rodando por ahí, rodando por ahí" | ...        (frase entera, limpia)

Rotor gana acá porque NO confía palabra-por-palabra en un ASR que patina
sobre voz enterrada: tiene la frase del coro y la estampa limpia en cada
repetición. Nosotros tenemos la misma información — `repetition_group`
(chorus_trim) ya agrupa las repeticiones y su texto canónico — pero ningún
paso la usaba para reparar los fragmentos.

QUÉ HACE
--------
Post-pass puro. Por cada grupo de repetición con una frase canónica clara
(un texto que aparece ≥2 veces exacto entre sus miembros):
  - dentro de la envolvente temporal del grupo,
  - una línea que es un FRAGMENTO o mishear de la canónica (sus palabras
    son mayormente un subconjunto de la canónica, o suena parecido pero
    imperfecto) se reescribe con el texto canónico, conservando su timing.
  - un órfano corto que quedó pegado a una línea ya snappeada idéntica se
    absorbe (merge) para no dejar "Por ahí" suelto.

NO toca líneas que ya matchean la canónica limpio, ni líneas que son OTRA
letra (ratio bajo): sólo repara lo que evidentemente es el coro mal cortado.

DEFENSAS
--------
- Sólo dentro de la envolvente de un grupo con canónica ≥2 exactos.
- La línea a reparar debe ser fragmento (subset ≥0.6) o mishear
  (0.45 ≤ ratio < 0.92) — nunca una letra distinta.
- Nunca alarga: el snap sólo cambia texto; el merge de órfanos exige que el
  segundo sea corto (< _ORPHAN_MAX_S) e idéntico tras snap.
- CHORUS_SNAP_ENABLED default off. Puro, never raises.
"""
from __future__ import annotations

import logging
import os

logger = logging.getLogger("genly.chorus_snap")

_TRUE = ("1", "true", "yes", "on")

_SUBSET_MIN = 0.6        # fracción de tokens de la línea que están en la canónica
_RATIO_LO = 0.45        # banda de mishear: por debajo, es otra letra
_RATIO_HI = 0.92        # por encima, ya está limpia
_MAX_LEN_RATIO = 1.6    # no snapear una línea mucho más larga que la canónica
_ORPHAN_MAX_S = 2.6     # órfano corto que se absorbe en la línea previa idéntica
_MERGE_GAP_S = 0.6      # separación máxima para absorber el órfano
_ZONE_PAD_MULT = 1.5    # envolvente = ± este múltiplo de la cadencia mediana


def is_enabled() -> bool:
    return os.environ.get("CHORUS_SNAP_ENABLED", "0").strip().lower() in _TRUE


def _f(v, default: float = 0.0) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def _tokens(text: str) -> list[str]:
    import unicodedata as _u
    s = _u.normalize("NFD", (text or "").lower())
    s = "".join(c for c in s if c.isalnum() or c.isspace())
    return [t for t in s.split() if t]


def _median(vals: list[float]) -> float:
    if not vals:
        return 0.0
    s = sorted(vals)
    return s[len(s) // 2]


def snap(segments: list[dict], *, min_group: int = 3) -> tuple[list[dict], dict]:
    """Devuelve (segmentos nuevos, stats). Nunca levanta; ante cualquier
    problema devuelve la entrada intacta."""
    stats = {"groups": 0, "snapped": 0, "merged": 0}
    if not segments or len(segments) < 3:
        return list(segments or []), stats
    try:
        return _snap_inner(segments, min_group, stats)
    except Exception as e:  # pragma: no cover — nunca romper la cascada
        logger.warning("[CHORUS-SNAP] declinó por excepción: %r", e)
        return list(segments), stats


def _snap_inner(segments, min_group, stats):
    from collections import Counter
    from chorus_trim import mark_repetitions
    from forced_align import _phonetic_ratio

    ann = mark_repetitions(segments)
    out = [dict(s) for s in ann]

    groups: dict[int, list[int]] = {}
    for i, s in enumerate(out):
        gid = s.get("repetition_group")
        if gid is not None:
            groups.setdefault(gid, []).append(i)

    # Perfil de cada grupo elegible: canónica + envolvente temporal.
    perfiles = []
    for gid, idxs in groups.items():
        if len(idxs) < min_group:
            continue
        norm_to_orig: dict[str, str] = {}
        cnt: Counter = Counter()
        for i in idxs:
            toks = _tokens(out[i].get("text", ""))
            if not toks:
                continue
            key = " ".join(toks)
            cnt[key] += 1
            norm_to_orig.setdefault(key, out[i].get("text", "").strip())
        if not cnt:
            continue
        canon_key, canon_n = cnt.most_common(1)[0]
        if canon_n < 2:
            continue
        starts = sorted(_f(out[i].get("start")) for i in idxs)
        gaps = [b - a for a, b in zip(starts, starts[1:])]
        med_gap = _median(gaps) if gaps else 6.0
        perfiles.append({
            "toks": canon_key.split(), "set": set(canon_key.split()),
            "text": norm_to_orig[canon_key],
            "lo": starts[0] - _ZONE_PAD_MULT * med_gap,
            "hi": max(_f(out[i].get("end")) for i in idxs)
            + _ZONE_PAD_MULT * med_gap,
        })
        stats["groups"] += 1
    if not perfiles:
        return list(segments), stats

    for i, s in enumerate(out):
        # NUNCA tocar una línea que ya es miembro de un grupo: es una
        # repetición limpia reconocida. Reparar sólo lo NO reconocido — que
        # es exactamente el fragmento mal cortado. (Sin esto, una línea
        # limpia del coro caía en la envolvente de OTRO grupo y se snapeaba
        # a su canónica: "Estuve rodando por ahí" -> "Todo eso me dice…".)
        if s.get("repetition_group") is not None:
            continue
        ln = _tokens(s.get("text", ""))
        if not ln:
            continue
        st = _f(s.get("start"))
        # Elegir el MEJOR grupo cuya envolvente contiene la línea, no el
        # primero: desambigua entre coros que coexisten en el outro.
        mejor = None
        for p in perfiles:
            if not (p["lo"] <= st <= p["hi"]):
                continue
            subset = sum(1 for t in ln if t in p["set"]) / len(ln)
            ratio = _phonetic_ratio(ln, p["toks"])
            score = max(subset, ratio)
            if mejor is None or score > mejor[0]:
                mejor = (score, subset, ratio, p)
        if mejor is None:
            continue
        _score, subset, ratio, p = mejor
        if " ".join(ln) == " ".join(p["toks"]):
            continue  # idéntica a la canónica: nada que reparar
        es_fragmento = subset >= _SUBSET_MIN or _RATIO_LO <= ratio < _RATIO_HI
        no_muy_larga = len(ln) <= len(p["toks"]) * _MAX_LEN_RATIO
        if es_fragmento and no_muy_larga:
            out[i] = {**s, "text": p["text"], "chorus_snapped": True}
            stats["snapped"] += 1

    # Absorber órfanos: dos líneas adyacentes idénticas tras el snap, la
    # segunda corta → se funden en una (elimina el "Por ahí" suelto).
    merged: list[dict] = []
    for s in sorted(out, key=lambda x: _f(x.get("start"))):
        if merged:
            prev = merged[-1]
            same = _tokens(prev.get("text", "")) == _tokens(s.get("text", ""))
            gap = _f(s.get("start")) - _f(prev.get("end"))
            dur = _f(s.get("end")) - _f(s.get("start"))
            if same and gap <= _MERGE_GAP_S and dur <= _ORPHAN_MAX_S \
                    and (prev.get("chorus_snapped") or s.get("chorus_snapped")):
                prev["end"] = round(_f(s.get("end")), 3)
                pw = (prev.get("words") or []) + (s.get("words") or [])
                if pw:
                    prev["words"] = pw
                stats["merged"] += 1
                continue
        merged.append(s)
    return merged, stats
