"""Backtest de la verificación punto por punto sobre un banco de pedidos.

Read-only y offline: no toca ninguna base. Lee un CSV exportado de
``delivery_change_requests`` (+ delivery) y un JSON con las versiones del
editor de cada job, y responde:

1. De los pedidos que repiten un tiempo ya pedido en un pedido anterior del
   mismo video, ¿en cuántos la verificación habría marcado ✘ al cerrar el
   pedido anterior? (contra la última versión aprobada al momento del cierre)
2. Falsos ✘: pedidos cerrados que NO volvieron a reclamarse, ¿cuántos habrían
   quedado con algún ✘?
3. ¿Cuántos puntos de texto piden la misma corrección en otra repetición de
   la misma línea (estribillo)?

Uso::

    python scripts/backtest_change_request_verification.py requests.csv jobs.json [--since 2026-09-01]

``requests.csv`` columnas (en este orden): id, delivery_id, job_id,
portal_id, tenant_snapshot, category, label, artist_snapshot,
song_title_snapshot, added_at, published_revision, removed_at, submitted_at,
resolved_at, resolution_source, resolved_by_user_id, comment, resolution_note.

``jobs.json``: ``{job_id: {"versions": [{"rev", "at", "approved", "segments"}]}}``.
"""
from __future__ import annotations

import argparse
import collections
import csv
import json
import os
import re
import sys
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import change_request_publish_guard as guard  # noqa: E402
import change_request_verification as verification  # noqa: E402

COLUMNS = ["id", "delivery_id", "job_id", "portal", "tenant", "category", "label", "artist", "song", "added_at",
           "published_revision", "removed_at", "submitted_at", "resolved_at", "source", "resolved_by", "comment", "note"]
NOISE = re.compile(r"\bqa test\b|\bignorar\b|\btest post-release\b", re.I)
TIME = re.compile(r"(?<![\d:])(?:\d{1,2}:)?\d{1,2}:[0-5]\d(?!\d)")
REPEAT_WINDOW = 3.0


def parse_time(value: str | None) -> datetime | None:
    if not value:
        return None
    value = value.strip().replace(" ", "T", 1)
    if re.search(r"[+-]\d\d$", value):
        value += ":00"
    value = re.sub(r"\.(\d+)", lambda m: "." + m.group(1)[:6].ljust(6, "0"), value)
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def seconds(raw: str) -> float:
    parts = [int(v) for v in raw.split(":")]
    return float(parts[-1] + 60 * parts[-2] + (3600 * parts[0] if len(parts) == 3 else 0))


def cited(comment: str) -> list[float]:
    return [seconds(t) for t in TIME.findall(comment or "")]


def version_at(job: dict | None, at: datetime | None, *, approved: bool = True) -> dict | None:
    if not job or at is None:
        return None
    rows = []
    for row in job.get("versions") or []:
        stamp = parse_time(row.get("at"))
        if stamp is None or stamp > at or (approved and not row.get("approved")):
            continue
        rows.append((stamp, row.get("rev") or 0, row))
    return max(rows, key=lambda item: (item[0], item[1]))[2] if rows else None


def is_art(row: dict) -> bool:
    return "art" in (row["category"] or "") or row["label"] == "Art Track"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("requests_csv")
    parser.add_argument("jobs_json")
    parser.add_argument("--since", default="2026-09-01")
    parser.add_argument("--examples", type=int, default=0)
    args = parser.parse_args()
    since = parse_time(args.since)

    with open(args.requests_csv, newline="") as handle:
        rows = [dict(zip(COLUMNS, row)) for row in list(csv.reader(handle))[1:]]
    with open(args.jobs_json) as handle:
        jobs = json.load(handle)
    rows = [r for r in rows if not NOISE.search(r["comment"] or "") and not is_art(r)]
    for row in rows:
        row["sub"] = parse_time(row["submitted_at"])
        row["res"] = parse_time(row["resolved_at"])
        row["times"] = cited(row["comment"])
    by_video = collections.defaultdict(list)
    for row in rows:
        by_video[(row["job_id"], row["portal"])].append(row)
    for items in by_video.values():
        items.sort(key=lambda r: (r["sub"], int(r["id"])))

    window = [r for r in rows if r["sub"] and r["sub"] >= since]
    print(f"pedidos desde {args.since}: {len(window)}")

    def check(request: dict, *, at: datetime | None) -> dict | None:
        job = jobs.get(request["job_id"])
        after = version_at(job, at)
        if after is None:
            return None
        before = version_at(job, request["sub"]) or version_at(job, request["sub"], approved=False)
        return verification.verify(request["comment"], after["segments"],
                                   before_segments=before["segments"] if before else None,
                                   metadata={"artist": request["artist"], "song_title": request["song"]})

    second, repeats, repeat_rows = 0, 0, []
    reclaimed_ids: set[str] = set()
    for request in window:
        previous = [p for p in by_video[(request["job_id"], request["portal"])] if p["sub"] < request["sub"]]
        if not previous:
            continue
        second += 1
        hit = None
        for prior in reversed(previous):
            shared = [t for t in request["times"] if any(abs(t - u) <= REPEAT_WINDOW for u in prior["times"])]
            if shared:
                hit = (prior, shared)
                break
        if hit:
            repeats += 1
            repeat_rows.append((request, *hit))
            reclaimed_ids.add(hit[0]["id"])
    print(f"2ª ronda o más: {second}; repiten un tiempo ya pedido: {repeats}")

    outcome = collections.Counter()
    examples = []
    for request, prior, shared in repeat_rows:
        if prior["res"] is None or prior["res"] > request["sub"]:
            outcome["anterior_abierto_al_repetir"] += 1
            continue
        result = check(prior, at=prior["res"])
        if result is None:
            outcome["sin_version_aprobada"] += 1
            continue
        outcome["evaluables"] += 1
        repeated_points = [p for p in result["points"]
                           if any(abs(t - s) <= REPEAT_WINDOW for t in p.get("times") or [] if t is not None for s in shared)]
        outcome["cerrado_por_" + (prior["source"] or "desconocido")] += 1
        after = version_at(jobs.get(prior["job_id"]), prior["res"])
        old_gate_passes = guard.check_requested_text(prior["comment"], after["segments"])["status"] != "not_found"
        if old_gate_passes:
            outcome["control_de_texto_actual_lo_dejaba_cerrar"] += 1
            if result["blocking"]:
                outcome["✘_nuevo_que_el_control_actual_dejaba_pasar"] += 1
        if any(p["status"] == verification.FAIL for p in repeated_points):
            outcome["✘_en_el_punto_repetido"] += 1
        elif any(p["status"] == verification.FAIL for p in result["points"]):
            outcome["✘_en_otro_punto"] += 1
        elif any(p["status"] == verification.MANUAL for p in repeated_points):
            outcome["punto_repetido_manual"] += 1
        else:
            outcome["todo_✔"] += 1
            examples.append((prior, request, repeated_points))
    print("repetidos, verificando el pedido anterior al cerrarlo:", dict(outcome))
    for prior, request, points in examples[:args.examples]:
        print(f"  #{prior['id']} -> #{request['id']}")
        for point in points:
            print("     ", point["kind"], point["status"], point.get("reason"), point["excerpt"][:90],
                  "| línea:", (point.get("line") or {}).get("text"))

    closed = [r for r in window if r["res"] is not None]
    false_fail = collections.Counter()
    point_status = collections.Counter()
    fail_reasons = collections.Counter()
    fail_examples = []
    for request in closed:
        if request["id"] in reclaimed_ids:
            continue
        result = check(request, at=request["res"])
        if result is None:
            false_fail["sin_version_aprobada"] += 1
            continue
        false_fail["evaluables"] += 1
        for point in result["points"]:
            point_status[(point["kind"], point["status"])] += 1
            if point["status"] == verification.FAIL:
                fail_reasons[(point["kind"], point.get("reason"))] += 1
        if result["blocking"]:
            false_fail["con_algún_✘"] += 1
            fail_examples.append((request, [p for p in result["points"] if p["status"] == verification.FAIL]))
    print("no reclamados de nuevo:", dict(false_fail))
    print("  puntos:", dict(sorted(point_status.items())))
    print("  motivos ✘:", fail_reasons.most_common())
    for request, points in fail_examples[:args.examples]:
        print(f"  #{request['id']}")
        for point in points[:3]:
            print("     ", point["kind"], point.get("reason"), point["excerpt"][:90], "| línea:",
                  (point.get("line") or {}).get("text"))

    # 3. Misma corrección en otra repetición de la misma línea.
    text_points = 0
    on_repeated_line = 0
    twins = 0
    twin_groups: set = set()
    explicit = 0
    for (job_id, _portal), items in by_video.items():
        job = jobs.get(job_id)
        entries = []
        for request in items:
            if request["sub"] < since:
                continue
            if re.search(r"tambi[eé]n\s+en\s+la\s+siguiente|todas\s+las\s+(?:partes|veces|apariciones)|resto\s+de\s+la\s+canci"
                         r"|cada\s+vez|se\s+repite|repetir\s+(?:lo\s+mismo|en)|igual\s+en\s+la\s+siguiente", request["comment"], re.I):
                explicit += 1
            before = version_at(job, request["sub"]) or version_at(job, request["sub"], approved=False)
            if before is None:
                continue
            segments = verification._clean_segments(before["segments"])
            for point in verification.extract_points(request["comment"]):
                if point["kind"] != "text" or not point.get("times") or point["times"][0] is None:
                    continue
                text_points += 1
                lines = verification._lines_at(segments, point["times"][0], 1.0)
                if len(lines) != 1:
                    continue
                original = verification.fold(segments[lines[0]]["text"])
                if len(original.split()) < 2:
                    continue
                siblings = [i for i, row in enumerate(segments) if i != lines[0] and verification.fold(row["text"]) == original]
                if not siblings:
                    continue
                on_repeated_line += 1
                entries.append((point, original, round(segments[lines[0]]["start"], 1),
                                verification.fold(point.get("expected") or "")))
        for point, original, start, wanted in entries:
            if any(o == original and s != start and w == wanted for p2, o, s, w in entries if p2 is not point):
                twins += 1
                twin_groups.add((job_id, original, wanted))
    print(f"puntos de texto con tiempo: {text_points}; sobre una línea que se repite: {on_repeated_line}; "
          f"misma corrección pedida en otra repetición de la misma línea: {twins}; "
          f"pedidos que piden explícitamente corregir en otras apariciones: {explicit}")
    print(f"  esas {twins} citas son {len(twin_groups)} correcciones distintas: {twins - len(twin_groups)} citas sobrarían "
          "si la corrección se propagara a las repeticiones")
    pending_points = pending_requests = 0
    for request in closed:
        result = check(request, at=request["res"])
        if result is None:
            continue
        hits = [p for p in result["points"] if p.get("repetitions_pending")]
        pending_points += len(hits)
        pending_requests += bool(hits)
    print(f"al cerrar: {pending_points} puntos de texto ✔ en {pending_requests} pedidos dejaban otra repetición de la misma "
          "línea original sin tocar")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
