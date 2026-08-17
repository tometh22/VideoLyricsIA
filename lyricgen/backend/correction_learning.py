"""Privacy-safe learning loop from machine lyrics to operator approval.

The trusted worker may read lyric snapshots while computing a delta, but no
raw lyric or audio content leaves this module. Persisted observations contain
only hashes, bounded counters, categorical features and HMAC identifiers.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import math
import os
import re
import statistics
import unicodedata
import uuid
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable

from evidence_contracts import (
    FINGERPRINT_VERSION,
    strong_hmac_secret_bytes,
)


STYLE_RE = re.compile(r"[^\w\s]", re.UNICODE)
VOCALIZATION_RE = re.compile(
    r"^(?:[ouaeh]+|(?:oh|uh|ah|eh|uoh|wow|woah|la|na|yeah|hey|no)+)$",
    re.IGNORECASE,
)
PROPOSAL_CONFIG_SCHEMA = {
    "prefer_mix_witness": (bool, None, None),
    "enable_second_asr": (bool, None, None),
    "enable_acoustic_dp": (bool, None, None),
    "require_ctc_confirmation": (bool, None, None),
    "preserve_unknown_vocal_events": (bool, None, None),
    "stem_mix_disagreement_threshold": ((int, float), 0.0, 1.0),
    "event_boundary_margin_ms": (int, 0, 2000),
}
ALLOWED_PROPOSAL_CONFIG = set(PROPOSAL_CONFIG_SCHEMA)
MIN_SUPPORT_JOBS = 10
MIN_SUPPORT_TENANTS = 3
MIN_SUPPORT_ARTISTS = 3
TRUST_DAYS = 14


class StaleCorrectionSnapshot(RuntimeError):
    """The editor advanced after correction analysis was enqueued."""


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _canonical_json(value: Any) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    ).encode("utf-8")


def sha256_json(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value)).hexdigest()


def env_enabled(name: str) -> bool:
    return os.environ.get(name, "0").strip().lower() in {"1", "true", "yes", "on"}


def validate_proposal_config(config: Any) -> dict:
    """Return one safe, typed ablation variable or fail closed."""
    if not isinstance(config, dict) or len(config) != 1:
        raise ValueError("candidate must change exactly one allow-listed variable")
    key, value = next(iter(config.items()))
    spec = PROPOSAL_CONFIG_SCHEMA.get(key)
    if spec is None:
        raise ValueError("candidate variable is not allow-listed")
    expected_type, minimum, maximum = spec
    if isinstance(value, bool) and expected_type is not bool:
        raise ValueError(f"candidate variable {key} has invalid type")
    if not isinstance(value, expected_type):
        raise ValueError(f"candidate variable {key} has invalid type")
    if isinstance(value, float) and not math.isfinite(value):
        raise ValueError(f"candidate variable {key} must be finite")
    if minimum is not None and not minimum <= value <= maximum:
        raise ValueError(
            f"candidate variable {key} must be between {minimum} and {maximum}"
        )
    return {key: value}


def _normalise_text(value: Any, *, style: bool = False) -> str:
    text = unicodedata.normalize("NFKC", str(value or "")).casefold().strip()
    text = " ".join(text.split())
    if style:
        return text
    return " ".join(STYLE_RE.sub(" ", text).split())


def _text_tokens(value: Any) -> list[str]:
    return [token for token in _normalise_text(value).split() if token]


def _is_vocalization(value: Any) -> bool:
    tokens = _text_tokens(value)
    if not tokens or len(tokens) > 8:
        return False
    compact = "".join(tokens)
    if VOCALIZATION_RE.fullmatch(compact):
        return True
    # Long held vowels ("nooooo", "uoooooh") are non-lexical evidence.
    return bool(re.fullmatch(r"[a-záéíóúüñ]*([aeiouáéíóúü])\1{3,}[a-z]*", compact))


def _segment_time(segment: dict) -> tuple[float, float]:
    try:
        start = max(0.0, float(segment.get("start") or 0.0))
        end = max(start, float(segment.get("end") or start))
    except (TypeError, ValueError):
        return 0.0, 0.0
    if not math.isfinite(start) or not math.isfinite(end):
        return 0.0, 0.0
    return start, end


def _levenshtein(left: list[str], right: list[str]) -> int:
    if len(left) > len(right):
        left, right = right, left
    previous = list(range(len(left) + 1))
    for row, token in enumerate(right, 1):
        current = [row]
        for col, other in enumerate(left, 1):
            current.append(min(
                current[-1] + 1, previous[col] + 1,
                previous[col - 1] + (token != other),
            ))
        previous = current
    return previous[-1]


def _similarity(left: Iterable[dict], right: Iterable[dict]) -> float:
    left_rows, right_rows = list(left), list(right)
    left_tokens = _text_tokens(" ".join(str(row.get("text") or "") for row in left_rows))
    right_tokens = _text_tokens(" ".join(str(row.get("text") or "") for row in right_rows))
    token_denom = max(len(left_tokens), len(right_tokens), 1)
    text_similarity = 1.0 - min(1.0, _levenshtein(left_tokens, right_tokens) / token_denom)
    ls = min((_segment_time(row)[0] for row in left_rows), default=0.0)
    le = max((_segment_time(row)[1] for row in left_rows), default=ls)
    rs = min((_segment_time(row)[0] for row in right_rows), default=0.0)
    re_ = max((_segment_time(row)[1] for row in right_rows), default=rs)
    overlap = max(0.0, min(le, re_) - max(ls, rs))
    union = max(le, re_) - min(ls, rs)
    temporal = overlap / union if union > 0 else float(abs(ls - rs) <= 0.25)
    midpoint_delta = abs(((ls + le) / 2) - ((rs + re_) / 2))
    proximity = max(0.0, 1.0 - midpoint_delta / 8.0)
    return 0.55 * text_similarity + 0.30 * temporal + 0.15 * proximity


def align_segments(original: list[dict], approved: list[dict]) -> list[dict]:
    """Segmental monotonic DP with 1:1, insert, delete, split and merge."""
    n, m = len(original), len(approved)
    inf = float("inf")
    dp = [[inf] * (m + 1) for _ in range(n + 1)]
    back: list[list[tuple[int, int, str] | None]] = [
        [None] * (m + 1) for _ in range(n + 1)
    ]
    dp[0][0] = 0.0
    for i in range(n + 1):
        for j in range(m + 1):
            current = dp[i][j]
            if not math.isfinite(current):
                continue
            transitions: list[tuple[int, int, str, float]] = []
            if i < n and j < m:
                transitions.append((1, 1, "match", 1.0 - _similarity(original[i:i+1], approved[j:j+1])))
            if i < n:
                transitions.append((1, 0, "delete", 0.88))
            if j < m:
                transitions.append((0, 1, "insert", 0.88))
            if i < n and j + 1 < m:
                transitions.append((1, 2, "split", 1.15 - _similarity(original[i:i+1], approved[j:j+2])))
            if i + 1 < n and j < m:
                transitions.append((2, 1, "merge", 1.15 - _similarity(original[i:i+2], approved[j:j+1])))
            for di, dj, operation, cost in transitions:
                ni, nj = i + di, j + dj
                candidate = current + max(0.0, cost)
                # Stable operation order is the deterministic tie breaker.
                if candidate < dp[ni][nj] - 1e-9:
                    dp[ni][nj] = candidate
                    back[ni][nj] = (i, j, operation)
    path: list[dict] = []
    i, j = n, m
    while i or j:
        previous = back[i][j]
        if previous is None:
            raise ValueError("segment alignment is incomplete")
        pi, pj, operation = previous
        path.append({
            "operation": operation,
            "original_indices": list(range(pi, i)),
            "approved_indices": list(range(pj, j)),
        })
        i, j = pi, pj
    path.reverse()
    return path


def _hmac_token(secret: str, token: str) -> str:
    key = strong_hmac_secret_bytes(secret)
    if key is None:
        raise ValueError("quality_learning_hmac_key_weak")
    return hmac.new(key, token.encode("utf-8"), hashlib.sha256).hexdigest()


def _versioned_hmac_token(secret: str, kind: str, value: Any,
                          *, key_id: str | None = None) -> str:
    generation = key_id or current_hmac_key_id()
    normalised = (
        _canonical_json(value).decode("utf-8")
        if isinstance(value, (dict, list, tuple))
        else _normalise_identifier(value)
    )
    digest = _hmac_token(secret, f"{kind}\x1f{normalised}")
    return f"{FINGERPRINT_VERSION}:{generation.lower()}:{digest}"


def _versioned_fingerprint_digest(value: Any, *, key_id: str) -> str | None:
    match = re.fullmatch(
        rf"{re.escape(FINGERPRINT_VERSION)}:{re.escape(key_id.lower())}:([0-9a-f]{{64}})",
        str(value or "").strip().lower(),
    )
    return match.group(1) if match else None


def _normalise_identifier(value: Any) -> str:
    """Canonicalise pseudonymous dimensions without losing component boundaries."""
    return "\x1f".join(
        " ".join(unicodedata.normalize("NFKC", part).casefold().split())
        for part in str(value).split("\x1f")
    )


def _normalise_entity_identifier(value: Any) -> str:
    """Conservatively collapse artist/song spelling variants for diversity gates."""
    parts = []
    for part in str(value).split("\x1f"):
        decomposed = unicodedata.normalize("NFKD", part).casefold()
        parts.append("".join(
            character for character in decomposed
            if unicodedata.category(character)[0] in {"L", "N"}
        ))
    return "\x1f".join(parts)


def current_hmac_key_id() -> str:
    key_id = os.environ.get("QUALITY_LEARNING_HMAC_KEY_ID", "").strip()
    if not key_id or not re.fullmatch(r"[A-Za-z0-9._-]{1,32}", key_id):
        raise RuntimeError("quality_learning_hmac_key_id_missing_or_invalid")
    return key_id


def hmac_identifier(kind: str, value: Any) -> str | None:
    """Pseudonymise an identifier before it enters analytics or RQ."""
    if value is None or str(value) == "":
        return None
    secret = os.environ.get("QUALITY_LEARNING_HMAC_KEY", "").strip()
    if strong_hmac_secret_bytes(secret) is None:
        raise RuntimeError("quality_learning_hmac_key_missing_or_weak")
    current_hmac_key_id()
    normalised = (
        _normalise_entity_identifier(value)
        if kind in {"artist", "song"} else _normalise_identifier(value)
    )
    return _hmac_token(secret, f"{kind}\x1f{normalised}")


def derive_server_active_edit_ms(db: Any, job: Any, version: Any,
                                 session_id: str | None) -> int | None:
    """Derive effort only from contiguous, server-timestamped editor samples."""
    if not session_id or version.created_by is None:
        return None
    from database import ProductEvent
    from evidence_attestation import lyric_snapshot_hash

    quality = dict(job.transcription_quality or {})
    release = str(quality.get("pipeline_release") or "unknown")[:64]
    config = str(quality.get("pipeline_config_fingerprint") or "unknown")[:32]
    approved_hash = lyric_snapshot_hash(version.segments or [])
    rows = db.query(ProductEvent).filter(
        ProductEvent.name == "editor_activity_heartbeat",
        ProductEvent.job_id == job.job_id,
        ProductEvent.user_id == version.created_by,
    ).order_by(ProductEvent.created_at.asc(), ProductEvent.id.asc()).all()
    candidates: list[tuple[Any, datetime, dict]] = []
    for row in rows:
        properties = dict(row.properties or {})
        timestamp = row.occurred_at or row.created_at
        if (
            timestamp is None
            or properties.get("session_id") != session_id
            or str(properties.get("pipeline_release") or "unknown") != release
            or str(properties.get("pipeline_config_fingerprint") or "unknown") != config
        ):
            continue
        if timestamp.tzinfo is None:
            timestamp = timestamp.replace(tzinfo=timezone.utc)
        candidates.append((row, timestamp.astimezone(timezone.utc), properties))
    terminal = [item for item in candidates if (
        int(item[2].get("revision", -1)) == int(version.revision)
        and item[2].get("snapshot_sha256") == approved_hash
    )]
    if not terminal:
        return None
    terminal_at = max(item[1] for item in terminal)
    accepted = [item for item in candidates if item[1] <= terminal_at]
    if len(accepted) < 2:
        return None
    sequences = [int(item[2].get("activity_seq", -1)) for item in accepted]
    if sequences != list(range(1, len(sequences) + 1)):
        return None
    revisions = [int(item[2].get("revision", -1)) for item in accepted]
    if any(right < left for left, right in zip(revisions, revisions[1:])):
        return None
    active_seconds = sum(
        gap for gap in (
            (right[1] - left[1]).total_seconds()
            for left, right in zip(accepted, accepted[1:])
        )
        if 0 < gap <= 20
    )
    return int(round(active_seconds * 1000)) if active_seconds > 0 else None


def machine_snapshot_provenance(job: Any, quality: dict | None) -> dict:
    """Return pseudonymous lineage for a tenant-scoped machine checkpoint.

    Raw SHA values may still exist in the job for audio CAS and editor OCC,
    but this projection never copies them.  Missing/weak privacy keys abstain
    instead of falling back to dictionary-attackable hashes.
    """
    payload = dict(quality or {})
    quality_job = dict(payload.get("quality_job") or {})
    audio_identity = str(
        payload.get("audio_sha256") or quality_job.get("audio_sha256") or ""
    )
    if re.fullmatch(r"[0-9a-f]{64}", audio_identity):
        audio_identity_kind = "audio_content_hmac"
    else:
        storage_identity = str(getattr(job, "input_r2_key", None) or "")
        audio_identity = storage_identity
        audio_identity_kind = (
            "storage_object_identity_hmac" if storage_identity else "unavailable"
        )
    secret = os.environ.get("QUALITY_LEARNING_HMAC_KEY", "").strip()
    try:
        key_id = current_hmac_key_id()
        audio_fingerprint = (
            _versioned_hmac_token(
                secret, "machine-audio-identity", audio_identity, key_id=key_id,
            ) if audio_identity else None
        )
        quality_fingerprint = _versioned_hmac_token(
            secret, "machine-quality-evidence", payload, key_id=key_id,
        )
        source_quality_fingerprint = (
            _versioned_hmac_token(
                secret, "source-quality-fingerprint",
                payload.get("quality_fingerprint"), key_id=key_id,
            ) if payload.get("quality_fingerprint") else None
        )
    except (RuntimeError, ValueError):
        key_id = None
        audio_fingerprint = None
        quality_fingerprint = None
        source_quality_fingerprint = None
    return {
        "schema": "machine-transcription-lineage-v1",
        "fingerprint_version": FINGERPRINT_VERSION,
        "hmac_key_id": key_id,
        "audio_fingerprint": audio_fingerprint,
        "audio_identity_kind": audio_identity_kind,
        "pipeline_release": str(payload.get("pipeline_release") or "unknown")[:64],
        "pipeline_config_fingerprint": str(
            payload.get("pipeline_config_fingerprint") or "unknown"
        )[:64],
        "timing_source": str(
            payload.get("timing_source")
            or getattr(job, "timing_source", None) or "unknown"
        )[:64],
        "quality_policy_version": str(payload.get("policy_version") or "unknown")[:64],
        "source_quality_fingerprint": source_quality_fingerprint,
        "quality_evidence_fingerprint": quality_fingerprint,
        "route": str(payload.get("route") or payload.get("decision") or "unknown")[:64],
    }


def _bounded_number(value: Any, minimum: float, maximum: float) -> float | None:
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return None
    value = float(value)
    if not math.isfinite(value):
        return None
    return round(max(minimum, min(maximum, value)), 6)


def classify_corrections(original: list[dict], approved: list[dict], *, secret: str) -> dict:
    if strong_hmac_secret_bytes(secret) is None:
        raise ValueError("QUALITY_LEARNING_HMAC_KEY must contain 32 strong bytes")
    key_id = current_hmac_key_id()
    path = align_segments(original, approved)
    categories: Counter[str] = Counter()
    timing_deltas: list[float] = []
    lexical_hmacs: list[str] = []
    for step in path:
        operation = step["operation"]
        left = [original[index] for index in step["original_indices"]]
        right = [approved[index] for index in step["approved_indices"]]
        if operation == "insert":
            categories["missing_event"] += len(right)
            if any(_is_vocalization(row.get("text")) for row in right):
                categories["missing_vocalization"] += 1
            continue
        if operation == "delete":
            categories["spurious_event"] += len(left)
            continue
        if operation in {"split", "merge"}:
            categories[operation] += 1
        if operation == "split" and len(right) > len(left):
            categories["missing_event"] += len(right) - len(left)
        if operation == "merge" and len(left) > len(right):
            categories["spurious_event"] += len(left) - len(right)
        if (
            operation == "split"
            and any(_is_vocalization(row.get("text")) for row in right)
            and not any(_is_vocalization(row.get("text")) for row in left)
        ):
            categories["missing_vocalization"] += 1
        if (
            operation == "merge"
            and any(_is_vocalization(row.get("text")) for row in left)
            and not any(_is_vocalization(row.get("text")) for row in right)
        ):
            categories["spurious_vocalization"] += 1
        left_text = " ".join(str(row.get("text") or "") for row in left)
        right_text = " ".join(str(row.get("text") or "") for row in right)
        left_normal = _normalise_text(left_text)
        right_normal = _normalise_text(right_text)
        if left_normal != right_normal:
            if _is_vocalization(left_text) or _is_vocalization(right_text):
                categories["vocalization_changed"] += 1
            else:
                categories["lexical_substitution"] += 1
            pair = f"{left_normal}\x1f{right_normal}"
            lexical_hmacs.append(_versioned_hmac_token(
                secret, "lexical-correction", pair, key_id=key_id,
            ))
        elif _normalise_text(left_text, style=True) != _normalise_text(right_text, style=True):
            categories["style_only"] += 1
        if left and right:
            ls = min(_segment_time(row)[0] for row in left)
            le = max(_segment_time(row)[1] for row in left)
            rs = min(_segment_time(row)[0] for row in right)
            re_ = max(_segment_time(row)[1] for row in right)
            onset_delta = rs - ls
            end_delta = re_ - le
            if abs(onset_delta) > 0.05:
                categories["timing_onset"] += 1
                timing_deltas.append(abs(onset_delta))
            if abs(end_delta) > 0.05:
                categories["timing_end"] += 1
                timing_deltas.append(abs(end_delta))
            if abs((re_ - rs) - (le - ls)) > 0.05:
                categories["timing_duration"] += 1
    original_ids = [str(row.get("_id")) for row in original if row.get("_id")]
    approved_ids = [str(row.get("_id")) for row in approved if row.get("_id")]
    common = set(original_ids) & set(approved_ids)
    if len(common) >= 2:
        left_rank = [value for value in original_ids if value in common]
        right_rank = [value for value in approved_ids if value in common]
        categories["reordered"] = sum(
            1 for index, value in enumerate(left_rank)
            if index >= len(right_rank) or right_rank[index] != value
        )
    temporal_invalid_original = sum(
        1 for index, row in enumerate(original)
        if index and _segment_time(row)[0] < _segment_time(original[index - 1])[0]
    )
    temporal_invalid_approved = sum(
        1 for index, row in enumerate(approved)
        if index and _segment_time(row)[0] < _segment_time(approved[index - 1])[0]
    )
    if temporal_invalid_original > temporal_invalid_approved:
        categories["temporal_order_repaired"] += temporal_invalid_original - temporal_invalid_approved
    def invalid_ranges(rows: list[dict]) -> int:
        count = 0
        for row in rows:
            try:
                count += int(float(row.get("end") or 0) < float(row.get("start") or 0))
            except (TypeError, ValueError):
                count += 1
        return count
    def destructive_overlaps(rows: list[dict]) -> int:
        return sum(
            _segment_time(row)[0] < _segment_time(rows[index - 1])[1] - 0.05
            for index, row in enumerate(rows) if index
        )
    range_delta = invalid_ranges(original) - invalid_ranges(approved)
    if range_delta > 0:
        categories["timing_inversion_repaired"] += range_delta
    overlap_delta = destructive_overlaps(original) - destructive_overlaps(approved)
    if overlap_delta > 0:
        categories["timing_overlap_repaired"] += overlap_delta
    return {
        "categories": {key: int(value) for key, value in sorted(categories.items()) if value},
        "metrics": {
            "original_events": len(original),
            "approved_events": len(approved),
            "event_count_delta": len(approved) - len(original),
            "changed_events": int(sum(categories.values())),
            "timing_delta_p50_ms": (
                round(statistics.median(timing_deltas) * 1000, 3)
                if timing_deltas else 0.0
            ),
            "timing_delta_max_ms": (
                round(max(timing_deltas) * 1000, 3) if timing_deltas else 0.0
            ),
            "lexical_hmacs": sorted(set(lexical_hmacs))[:64],
        },
        "alignment_counts": dict(Counter(step["operation"] for step in path)),
    }


def privacy_safe_features(job: Any) -> dict:
    quality = dict(getattr(job, "transcription_quality", None) or {})
    metrics = dict(quality.get("metrics") or {})
    reasons = {
        str(row.get("code")) for row in (quality.get("reasons") or [])
        if isinstance(row, dict) and row.get("code")
    }
    analyses = [
        row for row in (quality.get("analysis_windows") or [])
        if isinstance(row, dict)
    ]
    acoustic = [
        event for analysis in analyses
        for event in (
            (((analysis.get("structure") or {}).get("best_partition") or {}).get("events"))
            or ((analysis.get("acoustic") or {}).get("events")) or []
        )
        if isinstance(event, dict)
    ]
    feature = {
        "is_live": bool(metrics.get("is_live") or "live" in reasons),
        "stem_degraded": bool(metrics.get("stem_degraded") or "stem_degraded" in reasons),
        "voice_mix_only": bool(
            metrics.get("mix_only_vocals") or "strong_unassigned_vocal_events" in reasons
        ),
        "crowd_or_chorus": bool(
            metrics.get("crowd_or_chorus") or any(
                event.get("class") in {"crowd", "chorus", "overlap"}
                or float((event.get("type_posterior") or {}).get("crowd_or_overlap") or 0) >= .5
                for event in acoustic
            )
        ),
        "repetition_ambiguous": bool(
            metrics.get("repetition_ambiguous") or "event_count" in reasons
            or "structural_autorepair_uncalibrated" in reasons
        ),
        "sustained_vocal": any(
            event.get("class") == "sustained"
            or float((event.get("type_posterior") or {}).get("sustained_vocalization") or 0) >= .5
            for event in acoustic
        ),
        "asr_disagreement": bool(metrics.get("asr_disagreement")),
        "ctc_low_margin": bool(metrics.get("ctc_low_margin")),
        "timing_source": str(getattr(job, "timing_source", None) or "unknown")[:64],
        "quality_decision": str(quality.get("decision") or "unknown")[:32],
        "language": str(metrics.get("language") or "unknown")[:16],
    }
    for key in ("snr_db", "stem_mix_delta_db", "ctc_margin", "asr_agreement"):
        bounded = _bounded_number(metrics.get(key), -120.0, 120.0)
        if bounded is not None:
            feature[key] = bounded
    return feature


def observation_identity(job_id: str, original_hash: str, approved_hash: str,
                         release: str, config: str, *, secret: str) -> str:
    return _hmac_token(secret, "observation\x1f" + json.dumps({
        "job_id": job_id, "original_hash": original_hash,
        "approved_hash": approved_hash, "release": release, "config": config,
    }, sort_keys=True, separators=(",", ":")))


def create_observation(db: Any, job_id: str, approved_version_id: str,
                       *, active_edit_ms: int | None = None,
                       active_edit_source: str | None = None,
                       source_confidence: str = "exact",
                       session_hmac: str | None = None,
                       expected_revision: int | None = None,
                       expected_approved_hash: str | None = None,
                       expected_learning_epoch: int | None = None) -> Any:
    from database import (
        CorrectionObservation, EditorDocument, EditorVersion, Job,
    )
    from evidence_attestation import lyric_snapshot_hash

    secret = os.environ.get("QUALITY_LEARNING_HMAC_KEY", "").strip()
    if strong_hmac_secret_bytes(secret) is None:
        raise RuntimeError("quality_learning_hmac_key_missing_or_weak")
    hmac_key_id = current_hmac_key_id()
    job = db.query(Job).filter(Job.job_id == job_id).with_for_update().one()
    if (
        expected_learning_epoch is not None
        and int(job.quality_learning_epoch or 0) != int(expected_learning_epoch)
    ):
        raise StaleCorrectionSnapshot("quality_learning_epoch_advanced")
    document = db.query(EditorDocument).filter(
        EditorDocument.job_id == job_id,
    ).with_for_update().one()
    version = db.query(EditorVersion).filter(
        EditorVersion.id == approved_version_id,
        EditorVersion.job_id == job_id,
        EditorVersion.is_approved.is_(True),
    ).one()
    original = list(document.original_segments or [])
    approved = list(version.segments or [])
    original_snapshot_hash = lyric_snapshot_hash(original)
    approved_snapshot_hash = lyric_snapshot_hash(approved)
    if (
        expected_revision is not None
        and int(document.revision or 0) != int(expected_revision)
    ):
        raise StaleCorrectionSnapshot("editor_revision_advanced")
    if expected_approved_hash and approved_snapshot_hash != expected_approved_hash:
        raise StaleCorrectionSnapshot("approved_snapshot_hash_changed")
    if int(version.revision) != int(document.revision or 0):
        raise StaleCorrectionSnapshot("approved_version_is_not_current")
    original_versions = db.query(EditorVersion).filter(
        EditorVersion.job_id == job_id,
    ).order_by(EditorVersion.revision.asc()).all()
    original_version = next((
        candidate for candidate in original_versions
        if lyric_snapshot_hash(candidate.segments or []) == original_snapshot_hash
    ), None)
    original_provenance = dict(
        getattr(original_version, "provenance", None) or {}
    )
    verified_source = bool(
        original_version is not None
        and original_provenance.get("schema") == "machine-transcription-lineage-v1"
    )
    if not verified_source:
        source_confidence = "legacy_unverified"
    quality = dict(job.transcription_quality or {})
    provenance = original_provenance
    release = str(
        provenance.get("pipeline_release")
        or quality.get("pipeline_release") or "unknown"
    )[:64]
    config = str(
        provenance.get("pipeline_config_fingerprint")
        or quality.get("pipeline_config_fingerprint") or "unknown"
    )[:64]
    original_hash = _hmac_token(
        secret, f"original-lyric-snapshot\x1f{original_snapshot_hash}",
    )
    approved_hash = _hmac_token(
        secret, f"approved-lyric-snapshot\x1f{approved_snapshot_hash}",
    )
    identity = observation_identity(
        job_id, original_hash, approved_hash, release, config, secret=secret,
    )
    independent_review = bool(
        getattr(job, "approved_by", None) is not None
        and version.created_by is not None
        and int(job.approved_by) != int(version.created_by)
    )
    existing = db.query(CorrectionObservation).filter(
        CorrectionObservation.identity_hash == identity,
    ).first()
    if existing:
        if (
            independent_review and existing.source_confidence == "exact"
            and existing.invalidated_at is None
        ):
            existing.label_tier = "trusted"
            existing.trusted_at = now_utc()
            existing.updated_at = existing.trusted_at
        if (
            existing.active_edit_ms is None
            and active_edit_source == "server_product_events_v1"
            and isinstance(active_edit_ms, (int, float))
        ):
            existing.active_edit_ms = max(0, min(int(active_edit_ms), 14_400_000))
            existing.metrics = {
                **dict(existing.metrics or {}),
                "operator_time_source": "server_product_events_v1",
            }
        if existing.session_hmac is None and session_hmac:
            existing.session_hmac = str(session_hmac)[:64]
        db.flush()
        return existing
    delta = classify_corrections(original, approved, secret=secret)
    moment = now_utc()
    # A later approval supersedes every previous active label for this job.
    for stale in db.query(CorrectionObservation).filter(
        CorrectionObservation.job_id == job_id,
        CorrectionObservation.invalidated_at.is_(None),
    ).all():
        stale.label_tier = "invalidated"
        stale.invalidated_at = moment
        stale.invalidation_reason = "superseded_by_approval"
        stale.updated_at = moment
    measured_audio_hash = str(
        quality.get("audio_sha256")
        or (quality.get("quality_job") or {}).get("audio_sha256") or ""
    )
    audio_hash = (
        _hmac_token(secret, f"audio-content\x1f{measured_audio_hash}")
        if re.fullmatch(r"[0-9a-f]{64}", measured_audio_hash) else None
    )
    if audio_hash is None:
        audio_hash = _versioned_fingerprint_digest(
            provenance.get("audio_fingerprint"), key_id=hmac_key_id,
        )
    if audio_hash is None and re.fullmatch(
        r"[0-9a-f]{64}", str(provenance.get("audio_sha256") or ""),
    ):
        audio_hash = _hmac_token(
            secret, f"legacy-audio-content\x1f{provenance['audio_sha256']}",
        )
    input_key = str(getattr(job, "input_r2_key", None) or "")
    if not audio_hash and input_key:
        # Storage identity, not raw bytes; content hashes from the quality
        # cache take precedence when available.
        audio_hash = _hmac_token(secret, f"storage-object\x1f{input_key}")
    row = CorrectionObservation(
        id=str(uuid.uuid4()), identity_hash=identity, job_id=job_id,
        tenant_id=str(job.tenant_id), original_revision=int(
            original_version.revision if original_version is not None else 0
        ),
        approved_revision=int(version.revision), approved_version_id=version.id,
        original_hash=original_hash, approved_hash=approved_hash,
        audio_hash=audio_hash, pipeline_release=release,
        pipeline_config_fingerprint=config,
        timing_source=str(
            provenance.get("timing_source") or job.timing_source or "unknown"
        )[:64],
        pipeline_route=str(
            provenance.get("route") or quality.get("route") or "unknown"
        )[:64],
        label_tier=(
            "trusted" if independent_review and source_confidence == "exact"
            else "observed"
        ),
        source_confidence=source_confidence,
        operator_hmac=hmac_identifier("operator", version.created_by),
        session_hmac=(str(session_hmac)[:64] if session_hmac else None),
        artist_hmac=hmac_identifier("artist", getattr(job, "artist", None)),
        song_hmac=hmac_identifier(
            "song", f"{getattr(job, 'artist', '')}\x1f{getattr(job, 'song_title', '')}",
        ),
        hmac_key_id=hmac_key_id,
        categories=delta["categories"],
        features=privacy_safe_features(job),
        metrics={
            **delta["metrics"], "alignment_counts": delta["alignment_counts"],
            "operator_time_source": (
                "server_product_events_v1"
                if active_edit_source == "server_product_events_v1"
                else "untrusted_or_missing"
            ),
        },
        active_edit_ms=(
            max(0, min(int(active_edit_ms), 14_400_000))
            if active_edit_source == "server_product_events_v1"
            and isinstance(active_edit_ms, (int, float)) else None
        ),
        matures_at=moment + timedelta(days=TRUST_DAYS),
        trusted_at=(
            moment if independent_review and source_confidence == "exact" else None
        ),
        created_at=moment, updated_at=moment,
    )
    db.add(row)
    db.flush()
    return row


def invalidate_job_observations(db: Any, job_id: str, reason: str) -> int:
    from database import CorrectionObservation, Job
    moment = now_utc()
    job = db.query(Job).filter(Job.job_id == job_id).with_for_update().one()
    job.quality_learning_epoch = int(job.quality_learning_epoch or 0) + 1
    job.quality_learning_invalidated_at = moment
    rows = db.query(CorrectionObservation).filter(
        CorrectionObservation.job_id == job_id,
        CorrectionObservation.invalidated_at.is_(None),
    ).all()
    for row in rows:
        row.label_tier = "invalidated"
        row.invalidated_at = moment
        row.invalidation_reason = str(reason or "later_edit")[:120]
        row.updated_at = moment
    return len(rows)


def mature_observations(db: Any, *, at: datetime | None = None) -> dict:
    from database import CorrectionObservation, EditorVersion
    moment = at or now_utc()
    matured = invalidated = 0
    rows = db.query(CorrectionObservation).filter(
        CorrectionObservation.label_tier == "observed",
        CorrectionObservation.matures_at <= moment,
        CorrectionObservation.invalidated_at.is_(None),
    ).all()
    for row in rows:
        later = db.query(EditorVersion).filter(
            EditorVersion.job_id == row.job_id,
            EditorVersion.created_at > row.created_at,
            EditorVersion.revision > row.approved_revision,
        ).first()
        if later:
            row.label_tier = "invalidated"
            row.invalidated_at = moment
            row.invalidation_reason = "later_editor_revision"
            invalidated += 1
        elif row.source_confidence != "exact":
            # Legacy snapshots may inform aggregate debugging but never train.
            continue
        else:
            row.label_tier = "trusted"
            row.trusted_at = moment
            matured += 1
        row.updated_at = moment
    return {"matured": matured, "invalidated": invalidated}


def _log_rr_interval(group_pos: int, group_total: int,
                     other_pos: int, other_total: int) -> tuple[float, float, float]:
    # Haldane-Anscombe smoothing keeps sparse, privacy-thresholded buckets finite.
    a, b = group_pos + 0.5, group_total - group_pos + 0.5
    c, d = other_pos + 0.5, other_total - other_pos + 0.5
    risk_group = a / (a + b)
    risk_other = c / (c + d)
    rr = risk_group / max(risk_other, 1e-12)
    se = math.sqrt(max(0.0, 1 / a - 1 / (a + b) + 1 / c - 1 / (c + d)))
    log_rr = math.log(max(rr, 1e-12))
    return rr, math.exp(log_rr - 1.96 * se), math.exp(log_rr + 1.96 * se)


def _median_active_seconds(rows: list[Any]) -> float:
    values = [
        float(row.active_edit_ms) / 1000 for row in rows
        if row.active_edit_ms is not None
        and (row.metrics or {}).get("operator_time_source") == "server_product_events_v1"
    ]
    return statistics.median(values) if values else 0.0


def _proposal_for(category: str, context_key: str) -> tuple[str, str, dict]:
    if "stem_degraded=true" in context_key or "voice_mix_only=true" in context_key:
        return (
            "routing_rule", "Confirmar eventos contra la mezcla",
            {"prefer_mix_witness": True},
        )
    if "asr_disagreement=true" in context_key or category in {
        "lexical_substitution", "missing_vocalization",
    }:
        return (
            "routing_rule", "Activar segundo ASR en ventanas inseguras",
            {"enable_second_asr": True},
        )
    if "repetition_ambiguous=true" in context_key or category in {"split", "merge", "missing_event"}:
        return (
            "alignment_config", "Priorizar estructura acústica monotónica",
            {"enable_acoustic_dp": True},
        )
    if category.startswith("timing_") or category == "temporal_order_repaired":
        return (
            "alignment_config", "Exigir confirmación CTC para límites inseguros",
            {"require_ctc_confirmation": True},
        )
    return (
        "code_hypothesis", "Investigar evidencia asociada al patrón",
        {},
    )


def mine_patterns(db: Any, *, at: datetime | None = None) -> dict:
    """Deterministic association miner; it never claims causal evidence."""
    from database import AuditLog, CorrectionObservation, QualityFixProposal, QualityPattern
    moment = at or now_utc()
    proposals_enabled = env_enabled("QUALITY_LEARNING_PROPOSALS_ENABLED")
    hmac_key_id = current_hmac_key_id()
    raw_rows = db.query(CorrectionObservation).filter(
        CorrectionObservation.label_tier == "trusted",
        CorrectionObservation.invalidated_at.is_(None),
        CorrectionObservation.hmac_key_id == hmac_key_id,
    ).all()
    # Reuploads of the same master are one statistical unit, not independent
    # evidence. Keep the newest trusted observation deterministically.
    deduplicated: dict[str, Any] = {}
    for row in sorted(raw_rows, key=lambda item: (item.created_at, item.id)):
        deduplicated[str(row.audio_hash or row.job_id)] = row
    rows = list(deduplicated.values())
    existing_patterns = db.query(QualityPattern).all()
    confirmed_fingerprints = {
        pattern.fingerprint for pattern in existing_patterns
        if pattern.status == "confirmed"
    }
    for stale_pattern in existing_patterns:
        stale_pattern.status = "stale"
        stale_pattern.updated_at = moment
    if not rows and proposals_enabled:
        for proposal in db.query(QualityFixProposal).filter(
            QualityFixProposal.status.in_(("draft", "validating", "ready")),
        ).all():
            previous_status = proposal.status
            proposal.status = "superseded"
            proposal.updated_at = moment
            db.add(AuditLog(
                user_id=None,
                action="quality_learning.proposal.transition",
                detail={
                    "proposal_id": proposal.id,
                    "from": previous_status,
                    "to": "superseded",
                },
            ))
        return {"trusted": 0, "qualified": 0, "proposals": 0}
    if not rows:
        return {"trusted": 0, "qualified": 0, "proposals": 0}
    categories = sorted({key for row in rows for key, value in (row.categories or {}).items() if value})
    contexts: dict[str, list[Any]] = defaultdict(list)
    for row in rows:
        for key, value in sorted((row.features or {}).items()):
            if isinstance(value, bool) and value:
                contexts[f"{key}=true"].append(row)
            elif key in {"timing_source", "quality_decision", "language"} and value:
                contexts[f"{key}={str(value)[:64]}"] .append(row)
    qualified = proposals = 0
    all_median = _median_active_seconds(rows)
    for category in categories:
        total_positive = sum(bool((row.categories or {}).get(category)) for row in rows)
        baseline_rate = total_positive / len(rows)
        for context_key, group in contexts.items():
            tenants = {row.tenant_id for row in group}
            artists = {row.artist_hmac for row in group if row.artist_hmac}
            if (
                len(group) < MIN_SUPPORT_JOBS
                or len(tenants) < MIN_SUPPORT_TENANTS
                or len(artists) < MIN_SUPPORT_ARTISTS
            ):
                continue
            group_ids = {row.id for row in group}
            group_pos = sum(bool((row.categories or {}).get(category)) for row in group)
            others = [row for row in rows if row.id not in group_ids]
            other_pos = sum(bool((row.categories or {}).get(category)) for row in others)
            if not group_pos or not others:
                continue
            rr, ci_low, ci_high = _log_rr_interval(
                group_pos, len(group), other_pos, len(others),
            )
            impact = _median_active_seconds(group) - all_median
            is_qualified = ci_low > 1.0 and (rr >= 1.5 or impact >= 60.0)
            fingerprint = sha256_json({"category": category, "context": context_key})
            pattern = db.query(QualityPattern).filter(
                QualityPattern.fingerprint == fingerprint,
            ).first()
            if not pattern:
                pattern = QualityPattern(
                    id=str(uuid.uuid4()), fingerprint=fingerprint,
                    category=category, context_key=context_key,
                    evidence={}, first_seen_at=moment,
                )
                db.add(pattern)
                db.flush()
            previous_status = pattern.status
            pattern.support_jobs = len(group)
            pattern.support_tenants = len(tenants)
            pattern.support_artists = len(artists)
            pattern.baseline_rate = round(baseline_rate, 8)
            pattern.observed_rate = round(group_pos / len(group), 8)
            pattern.relative_risk = round(rr, 8)
            pattern.ci_low = round(ci_low, 8)
            pattern.ci_high = round(ci_high, 8)
            pattern.impact_seconds = round(impact, 3)
            pattern.evidence = {
                "association_only": True, "group_positive": group_pos,
                "group_total": len(group), "other_positive": other_pos,
                "other_total": len(others), "thresholds": {
                    "min_jobs": MIN_SUPPORT_JOBS,
                    "min_tenants": MIN_SUPPORT_TENANTS,
                    "min_artists": MIN_SUPPORT_ARTISTS,
                    "min_relative_risk": 1.5,
                    "min_impact_seconds": 60,
                },
            }
            pattern.status = (
                "confirmed" if is_qualified and fingerprint in confirmed_fingerprints
                else "correlated" if is_qualified else "emerging"
            )
            pattern.last_seen_at = moment
            pattern.updated_at = moment
            pattern.version = int(pattern.version or 0) + 1
            if pattern.status != previous_status:
                db.add(AuditLog(
                    user_id=None,
                    action="quality_learning.pattern.transition",
                    detail={
                        "pattern_id": pattern.id,
                        "from": previous_status,
                        "to": pattern.status,
                    },
                ))
            if not is_qualified:
                continue
            qualified += 1
            existing = db.query(QualityFixProposal).filter(
                QualityFixProposal.pattern_id == pattern.id,
                QualityFixProposal.status.in_(("draft", "validating", "ready", "approved")),
            ).first()
            if existing:
                continue
            proposal_type, title, config = _proposal_for(category, context_key)
            # A generic code hypothesis has no valid ablation variable. Keep the
            # correlated pattern, but never create an empty experiment.
            if not config or not proposals_enabled:
                continue
            try:
                config = validate_proposal_config(config)
            except ValueError:
                continue
            proposal_id = str(uuid.uuid4())
            db.add(QualityFixProposal(
                id=proposal_id, pattern_id=pattern.id,
                proposal_type=proposal_type, title=title,
                hypothesis=(
                    f"El factor desidentificado {context_key} está asociado con "
                    f"{category}; requiere ablation de una variable para confirmar causa."
                ),
                status="draft", candidate_config=config,
                expected_impact={
                    "target_category": category,
                    "minimum_relative_reduction": 0.20,
                    "observed_impact_seconds": round(impact, 3),
                    "render": False,
                },
                created_at=moment, updated_at=moment,
            ))
            db.add(AuditLog(
                user_id=None,
                action="quality_learning.proposal.created",
                detail={
                    "proposal_id": proposal_id,
                    "pattern_id": pattern.id,
                    "candidate_config_sha256": sha256_json(config),
                },
            ))
            proposals += 1
    db.flush()
    stale_ids = [
        pattern.id for pattern in db.query(QualityPattern).filter(
            QualityPattern.status == "stale",
        ).all()
    ]
    if stale_ids and proposals_enabled:
        for proposal in db.query(QualityFixProposal).filter(
            QualityFixProposal.pattern_id.in_(stale_ids),
            QualityFixProposal.status.in_(("draft", "validating", "ready")),
        ).all():
            previous_status = proposal.status
            proposal.status = "superseded"
            proposal.updated_at = moment
            db.add(AuditLog(
                user_id=None,
                action="quality_learning.proposal.transition",
                detail={
                    "proposal_id": proposal.id,
                    "from": previous_status,
                    "to": "superseded",
                },
            ))
    db.flush()
    return {"trusted": len(rows), "qualified": qualified, "proposals": proposals}


def model_readiness(db: Any) -> dict:
    from database import CorrectionObservation
    try:
        hmac_key_id = current_hmac_key_id()
    except RuntimeError as exc:
        return {
            "trusted_observations": 0, "minimum_observations": 500,
            "positive_counts": {}, "minimum_positive_per_category": 100,
            "ready_categories": [], "eligible": False,
            "mode": "shadow_only", "configuration_error": str(exc),
        }
    rows = db.query(CorrectionObservation).filter(
        CorrectionObservation.label_tier == "trusted",
        CorrectionObservation.invalidated_at.is_(None),
        CorrectionObservation.hmac_key_id == hmac_key_id,
    ).all()
    positives = Counter(
        key for row in rows for key, value in (row.categories or {}).items() if value
    )
    eligible = len(rows) >= 500
    ready_categories = sorted(key for key, count in positives.items() if count >= 100)
    return {
        "trusted_observations": len(rows), "minimum_observations": 500,
        "positive_counts": dict(sorted(positives.items())),
        "minimum_positive_per_category": 100,
        "ready_categories": ready_categories,
        "eligible": bool(eligible and ready_categories),
        # Training remains a separate signed, shadow-only operation.
        "mode": "shadow_only",
    }


def public_observation_summary(rows: list[Any]) -> dict:
    """Aggregate only; deliberately omits IDs, hashes, tenant and job data."""
    tiers = Counter(row.label_tier for row in rows)
    categories = Counter(
        key for row in rows for key, value in (row.categories or {}).items() if value
    )
    def minutes(rows_for_metric: list[Any], q: float) -> float | None:
        values = sorted(
            row.active_edit_ms for row in rows_for_metric
            if row.active_edit_ms is not None
            and (row.metrics or {}).get("operator_time_source")
            == "server_product_events_v1"
        )
        if not values:
            return None
        return round(values[max(0, math.ceil(q * len(values)) - 1)] / 60000, 3)
    def grouped(field: str) -> dict:
        buckets: dict[str, list[Any]] = defaultdict(list)
        for row in rows:
            buckets[str(getattr(row, field, None) or "unknown")[:64]].append(row)
        return {
            key: {
                "observations": len(group),
                "corrections": sum(
                    int(value) for row in group
                    for value in (row.categories or {}).values()
                    if isinstance(value, (int, float))
                ),
                "operator_minutes_p50": minutes(group, 0.5),
                "operator_minutes_p90": minutes(group, 0.9),
            }
            for key, group in sorted(buckets.items())
        }
    by_category = {}
    for category in sorted(categories):
        group = [row for row in rows if (row.categories or {}).get(category)]
        by_category[category] = {
            "observations": len(group),
            "corrections": int(categories[category]),
            "operator_minutes_p50": minutes(group, 0.5),
            "operator_minutes_p90": minutes(group, 0.9),
        }
    return {
        "total": len(rows), "tiers": dict(tiers),
        "categories": dict(categories),
        "operator_minutes": {"p50": minutes(rows, 0.5), "p90": minutes(rows, 0.9)},
        "by_release": grouped("pipeline_release"),
        "by_route": grouped("pipeline_route"),
        "by_timing_source": grouped("timing_source"),
        "by_category": by_category,
    }
