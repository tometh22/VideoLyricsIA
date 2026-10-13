#!/usr/bin/env python3
"""Validación offline: CTC en toda la canción aunque haya motivo corto repetido.

Solo lectura (base read-only, R2 solo GET, audio borrado al terminar cada
job). Para los jobs UMG recientes que NO terminaron en CTC por
``ctc_short_repeated_motif``, corre ``ctc_align.retime_segments`` sobre el
stem con el texto de máquina, salteando solo el declive por motivo, y le
aplica el mismo pulido de presentación que el pipeline (``lead_in.polish``
con los valores de staging). En las líneas FUERA de las corridas del motivo
compara contra el timing aprobado:

- % de inicios y de fines a ±150 ms del aprobado, CTC vs. lo que salió hoy.

Dentro del motivo, la variante conservaría el timing actual, así que no se
mide.

Uso (CTC_ALIGN_ENABLED=1, R2_*, DATABASE_URL, LYRIC_LEAD_IN_S=0.08,
LYRIC_HOLD_S=0.5):
    python3.11 scripts/simulate_motif_ctc_splice.py --json-out /tmp/motif.json
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

BACKEND = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND))

_spec = importlib.util.spec_from_file_location(
    "calibrate_display_timing", BACKEND / "scripts" / "calibrate_display_timing.py",
)
calibrate = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(calibrate)


def _r2_client():
    import boto3

    return boto3.client(
        "s3", endpoint_url=os.environ["R2_ENDPOINT_URL"],
        aws_access_key_id=os.environ["R2_ACCESS_KEY_ID"],
        aws_secret_access_key=os.environ["R2_SECRET_ACCESS_KEY"], region_name="auto",
    )


def fetch_audio(s3, bucket: str, audio_sha, input_key, directory: str):
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

TOLERANCE_S = 0.15


def _shim_torchaudio_io() -> None:
    """torchaudio >= 2.9 quitó ``info`` y delega ``load`` en torchcodec.
    Producción fija 2.8; para correr la simulación en otra máquina se
    reemplazan por equivalentes sobre soundfile (mismo contrato)."""
    import torchaudio

    if hasattr(torchaudio, "info"):
        return
    import types

    import soundfile
    import torch

    def info(path):
        meta = soundfile.info(path)
        return types.SimpleNamespace(sample_rate=meta.samplerate, num_frames=meta.frames,
                                     num_channels=meta.channels)

    def load(path, frame_offset=0, num_frames=-1, **_kwargs):
        stop = None if num_frames is None or num_frames < 0 else frame_offset + num_frames
        data, sr = soundfile.read(path, start=frame_offset, stop=stop, dtype="float32", always_2d=True)
        return torch.from_numpy(data.T.copy()), sr

    torchaudio.info = info
    torchaudio.load = load


def motif_job_ids(conn, tenant: str, limit: int) -> list[str]:
    cur = conn.cursor()
    cur.execute(
        """
        with last as (
          select v.job_id, min(v.created_at) a from editor_versions v join jobs j using(job_id)
           where v.is_approved and j.tenant_id = %s group by 1 order by a desc limit %s)
        select j.job_id from last join jobs j using(job_id) join editor_documents d using(job_id)
         where j.timing_source <> 'ctc_align'
           and (d.machine_evidence->'decisions'->'quality'->'unsafe_windows')::text
               like '%%ctc_short_repeated_motif%%'
        """,
        (tenant, limit),
    )
    return [row[0] for row in cur.fetchall()]


def within(a: float, b: float) -> bool:
    return abs(a - b) <= TOLERANCE_S


def evaluate_job(machine, approved, retimed, motif_lines: set[int]) -> dict:
    matched, _removed, _inserted, complete = calibrate._match_rows(machine, approved)
    counts = defaultdict(int)
    if not complete:
        counts["alignment_incomplete"] += 1
        return counts
    for b, a, _ in matched:
        if b in motif_lines:
            continue
        ap = approved[a]
        as_, ae = calibrate._finite_time(ap.get("start")), calibrate._finite_time(ap.get("end"))
        now_s = calibrate._finite_time(machine[b].get("start"))
        now_e = calibrate._finite_time(machine[b].get("end"))
        ctc_s = calibrate._finite_time(retimed[b].get("start"))
        ctc_e = calibrate._finite_time(retimed[b].get("end"))
        same_text = calibrate.normalize_text(machine[b].get("text")) == calibrate.normalize_text(ap.get("text"))
        for prefix in ("all", "same_text") if same_text else ("all",):
            counts[f"{prefix}_lines"] += 1
            counts[f"{prefix}_now_start"] += within(now_s, as_)
            counts[f"{prefix}_now_end"] += within(now_e, ae)
            counts[f"{prefix}_ctc_start"] += within(ctc_s, as_)
            counts[f"{prefix}_ctc_end"] += within(ctc_e, ae)
    return counts


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--tenant", default="universal_music")
    parser.add_argument("--limit", type=int, default=100)
    parser.add_argument("--json-out", default="")
    args = parser.parse_args()

    import ctc_align
    import lead_in
    import psycopg2

    _shim_torchaudio_io()
    # Solo se saltea el declive por motivo; el resto de los guardias
    # (idioma, colapso, mediana de score) sigue activo.
    ctc_align.has_short_repeated_motif = lambda lines: False

    conn = psycopg2.connect(os.environ["DATABASE_URL"], options="-c default_transaction_read_only=on")
    job_ids = motif_job_ids(conn, args.tenant, args.limit)
    jobs = calibrate.load_jobs_by_id(conn, job_ids, dataset="staging")
    cur = conn.cursor()
    cur.execute("select job_id, input_r2_key, input_audio_sha256 from jobs where job_id = any(%s)", (job_ids,))
    audio_meta = {row[0]: row[1:] for row in cur.fetchall()}
    conn.close()

    s3, bucket = _r2_client(), os.environ["R2_BUCKET"]
    totals = defaultdict(int)
    per_job = []
    for job in jobs:
        machine = [dict(s) for s in job["machine"] or [] if isinstance(s, dict)]
        approved = [dict(s) for s in job["approved"] or [] if isinstance(s, dict)]
        lines = [str(s.get("text") or "") for s in machine]
        motif_lines = {
            i for start, end in ctc_align.short_repeated_motif_runs(lines) for i in range(start, end + 1)
        }
        input_key, audio_sha = audio_meta.get(job["job_id"], (None, None))
        with tempfile.TemporaryDirectory() as directory:
            path, source = fetch_audio(s3, bucket, audio_sha, input_key, directory)
            retimed = ctc_align.retime_segments(
                path, [{k: v for k, v in s.items() if k != "words"} | {"text": s.get("text")}
                       for s in machine],
                job_id=job["job_id"], vocal_stem=(source == "stem"),
            ) if path else None
        if retimed is None or len(retimed) != len(machine):
            totals["jobs_declined"] += 1
            per_job.append({"job_id": job["job_id"], "status": "declined",
                            "reason": ctc_align.last_decline_reason or "other"})
            continue
        retimed = lead_in.polish(retimed)
        counts = evaluate_job(machine, approved, retimed, motif_lines)
        for key, value in counts.items():
            totals[key] += value
        totals["jobs_aligned"] += 1
        per_job.append({"job_id": job["job_id"], "status": "ok", "audio": source,
                        "motif_lines": len(motif_lines), "lines": len(machine), **counts})
        print(f"{job['job_id']} {source} motif={len(motif_lines)}/{len(machine)} {dict(counts)}",
              file=sys.stderr, flush=True)

    def share(key, base):
        return round(totals[key] / totals[base], 4) if totals[base] else None

    report = {
        "jobs": len(jobs),
        "jobs_aligned": totals["jobs_aligned"],
        "jobs_declined": totals["jobs_declined"],
        "outside_motif": {
            prefix: {
                "lines": totals[f"{prefix}_lines"],
                "start_within_150ms_now": share(f"{prefix}_now_start", f"{prefix}_lines"),
                "start_within_150ms_ctc": share(f"{prefix}_ctc_start", f"{prefix}_lines"),
                "end_within_150ms_now": share(f"{prefix}_now_end", f"{prefix}_lines"),
                "end_within_150ms_ctc": share(f"{prefix}_ctc_end", f"{prefix}_lines"),
            }
            for prefix in ("all", "same_text")
        },
        "per_job": per_job,
    }
    text = json.dumps(report, ensure_ascii=False, indent=1)
    if args.json_out:
        Path(args.json_out).write_text(text)
    print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
