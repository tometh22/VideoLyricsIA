"""Isolated acoustic diagnosis of ordinal 113; never bootstrap a fake baseline.

Execution is opt-in, shares campaign ownership/spend/cache identity, and must
run after the main owner exits. It cannot publish, approve or create live rows.
"""
import argparse
from dataclasses import replace
import json
from pathlib import Path
import time

from reviewer_acoustic_cache import cached_receipts, request_index
from reviewer_campaign import SpendLedger, atomic_json, counters, owner_lock, update_status
from reviewer_integral import union_seconds, windows
from reviewer_shadow import ShadowPolicy, source_binding, tokens
from reviewer_shadow_audio import extract_clip
from scripts.run_reviewer_campaign import (authorization, covered, execute_request_batches,
    previous_unknown, usage_estimate, verify_audio)
from scripts.watch_reviewer_campaign_budget import project
from shadow_reference_import import digest

JOB_ID = "a34129dd111b"
FAMILIES = ("openai/whisper-1", "google/gemini-2.5-flash-audio")


def target_song(snapshot, manifest):
    jobs = snapshot["jobs"]
    if len(jobs) != 300 or len({j["job_id"] for j in jobs}) != 300 or digest(jobs) != snapshot["snapshot_sha256"]:
        raise ValueError("exact_300_snapshot_required")
    if (manifest.get("campaign_id") != snapshot.get("campaign_id")
            or len(manifest.get("songs", [])) != 300
            or {r["job_id"] for r in manifest["songs"]} != {j["job_id"] for j in jobs}):
        raise ValueError("manifest_campaign_roster_mismatch")
    song = next(j for j in jobs if j["job_id"] == JOB_ID)
    rows = [r for r in manifest["songs"] if r["job_id"] == JOB_ID]
    if len(rows) != 1 or rows[0]["source"] != source_binding(song):
        raise ValueError("empty_target_source_mismatch")
    if (song.get("ordinal") != 113 or song.get("segments") != []
            or song.get("original_segments") != [] or song.get("approved_at")
            or song.get("status") in {"lyrics_approved", "done"}):
        raise ValueError("exact_unapproved_empty_target_required")
    if rows[0].get("windows") != windows(float(song["duration_seconds"])) or len(rows[0]["windows"]) != 8:
        raise ValueError("frozen_eight_window_plan_required")
    return song, rows[0]


class NoRetryLedger:
    """Only the 16 canonical identities: no retry identities for this diagnostic."""
    def __init__(self, ledger):
        self.ledger = ledger

    def reserve(self, *args):
        reserved, reason = self.ledger.reserve(*args)
        return (False, "empty_diagnostic_known_invalid_not_retried") if not reserved and reason == "invalid_response" else (reserved, reason)

    def finish(self, *args, **kwargs):
        return self.ledger.finish(*args, **kwargs)

    def totals(self):
        return self.ledger.totals()

    def hold_after_attempts(self, count):
        return self.ledger.hold_after_attempts(count)


def diagnose(song, cached):
    """Sequence/occurrence evidence, not text certification or endpoint proof."""
    events, agreements = [], []
    whispers = [r for r in cached["records"] if r["request"]["provider"] == "openai"]
    for record in cached["records"]:
        if record["request"]["provider"] != "google":
            continue
        for annotation in record["annotations"]:
            phrase = tokens(annotation["text"])
            evidence = {"event": annotation, "evidence_sha256": record["evidence_sha256"],
                        "window": record["request"]["window"], "matches": []}
            for witness in whispers:
                flattened = [(token, word) for word in witness["annotations"] for token in tokens(word["text"])]
                haystack = [token for token, _ in flattened]
                for i in range(len(haystack) - len(phrase) + 1) if phrase else []:
                    if haystack[i:i + len(phrase)] != phrase:
                        continue
                    first, last = flattened[i][1], flattened[i + len(phrase) - 1][1]
                    start, end = first["global_start"], last["global_end"]
                    if max(start, annotation["global_start"]) >= min(end, annotation["global_end"]):
                        continue
                    evidence["matches"].append({"evidence_sha256": witness["evidence_sha256"],
                        "window": witness["request"]["window"], "start": start, "end": end,
                        "token_start": i, "token_end": i + len(phrase),
                        "timestamp_status": "provider_hypothesis_not_alignment"})
            # A repeated window is corroborating context, not a new family.
            evidence["classification"] = ("vocalization_editorial_policy_unresolved" if annotation.get("kind") == "vocalization"
                else "speech_editorial_policy_unresolved" if annotation.get("kind") == "speech"
                else "cross_family_text_match_occurrence_unverified" if evidence["matches"]
                else "no_cross_family_text_match")
            evidence["text_certified"] = False
            events.append(evidence)
            if annotation.get("kind") == "sung" and evidence["matches"]:
                agreements.append({"text": annotation["text"], "current_segments": [],
                    "gemini_interval": [annotation["global_start"], annotation["global_end"]],
                    "whisper_occurrence_candidates": evidence["matches"],
                    "evidence_sha256": record["evidence_sha256"],
                    "status": "offline_insertion_hypothesis_not_adoptable",
                    "same_occurrence_certified": False, "lexicality_certified": False,
                    "timing_status": "provider_hypotheses_require_alignment_and_review",
                    "automatic_apply_allowed": False})
    coverage = {family: union_seconds([(r["start"], r["end"]) for r in cached["receipts"] if r["family"] == family]) for family in FAMILIES}
    full = all(abs(value - song["duration_seconds"]) < 1e-4 for value in coverage.values())
    reason = ("empty_baseline_acoustic_review_incomplete" if not full else
        "empty_baseline_insertion_hypotheses_require_occurrence_alignment_and_bridge" if agreements else
        "empty_baseline_vocalization_or_speech_editorial_review_required" if events and all(e["event"].get("kind") in {"vocalization", "speech"} for e in events) else
        "empty_baseline_no_cross_family_lexical_support")
    return {"schema": "empty-baseline-acoustic-diagnostic-v1", "source": source_binding(song),
        "baseline": [], "duration_seconds": song["duration_seconds"],
        "acoustic_review_complete": full, "coverage_seconds": coverage,
        "audio_evidence": cached["receipts"], "event_diagnostics": events,
        "offline_insertion_hypotheses": agreements,
        "word_evidence": [{"evidence_sha256": r["evidence_sha256"], "window": r["request"]["window"], "words": r["annotations"]} for r in whispers],
        "invalid_annotations": [{"evidence_sha256": r["evidence_sha256"], "items": r["invalid_annotations"]} for r in cached["records"] if r["invalid_annotations"]],
        "excluded_requests": [{"reason": r["reason"], "evidence_sha256": r.get("evidence_sha256"), "tool_status": r.get("request", {}).get("tool_status")} for r in cached["excluded"]],
        "blocker": reason, "complete_candidate": False, "human_checked": False,
        "original_documents_modified": False, "automatic_apply_allowed": False,
        "other_299_method_changed": False, "usage_cost_estimate_usd": usage_estimate(cached["records"]),
        "usage_is_invoice": False}


def run(root, snapshot_path, authorization_path=None, *, execute=False, concurrency=2):
    out = root / "campaign-300"
    started = time.monotonic()
    with owner_lock(out):
        manifest_path = out / "manifest.json"
        manifest = json.loads(manifest_path.read_text())
        snapshot = json.loads(snapshot_path.read_text())
        song, row = target_song(snapshot, manifest)
        index = request_index(root, max_files=25000)
        cached = cached_receipts(song, index=index)
        folder = out / JOB_ID
        folder.mkdir(mode=0o700, parents=True, exist_ok=True)
        errors, projection, spend = [], None, None
        attempts_before = attempts_after = 0
        if execute:
            auth = authorization(authorization_path, manifest)
            if auth["approved_usd"] != 20:
                raise ValueError("exact_usd20_campaign_authority_required")
            ledger = SpendLedger(out / "spend.sqlite", approved_usd=20, max_attempts=auth["max_attempts"])
            try:
                attempts_before = ledger.totals()["attempts"]
                projection = project(manifest, index, ledger.totals(), approved_usd=20)
                if projection["exceeds_budget"]:
                    ledger.hold_after_attempts(attempts_before)
                    errors.append("projected_remaining_exceeds_authorized_balance")
                else:
                    audio = Path(row["audio_path"])
                    verify_audio(audio, song)
                    policy = replace(ShadowPolicy(), max_calls_per_song=16)
                    def specifications():
                        for window in row["windows"]:
                            for provider, family in zip(("openai", "google"), FAMILIES):
                                if covered(cached["receipts"], family, window):
                                    continue
                                if previous_unknown(index, song, provider, window):
                                    errors.append("unknown_completion_not_repeated"); continue
                                identity = digest({"audio": {k: song[k] for k in ("job_id", "audio_sha256", "audio_revision")},
                                    "window": window, "provider": provider, "method": manifest["method_sha256"]})
                                clip = folder / (digest(window) + ".wav")
                                if not clip.exists(): extract_clip(audio, window, clip)
                                yield {"identity": identity, "provider": provider, "window": window,
                                       "clip": clip, "folder": folder, "policy": policy}
                    try:
                        execute_request_batches(specifications(), NoRetryLedger(ledger), source_binding(song),
                            errors, concurrency=concurrency)
                    except Exception as exc:
                        # Preserve completed receipts and a concrete failure stage;
                        # never expose provider exception strings or retry unknowns.
                        errors.append("empty_audio_execution_failed:" + type(exc).__name__)
                attempts_after = ledger.totals()["attempts"]
                spend = ledger.totals()
                manifest["spend"] = spend
            finally:
                ledger.db.close()
            cached = cached_receipts(song, index=request_index(root, max_files=25000))
        diagnostic = diagnose(song, cached)
        diagnostic.update(execution_errors=errors, budget_projection=projection, campaign_spend=spend,
            new_attempts=attempts_after-attempts_before, latency_seconds=round(time.monotonic()-started, 3))
        path = folder / "empty-baseline-diagnostic.json"
        atomic_json(path, diagnostic)
        update_status(row, cached["receipts"], blocker=diagnostic["blocker"])
        row.update(empty_baseline_diagnostic=str(path), acoustic_review_complete=diagnostic["acoustic_review_complete"],
            operationally_published=False, missing_audio_windows=[{"window": w, "family": f}
                for w in row["windows"] for f in FAMILIES if not covered(cached["receipts"], f, w)])
        manifest["counts"] = counters(manifest)
        atomic_json(manifest_path, manifest)
        print(json.dumps({"job_id": JOB_ID, "status": row["status"], "blocker": row["blocker"],
            "acoustic_review_complete": diagnostic["acoustic_review_complete"],
            "coverage_seconds": diagnostic["coverage_seconds"], "new_attempts": diagnostic["new_attempts"],
            "diagnostic": str(path)}), flush=True)
        return diagnostic


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--snapshot", type=Path, required=True)
    parser.add_argument("--authorization", type=Path)
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--provider-concurrency", type=int, choices=[2,4,8], default=2)
    args = parser.parse_args()
    run(args.root, args.snapshot, args.authorization, execute=args.execute, concurrency=args.provider_concurrency)
