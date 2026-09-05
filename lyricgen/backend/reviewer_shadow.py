"""Isolated reviewer experiment: pure planning, evidence selection and trace DTOs.

No database imports, operator publication, auto-apply, training or render calls.
Audio adapters must supply provenance; missing conditioning is NOT independence.
"""
from __future__ import annotations

from collections import Counter
from copy import deepcopy
from dataclasses import asdict, dataclass
from difflib import SequenceMatcher
import math
import random
import re
import unicodedata

from shadow_reference_import import digest, key


@dataclass(frozen=True)
class ShadowPolicy:
    version: str = "reviewer-shadow-v1"
    context_seconds: float = 2.0
    max_clip_seconds: float = 24.0
    max_windows_per_song: int = 4
    max_calls_per_song: int = 16
    retries: int = 0
    min_independent_audio_families: int = 2
    min_timing_change_seconds: float = 0.15
    no_overlap: bool = True
    automatic_apply_allowed: bool = False


def tokens(text):
    # Unlike metadata matching, accents and lexical differences survive.
    return re.findall(r"[^\W_]+(?:['’][^\W_]+)?", unicodedata.normalize("NFC", str(text)).casefold())


def sequence_discrepancies(segments, reference):
    current, owners = [], []
    for index, segment in enumerate(segments):
        words = tokens(segment.get("text", ""))
        current.extend(words)
        owners.extend([index] * len(words))
    candidate = tokens(reference)
    result = []
    for tag, a, b, c, d in SequenceMatcher(None, current, candidate, autojunk=False).get_opcodes():
        if tag == "equal":
            continue
        affected = sorted(set(owners[a:b]))
        if not affected and owners:
            affected = [owners[min(a, len(owners) - 1)]]
        result.append({"operation": tag, "baseline_token_range": [a, b],
                       "reference_token_range": [c, d], "line_indices": affected,
                       "current_tokens": current[a:b], "reference_tokens": candidate[c:d],
                       "classification": "insufficient_evidence"})
    if not result and "\n".join(s.get("text", "") for s in segments) != reference:
        result.append({"operation": "format", "line_indices": [],
                       "classification": "formatting_only", "timing_verified": False})
    return result


def validate_snapshot(song):
    if digest(song["segments"]) != song["segments_sha256"]:
        raise ValueError("segments_snapshot_hash_mismatch")
    if not re.fullmatch(r"[a-f0-9]{64}", song.get("audio_sha256", "")):
        raise ValueError("audio_identity_missing")
    if not isinstance(song.get("segments_revision"), int):
        raise ValueError("segments_revision_missing")


def source_binding(song):
    return {name: song[name] for name in ("job_id", "audio_sha256", "audio_revision",
                                         "segments_revision", "segments_sha256")}


def assert_current(proposal, song):
    validate_snapshot(song)
    if proposal["source"] != source_binding(song):
        raise ValueError("stale_proposal")


def local_to_global(start, end, *, offset, clip_duration, song_duration):
    values = (start, end, offset, clip_duration, song_duration)
    if any(not isinstance(x, (int, float)) or not math.isfinite(x) for x in values):
        raise ValueError("invalid_clock")
    if not (0 <= start < end <= clip_duration and 0 <= offset and offset + end <= song_duration + 1e-6):
        raise ValueError("invalid_clock")
    return round(offset + start, 6), round(offset + end, 6)


def plan_windows(song, policy=ShadowPolicy(), seed=20260905):
    """Same frozen windows for paired arms. Random/control-candidate + difficult.

    'Control candidate' is not certified correct until blind human annotation.
    Never consult score/traffic-light; structural features only select candidates.
    """
    validate_snapshot(song)
    segments = song["segments"]
    if not segments:
        return []
    rng = random.Random(f"{seed}:{song['job_id']}")
    selected = [(rng.randrange(len(segments)), "random")]
    if len(segments) > 1:
        selected.append((len(segments) - 1, "last_line"))
    by_gap = sorted(range(len(segments) - 1), key=lambda i:
                    float(segments[i + 1]["start"]) - float(segments[i]["end"]))
    if by_gap:
        selected += [(by_gap[0], "chained_candidate"), (by_gap[-1], "clear_pause_candidate")]
    out, seen = [], set()
    duration = float(song["duration_seconds"])
    for index, role in selected:
        if index in seen or len(out) >= policy.max_windows_per_song:
            continue
        seen.add(index)
        segment = segments[index]
        start = max(0.0, float(segment["start"]) - policy.context_seconds)
        end = min(duration, float(segment["end"]) + policy.context_seconds)
        if end - start > policy.max_clip_seconds:
            start = end - policy.max_clip_seconds
        if end <= start:
            continue
        line_id = str(segment.get("segment_id") or segment.get("id") or segment.get("_id") or f"index:{index}")
        out.append({"line_index": index, "line_id": line_id,
                    "occurrence_id": f"{song['job_id']}:{index}", "sample_role": role,
                    "start": round(start, 6), "end": round(end, 6),
                    "offset_seconds": round(start, 6), "full_line_in_clip": start <= float(segment["start"]),
                    "gold_status": "unannotated", "double_review_required": len(out) == 0,
                    "annotation_mode": "blind" if len(out) == 0 else "anchored"})
    return out


def freeze_sample(jobs, *, count=24, seed=20260905, base_commit):
    """Group connected artist, recording and song identities BEFORE selecting/splitting."""
    if count < 3:
        raise ValueError("sample too small")
    parents = {j["job_id"]: j["job_id"] for j in jobs}
    def find(i):
        while parents[i] != i:
            parents[i] = parents[parents[i]]
            i = parents[i]
        return i
    seen = {}
    for job in jobs:
        identities = ["artist:" + key(job["artist"]), "audio:" + job["audio_sha256"],
                      "song:" + key(job["artist"]) + ":" + key(job["title"])]
        if job.get("recording_group_id"):
            identities.append("recording:" + job["recording_group_id"])
        for identity in identities:
            if identity in seen:
                parents[find(job["job_id"])] = find(seen[identity])
            seen[identity] = job["job_id"]
    group_ids = sorted({find(j["job_id"]) for j in jobs})
    rng = random.Random(seed)
    rng.shuffle(group_ids)
    split = {g: "train" if i % 4 < 2 else "calibration" if i % 4 == 2 else "test"
             for i, g in enumerate(group_ids)}
    order = sorted(jobs, key=lambda j: j["job_id"])
    rng.shuffle(order)
    chosen = order[:max(0, count - 4)]
    chosen_ids = {j["job_id"] for j in chosen}
    predicates = [lambda j: "vivo" in key(j["title"]) or "live" in key(j["title"]),
                  lambda j: not (j.get("reference_hypothesis") or {}).get("reference_text"),
                  lambda j: any(s.get("locked") or s.get("operator_locked") for s in j["segments"]),
                  lambda j: not j["segments"]]
    for predicate in predicates:
        candidate = next((j for j in order if j["job_id"] not in chosen_ids and predicate(j)), None)
        if candidate:
            chosen.append(candidate)
            chosen_ids.add(candidate["job_id"])
    for job in order:
        if len(chosen) >= count:
            break
        if job["job_id"] not in chosen_ids:
            chosen.append(job)
            chosen_ids.add(job["job_id"])
    entries = []
    for i, job in enumerate(chosen):
        group = find(job["job_id"])
        entries.append({**source_binding(job), "artist": job["artist"], "title": job["title"],
                        "artist_group_id": key(job["artist"]), "recording_group_id": group,
                        "song_group_id": key(job["artist"]) + ":" + key(job["title"]),
                        "split": split[group], "selection": "random" if i < count - 4 else "difficult",
                        "windows": plan_windows(job, seed=seed)})
    result = {"schema": "reviewer-shadow-sample-v1", "seed": seed, "base_commit": base_commit,
              "frozen_before_evaluation": True, "used_traffic_light": False,
              "groups_require_human_version_audit": True, "songs": entries}
    result["manifest_sha256"] = digest(result)
    return result


def _family(value):
    # Same-family prompts/views/versions cannot manufacture witnesses.
    text = value.casefold()
    if "gemini" in text:
        return "google_gemini"
    if "whisper" in text or "gpt-4o-transcribe" in text:
        return "openai_asr"
    return text


def select_content(current, candidates, witnesses, *, minimum_families=2):
    """Select only whole occurrence text independently heard in this exact window."""
    accepted = []
    for candidate in candidates:
        if tokens(candidate) == tokens(current):
            continue
        support = {_family(w["family"]) for w in witnesses
                   if w.get("tool_status") == "ok" and w.get("received_audio") is True
                   and w.get("conditioning_texts") == [] and w.get("occurrence_verified") is True
                   and not w.get("editorial_ambiguity")
                   and tokens(w.get("text", "")) == tokens(candidate)}
        if len(support) >= minimum_families:
            accepted.append((candidate, sorted(support)))
    unique = {tuple(tokens(c)): (c, f) for c, f in accepted}
    if len(unique) == 1:
        text, families = next(iter(unique.values()))
        return {"decision": "propose", "text": text, "families": families,
                "classification": "probable_genly_error_audio_supported"}
    return {"decision": "abstain" if len(unique) > 1 or not witnesses else "keep",
            "reason": "conflicting_supported_candidates" if len(unique) > 1 else "no_sufficiently_supported_change",
            "correctness_certified": False}


def select_endpoint(segment, candidates, *, next_start, duration, policy=ShadowPolicy()):
    """Timing candidates MUST carry tool clocks + target-voice/phonetic evidence.

    Energy/pitch alone, text agreement and forced alignment are insufficient.
    Render clipping is evaluated, not hidden behind a no-overlap success.
    """
    if segment.get("locked") or segment.get("operator_locked"):
        return {"decision": "abstain", "reason": "human_locked", "candidates": candidates}
    for candidate in candidates:
        if candidate.get("editorial_ambiguity"):
            return {"decision": "abstain", "reason": "editorial_policy_pending", "candidates": candidates}
    qualified = [c for c in candidates if c.get("clock_source") == "acoustic_tool"
                 and c.get("target_voice_verified") is True and c.get("phonetic_end_supported") is True
                 and c.get("mix_stem_sync_verified") is True and c.get("tool_status") == "ok"]
    if len(qualified) != 1:
        return {"decision": "abstain", "reason": "insufficient_or_conflicting_endpoint_evidence", "candidates": candidates}
    candidate = qualified[0]
    endpoint = candidate["end_seconds"]
    if not isinstance(endpoint, (float, int)) or not math.isfinite(endpoint) or not float(segment["start"]) < endpoint <= duration:
        return {"decision": "abstain", "reason": "invalid_endpoint", "candidates": candidates}
    rendered = min(endpoint, next_start) if next_start is not None and policy.no_overlap else endpoint
    if rendered < candidate.get("acceptable_earliest_end_seconds", endpoint):
        return {"decision": "abstain", "reason": "render_overlap_policy_conflict", "candidates": candidates}
    if abs(rendered - float(segment["end"])) < policy.min_timing_change_seconds:
        return {"decision": "keep", "reason": "no_material_change", "correctness_certified": False}
    return {"decision": "propose", "end_seconds": rendered, "candidate": candidate,
            "reason": "tool_endpoint_for_shadow_evaluation", "automatic_apply_allowed": False}


def review_window(song, window, *, evidence, external_reference=None, commit, policy=ShadowPolicy()):
    validate_snapshot(song)
    index = window["line_index"]
    current = deepcopy(song["segments"][index])
    errors = [e for e in evidence if e.get("tool_status") not in {"ok", "not_run"}]
    witnesses = [e for e in evidence if e.get("kind") == "content"]
    candidates = [e["text"] for e in witnesses if e.get("text")]
    # External lines are hypotheses, not segmentation or clocks. They may be
    # selected only if independent audio witnesses support this exact occurrence.
    if external_reference:
        candidates += external_reference.splitlines()
    content = select_content(current.get("text", ""), candidates, witnesses,
                             minimum_families=policy.min_independent_audio_families)
    if content["decision"] != "propose" and any(e.get("kind") == "minimal_text_patch_request" for e in evidence):
        from reviewer_text_patch import propose_patches
        patches = propose_patches(current.get("text", ""),
            [e for e in evidence if e.get("kind") == "minimal_text_patch_request"])
        if patches:
            content = patches[0]
    timing = select_endpoint(current, [e for e in evidence if e.get("kind") == "endpoint"],
                             next_start=float(song["segments"][index + 1]["start"]) if index + 1 < len(song["segments"]) else None,
                             duration=float(song["duration_seconds"]), policy=policy)
    result = {"schema": "reviewer-shadow-decision-v1", "source": source_binding(song),
              "commit": commit, "window": window, "current": current,
              "content": content, "timing": timing, "evidence": deepcopy(evidence),
              "tool_errors": errors, "tool_error_count": len(errors),
              "policy": asdict(policy), "arm": "C_excel" if external_reference else "B_audio",
              "external_text_seen_by_selector": bool(external_reference),
              "automatic_apply_allowed": False, "operator_publish_allowed": False,
              "uncertainty": "uncalibrated; not a probability", "live_requires_human_review": True,
              "observed_cost_usd": None, "observed_cost_status": "provider_invoice_not_attached",
              "observed_calls": sum(int(e.get("calls", 0)) for e in evidence),
              "latency_seconds": sum(float(e.get("latency_seconds", 0)) for e in evidence)}
    result["proposal_id"] = digest({"source": result["source"], "window": window,
                                    "policy": asdict(policy), "evidence": evidence,
                                    "external_reference_sha256": digest(external_reference)})
    return result
