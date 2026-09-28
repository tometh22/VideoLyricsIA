"""Read-only, privacy-bounded sampling of operator revisions for audio review.

An editor revision locates a possible error; it is never a gold label. The
output contains no lyric text, and no row authorizes an automatic mutation.
"""
from __future__ import annotations

from collections import Counter, defaultdict
import hashlib
import hmac
import json
import math
import re
from typing import Any, Iterable

from evidence_contracts import strong_hmac_secret_bytes
from machine_evidence import snapshot_hash, validate_machine_evidence
from reviewer_edit_provenance import identity


SCHEMA = "operator-calibration-triage-v1"
MIN_TIMING_CHANGE_S = 0.05
MAX_CLIP_S = 40.0
MAX_CONTROLS_PER_SONG = 4
REVIEW_REASONS = {"manual", "autosave", "draft", "approve"}


def _token(key: bytes, kind: str, value: str) -> str:
    return hmac.new(key, f"{kind}\x1f{value}".encode(), hashlib.sha256).hexdigest()


def _interval(row: dict[str, Any]) -> tuple[float, float] | None:
    try:
        start, end = float(row["start"]), float(row["end"])
    except (KeyError, TypeError, ValueError):
        return None
    if not math.isfinite(start) or not math.isfinite(end) or start < 0 or end < start:
        return None
    return start, end


def _identity_sequence(rows: list[dict]) -> list[tuple[str, str]] | None:
    values = [identity(row) for row in rows]
    if None in values or len(values) != len(set(values)):
        return None
    return values


def _clip(before: tuple[float, float], after: tuple[float, float]) -> tuple[float, float] | None:
    start = max(0.0, min(before[0], after[0]) - 2.0)
    end = max(before[1], after[1]) + 2.0
    if end - start > MAX_CLIP_S:
        return None
    return round(start, 3), round(end, 3)


def _case(record: dict[str, Any], key: bytes) -> tuple[dict | None, str | None]:
    original = record.get("original_segments")
    edited = record.get("edited_segments")
    first = record.get("original_version") or {}
    target = record.get("edited_version") or {}
    evidence = record.get("machine_evidence")
    if not isinstance(original, list) or not isinstance(edited, list) or not original:
        return None, "segments_unavailable"
    if any(not isinstance(row, dict) for row in original + edited):
        return None, "segments_invalid"
    if (
        first.get("reason") != "transcription"
        or (first.get("provenance") or {}).get("schema")
        != "machine-transcription-lineage-v1"
        or snapshot_hash(first.get("segments")) != snapshot_hash(original)
        or first.get("revision") != 0
    ):
        return None, "machine_checkpoint_unverified"
    if (
        target.get("created_by") != record.get("operator_id")
        or target.get("reason") not in REVIEW_REASONS
        or int(target.get("revision") or 0) <= int(first["revision"])
        or int(target.get("revision") or 0) != int(record.get("current_revision") or 0)
        or snapshot_hash(target.get("segments")) != snapshot_hash(edited)
        or snapshot_hash(record.get("current_segments")) != snapshot_hash(edited)
    ):
        return None, "operator_checkpoint_unverified"
    try:
        validate_machine_evidence(evidence, original)
    except Exception:
        return None, "machine_evidence_unverified"
    pre_human = evidence["pre_human"]
    audio_hash = str(record.get("audio_sha256") or "").lower()
    if (
        not re.fullmatch(r"[0-9a-f]{64}", audio_hash)
        or pre_human.get("audio_sha256") != audio_hash
        or int(pre_human.get("audio_revision") or 0)
        != int(record.get("audio_revision") or 0)
        or not record.get("input_r2_key")
    ):
        return None, "audio_snapshot_unverified"
    old_ids = _identity_sequence(original)
    new_ids = _identity_sequence(edited)
    if old_ids is None or old_ids != new_ids:
        return None, "structure_or_occurrence_unverified"
    if any(_interval(row) is None for row in original + edited):
        return None, "timing_invalid"

    job_id = str(record["job_id"])
    artist = " ".join(str(record.get("artist") or "").casefold().split())
    artist_group = _token(key, "artist", artist or job_id)
    audio_group = _token(key, "audio", audio_hash)
    original_hash = snapshot_hash(original)
    edited_hash = snapshot_hash(edited)
    phrases = Counter(" ".join(str(row.get("text") or "").casefold().split()) for row in original)
    tasks = []
    controls = []
    skipped_lines = Counter()
    for index, (before, after, segment_id) in enumerate(zip(original, edited, old_ids)):
        before_time, after_time = _interval(before), _interval(after)
        delta_start = round(after_time[0] - before_time[0], 4)
        delta_end = round(after_time[1] - before_time[1], 4)
        text_changed = before.get("text") != after.get("text")
        timing_changed = max(abs(delta_start), abs(delta_end)) > MIN_TIMING_CHANGE_S
        micro_timing = bool(delta_start or delta_end) and not timing_changed
        if before.get("locked") or before.get("operator_locked") or after.get("locked") or after.get("operator_locked"):
            skipped_lines["protected"] += 1
            continue
        if micro_timing and not text_changed:
            skipped_lines["micro_timing"] += 1
            continue
        kind = (
            "joint_diagnosis" if text_changed and (timing_changed or micro_timing) else
            "content_review" if text_changed else
            "timing_review" if timing_changed else "unchanged_control"
        )
        clip = _clip(before_time, after_time)
        if clip is None:
            skipped_lines["long_window"] += 1
            continue
        stable_id = f"{segment_id[0]}:{segment_id[1]}"
        task_id = _token(
            key, "task", f"{job_id}\x1f{audio_hash}\x1f{edited_hash}\x1f{stable_id}\x1f{kind}",
        )
        repeated = phrases[" ".join(str(before.get("text") or "").casefold().split())] > 1
        task = {
            "schema": SCHEMA,
            "task_id": task_id,
            "job_id": job_id,
            "line_index": index,
            "task_type": kind,
            "sample_role": "control" if kind == "unchanged_control" else "difficult" if repeated else "changed",
            "clip_start_s": clip[0],
            "clip_end_s": clip[1],
            "repeated_phrase": repeated,
            "timing_source": str(record.get("timing_source") or "unknown")[:64],
            "is_live": record.get("is_live") if isinstance(record.get("is_live"), bool) else None,
            "blind_review_required": True,
            "gold": False,
            "automatic_apply_allowed": False,
        }
        receipt = {
            "task_id": task_id,
            "job_id": job_id,
            "line_identity": list(segment_id),
            "original_version_id": first.get("id"),
            "edited_version_id": target.get("id"),
            "original_segments_sha256": original_hash,
            "edited_segments_sha256": edited_hash,
            "audio_sha256": audio_hash,
            "audio_revision": int(record.get("audio_revision") or 0),
            "original_start_s": before_time[0],
            "original_end_s": before_time[1],
            "edited_start_s": after_time[0],
            "edited_end_s": after_time[1],
            "original_text_hmac": _token(key, "lyric", str(before.get("text") or "")),
            "edited_text_hmac": _token(key, "lyric", str(after.get("text") or "")),
            "operator_revision_is_gold": False,
        }
        if kind == "unchanged_control":
            controls.append((task, receipt))
        else:
            tasks.append((task, receipt))
    controls.sort(key=lambda pair: pair[0]["task_id"])
    tasks.extend(controls[:MAX_CONTROLS_PER_SONG])
    return {
        "job_id": job_id,
        "artist_group": artist_group,
        "audio_group": audio_group,
        "tasks": tasks,
        "skipped_lines": skipped_lines,
    }, None


def build_triage(records: Iterable[dict[str, Any]], *, secret: str) -> dict[str, Any]:
    """Build a frozen review queue; never score an operator edit as truth."""
    key = strong_hmac_secret_bytes(secret)
    if key is None:
        raise ValueError("calibration_hmac_key_missing_or_weak")
    exclusions = Counter()
    cases = []
    seen_jobs = set()
    for record in records:
        job_id = str(record.get("job_id") or "")
        if not job_id or job_id in seen_jobs:
            exclusions["job_missing_or_duplicate"] += 1
            continue
        seen_jobs.add(job_id)
        case, reason = _case(record, key)
        if reason:
            exclusions[reason] += 1
        else:
            cases.append(case)

    # Related artist and recording groups remain in one split. A connected
    # component can merge groups; no job or audio may leak across splits.
    parent = {case["job_id"]: case["job_id"] for case in cases}
    def root(job_id: str) -> str:
        while parent[job_id] != job_id:
            parent[job_id] = parent[parent[job_id]]
            job_id = parent[job_id]
        return job_id
    seen_group = {}
    for case in cases:
        for group in (case["artist_group"], case["audio_group"]):
            if group in seen_group:
                left, right = sorted((root(case["job_id"]), root(seen_group[group])))
                parent[right] = left
            else:
                seen_group[group] = case["job_id"]
    components = defaultdict(list)
    for case in cases:
        components[root(case["job_id"])].append(case)
    queue, provenance = [], []
    split_counts = Counter()
    skipped_lines = Counter()
    for component in components.values():
        component_key = min(case["artist_group"] for case in component)
        bucket = int(_token(key, "split", component_key)[:8], 16) % 10
        split = "train" if bucket < 6 else "calibration" if bucket < 8 else "test"
        for case in component:
            split_counts[split] += 1
            skipped_lines.update(case["skipped_lines"])
            for task, receipt in case["tasks"]:
                queue.append({**task, "split": split})
                provenance.append({**receipt, "split": split})
    queue.sort(key=lambda row: (row["split"], row["job_id"], row["line_index"], row["task_type"]))
    provenance.sort(key=lambda row: row["task_id"])
    kinds = Counter(row["task_type"] for row in queue)
    sources = Counter(row["timing_source"] for row in queue)
    summary = {
        "schema": SCHEMA,
        "jobs_seen": len(seen_jobs),
        "jobs_eligible": len(cases),
        "jobs_excluded": dict(sorted(exclusions.items())),
        "songs_by_split": dict(sorted(split_counts.items())),
        "tasks_by_type": dict(sorted(kinds.items())),
        "tasks_by_timing_source": dict(sorted(sources.items())),
        "skipped_lines": dict(sorted(skipped_lines.items())),
        "blind_gold_labels": 0,
        "automatic_apply_allowed": False,
    }
    summary["queue_sha256"] = hashlib.sha256(json.dumps(
        queue, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    ).encode()).hexdigest()
    return {"queue": queue, "provenance": provenance, "summary": summary}
