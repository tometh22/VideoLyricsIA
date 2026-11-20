"""Mide los avisos de sincronización contra los pedidos de UMG.

Uso (los datos son privados y NO viven en el repo)::

    python scripts/measure_lyric_timing_review.py --targets /ruta/targets.json \
        --jobs /ruta/jobs.json [--split all|even|odd] [--json]

``targets.json``: por job la letra ENTREGADA (``delivered``, ``delivered_rev``),
la FINAL que UMG aceptó (``final``) y los pedidos (``requests[].comment``).
``jobs.json``: por job ``versions`` (``rev`` + ``segments`` con ``words``) y
``evidence`` (``machine_evidence``). Si está instalado ``ijson`` se lee en
streaming (el archivo completo pesa GB); si no, ``json.load``.

Qué mide:

- **Aciertos**: de cada punto de timing de UMG ("alargar hasta 3:13",
  "entra tarde", "que coincida con la voz"…) y de cada "Desde X hasta Y:
  texto" (Argentina), si algún aviso sobre la letra ENTREGADA cae cerca del
  tiempo citado: la línea avisada contiene el tiempo (±1,5 s) o el tiempo
  medido/propuesto está a ±1,5 s.
- **Control por azar**: lo mismo con los tiempos citados corridos ±50 s
  (dentro de la canción). La diferencia contra el control es lo que aporta
  el detector; el control mide cuánto acierta marcar cualquier cosa.
- **Falsos positivos**: avisos por canción sobre la versión FINAL (UMG ya la
  aceptó: todo lo que se marque ahí es, para el cliente, ruido).
- **Dirección**: de los avisos sobre la entregada, cuántos coinciden con lo
  que terminó corrigiendo el operador (el mismo borde, en el mismo sentido,
  ≥0,25 s) y cuántos quedaron a ±0,6 s del tiempo final.
"""
from __future__ import annotations

import argparse
import difflib
import hashlib
import json
import os
import re
import sys
import unicodedata
from collections import Counter

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import lyric_timing_review as ltr  # noqa: E402
from heard_words import norm_token  # noqa: E402

PAD_S = 1.5
CONTROL_SHIFTS = (50.0, -50.0)
_TC = re.compile(r"(?<![\d:])(\d{1,2}):(\d{2})(?!\d)")
_RANGE = re.compile(r"\b(?:desde\s+)?(\d{1,2}):(\d{2})\s*(?:hasta|a)\s+(\d{1,2}):(\d{2})", re.IGNORECASE)
# Palabras de un pedido de sincronización (texto plegado, sin tildes).
_TIMING = [re.compile(p) for p in (
    r"sincroniz", r"\bsync", r"\balarg", r"\bextender", r"\badelant", r"\bretras",
    r"\batras(ar|a)\b", r"\bentr[ea]\b.{0,25}\b(antes|tarde|despues)",
    r"\baparezca\b.{0,12}(en|antes|despues|mas|un|\d)", r"desaparec", r"\bdur[ae]\b",
    r"\btermin[ea]\b.{0,20}(en|antes|cuando|puntual|\d)", r"\bcomien(za|ce)\b.{0,12}(en|antes|\d)",
    r"\barranqu", r"coincid", r"no se escucha", r"sin (el )?audio", r"sin voz",
    r"\bantes de tiempo", r"\bllega tarde", r"\bentra (muy )?tarde", r"mas tiempo",
    r"menos tiempo", r"\bcalce\b", r"\bdesfas", r"\bsalir en\b", r"\bmover la linea",
    r"antes de que cante", r"\bdejarla hasta",
    r"^(eliminar|borrar|sacar) (la )?(frase|letra|linea)s? (que aparece )?(en|a partir)",
)]
_NOT_TIMING = re.compile(r"\bcomienza con\b")


def fold(text: str) -> str:
    text = unicodedata.normalize("NFKD", str(text or "").lower())
    return "".join(c for c in text if not unicodedata.combining(c))


def split_points(comment: str) -> list[str]:
    parts = re.split(r"\n+|\s//\s|\s-\s(?=\d{1,2}:\d{2})", comment or "")
    return [p.strip(" -\t\"'") for p in parts if p.strip(" -\t\"'")]


def classify(point: str) -> tuple[str, list[float], tuple[float, float] | None]:
    folded = fold(point)
    times = [int(m.group(1)) * 60 + int(m.group(2)) for m in _TC.finditer(point)]
    rng = _RANGE.search(point)
    span = None
    if rng:
        span = (int(rng.group(1)) * 60 + int(rng.group(2)), int(rng.group(3)) * 60 + int(rng.group(4)))
    if any(p.search(folded) for p in _TIMING) and not _NOT_TIMING.search(folded):
        return "timing", times, span
    if span is not None:
        return "desde_hasta", times, span
    return "other", times, span


def _iter_jobs(path: str, wanted: set[str]):
    try:
        import ijson  # type: ignore
    except ImportError:  # pragma: no cover - depende del entorno
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
        for job_id, job in data.items():
            if job_id in wanted:
                yield job_id, job
        return
    with open(path, "rb") as fh:
        for job_id, job in ijson.kvitems(fh, "", use_float=True):
            if job_id in wanted:
                yield job_id, job


def _near(findings: list[dict], segments: list[dict], point: dict, shift: float = 0.0) -> bool:
    times = [t + shift for t in point["times"]]
    for f in findings:
        seg = segments[f["line"]]
        lo, hi = float(seg["start"]) - PAD_S, float(seg["end"]) + PAD_S
        if any(lo <= t <= hi for t in times):
            return True
        if any(abs(f[k] - t) <= PAD_S for t in times for k in ("voice", "proposed")):
            return True
        if point["range"]:
            a, b = point["range"][0] + shift, point["range"][1] + shift
            if hi >= a and lo <= b:
                return True
    return False


def _key(text: str) -> str:
    return " ".join(t for t in (norm_token(p) for p in str(text or "").split()) if t)


def match_lines(a: list[dict], b: list[dict]) -> dict[int, int]:
    """Línea entregada → línea final, por texto y después por solapamiento."""
    ka, kb = [_key(x.get("text")) for x in a], [_key(x.get("text")) for x in b]
    out: dict[int, int] = {}
    for tag, i1, i2, j1, j2 in difflib.SequenceMatcher(None, ka, kb, autojunk=False).get_opcodes():
        if tag == "equal":
            out.update({i1 + k: j1 + k for k in range(i2 - i1)})
        elif tag == "replace":
            for i in range(i1, i2):
                best, score = None, 0.0
                for j in range(j1, j2):
                    overlap = min(a[i]["end"], b[j]["end"]) - max(a[i]["start"], b[j]["start"])
                    ratio = difflib.SequenceMatcher(None, ka[i], kb[j]).ratio()
                    if overlap > 0 and ratio >= 0.5 and overlap * ratio > score:
                        best, score = j, overlap * ratio
                if best is not None:
                    out[i] = best
    return out


def measure(targets: dict, jobs_path: str, split: str = "all") -> dict:
    def keep(job_id: str) -> bool:
        if split == "all":
            return True
        parity = int(hashlib.sha1(job_id.encode()).hexdigest(), 16) % 2
        return parity == (0 if split == "even" else 1)

    wanted = {j for j in targets if keep(j)}
    points: list[dict] = []
    for job_id in wanted:
        for req in targets[job_id].get("requests") or []:
            for text in split_points(req.get("comment") or ""):
                cat, times, span = classify(text)
                if cat != "other" and times:
                    points.append({"job": job_id, "cat": cat, "times": times, "range": span})
    stats: Counter = Counter()
    per_kind_final: Counter = Counter()
    per_kind_delivered: Counter = Counter()
    flags: dict[str, tuple[list[dict], list[dict]]] = {}
    songs = 0
    for job_id, job in _iter_jobs(jobs_path, wanted):
        versions = {str(v.get("rev")): v for v in job.get("versions") or []}
        target = targets[job_id]
        delivered = (versions.get(str(target["delivered_rev"])) or {}).get("segments") or []
        final = (job.get("versions") or [{}])[-1].get("segments") or []
        evidence = job.get("evidence")
        if not delivered or not final:
            continue
        songs += 1
        found = ltr.find_timing_issues(delivered, evidence)
        flags[job_id] = (found, delivered)
        on_final = ltr.find_timing_issues(final, evidence)
        stats["final_findings"] += len(on_final)
        stats["final_required"] += sum(1 for f in on_final if f["required"])
        stats["final_songs_flagged"] += bool(on_final)
        per_kind_final.update(f["kind"] for f in on_final)
        per_kind_delivered.update(f["kind"] for f in found)
        stats["delivered_findings"] += len(found)
        stats["delivered_required"] += sum(1 for f in found if f["required"])
        matched = match_lines(delivered, final)
        for f in found:
            j = matched.get(f["line"])
            if j is None:
                continue
            now = f["current"]
            fin = float(final[j]["start" if f["field"] == "start" else "end"])
            moved = fin - now
            if abs(moved) >= 0.25 and (moved > 0) == (f["proposed"] > now):
                stats["direction_ok"] += 1
                stats["direction_ok_required"] += bool(f["required"])
                stats["within_0_6"] += abs(fin - f["proposed"]) <= 0.6
    result: dict = {"songs": songs, "stats": dict(stats),
                    "final_by_kind": dict(per_kind_final), "delivered_by_kind": dict(per_kind_delivered)}
    for cat in ("timing", "desde_hasta"):
        pts = [p for p in points if p["job"] in flags]
        pts = [p for p in pts if p["cat"] == cat]
        row = {"points": len(pts)}
        for tier in ("all", "required"):
            def pick(job_id: str, tier: str = tier) -> list[dict]:
                found, _ = flags[job_id]
                return found if tier == "all" else [f for f in found if f["required"]]
            row[f"hit_{tier}"] = sum(_near(pick(p["job"]), flags[p["job"]][1], p) for p in pts)
            ctrl = []
            for shift in CONTROL_SHIFTS:
                n = ok = 0
                for p in pts:
                    segs = flags[p["job"]][1]
                    dur = max(float(s["end"]) for s in segs)
                    if not all(0 <= t + shift <= dur for t in p["times"]):
                        continue
                    n += 1
                    ok += _near(pick(p["job"]), segs, p, shift)
                ctrl.append(ok / n if n else 0.0)
            row[f"control_{tier}"] = round(sum(ctrl) / len(ctrl), 4)
        result[cat] = row
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--targets", required=True)
    parser.add_argument("--jobs", required=True)
    parser.add_argument("--split", choices=("all", "even", "odd"), default="all")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    with open(args.targets, encoding="utf-8") as fh:
        targets = json.load(fh)
    result = measure(targets, args.jobs, args.split)
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    songs = result["songs"] or 1
    s = result["stats"]
    print(f"Canciones: {result['songs']}")
    for cat, label in (("timing", "Puntos de timing"), ("desde_hasta", '"Desde X hasta Y"')):
        row = result[cat]
        n = row["points"] or 1
        print(f"{label}: {row['points']}")
        print(f"  marcados (todos):        {row['hit_all']:3d} = {row['hit_all'] / n:5.1%}"
              f"  control ±50 s: {row['control_all']:5.1%}")
        print(f"  marcados (obligatorios): {row['hit_required']:3d} = {row['hit_required'] / n:5.1%}"
              f"  control ±50 s: {row['control_required']:5.1%}")
    print(f"Avisos sobre la ENTREGADA: {s.get('delivered_findings', 0)} "
          f"({s.get('delivered_findings', 0) / songs:.2f}/canción; obligatorios {s.get('delivered_required', 0)})")
    print(f"  coinciden con la corrección final: {s.get('direction_ok', 0)} "
          f"(obligatorios {s.get('direction_ok_required', 0)}; a ±0,6 s del tiempo final {s.get('within_0_6', 0)})")
    print(f"Avisos sobre la FINAL aceptada: {s.get('final_findings', 0)} "
          f"({s.get('final_findings', 0) / songs:.2f}/canción; obligatorios {s.get('final_required', 0)} = "
          f"{s.get('final_required', 0) / songs:.2f}/canción; canciones con algún aviso {s.get('final_songs_flagged', 0)})")
    print(f"  por tipo: {result['final_by_kind']}  (entregada: {result['delivered_by_kind']})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
