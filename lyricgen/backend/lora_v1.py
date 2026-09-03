"""Offline LoRA-v1 data contracts and evaluation helpers.

The helpers are dependency-light on purpose: preparing a private manifest and
computing song-level bootstrap metrics must work on a laptop even when the
optional CUDA/Transformers training stack is not installed.  Training itself
is implemented by ``scripts/train_lora_v1.py`` and is policy-gated.
"""
from __future__ import annotations

from collections import defaultdict
import hashlib
import json
import math
import random
import statistics
from pathlib import Path
from typing import Any, Iterable

SCHEMA_VERSION = 1
BASE_MODEL = "openai/whisper-large-v3-turbo"
CANONICAL_COHORT_SIZE = 23
# v2 policy: reconstructed/difficult rows are shown to the trainer three
# times (the original plus two repeats).  The first adapter remains v1 and
# is not retroactively reweighted.
V2_DIFFICULTY_OVERSAMPLE_RATIO = 3
V2_TRIGGER_SONGS = 100


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def read_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for line_number, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if not raw.strip():
            continue
        try:
            row = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ValueError(f"invalid JSONL at {path}:{line_number}") from exc
        if not isinstance(row, dict):
            raise ValueError(f"JSONL row {line_number} is not an object")
        rows.append(row)
    return rows


def _chunks(lines: Iterable[dict[str, Any]], maximum_s: float = 25.0) -> list[dict[str, Any]]:
    bounded: list[dict[str, Any]] = []
    for line in lines:
        text = str(line.get("text") or "").strip()
        if not text:
            continue
        try:
            start, end = float(line["start_s"]), float(line["end_s"])
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError("line has invalid timing") from exc
        duration = max(0.0, end - start)
        parts = max(1, math.ceil(duration / maximum_s))
        words = text.split()
        if parts > len(words):
            continue
        for part in range(parts):
            left = round(part * len(words) / parts)
            right = round((part + 1) * len(words) / parts)
            if right <= left:
                continue
            bounded.append({
                "start_s": start + duration * part / parts,
                "end_s": start + duration * (part + 1) / parts,
                "text": " ".join(words[left:right]),
            })
    chunks: list[dict[str, Any]] = []
    current: list[dict[str, Any]] = []
    for line in bounded:
        if current and float(line["end_s"]) - float(current[0]["start_s"]) > maximum_s:
            chunks.append({
                "start_s": float(current[0]["start_s"]),
                "end_s": float(current[-1]["end_s"]),
                "text": " ".join(str(item["text"]) for item in current),
            })
            current = []
        current.append(line)
    if current:
        chunks.append({
            "start_s": float(current[0]["start_s"]),
            "end_s": float(current[-1]["end_s"]),
            "text": " ".join(str(item["text"]) for item in current),
        })
    return chunks


def prepare_manifest(
    golden: Path, output: Path, *, historical_paths: Iterable[Path] = (),
    expected_samples: int | None = 498, canonical_size: int = CANONICAL_COHORT_SIZE,
    authorization_reference: str | None = None,
) -> dict[str, Any]:
    """Materialize approved labels without putting them in runtime code.

    All 65 songs remain represented in the manifest for traceability, but the
    canonical exact cohort is marked ``eval_only`` and training code must
    exclude it.  This avoids leaking the 23-song evaluation target into LoRA.
    """
    manifest_path = golden / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    cases = list(manifest.get("cases") or [])
    if not cases:
        raise ValueError("golden manifest has no cases")
    canonical = [
        item for item in cases if str(item.get("raw_quality") or "") == "exact"
    ]
    canonical_ids = {str(item.get("song_id")) for item in canonical}
    if len(canonical_ids) != canonical_size:
        raise ValueError(
            f"canonical cohort expected {canonical_size} exact songs, got {len(canonical_ids)}"
        )
    songs: list[dict[str, Any]] = []
    for item in cases:
        case = golden / str(item["path"])
        meta = json.loads((case / "meta.json").read_text(encoding="utf-8"))
        lines = json.loads((case / "lines.json").read_text(encoding="utf-8"))
        songs.append({
            "song_id": str(item["song_id"]),
            "artist": str(meta.get("artist") or "unknown"),
            "audio_path": str((case / str((meta.get("audio") or {}).get("filename") or "audio.wav")).resolve()),
            "language": str((meta.get("language") or {}).get("value") or "unknown"),
            "raw_quality": str(meta.get("raw_quality") or item.get("raw_quality") or "none"),
            "job_origin": str(meta.get("job_origin") or item.get("job_origin") or "unknown"),
            "eval_only": str(item["song_id"]) in canonical_ids,
            "chunks": _chunks(lines),
        })

    generator = random.Random(20260901)
    song_ids = [song["song_id"] for song in songs]
    generator.shuffle(song_ids)
    validation_ids = set(song_ids[: max(1, round(0.20 * len(song_ids)))])
    # Largest artist groups form the leave-artist-out fold, deterministically.
    # Los nombres se normalizan antes de agrupar: "Bersuit" / "Bersuit
    # Vergarabat" / "LosPericos" / "Los Pericos" eran artistas distintos para
    # el fold y por eso el leave-artist-out de v1 filtraba (auditoría 2026-09-03).
    counts: dict[str, int] = defaultdict(int)
    for song in songs:
        counts[normalize_artist(song["artist"])] += 1
    held_artists: set[str] = set()
    held_songs = 0
    for artist in sorted(counts, key=lambda name: (-counts[name], name)):
        if held_songs >= max(1, round(0.20 * len(songs))):
            break
        held_artists.add(artist)
        held_songs += counts[artist]

    rows: list[dict[str, Any]] = []
    for song in songs:
        difficulty = "difficult" if song["raw_quality"] in {"estimated", "none"} else "easy"
        for index, chunk in enumerate(song["chunks"]):
            rows.append({
                "sample_id": f"{song['song_id']}-{index:03d}",
                "song_id": song["song_id"], "artist": song["artist"],
                "audio_path": song["audio_path"], "language": song["language"],
                "raw_quality": song["raw_quality"], "job_origin": song["job_origin"],
                "difficulty": difficulty, "eval_only": song["eval_only"], **chunk,
                "song_split": "validation" if song["song_id"] in validation_ids else "train",
                "artist_split": "leave_artist_out" if normalize_artist(song["artist"]) in held_artists else "train",
            })
    # Registro de roles: ninguna canción marcada como holdout de evaluación
    # puede entrar al dataset. Antes esto dependía sólo de ``eval_only``, que
    # se calcula acá mismo; el registro es externo y audita el caso en que la
    # cohorte canónica cambie o una canción se reserve por otro motivo.
    try:
        from song_roles import SongRoleViolation, assert_trainable, role_split
    except ImportError:  # pragma: no cover - el registro es parte del backend
        assert_trainable = None  # type: ignore[assignment]
        role_split = None  # type: ignore[assignment]
        SongRoleViolation = RuntimeError  # type: ignore[misc,assignment]
    role_summary: dict[str, int] = {}
    if assert_trainable is not None:
        offenders: list[str] = []
        for row in rows:
            try:
                assert_trainable(str(row["song_id"]))
            except SongRoleViolation:
                offenders.append(str(row["song_id"]))
        if offenders:
            raise ValueError(
                "el dataset incluye canciones de holdout de evaluación: "
                + ", ".join(sorted(set(offenders)))
            )
        role_summary = role_split({str(row["song_id"]) for row in rows})

    historical: list[dict[str, Any]] = []
    historical_rejected = 0
    for path in historical_paths:
        for row in read_jsonl(path):
            row = dict(row)
            # Incomplete legacy rows are useful for an audit backlog but are
            # unsafe training labels (missing machine evidence/deltas). Keep
            # them out of the dataset rather than silently teaching the model
            # from a partially reconstructed approval.
            if row.get("complete") is not True:
                historical_rejected += 1
                continue
            row.setdefault("source", "historical_pair")
            row.setdefault("difficulty", "unknown")
            historical.append(row)
    output.mkdir(parents=True, exist_ok=True)
    samples_path = output / "samples.jsonl"
    lines = [json.dumps(row, ensure_ascii=False, sort_keys=True) for row in rows]
    samples_path.write_text("\n".join(lines) + ("\n" if lines else ""), encoding="utf-8")
    historical_path = output / "historical_pairs.jsonl"
    historical_lines = [json.dumps(row, ensure_ascii=False, sort_keys=True) for row in historical]
    historical_path.write_text(
        "\n".join(historical_lines) + ("\n" if historical_lines else ""), encoding="utf-8",
    )
    if expected_samples is not None and len(rows) != expected_samples:
        raise ValueError(f"expected {expected_samples} samples, materialized {len(rows)}")
    report = {
        "schema_version": SCHEMA_VERSION,
        "base_model": BASE_MODEL,
        "samples": len(rows), "historical_pairs": len(historical),
        "historical_pairs_rejected": historical_rejected, "songs": len(songs),
        "role_split": role_summary,
        "canonical_eval_cohort": {
            "size": len(canonical_ids), "song_ids": sorted(canonical_ids),
            "raw_quality": "exact", "training_excluded": True,
        },
        "song_split": {
            "train_songs": len(songs) - len(validation_ids),
            "validation_songs": len(validation_ids),
            "validation_song_ids": sorted(validation_ids),
        },
        "leave_artist_out": {
            "artists": sorted(held_artists), "songs": held_songs,
        },
        "data": {
            "samples_sha256": sha256_file(samples_path),
            "historical_sha256": sha256_file(historical_path),
            "audio_egress": False,
            "private_local_paths": True,
        },
        "authorization": {
            "enabled": True,
            "reference": authorization_reference,
            "reference_required_at_executor": False,
        },
        "runtime_policy": {
            "role": "additional_consensus_family",
            "replacement_allowed": False,
            "replacement_requires_consecutive_evals": 2,
        },
    }
    (output / "manifest.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8",
    )
    return report


def _tokens(text: str) -> list[str]:
    import unicodedata
    normalized = unicodedata.normalize("NFKD", str(text or "").casefold())
    return ["".join(ch for ch in token if ch.isalnum()) for token in normalized.split() if token]


def _edit_distance(left: list[str], right: list[str]) -> int:
    previous = list(range(len(right) + 1))
    for i, a in enumerate(left, 1):
        current = [i]
        for j, b in enumerate(right, 1):
            current.append(min(
                current[-1] + 1, previous[j] + 1,
                previous[j - 1] + (0 if a == b else 1),
            ))
        previous = current
    return previous[-1]


def word_error_rate(reference: str, hypothesis: str) -> float:
    ref, hyp = _tokens(reference), _tokens(hypothesis)
    if not ref:
        return 0.0 if not hyp else 1.0
    return _edit_distance(ref, hyp) / len(ref)


def _song_metrics(rows: Iterable[dict[str, Any]], hypothesis_key: str = "hypothesis") -> dict[str, dict[str, Any]]:
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        grouped[str(row.get("song_id") or "unknown")].append(row)
    output: dict[str, dict[str, Any]] = {}
    for song_id, items in grouped.items():
        refs = " ".join(str(row.get("reference") or row.get("text") or "") for row in items)
        hyps = " ".join(str(row.get(hypothesis_key) or "") for row in items)
        output[song_id] = {
            "song_id": song_id, "artist": str(items[0].get("artist") or "unknown"),
            "difficulty": str(items[0].get("difficulty") or "unknown"),
            "wer": word_error_rate(refs, hyps),
            "reference_words": len(_tokens(refs)),
        }
    return output


def _mean(values: Iterable[float]) -> float:
    values = list(values)
    return statistics.fmean(values) if values else 0.0


def song_bootstrap(values: dict[str, float], *, seed: int = 20260901, iterations: int = 2000) -> dict[str, float]:
    keys = sorted(values)
    if not keys:
        return {"estimate": 0.0, "ci_low": 0.0, "ci_high": 0.0, "songs": 0}
    generator = random.Random(seed)
    samples = []
    for _ in range(max(100, iterations)):
        samples.append(_mean(values[generator.choice(keys)] for _ in keys))
    samples.sort()
    low = samples[max(0, round(0.025 * (len(samples) - 1)))]
    high = samples[min(len(samples) - 1, round(0.975 * (len(samples) - 1)))]
    return {
        "estimate": _mean(values.values()), "ci_low": low, "ci_high": high,
        "songs": len(keys), "iterations": len(samples),
    }


def evaluate_predictions(
    baseline_rows: list[dict[str, Any]], candidate_rows: list[dict[str, Any]],
    *, canonical_song_ids: set[str] | None = None,
) -> dict[str, Any]:
    """Report overall + easy/difficult song bootstrap, never global WER only."""
    allowed = {str(item) for item in canonical_song_ids} if canonical_song_ids else None
    baseline = _song_metrics(
        [row for row in baseline_rows if allowed is None or str(row.get("song_id")) in allowed],
    )
    candidate = _song_metrics(
        [row for row in candidate_rows if allowed is None or str(row.get("song_id")) in allowed],
    )
    common = sorted(set(baseline) & set(candidate))
    if not common:
        raise ValueError("baseline and candidate have no common evaluation songs")
    result: dict[str, Any] = {"songs": len(common), "by_song": {}, "partitions": {}}
    deltas: dict[str, float] = {}
    for song_id in common:
        delta = baseline[song_id]["wer"] - candidate[song_id]["wer"]
        deltas[song_id] = delta
        result["by_song"][song_id] = {
            "baseline_wer": baseline[song_id]["wer"],
            "candidate_wer": candidate[song_id]["wer"],
            "relative_improvement": delta / baseline[song_id]["wer"] if baseline[song_id]["wer"] else None,
            "artist": candidate[song_id]["artist"], "difficulty": candidate[song_id]["difficulty"],
        }
    for partition in ("overall", "easy", "difficult"):
        ids = common if partition == "overall" else [
            song_id for song_id in common if candidate[song_id]["difficulty"] == partition
        ]
        base_values = {song_id: baseline[song_id]["wer"] for song_id in ids}
        cand_values = {song_id: candidate[song_id]["wer"] for song_id in ids}
        base_ci, cand_ci = song_bootstrap(base_values), song_bootstrap(cand_values)
        base_mean, cand_mean = base_ci["estimate"], cand_ci["estimate"]
        result["partitions"][partition] = {
            "baseline": base_ci, "candidate": cand_ci,
            "relative_improvement": (base_mean - cand_mean) / base_mean if base_mean else None,
            "non_regression": cand_ci["ci_high"] <= base_ci["ci_high"],
        }
    result["replacement_gate"] = {
        "additional_family_only": True,
        "runtime_replacement_allowed": False,
        "requires_consecutive_evaluations": 2,
    }
    result["song_delta_bootstrap"] = song_bootstrap(deltas)
    return result


def data_improvement_curve(
    baseline_rows: list[dict[str, Any]], candidate_rows: list[dict[str, Any]],
    *, sample_sizes: Iterable[int] = (0, 25, 50, 100, 250, 498),
    canonical_song_ids: set[str] | None = None,
) -> list[dict[str, Any]]:
    """Return a deterministic data→improvement curve from cumulative samples.

    A size of zero is the baseline.  For sizes beyond the available candidate
    rows the last available prefix is used and marked ``saturated`` instead of
    inventing an extrapolated score.
    """
    ordered = sorted(candidate_rows, key=lambda row: str(row.get("sample_id") or ""))
    curve = []
    for requested in sample_sizes:
        requested = max(0, int(requested))
        if requested == 0:
            curve.append({"samples": 0, "saturated": False, "relative_improvement": 0.0})
            continue
        used = min(requested, len(ordered))
        evaluated = evaluate_predictions(
            baseline_rows, ordered[:used], canonical_song_ids=canonical_song_ids,
        ) if used else None
        improvement = None
        if evaluated:
            improvement = evaluated["partitions"]["overall"]["relative_improvement"]
        curve.append({
            "samples": requested, "samples_used": used,
            "saturated": requested > len(ordered),
            "relative_improvement": improvement,
        })
    return curve


# Variantes de nombre de una misma banda que ninguna regla ortográfica puede
# unir. Salen de la auditoría del 2026-09-03 sobre el manifest de LoRA v1
# ("Bersuit" vs "Bersuit Vergarabat", "MercedesSosa", "Paez y Spinetta").
# Es un mapa explícito a propósito: se audita, no se adivina.
ARTIST_ALIASES = {
    "bersuitvergarabat": "bersuit",
    "paezyspinetta": "fitopaez",
    "fitopaezyspinetta": "fitopaez",
    "spinetta": "spinetta",
}


def normalize_artist(name: str) -> str:
    """Clave de artista para el leave-artist-out.

    Sin tildes, sin featuring, sin artículo inicial, sin espacios ni puntuación,
    y con el mapa de alias explícito de arriba. "Los Pericos", "LosPericos" y
    "Pericos" caen en la misma clave; "Bersuit Vergarabat" y "Bersuit" también,
    por alias. Antes cada grafía era un artista distinto para el fold y el
    leave-artist-out de v1 filtraba."""
    import re
    import unicodedata
    text = unicodedata.normalize("NFKD", str(name or ""))
    text = "".join(c for c in text if not unicodedata.combining(c)).casefold()
    text = re.sub(r"\s*(ft\.?|feat\.?|&|and)\s.*$", "", text)
    key = re.sub(r"[^a-z0-9]+", "", text)
    # Artículo inicial pegado o separado ("los pericos", "lospericos").
    key = re.sub(r"^(los|las|the)(?=[a-z]{4,})", "", key)
    key = re.sub(r"^(el|la)(?=[a-z]{5,})", "", key)
    return ARTIST_ALIASES.get(key, key)


def holdout_gate(evaluation: dict, *, baseline_delta_wer: float | None = None) -> dict:
    """Compuerta para que una familia entrenada vuelva al consenso.

    Pasa sólo si la mejora sobre el holdout (baseline − candidato, positivo =
    mejora) tiene un IC 95% por canción que NO cruza cero, y si la cohorte no
    contiene canciones de entrenamiento. ``baseline_delta_wer`` es el número a
    superar que dejó el adaptador archivado (LoRA v1: −0,2182, es decir, EMPEORÓ
    0,2182); cualquier candidato con IC positivo ya lo supera, se registra para
    que el reporte lo declare.
    """
    boot = dict(evaluation.get("song_delta_bootstrap") or {})
    roles = dict(evaluation.get("cohort_role_split") or {})
    ci_low = boot.get("ci_low")
    reasons = []
    if not boot.get("songs"):
        reasons.append("holdout_bootstrap_missing")
    if roles.get("train"):
        reasons.append("cohort_contains_train_songs")
    if isinstance(ci_low, (int, float)) and ci_low <= 0:
        reasons.append("ci_crosses_zero_or_worse")
    return {
        "schema": "lora-holdout-gate-v1",
        "passed": not reasons,
        "reasons": reasons,
        "estimate": boot.get("estimate"), "ci_low": ci_low, "ci_high": boot.get("ci_high"),
        "songs": boot.get("songs"), "cohort_role_split": roles,
        "baseline_to_beat_delta_wer": baseline_delta_wer,
    }
