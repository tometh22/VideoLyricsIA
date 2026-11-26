"""Mide la regla de voseo de la Revisión rápida contra el banco de pedidos de UMG.

Uso (los datos son privados y NO viven en el repo)::

    python scripts/measure_umg_voseo_bank.py --targets /ruta/targets.json \
        --jobs /ruta/jobs.json [--official /ruta/official.json] [--rows]

``targets.json``: por job, ``portal``, la letra ENTREGADA (``delivered``,
``delivered_rev``), la versión final aceptada (``final``) y los pedidos
(``requests[].comment``).
``jobs.json``: por job, ``original`` (segmentos de la máquina), ``evidence``
(``machine_evidence``: testigo y Gemini) y ``versions`` (o
``delivered_version``) para correr la revisión con los oídos reales.
``official.json``: por job, ``{"text": ...}`` con la letra oficial.

Puntos pedidos: cada parte de un pedido con minuto citado que contiene una
forma de vos del diccionario ("querés", "vení", "anotate", "sos") cuando la
letra entregada tiene, a ±4 s, la forma de tú correspondiente. Para cada
uno: no detectado / sugerido / obligatorio (y si un oído ya lo proponía
antes de la regla).

Falsos positivos: puntos de voseo que la revisión marca sobre la versión
FINAL que UMG aceptó, por canción y por portal. En Chile la regla no corre;
también se informa cuánto daría si corriera, para ver por qué no.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import unicodedata
from collections import Counter, defaultdict

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import lyric_review  # noqa: E402
from voseo_lexicon import VOS_FORMS  # noqa: E402

_TS = re.compile(r"(\d{1,2}):(\d{2})")
_WORD = re.compile(r"[^\W\d_]+", re.UNICODE)


def _nfc_lower(text: str) -> str:
    return unicodedata.normalize("NFC", str(text or "")).lower()


def request_parts(comment: str) -> list[tuple[list[int], str]]:
    parts = re.split(
        r"\s*//\s*|\n|\s+-\s+(?=\d)|(?<=[.\"”])\s+(?=\d{1,2}:\d\d)", comment or "")
    out = []
    for part in parts:
        times = [int(m.group(1)) * 60 + int(m.group(2)) for m in _TS.finditer(part)]
        if times:
            out.append((times, part.strip()))
    return out


def _lines_near(segments, times, window=4.0):
    hits = []
    for i, seg in enumerate(segments):
        start = float(seg.get("start") or 0)
        end = float(seg.get("end") or start)
        if any(start - window <= t <= end + window for t in times):
            hits.append(i)
    return hits


def _line_words(text: str) -> list[str]:
    return [_nfc_lower(w) for w in _WORD.findall(unicodedata.normalize("NFC", str(text or "")))]


def _same_line(text: str, asked: set[str], tu_forms: set[str]) -> bool:
    """La línea entregada es la que cita el pedido: comparte alguna palabra
    con contenido además de la de voseo (o es una línea de una o dos
    palabras)."""
    words = set(_line_words(text)) - tu_forms
    return len(words) <= 2 or bool(words & asked)


def _strip(segments):
    out = []
    for s in segments or []:
        if isinstance(s, dict) and s.get("start") is not None and s.get("end") is not None:
            out.append({k: v for k, v in s.items() if k != "qa_dismissed"})
    return out


def _segments(value):
    return value if isinstance(value, list) else (value or {}).get("segments", [])


def _delivered(job: dict, rev) -> list[dict] | None:
    versions = job.get("versions") or [job.get("delivered_version")]
    for v in versions:
        if isinstance(v, dict) and v.get("rev") == rev:
            return _strip(_segments(v.get("segments")))
    return None


def _review(segments, job, target, official, portal):
    return lyric_review.build_review(
        [dict(s) for s in segments],
        original_segments=(job or {}).get("original"),
        machine_evidence=(job or {}).get("evidence"),
        title=target.get("song", ""),
        official_text=official or None,
        portal=portal,
    )


def _hits(review, lines, vos_form, *, kinds=None, exclude=None):
    """Nivel del mejor punto que deja ``vos_form`` en alguna de ``lines``."""
    best = None
    for item in review["items"]:
        if kinds is not None and item["kind"] not in kinds:
            continue
        if exclude is not None and item["kind"] in exclude:
            continue
        for occ in item["occurrences"]:
            if occ.get("line_index") in lines and vos_form in _line_words(occ.get("after")) \
                    and vos_form not in _line_words(occ.get("before")):
                level = "required" if item["required"] else "suggested"
                if best != "required":
                    best = level
    return best


def measure(targets: dict, jobs: dict, officials: dict) -> dict:
    points = Counter()
    rows = []
    reviews: dict[tuple, dict] = {}
    for job_id, target in targets.items():
        portal = target.get("portal") or "?"
        job = jobs.get(job_id) or {}
        official = (officials.get(job_id) or {}).get("text") or ""
        seen = set()
        for req in target.get("requests") or []:
            # Cada pedido se mide sobre la versión que UMG tenía delante.
            rev = req.get("delivered_rev", target.get("delivered_rev"))
            delivered = _delivered(job, rev)
            if delivered is None and rev == target.get("delivered_rev"):
                delivered = _strip(target.get("delivered"))
            if not delivered:
                continue
            for times, text in request_parts(req.get("comment", "")):
                for word in set(_line_words(text)):
                    tu_forms = VOS_FORMS.get(word)
                    if not tu_forms:
                        continue
                    asked = {w for w in _line_words(text) if len(w) >= 4} - {word}
                    near = [i for i in _lines_near(delivered, times)
                            if _same_line(delivered[i].get("text"), asked, tu_forms)]
                    lines = [i for i in near if set(_line_words(delivered[i].get("text"))) & tu_forms]
                    if not lines:
                        # El pedido trae una forma de vos pero la entrega no
                        # tenía la de tú: ya estaba bien u oyó otra palabra.
                        already = any(word in _line_words(delivered[i].get("text")) for i in near)
                        mention = (rev, word, times[0])
                        if mention not in seen:
                            seen.add(mention)
                            points[(portal, "vos_mention_" + ("already_vos" if already else "other_word"))] += 1
                        continue
                    key = (rev, word, tuple(lines))
                    if key in seen:
                        continue
                    seen.add(key)
                    for mode in ("argentina", "baseline"):
                        if (job_id, rev, mode) not in reviews:
                            reviews[(job_id, rev, mode)] = _review(
                                delivered, job, target, official,
                                portal if mode == "argentina" else "umg")
                    rule = _hits(reviews[(job_id, rev, "argentina")], lines, word, kinds={"voseo"}) \
                        if portal == "argentina" else None
                    any_item = _hits(reviews[(job_id, rev, "argentina" if portal == "argentina" else "baseline")],
                                     lines, word)
                    before = _hits(reviews[(job_id, rev, "baseline")], lines, word)
                    outcome = rule or "not_detected"
                    points[(portal, "points")] += 1
                    points[(portal, "unique_points")] += int(
                        (word, delivered[lines[0]].get("text", "").strip().lower()) not in seen)
                    seen.add((word, delivered[lines[0]].get("text", "").strip().lower()))
                    points[(portal, outcome)] += 1
                    points[(portal, "any_item_" + (any_item or "none"))] += 1
                    points[(portal, "before_rule_" + (before or "none"))] += 1
                    rows.append({
                        "job": job_id, "portal": portal, "vos": word,
                        "line": delivered[lines[0]].get("text"), "outcome": outcome,
                        "before_rule": before or "none", "request": text[:100],
                    })
    return {"points": {f"{p}:{k}": v for (p, k), v in sorted(points.items())}, "rows": rows}


def _evidence(item: dict) -> str:
    sources = set(item.get("sources") or ())
    if not sources:
        return "solo_voseo_de_la_cancion"
    if "official" in sources:
        return "letra_oficial" + ("+oido" if sources - {"official"} else "")
    return "oidos" if len(sources) >= 2 else "un_oido"


def measure_false_positives(targets: dict, jobs: dict, officials: dict) -> dict:
    """Puntos de voseo sobre la versión FINAL aceptada. Se separa lo que UMG
    había pedido en algún pedido de esa canción y quedó sin aplicar (la
    final no es un oro: a veces le falta una corrección pedida)."""
    songs = Counter()
    counts: dict[str, Counter] = defaultdict(Counter)
    samples: dict[str, list] = defaultdict(list)
    for job_id, target in targets.items():
        final = _strip(target.get("final"))
        if not final:
            continue
        portal = target.get("portal") or "?"
        job = jobs.get(job_id) or {}
        official = (officials.get(job_id) or {}).get("text") or ""
        asked = set()
        for req in target.get("requests") or []:
            asked |= set(_line_words(req.get("comment", "")))
        songs[portal] += 1
        runs = [(portal, portal)]
        if portal != "argentina":
            runs.append((f"{portal}_si_fuera_argentina", "argentina"))
        for label, as_portal in runs:
            review = _review(final, job, target, official, as_portal)
            voseo = [i for i in review["items"] if i["kind"] == "voseo"]
            c = counts[label]
            for item in voseo:
                level = "required" if item["required"] else "suggested"
                vos = _line_words(item["occurrences"][0]["after"])
                before = set(_line_words(item["occurrences"][0]["before"]))
                new = [w for w in vos if w not in before]
                requested = bool(set(new) & asked)
                c[level + "_items"] += 1
                c[level + "_lines"] += len(item["occurrences"])
                c[f"{level}_{'pedido_por_umg_sin_aplicar' if requested else 'no_pedido'}"] += 1
                c[f"{level}_evidencia_{_evidence(item)}"] += 1
                if len(samples[label]) < 40:
                    occ = item["occurrences"][0]
                    samples[label].append(
                        f"{level[:3]} {job_id[:6]} {'PEDIDO ' if requested else ''}"
                        f"{occ['before']!r} -> {occ['after']!r} [{item['why']}]")
            c["songs_with_any"] += int(bool(voseo))
            c["songs_with_required"] += int(any(i["required"] for i in voseo))
    per_song = {}
    for label, c in counts.items():
        n = songs[label.split("_si_")[0]]
        per_song[label] = {k: round(v / max(1, n), 3) for k, v in c.items()
                           if k.endswith(("_items", "_lines")) or k.startswith("songs")}
    return {"songs": dict(songs), "counts": {k: dict(v) for k, v in counts.items()},
            "per_song": per_song, "samples": dict(samples)}


def measure_delivered_load(targets: dict, jobs: dict, officials: dict) -> dict:
    """Puntos de voseo por canción sobre la letra ENTREGADA (lo que ve el
    revisor antes de aprobar)."""
    c: dict[str, Counter] = defaultdict(Counter)
    for job_id, target in targets.items():
        portal = target.get("portal") or "?"
        job = jobs.get(job_id) or {}
        delivered = _delivered(job, target.get("delivered_rev")) or _strip(target.get("delivered"))
        if not delivered:
            continue
        official = (officials.get(job_id) or {}).get("text") or ""
        review = _review(delivered, job, target, official, portal)
        voseo = [i for i in review["items"] if i["kind"] == "voseo"]
        c[portal]["songs"] += 1
        c[portal]["required_items"] += sum(1 for i in voseo if i["required"])
        c[portal]["suggested_items"] += sum(1 for i in voseo if not i["required"])
        c[portal]["songs_with_any"] += int(bool(voseo))
    return {p: dict(v) for p, v in c.items()}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--targets", required=True)
    parser.add_argument("--jobs", required=True)
    parser.add_argument("--official")
    parser.add_argument("--rows", action="store_true")
    args = parser.parse_args()
    with open(args.targets) as fh:
        targets = json.load(fh)
    with open(args.jobs) as fh:
        jobs = json.load(fh)
    officials = {}
    if args.official:
        with open(args.official) as fh:
            officials = json.load(fh)
    report = {"requested": measure(targets, jobs, officials),
              "false_positives": measure_false_positives(targets, jobs, officials),
              "delivered_load": measure_delivered_load(targets, jobs, officials)}
    if not args.rows:
        report["requested"]["rows"] = [r for r in report["requested"]["rows"]
                                       if r["outcome"] == "not_detected"]
    print(json.dumps(report, ensure_ascii=False, indent=1, default=str))


if __name__ == "__main__":
    main()
