"""Bounded, read-only listening pilot using Genly's existing audio adapters.

First listen blindly; then change the acoustic context around discrepancies.
Provider clocks remain hypotheses. A proposal never authorizes a product edit.
No database, publication, credentials export or remote media fetching here.
"""
from dataclasses import replace
from pathlib import Path
import math

from full_audio_review import binding, compare_window, missing_intervals, usable_response
from reviewer_integral import windows, union_seconds
from reviewer_shadow import ShadowPolicy, tokens
from reviewer_shadow_audio import BlindAudioTools, extract_clip, file_sha, probe
from shadow_reference_import import digest

VERSION = "active-audio-review-v1"
PROVIDERS = ("google", "openai")


def validate(snapshot, duration):
    if not math.isfinite(duration) or duration <= 0:
        raise ValueError("invalid_audio_duration")
    for field in ("revision", "audio_revision"):
        if type(snapshot.get(field)) is not int or snapshot[field] < 0:
            raise ValueError("missing_source_revision")
    if not isinstance(snapshot.get("segments"), list):
        raise ValueError("missing_segments")
    for line in snapshot["segments"]:
        if not isinstance(line.get("text"), str):
            raise ValueError("invalid_segment_text")
        a, b = line.get("start"), line.get("end")
        if not all(type(v) in (int, float) and math.isfinite(v) for v in (a, b)):
            raise ValueError("invalid_segment_clock")
        if not 0 <= a < b <= duration + .05:
            raise ValueError("segment_outside_audio")


def focus_window(line, duration):
    """Fit an entire phrase with context; never truncate a long phrase silently."""
    if line["end"] - line["start"] > 20:
        return None
    start, end = max(0., line["start"] - 2.), min(duration, line["end"] + 2.)
    return {"start": start, "end": end}


def heard_line(response, provider, window, line):
    payload = response["response"]
    events = payload.get("events", []) if provider == "google" else payload.get("words", [])
    selected = [e for e in events if e["start"] + window["start"] < line["end"]
                and e["end"] + window["start"] > line["start"]]
    text = " ".join(e.get("text", e.get("word", "")) for e in selected).strip()
    return {"text": text, "ambiguous": bool(payload.get("editorial_ambiguity")),
            "boundary_crossing": any(e["start"] + window["start"] < line["start"] - .5
                or e["end"] + window["start"] > line["end"] + .75 for e in selected)}


def text_decision(line, observations, required_providers=PROVIDERS):
    """Require stable blind evidence in TWO contexts from BOTH model families.

    Agreement is a candidate for human review, not calibrated correctness.
    Reusing the same clip/cached observation is never a second context.
    """
    by_provider = {}
    for provider in required_providers:
        rows = [r for r in observations if r["provider"] == provider and r["valid"]]
        baseline = [r for r in rows if r["phase"] == "baseline"]
        focused = [r for r in rows if r["phase"] == "focused"]
        if not baseline or not focused:
            return {"status": "NEEDS_REVIEW", "reason": "missing_independent_contexts"}
        if not any(a["clip_sha256"] != b["clip_sha256"] for a in baseline for b in focused):
            return {"status": "NEEDS_REVIEW", "reason": "same_audio_context"}
        readings = [r["heard"] for r in rows]
        if any(r["ambiguous"] or r["boundary_crossing"] for r in readings):
            return {"status": "NEEDS_REVIEW", "reason": "ambiguous_or_crossing_phrase"}
        hypotheses = {tuple(tokens(r["text"])) for r in readings}
        if len(hypotheses) != 1 or not next(iter(hypotheses)):
            return {"status": "NEEDS_REVIEW", "reason": "unstable_acoustic_reading"}
        by_provider[provider] = readings[0]["text"]
    if len(by_provider) < 2:
        return {"status": "NEEDS_REVIEW", "reason": "insufficient_model_families"}
    if len({tuple(tokens(t)) for t in by_provider.values()}) != 1:
        return {"status": "NEEDS_REVIEW", "reason": "providers_disagree"}
    proposed = next(iter(by_provider.values()))
    if tokens(proposed) == tokens(line["text"]):
        return {"status": "TEXT_SUPPORTED", "reason": "stable_blind_agreement"}
    return {"status": "TEXT_PROPOSAL", "reason": "stable_blind_agreement",
            "proposed_text": proposed, "requires_realignment": True}


def review(snapshot, audio, cache_dir, *, providers=PROVIDERS, max_calls=48,
           max_focus_lines=6, listener_factory=BlindAudioTools, acoustic_timing=False):
    if not providers or len(set(providers)) != len(providers) or set(providers) - set(PROVIDERS):
        raise ValueError("invalid_providers")
    if type(max_calls) is not int or not 1 <= max_calls <= 128:
        raise ValueError("invalid_call_budget")
    if type(max_focus_lines) is not int or not 0 <= max_focus_lines <= 20:
        raise ValueError("invalid_focus_budget")
    audio = Path(audio)
    if file_sha(audio) != snapshot["audio_sha256"]:
        raise ValueError("audio_identity_changed")
    duration = probe(audio)
    validate(snapshot, duration)
    source = {"audio_sha256": snapshot["audio_sha256"],
              "tenant": digest(snapshot.get("tenant_id", "unknown")), "audit_version": VERSION}
    root = Path(cache_dir) / source["tenant"] / source["audio_sha256"] / VERSION
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    # Keep the validated full-review provider protocol/schema, including 24s cap.
    policy = replace(ShadowPolicy(), version="full-audio-review-v1", max_calls_per_song=max_calls)
    listener = listener_factory(root / "requests", policy=policy)
    report = {"schema_version": VERSION, "job_id": snapshot.get("job_id"),
              "binding": binding(snapshot), "duration": duration, "observations": [],
              "issues": [], "lines": [], "coverage": {}, "max_calls": max_calls,
              "providers_requested": list(providers),
              "automatic_apply_allowed": False, "timing_verified": False,
              "observed_cost_usd": None, "precision_measured": False}
    successful = {p: [] for p in providers}
    memo = {}
    unavailable = {}
    attempted = 0

    def listen(window, provider, phase):
        nonlocal attempted
        identity = digest([provider, window])
        if identity in memo:
            return memo[identity]  # No relabeling an old baseline as a new listen.
        row = {"provider": provider, "phase": phase, "window": window,
               "valid": False, "status": "budget_exhausted"}
        if provider in unavailable:
            return {**row, "status": "provider_unavailable", "reason": unavailable[provider]}
        if attempted >= max_calls:
            return row
        attempted += 1  # Includes failed/cached attempts: bounds work on reruns too.
        clip = root / (digest(window) + ".wav")
        try:
            if not clip.exists():
                extract_clip(audio, window, clip)
            row["clip_sha256"] = file_sha(clip)
            response = listener.listen(clip, provider=provider, view="mix", source=source, window=window)
            row.update(status=response.get("tool_status", "failed"),
                       valid=usable_response(response, provider, window["end"] - window["start"]),
                       cache_hit=response.get("cache_hit", False), usage=response.get("usage"),
                       received_audio=response.get("received_audio", False),
                       conditioning_texts=response.get("conditioning_texts"),
                       model_version=response.get("model_version", response.get("model")),
                       request_id=response.get("request_id"),
                       http_status=response.get("http_status"),
                       provider_error_code=response.get("provider_error_code"),
                       response=response.get("response", {}))
            if response.get("http_status") in (401, 403, 429):
                unavailable[provider] = response["http_status"]
            if row["valid"]:
                successful[provider].append([window["start"], window["end"]])
                row["issues"] = compare_window(window, response, snapshot["segments"], provider)
                report["issues"].extend(row["issues"])
            elif row["status"] == "ok":
                row["status"] = "invalid_response"
        except Exception as exc:
            row.update(status="failed", error_type=type(exc).__name__)
        memo[identity] = row
        report["observations"].append(row)
        return row

    # Reserve a small, explicit part of the budget for adaptive revisits.
    reserve = min(max_calls // 3, max_focus_lines * len(providers))
    baseline_limit = max_calls - reserve
    for window in windows(duration):
        available = [p for p in providers if p not in unavailable]
        if not available or attempted + len(available) > baseline_limit:
            break
        for provider in available:
            listen(window, provider, "baseline")

    candidates = []
    for index, line in enumerate(snapshot["segments"]):
        related = [i for i in report["issues"] if i["start"] < line["end"] and i["end"] > line["start"]]
        if related:
            candidates.append((index, line, sorted({i["code"] for i in related})))
    for index, line, reasons in candidates[:max_focus_lines]:
        window = focus_window(line, duration)
        if window:
            for provider in providers:
                listen(window, provider, "focused")
        evidence = []
        for row in report["observations"]:
            if row["valid"] and row["window"]["start"] <= line["start"] < line["end"] <= row["window"]["end"]:
                evidence.append({**row, "heard": heard_line(row, row["provider"], row["window"], line)})
        decision = text_decision(line, evidence)
        report["lines"].append({"index": index, "before": dict(line), "reasons": reasons,
                                "text": decision, "timing_status": "NEEDS_ACOUSTIC_VERIFICATION",
                                "focused_window": window, "automatic_apply_allowed": False})
        if acoustic_timing:
            from active_timing_review import investigate
            report["lines"][-1]["acoustic_timing"] = investigate(
                audio, snapshot["segments"], index, duration, text_decision=decision)
    for provider, intervals in successful.items():
        report["coverage"][provider] = {"seconds": union_seconds(intervals),
            "fraction": min(1., union_seconds(intervals) / duration),
            "missing": missing_intervals(duration, intervals)}
    report.update(calls=listener.calls, attempts=attempted,
                  unavailable_providers=unavailable,
                  focus_candidates=len(candidates), focus_deferred=max(0, len(candidates) - max_focus_lines))
    report["status"] = ("COMPLETE" if all(not c["missing"] for c in report["coverage"].values())
                        else "PARTIAL" if any(successful.values()) else "NOT_RUN")
    return report
