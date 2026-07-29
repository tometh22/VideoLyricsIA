"""Reconciliación de repeticiones — la ocurrencia del estribillo que faltó.

EL DEFECTO (job 6f4047db, "Rodando Por Ahí", 28-07-2026)
--------------------------------------------------------
El audio canta "Estuve rodando por ahí" 7 veces; la letra de referencia la
lista 6. Los motores de timing reparten las K líneas listadas sobre K de
las M ocurrencias cantadas — el Viterbi de ctc_align es monótono por
construcción (la 2ª sólo puede caer después de la 1ª) pero NO cuenta: con
K<M todo el grupo queda corrido exactamente una repetición (~±5,6 s por
línea, medido contra Rotor). La ocurrencia sin línea aparece como región
huérfana en `audio_coverage.uncovered_spans` (209,1–217,0 s con 9 palabras
del ASR en el caso real).

LA SEÑAL QUE YA EXISTÍA Y NADIE USABA
-------------------------------------
- `repetition_group` (chorus_trim.mark_repetitions) agrupa las líneas de
  texto repetido — metadata que ningún motor consumía.
- `ctc_lr` (confianza por línea del retime CTC) era telemetría pura: en
  las líneas desfasadas del caso real medía 5× peor que en las correctas
  (mediana −0,178 vs −0,032).
- `uncovered_spans` delata DÓNDE quedó la ocurrencia sin línea.

QUÉ HACE ESTE MÓDULO
--------------------
Post-pass puro (sin I/O) que corre después del retime CTC y del filtro de
ad-libs. Por cada grupo de repetición con `>= min_group` miembros:

  Tier 1 — INSERT: si en la envolvente temporal del grupo hay una región
  huérfana cuyo ASR SUENA al texto del grupo (`_phonetic_ratio >= 0.70`),
  inserta la ocurrencia faltante con el texto curado del grupo y el timing
  de las palabras reales del ASR. Es el caso medido.

  Tier 2 — REASSIGN (más raro, más conservador): un miembro que flota
  sobre nada (ningún onset del ASR a <1 s de su start) Y cuyo `ctc_lr` es
  outlier del grupo se mueve a la región huérfana matcheante más cercana.
  Doble condición a propósito: `ctc_lr` corrobora, nunca decide solo.

DISEÑO DEFENSIVO
----------------
Decline por defecto: una sub-región que suena casi igual a dos grupos se
saltea (no se sabe de quién es); miembros no monótonos declinan el grupo;
con más huérfanas que el tope se insertan solo las de mejor ratio.
Sin gates acústicos nuevos — el runbook de gap-transplant documenta que
los discriminadores acústicos verso↔coro fallaron 3 veces; acá la
evidencia es TEXTUAL (fonética) + TEMPORAL (envolvente del grupo), nunca
espectral. Kill switch: REPETITION_RECONCILE_ENABLED (default off).
Nunca levanta: ante cualquier error devuelve la entrada intacta.
"""
from __future__ import annotations

import logging
import os

logger = logging.getLogger("genly.repetition_reconcile")

_TRUE = ("1", "true", "yes", "on")

# Umbral fonético para "este audio ES el texto del grupo". Más alto que el
# 0.5 de anclaje de forced_align (acá insertamos, no solo ubicamos) y en
# línea con RECOVER_PHON_MIN=0.72 de whisperx_reconcile.
_PHON_MIN = 0.70
# Margen de decline por ambigüedad entre grupos: si otro grupo suena casi
# igual de parecido a la sub-región, no se inserta nada.
_AMBIG_MARGIN = 0.05
# Sub-regiones dentro de un span huérfano: corta por silencio o por largo
# (mismos valores que la recuperación de huecos de whisperx_reconcile).
_RUN_GAP_S = 1.2
_RUN_MAX_S = 6.0
# Tier 2: un miembro "flota" si ningún onset del ASR cae a menos de esto.
_FLOAT_TOL_S = 1.0
# Tier 2: outlier de confianza = ctc_lr < mediana del grupo − esto.
_LR_OUTLIER_DELTA = 0.10
# Máximo de inserts por grupo. El caso real ("Rodando Por Ahí") tiene 4
# ocurrencias huérfanas legítimas en el outro — declinar por "demasiadas"
# castigaría justo a las canciones con outro repetitivo, que son la clase
# que motivó este módulo. Si hay más candidatas que el tope, se insertan
# las de mejor ratio fonético; el resto queda para el editor.
_MAX_INSERTS_PER_GROUP = 4


def is_enabled() -> bool:
    return os.environ.get(
        "REPETITION_RECONCILE_ENABLED", "0"
    ).strip().lower() in _TRUE


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


def _word_tokens(words: list[dict]) -> list[str]:
    return _tokens(" ".join(str(w.get("word", "")) for w in words))


def _runs(span_words: list[dict]) -> list[list[dict]]:
    """Corta las palabras de un span huérfano en sub-regiones cantables
    (por silencio > _RUN_GAP_S o largo > _RUN_MAX_S)."""
    runs: list[list[dict]] = []
    cur: list[dict] = []
    for w in span_words:
        if cur:
            gap = _f(w.get("start")) - _f(cur[-1].get("end"))
            largo = _f(w.get("end")) - _f(cur[0].get("start"))
            if gap > _RUN_GAP_S or largo > _RUN_MAX_S:
                runs.append(cur)
                cur = []
        cur.append(w)
    if cur:
        runs.append(cur)
    return runs


def _median(vals: list[float]) -> float:
    if not vals:
        return 0.0
    s = sorted(vals)
    return s[len(s) // 2]


def reconcile(segments: list[dict], asr_words: list[dict], *,
              lead_s: float = 0.0, hold_s: float = 0.0,
              min_group: int = 3,
              phon_min: float = _PHON_MIN) -> tuple[list[dict], dict]:
    """Devuelve (segmentos nuevos ordenados por start, stats). Nunca levanta;
    ante cualquier duda o error devuelve una copia idéntica de la entrada."""
    stats: dict = {"groups": 0, "inserted": 0, "reassigned": 0, "declined": []}
    if not segments or not isinstance(asr_words, list) or len(asr_words) < 8:
        stats["declined"].append(("all", "sin_asr_words"))
        return list(segments or []), stats
    try:
        return _reconcile_inner(segments, asr_words, lead_s=lead_s,
                                hold_s=hold_s, min_group=min_group,
                                phon_min=phon_min, stats=stats)
    except Exception as e:  # pragma: no cover — nunca romper la cascada
        logger.warning("[REP-RECONCILE] declinó por excepción: %r", e)
        stats["declined"].append(("all", f"exc:{type(e).__name__}"))
        return list(segments), stats


def _reconcile_inner(segments, asr_words, *, lead_s, hold_s, min_group,
                     phon_min, stats):
    from audio_coverage import uncovered_spans
    from chorus_trim import mark_repetitions
    from forced_align import _phonetic_ratio

    annotated = mark_repetitions(segments)
    words = sorted(
        (w for w in asr_words if isinstance(w, dict)),
        key=lambda w: _f(w.get("start")),
    )

    # Grupos con membresía suficiente.
    groups: dict[int, list[dict]] = {}
    for s in annotated:
        gid = s.get("repetition_group")
        if gid is not None:
            groups.setdefault(gid, []).append(s)
    # Para ACTUAR se exige min_group; para el chequeo de ambigüedad
    # participan TODOS los grupos (un texto que aparece 2 veces también
    # puede ser el verdadero dueño de una región huérfana).
    all_groups = {g: sorted(m, key=lambda s: _f(s.get("start")))
                  for g, m in groups.items()}
    groups = {g: m for g, m in all_groups.items() if len(m) >= min_group}
    stats["groups"] = len(groups)
    if not groups:
        return annotated, stats

    spans = uncovered_spans(annotated, words)
    # Sub-regiones cantables de todos los spans huérfanos, con sus palabras.
    all_runs: list[dict] = []
    for a, b, _n in spans:
        sw = [w for w in words
              if a - 0.05 <= (_f(w.get("start")) + _f(w.get("end"))) / 2
              <= b + 0.05]
        for run in _runs(sw):
            if len(run) >= 2:
                all_runs.append({"words": run,
                                 "start": _f(run[0].get("start")),
                                 "end": _f(run[-1].get("end")),
                                 "used": False})

    # Ratio de cada run contra cada grupo — TODOS, para la ambigüedad.
    group_tokens = {g: _tokens(m[0].get("text", ""))
                    for g, m in all_groups.items()}
    for run in all_runs:
        rt = _word_tokens(run["words"])
        run["ratios"] = {g: _phonetic_ratio(toks, rt)
                        for g, toks in group_tokens.items() if toks}

    out = list(annotated)

    for gid, members in sorted(groups.items()):
        toks = group_tokens.get(gid) or []
        if not toks:
            continue
        starts = [_f(m.get("start")) for m in members]
        if starts != sorted(starts):
            stats["declined"].append((gid, "no_monotonico"))
            continue
        gaps = [b - a for a, b in zip(starts, starts[1:])]
        med_gap = _median(gaps)
        if med_gap <= 0:
            stats["declined"].append((gid, "sin_cadencia"))
            continue
        zone_lo = starts[0] - 1.5 * med_gap
        zone_hi = _f(members[-1].get("end")) + 1.5 * med_gap

        # Runs candidatas: dentro de la envolvente, suenan al grupo, y no
        # suenan casi igual a otro grupo (ambigüedad → afuera).
        cands = []
        for run in all_runs:
            if run["used"] or not (zone_lo <= run["start"] <= zone_hi):
                continue
            mine = run["ratios"].get(gid, 0.0)
            if mine < phon_min:
                continue
            others = [r for g, r in run["ratios"].items() if g != gid]
            if others and max(others) > mine - _AMBIG_MARGIN:
                # Esta run suena casi igual a otro grupo: no sabemos de
                # quién es → se saltea ESA run, no el grupo entero.
                stats["declined"].append((gid, "run_ambigua_entre_grupos"))
                continue
            if len(run["words"]) < max(2, int(0.6 * len(toks))):
                continue
            cands.append(run)
        if not cands:
            continue
        if len(cands) > _MAX_INSERTS_PER_GROUP:
            cands = sorted(cands,
                           key=lambda r: -r["ratios"].get(gid, 0.0)
                           )[:_MAX_INSERTS_PER_GROUP]
            cands.sort(key=lambda r: r["start"])
            stats["declined"].append((gid, "huerfanas_extra_recortadas"))

        # ── Tier 2 primero: reasignar miembros flotantes ─────────────────
        # Corre ANTES del insert a propósito: si un miembro flota y hay
        # una región huérfana matcheante al lado, eso es un MOVE — si el
        # insert corriera primero se comería la región y el miembro
        # quedaría flotando + línea nueva = duplicado en pantalla.
        lrs = [_f(m.get("ctc_lr")) for m in members
               if m.get("ctc_lr") is not None]
        med_lr = _median(lrs) if lrs else None
        onsets = [_f(w.get("start")) for w in words]
        for m in members:
            if med_lr is None or m.get("ctc_lr") is None:
                continue
            m_start = _f(m.get("start"))
            floats = not any(abs(o - m_start) <= _FLOAT_TOL_S for o in onsets)
            outlier = _f(m.get("ctc_lr")) < med_lr - _LR_OUTLIER_DELTA
            if not (floats and outlier):
                continue
            near = [r for r in all_runs
                    if not r["used"]
                    and r["ratios"].get(gid, 0.0) >= phon_min
                    and abs(r["start"] - m_start) <= 1.5 * med_gap]
            if len(near) != 1:
                if near:
                    stats["declined"].append((gid, "reassign_ambiguo"))
                continue
            run = near[0]
            m["start"] = round(run["start"], 3)
            m["end"] = round(run["end"], 3)
            m["words"] = [dict(w) for w in run["words"]]
            m.pop("ctc_lr", None)
            m["repetition_reassigned"] = True
            run["used"] = True
            stats["reassigned"] += 1
            logger.info(
                "[REP-RECONCILE] grupo %d: miembro flotante (%.1fs, ctc_lr "
                "outlier) reasignado al onset real %.1fs", gid, m_start,
                run["start"],
            )
        # ── Tier 1: insertar la(s) ocurrencia(s) faltante(s) restantes ───
        template = members[0]
        for run in cands:
            if run["used"]:
                continue  # consumida por un reassign de este mismo grupo
            new = dict(template)
            for k in ("ctc_lr", "ctc_skipped", "review", "gap_recovered",
                      "words"):
                new.pop(k, None)
            ini = max(0.0, run["start"] - max(0.0, lead_s))
            fin = run["end"] + max(0.0, hold_s)
            # Clamp contra vecinos reales en la línea de tiempo actual.
            for s in out:
                s_end = _f(s.get("end"))
                s_start = _f(s.get("start"))
                if s_end <= run["start"] and s_end > ini:
                    ini = s_end + 0.01
                if s_start >= run["end"] and s_start < fin:
                    fin = s_start - 0.01
            if fin - ini < 0.3 or _f(run["end"]) - _f(run["start"]) < 0.3:
                stats["declined"].append((gid, "insert_clampeado"))
                continue
            new["start"] = round(ini, 3)
            new["end"] = round(fin, 3)
            new["words"] = [dict(w) for w in run["words"]]
            new["repetition_recovered"] = True
            out.append(new)
            run["used"] = True
            stats["inserted"] += 1
            logger.info(
                "[REP-RECONCILE] grupo %d: ocurrencia faltante insertada en "
                "%.1f-%.1fs (%d palabras, texto %r)", gid, ini, fin,
                len(run["words"]), str(template.get("text", ""))[:40],
            )

    out.sort(key=lambda s: _f(s.get("start")))
    # Sanidad final: sin solapes tras los inserts.
    for a, b in zip(out, out[1:]):
        if _f(a.get("end")) > _f(b.get("start")):
            a["end"] = round(max(_f(a.get("start")) + 0.1,
                                 _f(b.get("start")) - 0.01), 3)
    return out, stats
