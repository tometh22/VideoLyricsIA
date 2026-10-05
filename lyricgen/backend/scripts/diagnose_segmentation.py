#!/usr/bin/env python3
"""Cómo corta líneas el pipeline y cómo las corta el operador.

Solo lectura. Jobs: los de staging de la fase 1 (cargadores de
``calibrate_display_timing``). Texto de máquina y aprobado se alinean palabra
por palabra (``difflib`` sobre tokens normalizados) para saber a qué línea
aprobada fue a parar cada palabra de máquina.

a) Por línea de máquina: igual (mismos bordes), partida (sus palabras van a 2+
   líneas aprobadas), unida (comparte línea aprobada con otra de máquina),
   partida y unida, o borrada. Por origen.
b) Duración y palabras por línea, máquina vs aprobada.
c) Cortes que agregó el operador (dentro de una línea de máquina) y cortes del
   pipeline que eliminó (al unir): pausa acústica según las palabras de
   máquina, puntuación, rima, estribillo y largo de la línea original.
   Referencia: pausas internas que el operador NO cortó y bordes que respetó.
d) Variabilidad del largo de las líneas por canción (la guía pide "largo
   parecido").
e) Simulación: bordes que producen las reglas de corte con distintos
   parámetros, comparados con los aprobados.

Uso:
    DATABASE_URL=... python3.11 scripts/diagnose_segmentation.py --json-out /tmp/seg.json
"""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
import difflib
import importlib.util
import itertools
import json
from pathlib import Path
import re
import statistics
import sys

BACKEND = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND))

_spec = importlib.util.spec_from_file_location(
    "calibrate_display_timing", BACKEND / "scripts" / "calibrate_display_timing.py",
)
calibrate = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(calibrate)

_PUNCT_END = re.compile(r"[,;.:!?…]$")


def tokens_of(text: str) -> list[str]:
    return [t for t in calibrate.normalize_text(text).split() if t]


def quantiles(values) -> dict:
    values = sorted(v for v in values if v is not None)
    if not values:
        return {"n": 0}
    n = len(values)
    return {"n": n, **{f"p{int(p * 100)}": round(values[int(p * (n - 1))], 3)
                       for p in (0.25, 0.5, 0.75, 0.9)}}


def stream(segments: list[dict]) -> list[tuple[int, str, str]]:
    """(línea, token normalizado, token crudo) en orden."""
    out = []
    for index, seg in enumerate(segments):
        raw = str(seg.get("text") or "").split()
        for token in raw:
            norm = calibrate.normalize_text(token)
            if norm:
                out.append((index, norm, token))
    return out


def token_map(machine: list[dict], approved: list[dict]) -> tuple[list, list, dict]:
    """Mapa token de máquina → línea aprobada (solo tokens emparejados)."""
    ms, as_ = stream(machine), stream(approved)
    matcher = difflib.SequenceMatcher(a=[t[1] for t in ms], b=[t[1] for t in as_], autojunk=False)
    mapping: dict[int, int] = {}
    for block in matcher.get_matching_blocks():
        for k in range(block.size):
            mapping[block.a + k] = as_[block.b + k][0]
    return ms, as_, mapping


def word_times(seg: dict) -> list[tuple[float, float]] | None:
    """Tiempos por token si las palabras coinciden 1:1 con el texto."""
    words = [w for w in (seg.get("words") or []) if isinstance(w, dict)]
    if not words or len(words) != len(tokens_of(seg.get("text"))):
        return None
    out = []
    for w in words:
        try:
            out.append((float(w["start"]), float(w["end"])))
        except (KeyError, TypeError, ValueError):
            return None
    return out


def rhyme_key(token: str) -> str:
    return re.sub(r"[^a-záéíóúñü]", "", token.lower())[-2:]


def classify_job(job: dict) -> dict:
    machine = [dict(s) for s in job["machine"] or [] if isinstance(s, dict)]
    approved = [dict(s) for s in job["approved"] or [] if isinstance(s, dict)]
    ms, as_, mapping = token_map(machine, approved)
    origin = calibrate.origin_for(job["timing_source"])
    by_machine: dict[int, list[int | None]] = defaultdict(list)
    for position, (line, _norm, _raw) in enumerate(ms):
        by_machine[line].append(mapping.get(position))
    approved_sources: dict[int, set[int]] = defaultdict(set)
    for position, (line, _n, _r) in enumerate(ms):
        if position in mapping:
            approved_sources[mapping[position]].add(line)
    approved_texts = Counter(calibrate.normalize_text(s.get("text")) for s in approved)
    approved_ends = [rhyme_key((tokens_of(s.get("text")) or [""])[-1]) for s in approved]

    lines, cuts_added, cuts_removed, kept_internal, kept_borders = [], [], [], [], []
    for index, seg in enumerate(machine):
        targets = by_machine.get(index, [])
        matched = [t for t in targets if t is not None]
        approved_set = sorted(set(matched))
        if not targets or len(matched) < max(1, len(targets) // 2):
            kind = "deleted_or_rewritten"
        else:
            split = len(approved_set) >= 2
            merged = any(len(approved_sources[a]) >= 2 for a in approved_set)
            kind = ("split_and_merged" if split and merged else "split" if split
                    else "merged" if merged else "same")
        lines.append({"origin": origin, "kind": kind})

        times = word_times(seg)
        toks = tokens_of(seg.get("text"))
        raw_tokens = str(seg.get("text") or "").split()
        duration = calibrate._finite_time(seg.get("end")) - calibrate._finite_time(seg.get("start"))
        for k in range(len(targets) - 1):
            left, right = targets[k], targets[k + 1]
            pause = (times[k + 1][0] - times[k][1]) if times else None
            if left is not None and right is not None and left != right:
                fragment = calibrate.normalize_text(approved[left].get("text"))
                neighbours = [approved_ends[j] for j in (left - 1, left + 1)
                              if 0 <= j < len(approved_ends) and j != left]
                cuts_added.append({
                    "origin": origin, "pause_s": pause,
                    "punctuation": bool(raw_tokens[k:k + 1] and _PUNCT_END.search(raw_tokens[k])),
                    "rhyme": bool(toks[k:k + 1] and rhyme_key(toks[k]) and rhyme_key(toks[k]) in neighbours),
                    "chorus": approved_texts[fragment] >= 2,
                    "line_words": len(toks), "line_s": round(duration, 3),
                })
            elif left is not None and left == right and pause is not None:
                kept_internal.append(pause)
        if index + 1 < len(machine):
            nxt = machine[index + 1]
            this_times, next_times = times, word_times(nxt)
            pause = (next_times[0][0] - this_times[-1][1]) if this_times and next_times else None
            a_last = next((t for t in reversed(targets) if t is not None), None)
            b_first = next((t for t in by_machine.get(index + 1, []) if t is not None), None)
            if a_last is not None and b_first is not None:
                if a_last == b_first:
                    cuts_removed.append({"origin": origin, "pause_s": pause})
                elif pause is not None:
                    kept_borders.append(pause)
    return {
        "lines": lines, "cuts_added": cuts_added, "cuts_removed": cuts_removed,
        "kept_internal": kept_internal, "kept_borders": kept_borders,
        "machine_dur": [calibrate._finite_time(s.get("end")) - calibrate._finite_time(s.get("start")) for s in machine],
        "approved_dur": [calibrate._finite_time(s.get("end")) - calibrate._finite_time(s.get("start")) for s in approved],
        "machine_words": [len(tokens_of(s.get("text"))) for s in machine],
        "approved_words": [len(tokens_of(s.get("text"))) for s in approved],
        "boundaries": boundaries(machine, approved, ms, mapping),
    }


def boundaries(machine, approved, ms, mapping) -> dict:
    """Bordes en el espacio de tokens de máquina: después de qué token termina
    una línea de máquina y después de cuál termina una aprobada."""
    machine_ends = {i for i in range(len(ms) - 1) if ms[i][0] != ms[i + 1][0]}
    approved_ends = {
        i for i in range(len(ms) - 1)
        if i in mapping and i + 1 in mapping and mapping[i] != mapping[i + 1]
    }
    words = []
    for seg in machine:
        times = word_times(seg)
        toks = tokens_of(seg.get("text"))
        if times:
            words.extend({"word": t, "start": s, "end": e} for t, (s, e) in zip(toks, times))
        else:
            words.extend({"word": t, "start": None, "end": None} for t in toks)
    return {"machine": sorted(machine_ends), "approved": sorted(approved_ends),
            "n_tokens": len(ms), "words": words, "line_of": [t[0] for t in ms]}


# ------------------------------------------------------------- e) simulación


def simulate_cuts(words: list[dict], line_of: list[int], *, mode: str, params: dict) -> set[int] | None:
    """Bordes resultantes de aplicar una regla de corte sobre las líneas de
    máquina (solo agrega cortes) o, con ``dp_song``, sobre toda la canción."""
    import phrase_segmenter
    from whisperx_transcribe import _split_long_segments

    if any(w["start"] is None for w in words):
        return None
    base = {i for i in range(len(line_of) - 1) if line_of[i] != line_of[i + 1]}
    if mode == "merge_short_pause":
        # Regla inversa: unir líneas vecinas si la pausa entre ellas es corta
        # y la línea unida no excede palabras/duración.
        cuts, start = set(), 0
        ordered = sorted(base) + [len(line_of) - 1]
        current_start = 0
        for end in ordered:
            if end == len(line_of) - 1:
                break
            pause = words[end + 1]["start"] - words[end]["end"]
            nxt_end = next((e for e in ordered if e > end), len(line_of) - 1)
            merged_words = nxt_end - current_start + 1
            merged_s = words[nxt_end]["end"] - words[current_start]["start"]
            if (pause < params["max_pause"] and merged_words <= params["max_words"]
                    and merged_s <= params["max_s"]):
                continue                      # se une: no hay corte
            cuts.add(end)
            current_start = end + 1
        del start
        return cuts
    if mode == "dp_song":
        groups = phrase_segmenter.segment_words(words, **params)
        cuts, position = set(), -1
        for group in groups[:-1]:
            position += len(group)
            cuts.add(position)
        return cuts
    cuts = set(base)
    spans: list[tuple[int, int]] = []
    start = 0
    for i in range(len(line_of)):
        if i == len(line_of) - 1 or line_of[i] != line_of[i + 1]:
            spans.append((start, i))
            start = i + 1
    for a, b in spans:
        line_words = words[a:b + 1]
        if mode == "max_line_s":
            seg = {"start": line_words[0]["start"], "end": line_words[-1]["end"],
                   "text": " ".join(w["word"] for w in line_words),
                   "words": [dict(w) for w in line_words]}
            parts = _split_long_segments([seg], max_dur=params["max_line_s"])
        elif mode == "gap_split":
            parts = _gap_split(line_words, **params)
        elif mode == "phrase_segmenter":
            groups = (phrase_segmenter.segment_words(line_words, **params)
                      if len(line_words) > params["max_len"] or
                      line_words[-1]["end"] - line_words[0]["start"] > params["max_dur"]
                      else [line_words])
            parts = [{"words": g} for g in groups]
        else:
            raise ValueError(mode)
        position = a - 1
        for part in parts[:-1]:
            position += len(part.get("words") or [])
            cuts.add(position)
    return cuts


def _gap_split(words: list[dict], *, min_words: int, min_s: float, min_gap: float) -> list[dict]:
    """Regla de post_reconcile._split_at_word_gaps: líneas de >= min_words y
    >= min_s se parten en cada pausa >= min_gap."""
    if len(words) < min_words or words[-1]["end"] - words[0]["start"] < min_s:
        return [{"words": words}]
    parts, current = [], [words[0]]
    for prev, word in zip(words, words[1:]):
        if word["start"] - prev["end"] >= min_gap:
            parts.append({"words": current})
            current = []
        current.append(word)
    parts.append({"words": current})
    return parts


def line_match_share(cuts: set[int], approved: set[int], n_tokens: int) -> tuple[int, int]:
    """Líneas simuladas cuyos dos bordes coinciden con bordes aprobados."""
    edges = [-1] + sorted(cuts) + [n_tokens - 1]
    approved_edges = {-1, n_tokens - 1} | approved
    total = len(edges) - 1
    good = sum(1 for a, b in zip(edges, edges[1:]) if a in approved_edges and b in approved_edges)
    return good, total


GRIDS = {
    "max_line_s": [{"max_line_s": v} for v in (3.0, 4.0, 5.0, 6.0, 8.0)],
    "gap_split": [{"min_words": w, "min_s": s, "min_gap": g}
                  for w, s, g in itertools.product((5, 7, 9), (2.0, 3.0, 4.0), (0.4, 0.7, 1.0))],
    "phrase_segmenter": [{"target_len": t, "max_len": m, "min_len": 3, "max_dur": d, "gap_cap": 1.5}
                         for t, m, d in itertools.product((5, 6, 7), (9, 11, 13), (4.0, 5.0, 6.0))],
    "merge_short_pause": [{"max_pause": p, "max_words": w, "max_s": d}
                          for p, w, d in itertools.product((0.1, 0.2, 0.3, 0.4), (8, 10, 12), (5.0, 6.0, 7.0))],
    "dp_song": [{"target_len": t, "max_len": m, "min_len": 3, "max_dur": d, "gap_cap": 1.5}
                for t, m, d in itertools.product((5, 6, 7, 8), (9, 11, 13), (4.0, 5.0, 6.0))],
}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--roles", default=str(BACKEND / "data" / "song_roles.json"))
    parser.add_argument("--staging-limit", type=int, default=100)
    parser.add_argument("--tenant", action="append", default=[])
    parser.add_argument("--json-out", default="")
    parser.add_argument("--jobs-pickle", default="",
                        help="pickle de calibrate_display_timing --lines-out (evita releer la base)")
    args = parser.parse_args()
    args.tenant = args.tenant or ["universal_music"]
    args.gold_dir = ""

    if args.jobs_pickle:
        import pickle

        jobs = pickle.loads(Path(args.jobs_pickle).read_bytes())["jobs"]
    else:
        jobs, _lines = calibrate.collect(args)
    jobs = [j for j in jobs if j["dataset"] == "staging"]
    results = [classify_job(job) for job in jobs]

    lines = [line for r in results for line in r["lines"]]
    kinds = {}
    for origin in ("all", "ctc", "whisperx", "whisperx_reconciled", "other"):
        subset = [x for x in lines if origin == "all" or x["origin"] == origin]
        if subset:
            counts = Counter(x["kind"] for x in subset)
            kinds[origin] = {"lines": len(subset),
                             **{k: round(v / len(subset), 4) for k, v in counts.items()}}
    cuts_added = [c for r in results for c in r["cuts_added"]]
    cuts_removed = [c for r in results for c in r["cuts_removed"]]
    report = {
        "jobs": len(jobs),
        "a_line_structure": kinds,
        "b_distributions": {
            "machine_duration_s": quantiles(v for r in results for v in r["machine_dur"]),
            "approved_duration_s": quantiles(v for r in results for v in r["approved_dur"]),
            "machine_words": quantiles(v for r in results for v in r["machine_words"]),
            "approved_words": quantiles(v for r in results for v in r["approved_words"]),
        },
        "c_cuts_added_by_operator": {
            "count": len(cuts_added),
            "pause_s": quantiles(c["pause_s"] for c in cuts_added),
            "with_pause_measured": sum(c["pause_s"] is not None for c in cuts_added),
            "punctuation_share": round(sum(c["punctuation"] for c in cuts_added) / max(1, len(cuts_added)), 4),
            "rhyme_share": round(sum(c["rhyme"] for c in cuts_added) / max(1, len(cuts_added)), 4),
            "chorus_share": round(sum(c["chorus"] for c in cuts_added) / max(1, len(cuts_added)), 4),
            "original_line_words": quantiles(c["line_words"] for c in cuts_added),
            "original_line_s": quantiles(c["line_s"] for c in cuts_added),
            "reference_pause_not_cut_s": quantiles(v for r in results for v in r["kept_internal"]),
        },
        "c_cuts_removed_by_operator": {
            "count": len(cuts_removed),
            "pause_s": quantiles(c["pause_s"] for c in cuts_removed),
            "reference_pause_kept_border_s": quantiles(v for r in results for v in r["kept_borders"]),
        },
        "d_words_per_line_cv": {
            name: quantiles(
                statistics.pstdev(r[key]) / statistics.fmean(r[key])
                for r in results if r[key] and statistics.fmean(r[key]) > 0
            )
            for name, key in (("machine", "machine_words"), ("approved", "approved_words"))
        },
    }

    # e) Simulación sobre los jobs con palabras completas.
    simulable = [r["boundaries"] for r in results
                 if all(w["start"] is not None for w in r["boundaries"]["words"])
                 and r["boundaries"]["n_tokens"] > 3]
    current_good = current_total = 0
    for b in simulable:
        good, total = line_match_share(set(b["machine"]), set(b["approved"]), b["n_tokens"])
        current_good += good
        current_total += total
    sims = {"jobs_simulable": len(simulable),
            "current": {"lines": current_total, "match": round(current_good / max(1, current_total), 4)}}
    for mode, grid in GRIDS.items():
        rows = []
        for params in grid:
            good = total = 0
            for b in simulable:
                cuts = simulate_cuts(b["words"], b["line_of"], mode=mode, params=params)
                if cuts is None:
                    continue
                g, t = line_match_share(cuts, set(b["approved"]), b["n_tokens"])
                good += g
                total += t
            rows.append({"params": params, "lines": total, "match": round(good / max(1, total), 4)})
        rows.sort(key=lambda r: -r["match"])
        sims[mode] = {"best": rows[0], "top": rows[:3], "worst": rows[-1]}
    report["e_simulation"] = sims

    text = json.dumps(report, ensure_ascii=False, indent=1)
    if args.json_out:
        Path(args.json_out).write_text(text)
    print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
