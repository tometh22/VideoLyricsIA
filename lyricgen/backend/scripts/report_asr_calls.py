#!/usr/bin/env python3
"""Declara, por job, si el ASR corrió de verdad o vino de cache.

Regla del programa (2026-09-03): ningún resultado se reporta sin decir si el ASR
corrió. En el canary del 2026-09-02, 15 de 30 canciones sirvieron resultados
cacheados de agosto y el lote igual se presentó como evidencia del pipeline
nuevo.

Lee dos fuentes y las cruza:

* ``jobs.transcription_quality.metrics.asr_calls`` (contadores nuevos);
* la procedencia persistida en ``editor_documents.machine_evidence``, donde cada
  hipótesis trae ``transformation`` = ``replicate_raw`` (llamada real) o
  ``cache_hit_raw`` (cache). Sirve para jobs anteriores a los contadores.

Sale con 1 si NINGÚN job del conjunto llamó al ASR: un lote que no llamó al
motor no prueba nada sobre el motor.
"""
from __future__ import annotations

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))


def _from_evidence(evidence: dict | None) -> dict[str, int]:
    counters = {"whisperx_real_calls": 0, "whisperx_cache_hits": 0}
    for hypothesis in ((evidence or {}).get("hypotheses_by_family") or []):
        if not isinstance(hypothesis, dict):
            continue
        transformation = str(hypothesis.get("transformation") or "")
        if transformation == "cache_hit_raw":
            counters["whisperx_cache_hits"] += 1
        elif transformation == "replicate_raw":
            counters["whisperx_real_calls"] += 1
    return counters


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--tenant", default="")
    parser.add_argument("--status", default="pending_review")
    parser.add_argument("--job-ids", default="", help="lista separada por coma; ignora --tenant/--status")
    parser.add_argument("--limit", type=int, default=200)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    from database import EditorDocument, Job, SessionLocal

    db = SessionLocal()
    try:
        query = db.query(Job)
        job_ids = [value.strip() for value in args.job_ids.split(",") if value.strip()]
        if job_ids:
            query = query.filter(Job.job_id.in_(job_ids))
        else:
            if args.tenant:
                query = query.filter(Job.tenant_id == args.tenant)
            if args.status:
                query = query.filter(Job.status == args.status)
        jobs = query.order_by(Job.created_at.desc()).limit(args.limit).all()

        rows = []
        for job in jobs:
            quality = job.transcription_quality or {}
            counters = ((quality.get("metrics") or {}).get("asr_calls") or {})
            source = "metrics"
            if not counters:
                document = (
                    db.query(EditorDocument)
                    .filter(EditorDocument.job_id == job.job_id)
                    .first()
                )
                counters = _from_evidence(getattr(document, "machine_evidence", None))
                source = "machine_evidence"
            real = int(counters.get("whisperx_real_calls") or 0)
            hits = int(counters.get("whisperx_cache_hits") or 0)
            rows.append({
                "job_id": job.job_id,
                "filename": (job.filename or "")[:48],
                "whisperx_real_calls": real,
                "whisperx_cache_hits": hits,
                "asr_actually_ran": real > 0,
                "source": source,
            })
    finally:
        db.close()

    ran = sum(1 for row in rows if row["asr_actually_ran"])
    summary = {
        "jobs": len(rows),
        "jobs_with_real_asr_call": ran,
        "jobs_served_from_cache_only": len(rows) - ran,
    }
    if args.json:
        print(json.dumps({"summary": summary, "rows": rows}, ensure_ascii=False, indent=1))
    else:
        for row in rows:
            flag = "REAL " if row["asr_actually_ran"] else "CACHE"
            print(f"{flag} real={row['whisperx_real_calls']} hit={row['whisperx_cache_hits']} "
                  f"{row['job_id']} {row['filename']} ({row['source']})")
        print(f"-- {summary}")

    if rows and ran == 0:
        print("FALLA: ningún job llamó al ASR; este lote no prueba nada del motor", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
