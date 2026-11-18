#!/usr/bin/env python3
"""Simula el realineado tras corregir el texto sobre los casos históricos.

Solo lectura (base en read-only; R2 solo GET; el audio se borra al terminar
cada job). Casos: líneas de los jobs de staging de la fase 1 con texto
corregido y timing movido más de 500 ms. Para cada ventana (±1, ±2, ±4 s
alrededor del timing de máquina) corre ``ctc_align.align_line`` con el texto
APROBADO y compara contra el inicio/fin aprobados:

- ``correct``: inicio y fin dentro de ±150 ms;
- ``wrong``: inicio o fin a más de 500 ms (con score alto = "plausible pero
  equivocada");
- ``near``: el resto.

Con ±4 s, si el texto normalizado aparece en otra línea a menos de 8 s, no
hay propuesta (``repeated_blocked``), como en el plan.

Uso (necesita CTC_ALIGN_ENABLED=1, R2_* y DATABASE_URL):
    python3.11 scripts/simulate_realign_windows.py --rows-out /tmp/rows.jsonl --json-out /tmp/sim.json
"""
from __future__ import annotations

import argparse
from collections import defaultdict
import importlib.util
import json
import os
from pathlib import Path
import sys
import tempfile
import time

BACKEND = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND))

_spec = importlib.util.spec_from_file_location(
    "calibrate_display_timing", BACKEND / "scripts" / "calibrate_display_timing.py",
)
calibrate = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(calibrate)

MARGINS = (1.0, 2.0, 4.0)
REPEAT_RADIUS_S = 8.0
CORRECT_S = 0.15
WRONG_S = 0.5
THRESHOLDS = tuple(round(x / 20, 2) for x in range(0, 20))


def outcome(proposal: dict, approved_start: float, approved_end: float) -> str:
    if proposal.get("status") != "ok":
        return "declined"
    ds = abs(proposal["start"] - approved_start)
    de = abs(proposal["end"] - approved_end)
    if ds <= CORRECT_S and de <= CORRECT_S:
        return "correct"
    if ds > WRONG_S or de > WRONG_S:
        return "wrong"
    return "near"


def repeated_nearby(segments: list[dict], index: int, text: str, radius: float) -> bool:
    key = calibrate.normalize_text(text)
    if not key:
        return False
    here = calibrate._finite_time(segments[index].get("start"))
    for j, other in enumerate(segments):
        if j == index:
            continue
        if (calibrate.normalize_text(other.get("text")) == key
                and abs(calibrate._finite_time(other.get("start")) - here) <= radius):
            return True
    return False


def summarize(rows: list[dict]) -> dict:
    """Por ventana y umbral: % correctas, % plausibles-equivocadas, % sin propuesta."""
    n = len(rows)
    out: dict = {"cases": n, "fixed_windows": {}, "adaptive": {}}
    for margin in MARGINS:
        key = f"{margin:g}s"
        table = []
        for t in THRESHOLDS:
            proposed = [r for r in rows if r["windows"][key]["status"] == "ok"
                        and r["windows"][key]["score"] >= t]
            table.append({
                "threshold": t,
                "correct": round(sum(r["windows"][key]["outcome"] == "correct" for r in proposed) / n, 4),
                "near": round(sum(r["windows"][key]["outcome"] == "near" for r in proposed) / n, 4),
                "plausible_wrong": round(sum(r["windows"][key]["outcome"] == "wrong" for r in proposed) / n, 4),
                "no_proposal": round(1 - len(proposed) / n, 4),
            })
        out["fixed_windows"][key] = table
    for t in THRESHOLDS:
        counts = defaultdict(int)
        for r in rows:
            chosen = None
            for margin in MARGINS:
                w = r["windows"][f"{margin:g}s"]
                if margin == 4.0 and r["repeated_within_8s"]:
                    break
                if w["status"] == "ok" and w["score"] >= t:
                    chosen = w
                    break
            counts[chosen["outcome"] if chosen else "no_proposal"] += 1
        out["adaptive"][str(t)] = {k: round(v / n, 4) for k, v in counts.items()}
    return out


def _r2_client():
    import boto3

    return boto3.client(
        "s3", endpoint_url=os.environ["R2_ENDPOINT_URL"],
        aws_access_key_id=os.environ["R2_ACCESS_KEY_ID"],
        aws_secret_access_key=os.environ["R2_SECRET_ACCESS_KEY"], region_name="auto",
    )


def fetch_audio(s3, bucket: str, audio_sha: str | None, input_key: str | None,
                directory: str) -> tuple[str | None, str]:
    """Stem cacheado (stems/{sha}_*) si existe; si no, la mezcla original."""
    if audio_sha:
        listing = s3.list_objects_v2(Bucket=bucket, Prefix=f"stems/{audio_sha}_", MaxKeys=5)
        keys = [o["Key"] for o in listing.get("Contents", [])]
        if keys:
            path = os.path.join(directory, "stem.wav")
            s3.download_file(bucket, sorted(keys)[-1], path)
            return path, "stem"
    if input_key:
        path = os.path.join(directory, "mix" + (os.path.splitext(input_key)[1] or ".wav"))
        s3.download_file(bucket, input_key, path)
        return path, "mix"
    return None, "none"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--roles", default=str(BACKEND / "data" / "song_roles.json"))
    parser.add_argument("--staging-limit", type=int, default=100)
    parser.add_argument("--tenant", action="append", default=[])
    parser.add_argument("--rows-out", default="", help="JSONL por caso (incluye texto: no commitear)")
    parser.add_argument("--json-out", default="")
    args = parser.parse_args()
    args.tenant = args.tenant or ["universal_music"]
    args.gold_dir = ""

    import ctc_align
    import psycopg2

    jobs, _lines = calibrate.collect(args)
    jobs = [j for j in jobs if j["dataset"] == "staging"]
    conn = psycopg2.connect(os.environ["DATABASE_URL"],
                            options="-c default_transaction_read_only=on")
    cur = conn.cursor()
    cur.execute("select job_id, input_r2_key, input_audio_sha256 from jobs where job_id = any(%s)",
                ([j["job_id"] for j in jobs],))
    audio_meta = {row[0]: row[1:] for row in cur.fetchall()}
    conn.close()

    s3, bucket = _r2_client(), os.environ["R2_BUCKET"]
    rows: list[dict] = []
    sources = defaultdict(int)
    started = time.time()
    for job in jobs:
        machine = [dict(s) for s in job["machine"] or [] if isinstance(s, dict)]
        approved = [dict(s) for s in job["approved"] or [] if isinstance(s, dict)]
        matched, _removed, _inserted, complete = calibrate._match_rows(machine, approved)
        if not complete:
            continue
        cases = []
        for b, a, _ in matched:
            m, ap = machine[b], approved[a]
            text_changed = calibrate.normalize_text(m.get("text")) != calibrate.normalize_text(ap.get("text"))
            ms, me = calibrate._finite_time(m.get("start")), calibrate._finite_time(m.get("end"))
            as_, ae = calibrate._finite_time(ap.get("start")), calibrate._finite_time(ap.get("end"))
            if text_changed and (abs(as_ - ms) > WRONG_S or abs(ae - me) > WRONG_S):
                cases.append((b, m, ap, ms, me, as_, ae))
        if not cases:
            continue
        input_key, audio_sha = audio_meta.get(job["job_id"], (None, None))
        with tempfile.TemporaryDirectory() as directory:
            path, source = fetch_audio(s3, bucket, audio_sha, input_key, directory)
            sources[source] += 1
            for b, m, ap, ms, me, as_, ae in cases:
                text = str(ap.get("text") or "")
                windows = {}
                for margin in MARGINS:
                    proposal = ctc_align.align_line(path, text, ms - margin, me + margin,
                                                    job_id=job["job_id"]) if path else {
                        "status": "declined", "reason": "audio_missing"}
                    windows[f"{margin:g}s"] = {
                        "status": proposal.get("status"),
                        "reason": proposal.get("reason"),
                        "score": proposal.get("score", 0.0) or 0.0,
                        "min_score": proposal.get("min_score"),
                        "start": proposal.get("start"),
                        "end": proposal.get("end"),
                        "outcome": outcome(proposal, as_, ae),
                    }
                rows.append({
                    "job_id": job["job_id"], "index": b, "origin": calibrate.origin_for(job["timing_source"]),
                    "audio_source": source, "text": text,
                    "machine": [ms, me], "approved": [as_, ae],
                    "approved_inside_1s": as_ >= ms - 1 and ae <= me + 1,
                    "repeated_within_8s": repeated_nearby(machine, b, text, REPEAT_RADIUS_S),
                    "windows": windows,
                })
        print(f"[{time.time() - started:6.0f}s] {job['job_id']} {source} casos={len(cases)} total={len(rows)}",
              file=sys.stderr, flush=True)
    report = {"audio_sources": dict(sources), **summarize(rows)}
    if args.rows_out:
        with open(args.rows_out, "w") as handle:
            for row in rows:
                handle.write(json.dumps(row, ensure_ascii=False) + "\n")
    text = json.dumps(report, ensure_ascii=False, indent=1)
    if args.json_out:
        Path(args.json_out).write_text(text)
    print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
