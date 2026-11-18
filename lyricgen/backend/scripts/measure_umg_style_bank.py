"""Mide las reglas de estilo y de metadatos contra el banco de pedidos de UMG.

Uso (los datos son privados y NO viven en el repo)::

    python scripts/measure_umg_style_bank.py --targets /ruta/targets.json \
        [--events /ruta/pub_events.csv] [--json]

``targets.json``: por job, la letra ENTREGADA (``delivered``), la versión
final tras las correcciones (``final``), el portal y los pedidos.
``pub_events.csv``: eventos ``delivery.create``/``delivery.update`` con el
artista y el título publicados en cada versión (familia 1).

Familia 2 (estilo): cada punto de un pedido que habla de signos de
pregunta, punto final, paréntesis o tildes se busca en la letra entregada
(±4 s del minuto citado). Si el error está, se corre la Revisión rápida y
se clasifica: no detectado / sugerido / obligatorio, y si el arreglo de un
clic deja la línea como la pidió UMG.

Falsos positivos: puntos de estilo que la Revisión rápida sigue marcando
sobre la versión FINAL (la que UMG ya aceptó), por canción.

Familia 1 (metadatos): cada cambio de artista/título publicado se pasa por
``umg_metadata.normalize`` con el valor ANTERIOR; cuenta si sale el valor
pedido. Además, sobre todas las publicaciones, cuántas cambiaría.
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import re
import sys
import unicodedata
from collections import Counter, defaultdict

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import lyric_review  # noqa: E402

STYLE_KINDS = {
    "question_marks", "terminal_period", "parentheses", "accent_missing",
    "accent_inconsistent",
}
_TS = re.compile(r"(\d{1,2}):(\d{2})")
_PATTERNS = [
    ("question_add", re.compile(
        r"(?i)(agreg\w*|añad\w*)[^.]{0,20}signos de interrogaci")),
    ("remove_question", re.compile(
        r"(?i)((sac\w*|quit\w*|elimin\w*)[^.]{0,30}signos? de (pregunta|interrogaci)"
        r"|sin signos de interrogaci|no es (una )?pregunta)")),
    ("terminal_period", re.compile(r"(?i)punto final")),
    ("parentheses", re.compile(r"(?i)(sac\w*|quit\w*)[^.]{0,25}par[eé]ntesis")),
    ("accent_add", re.compile(
        r"(?i)(agreg\w*|añad\w*|falta\w*)[^.]{0,20}(tilde|acento)(?! al nombre)(?! del artista)")),
]


def fold(text: str) -> str:
    text = unicodedata.normalize("NFKD", str(text or "").lower())
    return "".join(c for c in text if not unicodedata.combining(c))


def request_items(comment: str) -> list[tuple[str, list[int], str]]:
    parts = re.split(
        r"\s*//\s*|\n|\s+-\s+(?=\d)|(?<=[.\"”])\s+(?=\d{1,2}:\d\d)", comment or "")
    out = []
    for part in parts:
        for kind, rx in _PATTERNS:
            if rx.search(part) and "artista" not in part.lower() and "título" not in part.lower():
                times = [int(m.group(1)) * 60 + int(m.group(2)) for m in _TS.finditer(part)]
                out.append((kind, times, part.strip()))
                break
    return out


def _accent_target(text: str) -> str | None:
    """La palabra con tilde que UMG pide ("ARDÍAN", "ESTÁ", "Descubriéndose")."""
    for word in re.findall(r"[^\W\d_]+", text):
        if any(c in "áéíóúÁÉÍÓÚ" for c in word) and fold(word) not in {
                "tilde", "acento", "linea", "frase", "despues", "esta", "tambien"}:
            return word
    for word in re.findall(r"\"([^\"]+)\"", text):
        return word.split()[0]
    return None


def _has_error(kind: str, text: str, target: str | None) -> bool:
    if kind == "remove_question":
        return "?" in text or "¿" in text
    if kind == "terminal_period":
        return text.rstrip().endswith(".") and not text.rstrip().endswith("..")
    if kind == "parentheses":
        return "(" in text or ")" in text
    if kind == "accent_add":
        if not target:
            return False
        words = re.findall(r"[^\W\d_]+", text)
        return any(fold(w) == fold(target) and w.lower() != target.lower() for w in words)
    if kind == "question_add":
        return "¿" not in text
    return False


def _fixed(kind: str, after: str, target: str | None) -> bool:
    if kind == "remove_question":
        return "?" not in after and "¿" not in after
    if kind == "terminal_period":
        return not _has_error("terminal_period", after, None)
    if kind == "parentheses":
        return "(" not in after and ")" not in after
    if kind == "accent_add":
        return target is not None and any(
            w.lower() == target.lower() for w in re.findall(r"[^\W\d_]+", after))
    if kind == "question_add":
        return "¿" in after and "?" in after
    return False


def _review(segments, title, portal):
    kwargs = {"title": title}
    if "portal" in lyric_review.build_review.__code__.co_varnames:
        kwargs["portal"] = portal
    return lyric_review.build_review([dict(s) for s in segments], **kwargs)


def _lines_near(segments, times, window=4.0):
    if not times:
        return list(range(len(segments)))
    hits = []
    for i, seg in enumerate(segments):
        start = float(seg.get("start") or 0)
        end = float(seg.get("end") or start)
        if any(start - window <= t <= end + window for t in times):
            hits.append(i)
    return hits


RANK = ["not_detected", "suggested_wrong_fix", "suggested_alternative_fixes",
        "suggested_fixes", "required_wrong_fix", "required_alternative_fixes",
        "required_fixes"]


def measure_style(targets: dict) -> dict:
    status = Counter()
    by_kind: dict[str, Counter] = defaultdict(Counter)
    rows = []
    for job_id, v in targets.items():
        delivered = v.get("delivered") or []
        review = None
        for req in v.get("requests") or []:
            for kind, times, text in request_items(req.get("comment", "")):
                target = _accent_target(text) if kind == "accent_add" else None
                lines = [i for i in _lines_near(delivered, times)
                         if _has_error(kind, delivered[i].get("text", ""), target)]
                if not lines:
                    outcome = "error_absent_in_delivered"
                else:
                    if review is None:
                        review = _review(delivered, v.get("song", ""), v.get("portal"))
                    outcome = "not_detected"
                    for item in review["items"]:
                        if item["kind"] not in STYLE_KINDS:
                            continue
                        for occ in item["occurrences"]:
                            if occ["line_index"] in lines:
                                fixed = _fixed(kind, occ["after"], target)
                                alt = any(
                                    _fixed(kind, lyric_review._preview(
                                        delivered, occ["line_index"], alt["fix"])[1], target)
                                    for alt in item.get("alternatives") or [] if alt.get("fix"))
                                level = "required" if item["required"] else "suggested"
                                suffix = "_fixes" if fixed else "_alternative_fixes" if alt else "_wrong_fix"
                                candidate = level + suffix
                                if RANK.index(candidate) > RANK.index(outcome):
                                    outcome = candidate
                status[outcome] += 1
                by_kind[kind][outcome] += 1
                rows.append({"job": job_id, "portal": v.get("portal"), "kind": kind,
                             "outcome": outcome, "text": text[:90]})
    return {"status": dict(status), "by_kind": {k: dict(c) for k, c in by_kind.items()},
            "rows": rows}


def measure_false_positives(targets: dict) -> dict:
    per_portal: dict[str, Counter] = defaultdict(Counter)
    kinds: dict[str, Counter] = defaultdict(Counter)
    songs: Counter = Counter()
    samples: dict[str, list] = defaultdict(list)
    for v in targets.values():
        final = v.get("final") or []
        if not final:
            continue
        portal = v.get("portal") or "?"
        songs[portal] += 1
        review = _review(final, v.get("song", ""), portal)
        for item in review["items"]:
            if item["kind"] not in STYLE_KINDS:
                continue
            level = "required" if item["required"] else "suggested"
            n = len(item["occurrences"])
            per_portal[portal][level + "_items"] += 1
            per_portal[portal][level + "_lines"] += n
            kinds[f"{portal}:{item['kind']}"][level] += 1
            if len(samples[item["kind"]]) < 12:
                occ = item["occurrences"][0]
                samples[item["kind"]].append(f"{portal[:2]} {level[:3]}: {occ['before']!r} -> {occ['after']!r}")
    return {
        "songs": dict(songs),
        "per_portal": {p: dict(c) for p, c in per_portal.items()},
        "per_song": {p: {k: round(c / max(1, songs[p]), 2) for k, c in per_portal[p].items()}
                     for p in per_portal},
        "kinds": {k: dict(c) for k, c in kinds.items()},
        "samples": dict(samples),
    }


def measure_metadata(events_path: str) -> dict:
    try:
        import umg_metadata
    except ImportError:
        return {"error": "umg_metadata no existe en esta versión"}
    by_job: dict[str, list] = defaultdict(list)
    published = Counter()
    with open(events_path, newline="") as fh:
        for row in csv.DictReader(fh):
            try:
                detail = json.loads(row["detail"])
            except (KeyError, ValueError):
                continue
            if not detail.get("job_id"):
                continue
            by_job[detail["job_id"]].append((row["created_at"], detail.get("artist") or "",
                                              detail.get("song") or ""))
    corrections = []
    for job_id, events in by_job.items():
        events.sort()
        last = None
        for _, artist, song in events:
            if last and (artist, song) != last:
                corrections.append((job_id, last, (artist, song)))
            last = (artist, song)
        published[(last[0], last[1])] += 1
    caught = []
    for job_id, before, after in corrections:
        result = umg_metadata.normalize(before[0], before[1])
        ok = (result.artist, result.title) == after
        # "LOS ENANITOS VERDES" / "Coti - Ft. …": lo que tipeó el operador al
        # aplicar el pedido; el catálogo da la misma escritura con otra caja.
        same = ok or (umg_metadata.key(result.artist), umg_metadata.key(result.title)) == (
            umg_metadata.key(after[0]), umg_metadata.key(after[1])) and (
            result.artist, result.title) != before
        caught.append({"job": job_id, "before": before, "after": after,
                       "normalized": (result.artist, result.title), "caught": ok,
                       "caught_same_spelling": same})
    changed_now = Counter()
    blocking = 0
    for (artist, title), n in published.items():
        result = umg_metadata.normalize(artist, title)
        if (result.artist, result.title) != (artist, title):
            changed_now[f"{artist} | {title} -> {result.artist} | {result.title}"] += n
            if any(c["reason"] not in {"whitespace", "case"} for c in result.changes):
                blocking += n
    return {
        "corrections": len(corrections),
        "caught": sum(1 for c in caught if c["caught"]),
        "caught_same_spelling": sum(1 for c in caught if c["caught_same_spelling"]),
        "rows": caught,
        "current_publications": sum(published.values()),
        "current_publications_changed": sum(changed_now.values()),
        "current_publications_blocked_until_rerender": blocking,
        "changed_samples": changed_now.most_common(40),
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--targets", required=True)
    parser.add_argument("--events")
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--rows", action="store_true")
    args = parser.parse_args()
    with open(args.targets) as fh:
        targets = json.load(fh)
    report = {"style": measure_style(targets), "false_positives": measure_false_positives(targets)}
    if args.events:
        report["metadata"] = measure_metadata(args.events)
    if not args.rows:
        report["style"].pop("rows", None)
        if "metadata" in report:
            report["metadata"]["rows"] = [r for r in report["metadata"].get("rows", []) if not r["caught"]]
    print(json.dumps(report, ensure_ascii=False, indent=1, default=str))


if __name__ == "__main__":
    main()
