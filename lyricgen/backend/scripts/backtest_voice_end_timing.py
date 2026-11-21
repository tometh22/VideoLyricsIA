#!/usr/bin/env python3
"""Backtest offline de VOICE_END_TIMING_MODE: fin de línea = fin de la voz.

Reproduce la medición del 20-11-2026 sobre una lista de jobs aprobados:
compara, línea por línea, el fin que deja cada regla contra el fin que dejó
el operador en la última versión aprobada.

SÓLO LECTURA. La base se abre con ``default_transaction_read_only=on`` y de R2
sólo se listan y bajan objetos (audio original y stem ``stems/{sha}_…``
cacheado). Nunca corre Demucs ni escribe nada remoto. Lo bajado queda en
``--cache-dir`` (local, borrarlo al terminar).

    DATABASE_URL_FILE=/tmp/x.url R2_ENV_FILE=/tmp/r2.env \\
        python scripts/backtest_voice_end_timing.py --jobs jobs.txt --cache-dir /tmp/ve

``--jobs``: un job_id por línea (lo que sigue al primer espacio se ignora).
``R2_ENV_FILE``: líneas ``CLAVE=valor`` con R2_ENDPOINT_URL, R2_ACCESS_KEY_ID,
R2_SECRET_ACCESS_KEY y R2_BUCKET (o esas variables en el entorno).

Líneas evaluadas (igual que la medición): las de la versión aprobada que se
pueden rastrear a una línea de máquina por sus palabras, cuyo inicio humano
está a ≤0,3 s de la primera palabra de máquina, que no parten una línea de
máquina y que terminan en la última palabra de esa línea.

Dos topes para "la línea siguiente":
- ``humano``: inicio de la línea siguiente APROBADA (lo que usó la medición
  del 20-11; mide la regla aislada del error de inicio de la máquina);
- ``máquina``: inicio de la línea siguiente de la máquina (lo que ve el
  pipeline en producción). Es el número a mirar antes de pasar a ``apply``.
"""
from __future__ import annotations

import argparse
import collections
import hashlib
import json
import os
import re
import statistics
import sys
import unicodedata
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import voice_end_timing as ve  # noqa: E402

HOLD_S = 0.5            # LYRIC_HOLD_S de la flota
HIT_S = 0.3
_LIVE = ("vivo", "live", "en directo", "unplugged", "acústico")


def _fold(text: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFKD", (text or "").lower())
                   if not unicodedata.combining(c))


def _tokens(text: str) -> list[str]:
    return re.findall(r"[a-zñ0-9']+", _fold(text))


def _segments(value) -> list[dict]:
    if isinstance(value, str):
        value = json.loads(value)
    if isinstance(value, dict):
        value = value.get("segments") or []
    return [s for s in value or [] if isinstance(s, dict)]


def match_lines(original: list[dict], approved: list[dict]) -> list[dict]:
    """Empareja cada línea aprobada con el tramo de palabras de máquina que dice lo mismo."""
    stream = []
    for li, seg in enumerate(original):
        for word in seg.get("words") or []:
            if not isinstance(word, dict) or word.get("start") is None or word.get("end") is None:
                continue
            for tok in _tokens(word.get("word") or word.get("text") or ""):
                stream.append((tok, float(word["start"]), float(word["end"]), li))
    if len(stream) < 5:
        return []
    toks = [s[0] for s in stream]
    rows = []
    for k, line in enumerate(approved):
        want = _tokens(line.get("text"))
        if len(want) < 2:
            continue
        hs, he = float(line["start"]), float(line["end"])
        best = None
        for i, tok in enumerate(toks):
            if tok != want[0]:
                continue
            j, m, last = i, 0, i
            while j < len(toks) and m < len(want) and j - i <= len(want) + 2:
                if toks[j] == want[m]:
                    m += 1
                    last = j
                j += 1
            if m >= max(2, round(0.8 * len(want))):
                dist = abs(stream[i][1] - hs)
                if best is None or dist < best[0]:
                    best = (dist, i, last)
        if not best:
            continue
        _, i, last = best
        oi = stream[last][3]
        orig = original[oi]
        word_ends = [float(w["end"]) for w in orig.get("words") or []
                     if isinstance(w, dict) and w.get("end") is not None]
        rows.append({
            "k": k, "hs": hs, "he": he, "ws": stream[i][1], "wls": stream[last][1],
            "we": stream[last][2], "oe": float(orig["end"]),
            "next_human": float(approved[k + 1]["start"]) if k + 1 < len(approved) else None,
            "next_machine": (float(original[oi + 1]["start"])
                             if oi + 1 < len(original) else None),
            "split": stream[i][3] != oi,
            "last_of_orig": bool(word_ends) and abs(stream[last][2] - max(word_ends)) < 1e-6,
        })
    return [r for r in rows
            if abs(r["hs"] - r["ws"]) <= HIT_S and not r["split"] and r["last_of_orig"]]


def classify(row: dict) -> str:
    delta = row["he"] - row["oe"]
    if delta > 0.05:
        return "alargo"
    if delta < -0.05:
        return "acorto"
    return "no_toco"


def rule_hold(row: dict, nxt: float | None, duration: float) -> float:
    limit = (nxt - ve.NEXT_LINE_GAP_S) if nxt is not None else duration
    return max(row["we"], min(row["we"] + HOLD_S, limit))


def rule_voice(row: dict, nxt: float | None, envelope: ve.VoiceEnvelope) -> tuple[float, str]:
    end, evidence = ve.line_voice_end(row["wls"], row["we"], nxt, envelope)
    if end is None:  # sin evidencia: queda el hold actual
        return rule_hold(row, nxt, envelope.duration_s), evidence["status"]
    return end, evidence["status"]


class Sources:
    def __init__(self, cache: Path):
        import boto3
        import psycopg2
        from botocore.config import Config

        url = os.environ.get("DATABASE_URL")
        if not url and os.environ.get("DATABASE_URL_FILE"):
            url = Path(os.environ["DATABASE_URL_FILE"]).read_text().strip()
        if not url:
            raise SystemExit("Falta DATABASE_URL o DATABASE_URL_FILE")
        env = dict(os.environ)
        if os.environ.get("R2_ENV_FILE"):
            for line in Path(os.environ["R2_ENV_FILE"]).read_text().splitlines():
                if "=" in line and not line.lstrip().startswith("#"):
                    key, value = line.split("=", 1)
                    env[key.strip()] = value.strip().strip('"')
        self.db = psycopg2.connect(url, options="-c default_transaction_read_only=on")
        self.db.set_session(readonly=True, autocommit=True)
        self.bucket = env["R2_BUCKET"]
        self.s3 = boto3.client(
            "s3", endpoint_url=env["R2_ENDPOINT_URL"],
            aws_access_key_id=env["R2_ACCESS_KEY_ID"],
            aws_secret_access_key=env["R2_SECRET_ACCESS_KEY"],
            config=Config(signature_version="s3v4", retries={"max_attempts": 5}),
        )
        self.cache = cache
        cache.mkdir(parents=True, exist_ok=True)

    def job(self, job_id: str) -> dict | None:
        cur = self.db.cursor()
        cur.execute("select input_r2_key, song_title, artist, tenant_id from jobs where job_id=%s",
                    (job_id,))
        row = cur.fetchone()
        if not row:
            return None
        cur.execute("select original_segments from editor_documents where job_id=%s", (job_id,))
        doc = cur.fetchone()
        cur.execute("select segments from editor_versions where job_id=%s and is_approved "
                    "order by created_at desc limit 1", (job_id,))
        approved = cur.fetchone()
        return {"input_key": row[0], "song": row[1] or "", "artist": row[2] or "",
                "tenant": row[3], "original": _segments(doc[0]) if doc else [],
                "approved": _segments(approved[0]) if approved else []}

    def _download(self, key: str, path: Path) -> Path:
        if not path.exists():
            tmp = path.with_suffix(path.suffix + ".part")
            self.s3.download_file(self.bucket, key, str(tmp))
            tmp.rename(path)
        return path

    def audio_and_stem(self, job_id: str, input_key: str) -> tuple[Path, Path | None, str | None]:
        suffix = Path(input_key).suffix or ".audio"
        mix = self._download(input_key, self.cache / f"{job_id}_mix{suffix}")
        digest = hashlib.sha256()
        with open(mix, "rb") as fh:
            for chunk in iter(lambda: fh.read(1 << 20), b""):
                digest.update(chunk)
        prefix = f"stems/{digest.hexdigest()}_mdx_extra_"
        listed = self.s3.list_objects_v2(Bucket=self.bucket, Prefix=prefix).get("Contents") or []
        if not listed:
            return mix, None, None
        newest = max(listed, key=lambda o: o["LastModified"])
        stem = self._download(newest["Key"], self.cache / f"{job_id}_voc.wav")
        return mix, stem, newest["Key"]


def hit(values, t=HIT_S):
    return sum(abs(v) <= t for v in values) / max(1, len(values))


def report(rows: list[dict]) -> dict:
    out = {"lines": len(rows), "songs": len({r["job"] for r in rows}),
           "classes": dict(collections.Counter(r["cls"] for r in rows))}
    for rule in ("hold", "voice_human_cap", "hold_machine_cap", "voice_machine_cap", "delivered"):
        deltas = [r[rule] - r["he"] for r in rows]
        by = collections.defaultdict(list)
        for r, d in zip(rows, deltas):
            by[r["cls"]].append(d)
            by["vivo" if r["live"] else "estudio"].append(d)
        out[rule] = {
            "hit_0_3": round(hit(deltas), 4),
            "hit_0_2": round(hit(deltas, 0.2), 4),
            "early_gt_0_3": round(sum(d < -HIT_S for d in deltas) / max(1, len(deltas)), 4),
            "late_gt_0_3": round(sum(d > HIT_S for d in deltas) / max(1, len(deltas)), 4),
            "mae_s": round(statistics.mean(abs(d) for d in deltas), 3) if deltas else None,
            **{f"hit_{k}": round(hit(v), 4) for k, v in sorted(by.items())},
            **{f"n_{k}": len(v) for k, v in sorted(by.items())},
        }
    out["voice_status"] = dict(collections.Counter(r["status"] for r in rows))
    return out


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--jobs", type=Path, required=True)
    parser.add_argument("--cache-dir", type=Path, required=True)
    parser.add_argument("--output", type=Path, help="JSON con el detalle por línea (privado)")
    args = parser.parse_args(argv)
    job_ids = [line.split()[0] for line in args.jobs.read_text().splitlines() if line.strip()]
    sources = Sources(args.cache_dir)
    rows, skipped, clocks = [], collections.Counter(), collections.Counter()
    for job_id in job_ids:
        data = sources.job(job_id)
        if not data or not data["original"] or not data["approved"] or not data["input_key"]:
            skipped["sin_datos"] += 1
            continue
        lines = match_lines(data["original"], data["approved"])
        if not lines:
            skipped["sin_lineas_rastreables"] += 1
            continue
        try:
            mix, stem, _key = sources.audio_and_stem(job_id, data["input_key"])
        except Exception as exc:
            skipped[f"r2_{type(exc).__name__}"] += 1
            continue
        if stem is None:
            skipped["sin_stem_cacheado"] += 1
            continue
        envelope = ve.load_voice_envelope(str(stem), str(mix))
        clocks["verificado" if envelope.clock.get("verified") else "no_verificado"] += 1
        live = any(t in _fold(data["song"]) for t in _LIVE)
        for line in lines:
            voice_h, status = rule_voice(line, line["next_human"], envelope)
            voice_m, _ = rule_voice(line, line["next_machine"], envelope)
            rows.append({
                "job": job_id, "k": line["k"], "he": line["he"], "we": line["we"],
                "cls": classify(line), "live": live, "status": status,
                "hold": rule_hold(line, line["next_human"], envelope.duration_s),
                "hold_machine_cap": rule_hold(line, line["next_machine"], envelope.duration_s),
                "voice_human_cap": voice_h, "voice_machine_cap": voice_m,
                "delivered": line["oe"],
                "clock_offset_s": envelope.offset_s,
            })
        print(f"{job_id} {len(lines)} líneas reloj={envelope.clock.get('verified')} "
              f"offset={envelope.offset_s:+.3f}s", flush=True)
    summary = {**report(rows), "skipped": dict(skipped), "clock": dict(clocks),
               "policy_version": ve.POLICY_VERSION}
    print(json.dumps(summary, indent=2, ensure_ascii=False))
    if args.output:
        args.output.write_text(json.dumps({"summary": summary, "rows": rows}, ensure_ascii=False))
        os.chmod(args.output, 0o600)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
