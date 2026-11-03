#!/usr/bin/env python3
"""Calibra las mutaciones de timing previas a la revisión contra lo aprobado.

Solo lectura. No cambia ningún parámetro: simula el pipeline con otros
valores y mide contra el timing que aprobó el operador.

Mutaciones simuladas, en el orden en que corren en el emisor
(``whisperx_transcribe`` → ``main._snap``):

1. Adelanto de WhisperX (``LYRIC_LEAD_IN_MS``, solo líneas de origen WhisperX).
2. Beat snap (``BEAT_SNAP_ENABLED``/``BEAT_SNAP_WINDOW_MS``, solo líneas sin
   ninguna palabra con score >= 0,5; en el camino CTC el retime lo pisa).
3. Adelanto de ``lead_in.polish`` (``LYRIC_LEAD_IN_S``, todas las líneas).
4. Hold de ``lead_in.polish`` (``LYRIC_HOLD_S``, extiende el fin hasta el
   inicio de la siguiente).

La reconstrucción parte de las palabras, que ninguna de las cuatro mueve:
inicio crudo = primera palabra, fin crudo = fin de la última palabra. Las
líneas sin palabras no se pueden reconstruir y quedan fuera (se informa).

Datasets: los casos de ``umg-gold-v1`` con ``machine_original.json`` y los
jobs aprobados de staging de la fase 1 (``report_review_baseline``).

Sesgo de anclaje: en las líneas que el operador no tocó, lo aprobado es lo
que el pipeline produjo con los parámetros de ese momento. Por eso se
informa también el valor que el operador eligió en las líneas que sí
corrigió (estimador libre de anclaje) y cada dataset por separado.

Uso:
    DATABASE_URL=... python3.11 scripts/calibrate_display_timing.py \\
        --gold-dir .../umg-gold-v1/cases --json-out /tmp/calibration.json
"""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
import importlib.util
import itertools
import json
import os
from pathlib import Path
import re
import sys

import numpy as np

BACKEND = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND))

from training_corpus import _finite_time, _match_rows  # noqa: E402

_spec = importlib.util.spec_from_file_location(
    "report_review_baseline", BACKEND / "scripts" / "report_review_baseline.py",
)
baseline = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(baseline)

RELIABLE_WORD_SCORE = 0.5            # beat_snap._RELIABLE_WORD_SCORE
WX_FLOOR_GAP_S = 0.005               # whisperx_transcribe._apply_lead_in
POLISH_MIN_GAP_S = 0.01              # lead_in._MIN_GAP_S
SNAP_FLOOR_GAP_S = 0.005             # beat_snap.snap_segments

CURRENT = {"wx_lead_ms": 80, "polish_lead_ms": 80, "hold_ms": 500, "snap_ms": 80}
CURRENT_HOLD_GAP_MS = 10
# Aire mínimo entre el fin extendido por el hold y la línea siguiente. No es
# una de las cuatro mutaciones pedidas, pero es la que explica las ediciones
# de fin (ver "air_sweep").
HOLD_GAP_GRID = (10, 50, 100, 150, 200, 250, 300)
GRID = {
    "wx_lead_ms": tuple(range(0, 201, 20)),
    "polish_lead_ms": tuple(range(0, 201, 20)),
    "hold_ms": tuple(range(0, 501, 50)),
    "snap_ms": (0, 40, 80),
}
TOLERANCE_S = 0.10
EDIT_THRESHOLD_S = 0.05

ORIGIN_BY_SOURCE = {
    "ctc_align": "ctc",
    "whisperx": "whisperx",
    "whisperx_reconciled": "whisperx_reconciled",
}
# El adelanto de WhisperX corre dentro de whisperx_transcribe; el retime de
# CTC rehace los inicios desde los onsets, así que ahí no sobrevive.
WX_LEAD_ORIGINS = frozenset({"whisperx", "whisperx_reconciled"})
SNAP_ORIGINS = frozenset({"whisperx", "whisperx_reconciled", "other"})


def origin_for(timing_source) -> str:
    return ORIGIN_BY_SOURCE.get(str(timing_source or ""), "other")


def reliable_words(segment: dict) -> bool:
    for word in segment.get("words") or []:
        if not isinstance(word, dict):
            continue
        try:
            if float(word.get("score", 0.0)) >= RELIABLE_WORD_SCORE:
                return True
        except (TypeError, ValueError):
            continue
    return False


def word_bounds(segment: dict) -> tuple[float, float] | None:
    words = [
        w for w in (segment.get("words") or [])
        if isinstance(w, dict) and w.get("start") is not None
    ]
    if not words:
        return None
    onset = min(_finite_time(w.get("start")) for w in words)
    end = max(_finite_time(w.get("end", w.get("start"))) for w in words)
    return onset, max(onset, end)


_PUNCT_RE = re.compile(r"[^\w\s]", re.UNICODE)


def normalize_text(value) -> str:
    """Minúsculas, sin puntuación, espacios colapsados. Conserva tildes."""
    return " ".join(_PUNCT_RE.sub(" ", str(value or "").casefold()).split())


# ---------------------------------------------------------------- roles


def load_roles(path: Path) -> tuple[dict[str, str], dict[str, str]]:
    data = json.loads(path.read_text())
    by_job: dict[str, str] = {}
    by_sha: dict[str, str] = {}
    for entry in data.get("entries") or []:
        role = entry.get("role")
        if entry.get("sha256"):
            by_sha[str(entry["sha256"])] = role
        for known in entry.get("known_job_ids") or []:
            if known.get("job_id"):
                by_job[str(known["job_id"])] = role
    return by_job, by_sha


# ---------------------------------------------------------------- datasets


def load_gold(gold_dir: Path) -> list[dict]:
    jobs = []
    for case in sorted(gold_dir.iterdir()):
        machine_path = case / "machine_original.json"
        if not machine_path.exists():
            continue
        meta = json.loads((case / "metadata.json").read_text())
        song = meta.get("song") or {}
        live, live_source = baseline.live_class(None, song.get("title"), song.get("artist"))
        jobs.append({
            "dataset": "gold",
            "job_id": str(meta.get("job_id")),
            "timing_source": (meta.get("pipeline") or {}).get("timing_source"),
            "live": live,
            "live_source": live_source,
            "machine_quality": (meta.get("historical_machine_baseline") or {}).get("quality"),
            "audio_sha256": None,
            "machine": json.loads(machine_path.read_text()),
            "approved": json.loads((case / "human_gold.json").read_text()),
            "transcription_quality": None,
        })
    return jobs


def load_staging(conn, *, limit: int, tenants: list[str]) -> list[dict]:
    cur = conn.cursor()
    cur.execute(
        """
        select v.job_id, min(v.created_at) as first_approved_at
          from editor_versions v join jobs j on j.job_id = v.job_id
         where v.is_approved and j.tenant_id = any(%s::text[])
         group by v.job_id order by first_approved_at desc limit %s
        """,
        (tenants, limit),
    )
    job_ids = [row[0] for row in cur.fetchall()]
    return load_jobs_by_id(conn, job_ids, dataset="staging")


def load_jobs_by_id(conn, job_ids: list[str], *, dataset: str) -> list[dict]:
    if not job_ids:
        return []
    cur = conn.cursor()
    cur.execute(
        """
        select job_id, timing_source, transcription_quality, song_title, filename,
               input_audio_sha256
          from jobs where job_id = any(%s)
        """,
        (job_ids,),
    )
    meta = {row[0]: row for row in cur.fetchall()}
    cur.execute(
        """
        select job_id, revision, reason, is_approved, created_at, segments
          from editor_versions where job_id = any(%s)
        """,
        (job_ids,),
    )
    versions: dict[str, list[dict]] = defaultdict(list)
    for job_id, revision, reason, approved, created_at, segments in cur.fetchall():
        versions[job_id].append({
            "revision": revision, "reason": reason, "is_approved": approved,
            "created_at": created_at, "segments": segments,
        })
    jobs = []
    for job_id in job_ids:
        row = meta.get(job_id)
        chosen = baseline.pick_versions(versions.get(job_id, []))
        if row is None or not chosen["machine"] or not chosen["approved"]:
            continue
        _, timing_source, quality, title, filename, audio_sha = row
        live, live_source = baseline.live_class(quality, title, filename)
        jobs.append({
            "dataset": dataset,
            "job_id": job_id,
            "timing_source": timing_source,
            "live": live,
            "live_source": live_source,
            "machine_quality": chosen["machine"]["reason"],
            "audio_sha256": audio_sha,
            "machine": chosen["machine"]["segments"],
            "approved": chosen["approved"]["segments"],
            "transcription_quality": quality,
        })
    return jobs


def transcription_quality_for(conn, job_ids: list[str]) -> dict[str, dict]:
    if not job_ids:
        return {}
    cur = conn.cursor()
    cur.execute(
        "select job_id, transcription_quality, input_audio_sha256 from jobs where job_id = any(%s)",
        (job_ids,),
    )
    return {row[0]: {"quality": row[1], "audio_sha256": row[2]} for row in cur.fetchall()}


# ---------------------------------------------------------------- lines


def build_lines(job: dict, role: str) -> list[dict]:
    """Una fila por línea de máquina, con su par aprobado si existe."""
    machine = [dict(r) for r in (job["machine"] or []) if isinstance(r, dict)]
    approved = [dict(r) for r in (job["approved"] or []) if isinstance(r, dict)]
    matched, removed, _inserted, complete = _match_rows(machine, approved)
    pair = {b: a for b, a, _ in matched} if complete else {}
    origin = origin_for(job["timing_source"])
    rows = []
    previous_raw_end = None
    for index, segment in enumerate(machine):
        bounds = word_bounds(segment)
        approved_row = approved[pair[index]] if index in pair else None
        raw_onset, raw_end = bounds if bounds else (None, None)
        rows.append({
            "dataset": job["dataset"],
            "job_id": job["job_id"],
            "index": index,
            "origin": origin,
            "live": job["live"],
            "role": role,
            "machine_quality": job["machine_quality"],
            "has_words": bounds is not None,
            "reliable": reliable_words(segment),
            "raw_onset": raw_onset,
            "raw_end": raw_end,
            # Para clamps de líneas sin palabras se usa su timing de máquina.
            "prev_raw_end": previous_raw_end,
            "machine_start": _finite_time(segment.get("start")),
            "machine_end": _finite_time(segment.get("end")),
            "approved_start": _finite_time(approved_row.get("start")) if approved_row else None,
            "approved_end": _finite_time(approved_row.get("end")) if approved_row else None,
            "deleted": complete and index in removed,
            "text_changed": bool(
                approved_row is not None
                and normalize_text(segment.get("text")) != normalize_text(approved_row.get("text"))
            ),
            "matched": approved_row is not None,
            "alignment_complete": bool(complete),
            "segment": segment,
        })
        previous_raw_end = raw_end if bounds else _finite_time(segment.get("end"))
    return rows


# ---------------------------------------------------------------- simulation


def modal_leads(lines: list[dict]) -> dict[str, float]:
    """Adelanto más frecuente por job (inicio crudo − inicio de máquina)."""
    per_job: dict[str, Counter] = defaultdict(Counter)
    for r in lines:
        if r["has_words"]:
            per_job[r["job_id"]][round(r["raw_onset"] - r["machine_start"], 2)] += 1
    return {job: counts.most_common(1)[0][0] for job, counts in per_job.items() if counts}


class Simulator:
    """Simulación vectorizada sobre todas las líneas de todos los jobs."""

    def __init__(self, lines: list[dict], beats: dict[str, list[float]] | None = None):
        self.lines = lines
        n = len(lines)
        self.has_words = np.array([r["has_words"] for r in lines], dtype=bool)
        self.onset = np.array([r["raw_onset"] if r["has_words"] else r["machine_start"] for r in lines])
        self.raw_end = np.array([r["raw_end"] if r["has_words"] else r["machine_end"] for r in lines])
        self.machine_start = np.array([r["machine_start"] for r in lines])
        self.machine_end = np.array([r["machine_end"] for r in lines])
        prev = [r["prev_raw_end"] for r in lines]
        self.has_prev = np.array([p is not None for p in prev], dtype=bool)
        self.prev_raw_end = np.array([p if p is not None else -1.0 for p in prev])
        self.wx = np.array([r["origin"] in WX_LEAD_ORIGINS for r in lines], dtype=bool)
        self.snappable = np.array(
            [r["has_words"] and not r["reliable"] and r["origin"] in SNAP_ORIGINS for r in lines],
            dtype=bool,
        )
        # Siguiente línea del mismo job (−1 si es la última).
        self.next_index = np.full(n, -1)
        for i in range(n - 1):
            if lines[i + 1]["job_id"] == lines[i]["job_id"]:
                self.next_index[i] = i + 1
        self.beats = beats or {}
        # Piso observado: si la línea de máquina tiene menos adelanto que el
        # modal de su job, el pipeline la clampeó contra el fin de la línea
        # anterior (que las palabras subestiman: el segmento puede terminar
        # después de su última palabra). Ese piso no depende de los parámetros.
        self.clamp_floor = np.full(n, -np.inf)
        modal = modal_leads(lines)
        for i, r in enumerate(lines):
            if not r["has_words"]:
                continue
            observed = r["raw_onset"] - r["machine_start"]
            expected = modal.get(r["job_id"])
            if expected is not None and expected > 0.005 and observed < expected - 0.005:
                self.clamp_floor[i] = r["machine_start"]

    def _nearest_beat(self, starts: np.ndarray) -> np.ndarray:
        nearest = np.full(len(starts), np.nan)
        by_job: dict[str, list[int]] = defaultdict(list)
        for i in np.flatnonzero(self.snappable):
            by_job[self.lines[i]["job_id"]].append(i)
        for job_id, indices in by_job.items():
            grid = self.beats.get(job_id)
            if not grid:
                continue
            grid = np.asarray(sorted(grid))
            values = starts[indices]
            pos = np.searchsorted(grid, values)
            left = grid[np.clip(pos - 1, 0, len(grid) - 1)]
            right = grid[np.clip(pos, 0, len(grid) - 1)]
            nearest[indices] = np.where(np.abs(left - values) <= np.abs(right - values), left, right)
        return nearest

    def run(self, *, wx_lead_ms: int, polish_lead_ms: int, hold_ms: int, snap_ms: int,
            hold_gap_ms: int = 10):
        W, P, H, S = wx_lead_ms / 1000, polish_lead_ms / 1000, hold_ms / 1000, snap_ms / 1000
        gap = hold_gap_ms / 1000
        onset, raw_end, prev = self.onset, self.raw_end, self.prev_raw_end
        start = onset.copy()
        # 1. Adelanto de WhisperX: piso en el fin anterior + 5 ms y en 0.
        floor = np.where(self.has_prev, np.maximum(0.0, prev + WX_FLOOR_GAP_S), 0.0)
        floor = np.maximum(floor, self.clamp_floor)
        wx_start = np.minimum(np.maximum(onset - W, floor), onset)
        start = np.where(self.wx & self.has_words, wx_start, start)
        # 2. Beat snap.
        if S > 0 and self.beats:
            nearest = self._nearest_beat(start)
            snap_floor = np.where(self.has_prev, prev + SNAP_FLOOR_GAP_S, SNAP_FLOOR_GAP_S)
            candidate = np.maximum(nearest, snap_floor)
            ok = (
                self.snappable & ~np.isnan(nearest)
                & (np.abs(nearest - start) <= S) & (candidate < raw_end)
            )
            start = np.where(ok, candidate, start)
        # 3. Adelanto de lead_in.polish: piso en el fin anterior + 10 ms y en 0.
        polish_floor = np.where(self.has_prev, prev + POLISH_MIN_GAP_S, 0.0)
        polish_floor = np.maximum(polish_floor, self.clamp_floor)
        target = np.maximum(np.maximum(0.0, start - P), polish_floor)
        start = np.where(self.has_words & (target < start), target, start)
        start = np.where(self.has_words, start, self.machine_start)
        # 4. Hold: hasta el inicio de la siguiente − aire (hoy 10 ms,
        #    lead_in._MIN_GAP_S); nunca acorta.
        next_start = np.where(self.next_index >= 0, start[np.maximum(self.next_index, 0)], np.inf)
        hold_target = np.minimum(raw_end + H, next_start - gap)
        end = np.where((self.next_index >= 0) & (hold_target > raw_end), hold_target, raw_end)
        end = np.where(self.has_words, end, self.machine_end)
        return start, end


# ---------------------------------------------------------------- metrics


def evaluation_mask(lines: list[dict]) -> np.ndarray:
    """Líneas calibrables: con palabras, emparejadas y con el mismo texto."""
    return np.array([
        r["has_words"] and r["matched"] and not r["text_changed"] for r in lines
    ], dtype=bool)


def within(sim_start, sim_end, approved_start, approved_end, mask, tolerance=TOLERANCE_S):
    start_ok = np.abs(sim_start - approved_start) <= tolerance
    end_ok = np.abs(sim_end - approved_end) <= tolerance
    count = int(mask.sum())
    if not count:
        return {"lines": 0}
    return {
        "lines": count,
        "start_within": round(float((start_ok & mask).sum()) / count, 4),
        "end_within": round(float((end_ok & mask).sum()) / count, 4),
        "both_within": round(float((start_ok & end_ok & mask).sum()) / count, 4),
    }


def timing_edits(sim_start, sim_end, machine_start, machine_end,
                 approved_start, approved_end, mask, threshold=EDIT_THRESHOLD_S,
                 tolerance_start=None, tolerance_end=None):
    """Líneas con edición de timing hoy vs. con los parámetros simulados.

    ``tolerance_*``: si se pasan, una línea que hoy NO se editó solo cuenta
    como edición nueva si el valor simulado se aleja más que esa tolerancia
    de lo aprobado (el operador aceptó lo que vio; no sabemos si aceptaría
    otro valor cercano).
    """
    def boundary(machine, simulated, approved, tolerance):
        now = np.abs(machine - approved) >= threshold
        off = np.abs(simulated - approved)
        sim = off >= threshold
        if tolerance is not None:
            sim = (sim & now) | (sim & ~now & (off >= tolerance))
        now, sim = now & mask, sim & mask
        return {
            "edited_now": int(now.sum()),
            "edited_sim": int(sim.sum()),
            "removed": int((now & ~sim).sum()),
            "created": int((sim & ~now).sum()),
            "net_reduction_share": round(float(now.sum() - sim.sum()) / max(1, int(now.sum())), 4),
        }

    return {
        "lines": int(mask.sum()),
        "start": boundary(machine_start, sim_start, approved_start, tolerance_start),
        "end": boundary(machine_end, sim_end, approved_end, tolerance_end),
    }


def group_masks(lines: list[dict], base: np.ndarray) -> dict[str, np.ndarray]:
    groups: dict[str, np.ndarray] = {"all": base}

    def add(prefix, key):
        for value in sorted({str(r[key]) for r in lines}):
            groups[f"{prefix}={value}"] = base & np.array([str(r[key]) == value for r in lines])

    add("dataset", "dataset")
    add("origin", "origin")
    add("live", "live")
    add("role", "role")
    for dataset in sorted({r["dataset"] for r in lines}):
        dmask = np.array([r["dataset"] == dataset for r in lines])
        for origin in sorted({r["origin"] for r in lines}):
            omask = np.array([r["origin"] == origin for r in lines])
            groups[f"{dataset}|origin={origin}"] = base & dmask & omask
    return groups


def operator_preference(lines: list[dict], mask: np.ndarray) -> dict:
    """Adelanto y hold que eligió el operador en las líneas que corrigió.

    Adelanto elegido = inicio crudo − inicio aprobado; hold elegido = fin
    aprobado − fin crudo (solo líneas con aire hasta la siguiente).
    """
    out: dict[str, dict] = {}
    for origin in sorted({r["origin"] for r in lines}) + ["all"]:
        leads, holds = [], []
        for i, r in enumerate(lines):
            if not mask[i] or (origin != "all" and r["origin"] != origin):
                continue
            if abs(r["machine_start"] - r["approved_start"]) >= EDIT_THRESHOLD_S:
                lead = r["raw_onset"] - r["approved_start"]
                if abs(lead) <= 1.0:
                    leads.append(lead)
            if abs(r["machine_end"] - r["approved_end"]) >= EDIT_THRESHOLD_S:
                hold = r["approved_end"] - r["raw_end"]
                if -1.0 <= hold <= 2.0:
                    holds.append(hold)
        out[origin] = {
            "corrected_starts": len(leads),
            "chosen_lead_s": baseline.distribution(leads),
            "corrected_ends": len(holds),
            "chosen_hold_s": baseline.distribution(holds),
        }
    return out


def correction_magnitudes(lines: list[dict], mask: np.ndarray) -> dict:
    starts = [abs(r["machine_start"] - r["approved_start"]) for i, r in enumerate(lines)
              if mask[i] and abs(r["machine_start"] - r["approved_start"]) >= EDIT_THRESHOLD_S]
    ends = [abs(r["machine_end"] - r["approved_end"]) for i, r in enumerate(lines)
            if mask[i] and abs(r["machine_end"] - r["approved_end"]) >= EDIT_THRESHOLD_S]
    return {"start": baseline.distribution(starts), "end": baseline.distribution(ends)}


def sweep(sim: Simulator, lines: list[dict], mask: np.ndarray, groups: dict[str, np.ndarray],
          grid: dict = GRID) -> list[dict]:
    approved_start = np.array([r["approved_start"] if r["approved_start"] is not None else np.nan for r in lines])
    approved_end = np.array([r["approved_end"] if r["approved_end"] is not None else np.nan for r in lines])
    results = []
    keys = list(grid)
    for values in itertools.product(*(grid[k] for k in keys)):
        params = dict(zip(keys, values))
        start, end = sim.run(**params)
        row = {"params": params}
        for name, gmask in groups.items():
            row[name] = within(start, end, approved_start, approved_end, gmask)
        results.append(row)
    return results


def best_by(results: list[dict], group: str, metric: str = "both_within") -> dict:
    eligible = [r for r in results if r.get(group, {}).get("lines")]
    return max(eligible, key=lambda r: (r[group][metric], -_distance_from_current(r["params"])))


def _distance_from_current(params: dict) -> float:
    return sum(abs(params[k] - CURRENT[k]) for k in CURRENT)


def marginal(sim: "Simulator", approved_start, approved_end, anchor: dict, key: str,
             values, mask: np.ndarray) -> list[dict]:
    """Curva de una variable con las demás fijas en ``anchor``."""
    rows = []
    for value in values:
        start, end = sim.run(**{**anchor, key: value})
        rows.append({key: value, **within(start, end, approved_start, approved_end, mask)})
    return rows


def regime_analysis(lines: list[dict]) -> dict:
    """Experimento natural: tasa de ediciones según el hold/adelanto del job.

    El hold y el adelanto con que se generó cada job se leen de la máquina
    (modal de fin − última palabra en líneas con aire, y de primera palabra
    − inicio). Para el hold, las líneas con hueco <= 0,25 s a la siguiente
    salen idénticas con 0,25 y con 0,5 (ambas quedan pegadas): son el control
    de cuánto difiere el criterio de revisión entre cohortes.
    """
    by_job: dict[str, list[dict]] = defaultdict(list)
    for r in lines:
        by_job[r["job_id"]].append(r)
    hold_rows: dict[tuple, Counter] = defaultdict(Counter)
    lead_rows: dict[tuple, Counter] = defaultdict(Counter)
    hold_jobs: dict[tuple, set] = defaultdict(set)
    lead_jobs: dict[tuple, set] = defaultdict(set)
    air: dict[str, dict[str, list[float]]] = defaultdict(lambda: defaultdict(list))
    leads_modal = modal_leads(lines)
    for job_id, rows in by_job.items():
        holds = [
            round(r["machine_end"] - r["raw_end"], 2)
            for k, r in enumerate(rows[:-1])
            if r["has_words"] and rows[k + 1]["machine_start"] - r["raw_end"] > 0.6
        ]
        hold = Counter(holds).most_common(1)[0][0] if holds else None
        lead = leads_modal.get(job_id)
        for k, r in enumerate(rows):
            if not (r["has_words"] and r["matched"] and not r["text_changed"]):
                continue
            origin = "ctc" if r["origin"] == "ctc" else "no_ctc"
            start_edit = abs(r["machine_start"] - r["approved_start"]) >= EDIT_THRESHOLD_S
            lkey = (r["dataset"], origin, f"lead={lead}")
            lead_rows[lkey]["lines"] += 1
            lead_rows[lkey]["start_edit"] += start_edit
            lead_jobs[lkey].add(job_id)
            if k + 1 >= len(rows):
                continue
            nxt = rows[k + 1]
            gap = nxt["machine_start"] - r["raw_end"]
            bucket = ("a_gap<=0.25_control" if gap <= 0.25
                      else "b_gap_0.25-0.5" if gap <= 0.5 else "c_gap>0.5")
            end_edit = abs(r["machine_end"] - r["approved_end"]) >= EDIT_THRESHOLD_S
            hkey = (r["dataset"], origin, f"hold={hold}", bucket)
            hold_rows[hkey]["lines"] += 1
            hold_rows[hkey]["end_edit"] += end_edit
            hold_jobs[hkey].add(job_id)
            if nxt["approved_start"] is not None and r["origin"] == "ctc":
                kind = "edited_end" if end_edit else "untouched_end"
                air[r["dataset"]][kind].append(nxt["approved_start"] - r["approved_end"])
    return {
        "hold": {
            "|".join(k): {"jobs": len(hold_jobs[k]), "lines": v["lines"],
                          "end_edit_rate": round(v["end_edit"] / v["lines"], 4),
                          "ci95": wilson(v["end_edit"], v["lines"])}
            for k, v in sorted(hold_rows.items())
        },
        "lead": {
            "|".join(k): {"jobs": len(lead_jobs[k]), "lines": v["lines"],
                          "start_edit_rate": round(v["start_edit"] / v["lines"], 4),
                          "ci95": wilson(v["start_edit"], v["lines"])}
            for k, v in sorted(lead_rows.items())
        },
        "air_to_next_line_ctc": {
            dataset: {kind: baseline.distribution(values) for kind, values in kinds.items()}
            for dataset, kinds in air.items()
        },
    }


def wilson(successes: int, total: int, z: float = 1.96) -> list[float] | None:
    if not total:
        return None
    p = successes / total
    denom = 1 + z * z / total
    centre = (p + z * z / (2 * total)) / denom
    half = z * ((p * (1 - p) / total + z * z / (4 * total * total)) ** 0.5) / denom
    return [round(centre - half, 4), round(centre + half, 4)]


# ---------------------------------------------------------------- beat snap


def beat_snap_effect(sim: Simulator, lines: list[dict], mask: np.ndarray, params: dict) -> dict:
    """Líneas que el snap movió y si el operador lo deshizo."""
    if not sim.beats:
        return {"available": False}
    with_snap = sim.run(**params)
    without = sim.run(**{**params, "snap_ms": 0})
    moved = np.abs(with_snap[0] - without[0]) >= 0.005
    approved_start = np.array([r["approved_start"] if r["approved_start"] is not None else np.nan for r in lines])
    considered = moved & mask
    closer_to_unsnapped = np.abs(approved_start - without[0]) < np.abs(approved_start - with_snap[0])
    untouched = np.abs(approved_start - with_snap[0]) < 0.005
    n = int(considered.sum())
    return {
        "available": True,
        "jobs_with_beats": len(sim.beats),
        "snappable_lines": int((sim.snappable & mask).sum()),
        "moved_by_snap": n,
        "operator_undid_snap": int((considered & closer_to_unsnapped).sum()),
        "operator_kept_snap_exactly": int((considered & untouched).sum()),
        "undo_share": round(float((considered & closer_to_unsnapped).sum()) / n, 4) if n else None,
    }


def observed_snap_lines(lines: list[dict], mask: np.ndarray) -> dict:
    """Sin audio: líneas snappables cuyo inicio de máquina no es onset − adelantos."""
    rows = []
    for i, r in enumerate(lines):
        if not (mask[i] and r["has_words"] and not r["reliable"] and r["origin"] in SNAP_ORIGINS):
            continue
        rows.append(r)
    return {"snappable_lines": len(rows), "jobs": len({r["job_id"] for r in rows})}


# ---------------------------------------------------------------- main


def collect(args) -> tuple[list[dict], list[dict]]:
    by_job, by_sha = load_roles(Path(args.roles))
    jobs: list[dict] = []
    conn = None
    if os.environ.get("DATABASE_URL"):
        import psycopg2

        conn = psycopg2.connect(
            os.environ["DATABASE_URL"],
            options="-c default_transaction_read_only=on -c statement_timeout=300000",
        )
    try:
        if args.gold_dir:
            gold = load_gold(Path(args.gold_dir))
            if conn is not None:
                extra = transcription_quality_for(conn, [j["job_id"] for j in gold])
                for job in gold:
                    info = extra.get(job["job_id"]) or {}
                    job["transcription_quality"] = info.get("quality")
                    job["audio_sha256"] = info.get("audio_sha256")
            jobs.extend(gold)
        if conn is not None and args.staging_limit:
            gold_ids = {j["job_id"] for j in jobs}
            staging = load_staging(conn, limit=args.staging_limit, tenants=args.tenant)
            jobs.extend(j for j in staging if j["job_id"] not in gold_ids)
    finally:
        if conn is not None:
            conn.close()
    lines: list[dict] = []
    for job in jobs:
        role = by_job.get(job["job_id"]) or by_sha.get(str(job.get("audio_sha256") or "")) or "unregistered"
        job["role"] = role
        lines.extend(build_lines(job, role))
    return jobs, lines


def load_beats(path: str | None) -> dict[str, list[float]]:
    if not path or not Path(path).exists():
        return {}
    return {k: v for k, v in json.loads(Path(path).read_text()).items() if v}


CANDIDATES = {
    "current": {**CURRENT, "hold_gap_ms": CURRENT_HOLD_GAP_MS},
    "single_lead_80": {**CURRENT, "wx_lead_ms": 0, "hold_gap_ms": CURRENT_HOLD_GAP_MS},
    "single_lead_0": {**CURRENT, "wx_lead_ms": 0, "polish_lead_ms": 0, "hold_gap_ms": CURRENT_HOLD_GAP_MS},
    "single_lead_80_snap_off": {**CURRENT, "wx_lead_ms": 0, "snap_ms": 0, "hold_gap_ms": CURRENT_HOLD_GAP_MS},
    "single_lead_80_hold_250": {**CURRENT, "wx_lead_ms": 0, "hold_ms": 250, "hold_gap_ms": CURRENT_HOLD_GAP_MS},
    "single_lead_80_air_200": {**CURRENT, "wx_lead_ms": 0, "hold_gap_ms": 200},
    "single_lead_80_air_250": {**CURRENT, "wx_lead_ms": 0, "hold_gap_ms": 250},
}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--gold-dir", default="")
    parser.add_argument("--roles", default=str(BACKEND / "data" / "song_roles.json"))
    parser.add_argument("--staging-limit", type=int, default=100)
    parser.add_argument("--tenant", action="append", default=[])
    parser.add_argument("--beats", default="", help="JSON {job_id: [beats_s]} de beat_snap.detect_beats")
    parser.add_argument("--json-out", default="")
    parser.add_argument("--lines-out", default="", help="pickle de líneas para evaluate_line_signals.py")
    args = parser.parse_args()
    args.tenant = args.tenant or ["universal_music"]

    jobs, lines = collect(args)
    beats = load_beats(args.beats)
    sim = Simulator(lines, beats)
    mask = evaluation_mask(lines)
    groups = group_masks(lines, mask)
    approved_start = np.array([r["approved_start"] if r["approved_start"] is not None else np.nan for r in lines])
    approved_end = np.array([r["approved_end"] if r["approved_end"] is not None else np.nan for r in lines])
    current_start, current_end = sim.run(**CANDIDATES["current"])

    def fidelity(dataset, origin):
        m = sim.has_words & np.array([r["dataset"] == dataset and r["origin"] == origin for r in lines])
        n = int(m.sum())
        if not n:
            return None
        return {
            "lines": n,
            "start_within_20ms": round(float(((np.abs(current_start - sim.machine_start) <= 0.02) & m).sum()) / n, 4),
            "end_within_20ms": round(float(((np.abs(current_end - sim.machine_end) <= 0.02) & m).sum()) / n, 4),
        }

    report: dict = {
        "jobs": dict(Counter(j["dataset"] for j in jobs)),
        "jobs_by_origin": dict(Counter(f"{j['dataset']}|{origin_for(j['timing_source'])}" for j in jobs)),
        "jobs_by_role": dict(Counter(f"{j['dataset']}|{j['role']}" for j in jobs)),
        "jobs_by_live": dict(Counter(f"{j['dataset']}|live={j['live']}" for j in jobs)),
        "lines": len(lines),
        "lines_with_words": int(sim.has_words.sum()),
        "calibration_lines": int(mask.sum()),
        "calibration_lines_by_group": {k: int(v.sum()) for k, v in groups.items()},
        "current_params": CANDIDATES["current"],
        "beats_jobs": len(beats),
        "reproduction_with_current_params": {
            f"{d}|{o}": fidelity(d, o)
            for d in sorted({r["dataset"] for r in lines})
            for o in sorted({r["origin"] for r in lines}) if fidelity(d, o)
        },
        "machine_vs_approved": {
            name: within(sim.machine_start, sim.machine_end, approved_start, approved_end, gmask)
            for name, gmask in groups.items()
        },
        "operator_preference": {
            dataset: operator_preference(lines, mask & np.array([r["dataset"] == dataset for r in lines]))
            for dataset in sorted({r["dataset"] for r in lines})
        },
        "correction_magnitudes": correction_magnitudes(lines, mask),
        "regimes": regime_analysis(lines),
    }

    # (a) Barrido completo de las cuatro mutaciones, con el aire actual.
    results = sweep(sim, lines, mask, groups)
    report["best_by_group"] = {
        name: {"params": top["params"], **top[name]}
        for name in groups if groups[name].sum()
        for top in [best_by(results, name)]
    }
    report["fit_by_role"] = {
        fit: {"params": top["params"], "fit": top[fit],
              "on_other_roles": {g: top[g] for g in groups if g.startswith("role=") and g != fit}}
        for fit in groups if fit.startswith("role=") and groups[fit].sum()
        for top in [best_by(results, fit)]
    }
    report["fit_by_dataset"] = {
        fit: {"params": top["params"], "on_datasets": {g: top[g] for g in groups if g.startswith("dataset=")}}
        for fit in groups if fit.startswith("dataset=") and groups[fit].sum()
        for top in [best_by(results, fit)]
    }
    curve_groups = [g for g in groups if groups[g].sum() >= 30 and (
        g == "all" or "|origin=" in g or g.startswith(("live=", "role=")))]
    anchor = CANDIDATES["current"]
    report["marginals_from_current"] = {
        g: {
            **{key: marginal(sim, approved_start, approved_end, anchor, key, GRID[key], groups[g])
               for key in GRID},
            "hold_gap_ms": marginal(sim, approved_start, approved_end, anchor, "hold_gap_ms",
                                    HOLD_GAP_GRID, groups[g]),
        }
        for g in curve_groups
    }
    # Hold × aire con el adelanto único.
    report["air_sweep"] = {
        g: [
            {"hold_ms": h, "hold_gap_ms": a,
             **within(*sim.run(**{**CANDIDATES["single_lead_80"], "hold_ms": h, "hold_gap_ms": a}),
                      approved_start, approved_end, groups[g])}
            for h in GRID["hold_ms"] for a in HOLD_GAP_GRID
        ]
        for g in ("dataset=staging", "dataset=gold", "staging|origin=ctc", "staging|origin=whisperx")
        if g in groups and groups[g].sum()
    }

    report["candidates_within"] = {
        label: {g: within(*sim.run(**params), approved_start, approved_end, m)
                for g, m in groups.items()}
        for label, params in CANDIDATES.items()
    }

    # (b) Ediciones de timing que desaparecen o aparecen con cada candidato.
    magnitudes = report["correction_magnitudes"]
    tolerance_start = magnitudes["start"].get("p10") or EDIT_THRESHOLD_S
    tolerance_end = magnitudes["end"].get("p10") or EDIT_THRESHOLD_S
    edit_groups = {g: groups[g] for g in groups if g == "all" or g.startswith(("dataset=", "live=", "role="))
                   or "|origin=" in g}
    report["edit_simulation"] = {"tolerance_start_s": tolerance_start, "tolerance_end_s": tolerance_end}
    for label, params in CANDIDATES.items():
        start, end = sim.run(**params)
        report["edit_simulation"][label] = {
            "params": params,
            "strict": {g: timing_edits(start, end, sim.machine_start, sim.machine_end,
                                       approved_start, approved_end, m) for g, m in edit_groups.items()},
            "tolerant": {g: timing_edits(start, end, sim.machine_start, sim.machine_end,
                                         approved_start, approved_end, m,
                                         tolerance_start=tolerance_start, tolerance_end=tolerance_end)
                         for g, m in edit_groups.items()},
            # Misma tolerancia que el criterio de ±100 ms: una línea aceptada
            # solo cuenta como edición nueva si el valor simulado se aleja
            # más de 100 ms de lo que el operador aprobó.
            "tolerant_100ms": {g: timing_edits(start, end, sim.machine_start, sim.machine_end,
                                               approved_start, approved_end, m,
                                               tolerance_start=TOLERANCE_S, tolerance_end=TOLERANCE_S)
                               for g, m in edit_groups.items()},
        }

    # (c) Beat snap.
    report["beat_snap"] = {
        "snappable": observed_snap_lines(lines, mask),
        "current": beat_snap_effect(sim, lines, mask, CANDIDATES["current"]),
        "single_lead_80": beat_snap_effect(sim, lines, mask, CANDIDATES["single_lead_80"]),
    }

    text = json.dumps(report, ensure_ascii=False, indent=1, default=str)
    if args.json_out:
        Path(args.json_out).write_text(text)
    if args.lines_out:
        import pickle

        with open(args.lines_out, "wb") as handle:
            pickle.dump({"jobs": jobs, "lines": lines}, handle)
    print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
