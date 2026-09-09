"""One final, window-only extension of the first-ten short-gap recovery.

No timing rule/model/prompt/selector changes. At most four <=6 s uncovered gaps
per song, each with <=0.5 s context per side. Known invalid responses only;
Google unknown/tool-error overlap is never repurchased through a shorter window.
OpenAI uncertainty remains reserved but does not veto a different provider.
Default is plan-only. Root campaign owner is the sole authorized executor.
"""
import argparse
from dataclasses import replace
import json
import math
from pathlib import Path

from reviewer_acoustic_cache import cached_receipts, request_index
from reviewer_campaign import SpendLedger, atomic_json, owner_lock
from reviewer_shadow import ShadowPolicy, source_binding
from reviewer_shadow_audio import BlindAudioTools, PROMPT_VERSION, extract_clip
from scripts.run_reviewer_campaign import authorization, covered, verify_audio
from scripts.watch_reviewer_campaign_budget import project
from shadow_reference_import import digest

FAMILY = "google/gemini-2.5-flash-audio"
VERSION = "campaign-final-short-gap-round-1"
V2 = "blind-vocal-events-shadow-v2-bounded-schema"


def audio_identity(song):
    return {k: song[k] for k in ("job_id", "audio_sha256", "audio_revision")}


def overlaps(a, b):
    return max(a["start"], b["start"]) < min(a["end"], b["end"]) - 1e-6


def uncertain_google_or_unattributed(request):
    # Missing/unsupported attribution stays conservative. Only an explicitly
    # attributable OpenAI call is outside this Google-only no-repurchase gate.
    return (request.get("provider") != "openai"
        and request.get("tool_status") in {"unknown_completion", "tool_error", "reserved_unknown_completion"})


def uncovered(duration, receipts):
    cursor, gaps = 0., []
    for row in sorted((r for r in receipts if r["family"] == FAMILY), key=lambda r: r["start"]):
        if row["start"] > cursor + 1e-6:
            gaps.append({"start": cursor, "end": row["start"]})
        cursor = max(cursor, row["end"])
    if cursor < duration - 1e-6:
        gaps.append({"start": cursor, "end": duration})
    return gaps


def plan_final_gaps(song, index, *, subdivided=(), unknown_windows=()):
    duration = song["duration_seconds"]
    if not isinstance(duration, (int, float)) or not math.isfinite(duration) or duration <= 0:
        raise ValueError("invalid_song_duration")
    receipts = cached_receipts(song, index=index)["receipts"]
    gaps = uncovered(duration, receipts)
    result = {"schema": VERSION, "audio": audio_identity(song), "source": source_binding(song),
        "gaps": gaps, "clips": [], "blockers": [], "maximum_lifetime_calls": 4,
        "only_changed_component": "window_duration", "automatic_apply_allowed": False}
    if (sum(math.ceil((w["end"] - w["start"] - 1e-6) / 6) for w in gaps) > 4
            or sum(w["end"] - w["start"] for w in gaps) > 24 + 1e-6):
        result["blockers"].append("final_round_gap_count_or_total_exceeded")
        return result
    records = [entry for entry in index if all(entry.get("request", {}).get("source", {}).get(k) == v
        for k, v in audio_identity(song).items())]
    chunks = []
    for gap in gaps:
        start = gap["start"]
        while start < gap["end"] - 1e-6:
            end = min(start + 6., gap["end"])
            chunks.append({"start": start, "end": end}); start = end
    for gap in chunks:
        window = {"start": max(0., gap["start"] - .5), "end": min(duration, gap["end"] + .5)}
        window["offset_seconds"] = window["start"]
        unsafe = [entry for entry in records if entry["request"].get("view") == "mix"
            and uncertain_google_or_unattributed(entry["request"])
            and overlaps(entry["request"].get("window", {"start": 0., "end": duration}), window)]
        if unsafe or any(overlaps(w, window) for w in unknown_windows):
            result["blockers"].append("unknown_or_tool_error_overlaps_final_clip")
            continue
        parents = []
        for entry in records:
            failed = entry["request"]; parent = failed.get("window", {})
            if (failed.get("provider") != "google" or failed.get("model") != "gemini-2.5-flash"
                or failed.get("family") != FAMILY or failed.get("view") != "mix"
                or failed.get("tool_status") != "invalid_response" or failed.get("received_audio") is not True
                or failed.get("conditioning_texts") != [] or failed.get("prompt_version") != V2
                or not isinstance(failed.get("clip_sha256"), str) or len(failed["clip_sha256"]) != 64
                or not 0 <= parent.get("start", -1) < parent.get("end", 0) <= duration + .001
                or parent["end"] - parent["start"] > 24.001
                or parent.get("offset_seconds") != parent["start"]
                or not parent.get("start", duration) <= gap["start"] < gap["end"] <= parent.get("end", 0)):
                continue
            short = parent["end"] - parent["start"] <= 18
            if short or digest(failed) in subdivided:
                parents.append(entry)
        if not parents:
            result["blockers"].append("known_failed_v2_short_or_subdivided_parent_required")
            continue
        parent = min(parents, key=lambda e: e["request"]["window"]["end"] - e["request"]["window"]["start"])
        identity = digest({"audio": audio_identity(song), "window": window, "final_round": 1})
        result["clips"].append({"identity": identity, "gap": gap, "window": window,
            "failed_evidence_sha256": parent["evidence_sha256"], "status": "planned"})
    return result


def reservation_windows(ledger, manifest, index, song, existing_plan=None, other_final_plans=()):
    """Map durable unknown reservations, including crash-before-attempt-file.

    An unmapped reservation cannot be proven disjoint, so fails closed. This
    deliberately does not infer that an absent request file means no charge.
    """
    known = {}
    for row in manifest["songs"]:
        audio = {k: row["source"][k] for k in ("job_id", "audio_sha256", "audio_revision")}
        for window in row.get("windows", []):
            for provider in ("google", "openai"):
                identity = digest({"audio": audio, "window": window, "provider": provider,
                    "method": manifest["method_sha256"]})
                known[identity] = known[digest({"retry_of": identity, "retry_number": 1})] = (audio, window, provider)
                if provider == "openai":
                    known[digest({"quota_retry_of": identity, "quota_retry_number": 1})] = (audio, window, provider)
    for entry in index:
        failed = entry.get("request", {}); parent = failed.get("window", {})
        if failed.get("tool_status") != "invalid_response" or not 18 < parent.get("end", 0) - parent.get("start", 0) <= 24:
            continue
        mid = (parent["start"] + parent["end"]) / 2
        for start, end in [(parent["start"], mid + 1), (mid - 1, parent["end"])]:
            window = {"start": start, "end": end, "offset_seconds": start}
            identity = digest({"recovery_of": digest(failed), "window": window, "attempt": 1})
            known[identity] = (failed.get("source", {}), window, failed.get("provider"))
    for final_plan in [existing_plan or {}, *other_final_plans]:
        for clip in final_plan.get("clips", []):
            known[clip["identity"]] = (final_plan["audio"], clip["window"], "google")
    unknown = []
    for identity, status in ledger.db.execute("SELECT id,status FROM attempts WHERE status IN ('reserved_unknown_completion','unknown_completion','tool_error')"):
        match = known.get(identity)
        if match is None:
            raise ValueError("unattributed_unknown_reservation_blocks_final_round")
        source, window, provider = match
        if provider not in {"google", "openai"}:
            raise ValueError("unattributed_unknown_reservation_blocks_final_round")
        if provider == "google" and all(source.get(k) == v for k, v in audio_identity(song).items()):
            unknown.append(window)
    return unknown


def plan_identity(plan):
    return digest({"audio": plan["audio"], "clips": [
        {k: clip[k] for k in ("identity", "gap", "window", "failed_evidence_sha256")}
        for clip in plan["clips"]]})


def recover(root, snapshot, job_id, *, execute=False, authorization_path=None):
    root = Path(root); folder = root / "campaign-300"
    with owner_lock(folder):
        manifest = json.loads((folder / "manifest.json").read_text())
        auth = authorization(authorization_path or folder / "authorization.json", manifest)
        if auth["approved_usd"] != 20:
            raise ValueError("final_round_requires_exact_authorized_usd20")
        if PROMPT_VERSION != V2:
            raise ValueError("frozen_v2_prompt_required")
        song = next(s for s in snapshot["jobs"] if s["job_id"] == job_id)
        row = next(s for s in manifest["songs"] if s["job_id"] == job_id)
        if source_binding(song) != row["source"]:
            raise ValueError("manifest_source_mismatch")
        audio = root / "audio" / f"{job_id}-mix.wav"
        verify_audio(audio, song)
        ledger = SpendLedger(folder / "spend.sqlite", approved_usd=20, max_attempts=auth["max_attempts"])
        try:
            out = folder / job_id / "final-gap-round-1"
            state_path = out / "round.json"
            previous = json.loads(state_path.read_text()) if state_path.exists() else None
            if previous and (previous.get("audio") != audio_identity(song) or previous.get("schema") != VERSION):
                raise ValueError("one_final_round_lifetime_identity_mismatch")
            if previous and previous.get("plan_identity_sha256") != plan_identity(previous):
                raise ValueError("immutable_final_round_plan_mismatch")
            index = request_index(root, max_files=25000)
            other_final_plans = [json.loads(path.read_text()) for path in folder.glob("*/final-gap-round-1/round.json")
                                 if path != state_path]
            subdivided = []
            for path in (folder / job_id / "bounded-recovery").glob("*/report.json"):
                report = json.loads(path.read_text())
                if report.get("job_id") == job_id and report.get("subdivision_rounds") == 1:
                    subdivided.append(report.get("failed_evidence_sha256"))
            unknown = reservation_windows(ledger, manifest, index, song, previous, other_final_plans)
            plan = plan_final_gaps(song, index, subdivided=subdivided, unknown_windows=unknown)
            if not execute:
                return {**plan, "executed": False, "spend": ledger.totals()}
            if previous is None:
                previous = plan
                previous["plan_identity_sha256"] = plan_identity(previous)
                atomic_json(state_path, previous)
            if len(previous.get("clips", [])) > 4:
                raise ValueError("final_round_lifetime_call_cap")
            for clip in previous["clips"]:
                if clip["status"] != "planned":
                    continue
                receipts = cached_receipts(song, index=index)["receipts"]
                if covered(receipts, FAMILY, clip["gap"]):
                    clip["status"] = "covered_by_existing_evidence"; atomic_json(state_path, previous); continue
                unknown = reservation_windows(ledger, manifest, index, song, previous, other_final_plans)
                # Keep the frozen window identity after earlier successful
                # chunks change the coverage edge by their 0.5 s context.
                plan = plan_final_gaps(song, index, subdivided=subdivided, unknown_windows=unknown)
                previous["current_blockers"] = plan["blockers"]
                blocked_overlap = any(overlaps(w, clip["window"]) for w in unknown) or any(
                    uncertain_google_or_unattributed(entry.get("request", {}))
                    and all(entry.get("request", {}).get("source", {}).get(k) == v for k, v in audio_identity(song).items())
                    and overlaps(entry["request"].get("window", {"start": 0., "end": song["duration_seconds"]}), clip["window"])
                    for entry in index)
                if plan["blockers"] or blocked_overlap:
                    clip["blocker"] = "final_round_current_eligibility_failed"; continue
                projection = project(manifest, index, ledger.totals(), approved_usd=20)
                if projection["exceeds_budget"]:
                    ledger.hold_after_attempts(ledger.totals()["attempts"])
                    previous["blockers"] = ["projected_budget_exceeded"]; break
                # Own per-clip request folder. Persistent marker before reserve
                # makes even a crash-before-provider an explicit no-repeat.
                clip_out = out / clip["identity"]; clip_out.mkdir(parents=True, mode=0o700, exist_ok=True)
                requests = clip_out / "requests"
                if requests.exists() and any(requests.glob("*.json")):
                    clip["status"] = "prior_attempt_not_repeated"; atomic_json(state_path, previous); continue
                clip["status"] = "reserved_or_unknown"; atomic_json(state_path, previous)
                reserved, reason = ledger.reserve(clip["identity"], "google", clip["window"]["end"] - clip["window"]["start"])
                if not reserved:
                    clip.update(status="not_repeated", reason=reason); atomic_json(state_path, previous); continue
                wav = clip_out / "mix.wav"
                try:
                    extract_clip(audio, clip["window"], wav)
                    listener = BlindAudioTools(requests, policy=replace(ShadowPolicy(), max_calls_per_song=1))
                    result = listener.listen(wav, provider="google", view="mix", source=source_binding(song), window=clip["window"])
                    ledger.finish(clip["identity"], result["tool_status"], requests, request=result)
                    clip.update(status=result["tool_status"], response_sha256=digest(result),
                        calls_this_run=result.get("calls_this_run", 0),
                        latency_seconds=result.get("latency_seconds"), error_type=result.get("error_type"))
                except Exception as exc:
                    clip.update(status="unknown_completion", error_type=type(exc).__name__)
                    atomic_json(clip_out / "failure.json", {"identity": clip["identity"], "window": clip["window"],
                        "source": source_binding(song), "status": "unknown_completion", "error_type": type(exc).__name__})
                atomic_json(state_path, previous)
                index = request_index(root, max_files=25000)
            previous["spend"] = ledger.totals()
            previous["final_round_receipt"] = {"calls_lifetime_upper_bound": sum(c["status"] not in {"planned", "covered_by_existing_evidence"} for c in previous["clips"]),
                "provider_calls_reported": sum(c.get("calls_this_run", 0) for c in previous["clips"]),
                "documents_modified": False, "candidates_modified": False, "publication_modified": False}
            atomic_json(state_path, previous)
            return previous
        finally:
            ledger.db.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--snapshot", type=Path, required=True)
    parser.add_argument("--job", required=True)
    parser.add_argument("--authorization", type=Path)
    parser.add_argument("--execute", action="store_true")
    args = parser.parse_args()
    print(json.dumps(recover(args.root, json.loads(args.snapshot.read_text()), args.job,
        execute=args.execute, authorization_path=args.authorization)))
