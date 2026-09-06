"""Read-only reuse of blind mix listening, never stem clock transfer or certification."""
from __future__ import annotations

import hashlib
import json
import math
from pathlib import Path

from reviewer_shadow import source_binding

PROVIDERS = {
    "openai": ("whisper-1", "openai/whisper-1", "no-prompt-v1"),
    "google": ("gemini-2.5-flash", "google/gemini-2.5-flash-audio", "blind-vocal-events-shadow-v1"),
}


def _number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def _hash(value):
    return isinstance(value, str) and len(value) == 64 and all(c in "0123456789abcdef" for c in value)


def request_index(artifact_root, *, max_files=10000):
    """Small immutable request records only; never recursively parse snapshots/candidates."""
    root = Path(artifact_root).resolve()
    paths = sorted(root.glob("**/requests/*.json"))
    if len(paths) > max_files:
        raise ValueError("acoustic_cache_file_limit")
    records = []
    for path in paths:
        if path.is_symlink() or root not in path.resolve().parents:
            continue
        if path.stat().st_size > 4 * 1024 * 1024:
            continue
        raw = path.read_bytes()
        try:
            request = json.loads(raw)
        except (ValueError, UnicodeError):
            records.append({"cache_path": str(path), "error": "invalid_cache_json"})
            continue
        if not isinstance(request, dict):
            continue
        if path.name.endswith(".attempt.json"):
            result = path.with_name(path.name.replace(".attempt.json", ".json"))
            if result.exists():
                continue
            request = {**request.get("identity", {}), "tool_status": "unknown_completion",
                       "received_audio": False, "conditioning_texts": []}
        records.append({"request": request, "cache_path": str(path),
                        "evidence_sha256": hashlib.sha256(raw).hexdigest()})
    return records


def _reason(request, song):
    original = request.get("source", {})
    if not isinstance(original, dict):
        return "missing_audio_identity"
    if any(original.get(k) != song.get(k) for k in ("job_id", "audio_sha256", "audio_revision")):
        return "audio_identity_mismatch"
    if not _hash(original.get("audio_sha256")) or not _hash(request.get("clip_sha256")):
        return "missing_audio_identity"
    if request.get("view") != "mix":
        return "stem_clock_not_transferred"
    specification = PROVIDERS.get(request.get("provider"))
    if not specification or (request.get("model"), request.get("family"), request.get("prompt_version")) != specification:
        return "unsupported_model_or_blind_prompt"
    if request.get("conditioning_texts") != []:
        return "text_conditioning_unverified"
    window = request.get("window", {})
    if not isinstance(window, dict):
        return "invalid_mix_window"
    start, end, offset = (window.get(k) for k in ("start", "end", "offset_seconds"))
    duration = song.get("duration_seconds")
    if not all(_number(v) for v in (start, end, offset, duration)) or not 0 <= start < end <= duration + .001 or end - start > 24.001 or abs(offset - start) > 1e-6:
        return "invalid_mix_window"
    if request.get("tool_status") != "ok" or request.get("received_audio") is not True:
        return request.get("tool_status") or "audio_not_received"
    response = request.get("response")
    if not isinstance(response, dict):
        return "unusable_audio_response"
    if request["provider"] == "google":
        if not isinstance(response.get("events"), list):
            return "unusable_audio_response"
    elif not isinstance(response.get("words"), list) and not isinstance(response.get("text"), str):
        return "unusable_audio_response"
    return None


def _annotations(request):
    response = request["response"]
    offset = request["window"]["start"]
    duration = request["window"]["end"] - offset
    key = "words" if request["provider"] == "openai" else "events"
    valid, invalid = [], []
    for index, item in enumerate(response.get(key, [])):
        if not isinstance(item, dict):
            invalid.append({"index": index, "reason": "invalid_annotation", "raw": item})
            continue
        start, end = item.get("start"), item.get("end")
        text = item.get("word", item.get("text"))
        if not isinstance(text, str) or not text.strip() or not all(_number(v) for v in (start, end)) or not 0 <= start < end <= duration + .001:
            invalid.append({"index": index, "reason": "invalid_provider_timestamp_or_text", "raw": item})
            continue
        valid.append({**item, "text": text, "local_start": start, "local_end": end,
                      "global_start": start + offset, "global_end": end + offset,
                      "timestamp_status": "provider_hypothesis_not_alignment"})
    return valid, invalid


def cached_receipts(song, artifact_root=None, *, index=None):
    """Return audited receipts plus original requests for line/occurrence reconciliation.

    Document revisions may differ only because these known prompts are blind.
    Audio identity must match exactly; original provenance is always retained.
    Successful listening is coverage, NOT evidence that the transcript is correct.
    """
    indexed = request_index(artifact_root) if index is None else index
    receipts, records, excluded, seen = [], [], [], set()
    binding = source_binding(song)
    for entry in indexed:
        request = entry.get("request", {})
        original = request.get("source", {})
        if not isinstance(original, dict) or original.get("job_id") != song["job_id"]:
            continue
        reason = _reason(request, song)
        if reason:
            excluded.append({**entry, "reason": reason})
            continue
        window = request["window"]
        identity = (request["family"], request["clip_sha256"], window["start"], window["end"])
        if identity in seen:
            continue
        seen.add(identity)
        valid, invalid = _annotations(request)
        provenance = {"original_source": request["source"], "source": binding,
                      "source_rebound": request["source"] != binding,
                      "rebind_basis": "identical_audio_identity_and_known_unconditioned_prompt",
                      "cache_path": entry["cache_path"], "evidence_sha256": entry["evidence_sha256"]}
        receipt_end = min(window["end"], song["duration_seconds"])
        rounding = {"kind": "metadata_duration_rounding_only",
                    "end_delta_seconds": receipt_end - window["end"]} if receipt_end != window["end"] else None
        receipts.append({**provenance, "family": request["family"], "tool_status": "ok",
                         "received_audio": True, "clock": "original_mix_decoded",
                         "start": window["start"], "end": receipt_end,
                         "original_window": dict(window), "clock_adjustment": rounding,
                         "clip_sha256": request["clip_sha256"], "certifies_text": False})
        records.append({**provenance, "request": request, "annotations": valid,
                        "invalid_annotations": invalid, "reused": True,
                        "calls_this_run": 0, "timestamp_status": "provider_hypothesis_not_alignment"})
    return {"receipts": receipts, "records": records, "excluded": excluded,
            "summary": {"reused_requests": len(records), "excluded_requests": len(excluded),
                        "invalid_annotations": sum(len(r["invalid_annotations"]) for r in records),
                        "new_calls": 0}}
