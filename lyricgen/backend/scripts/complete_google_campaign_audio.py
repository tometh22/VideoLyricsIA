"""Google-only canonical acoustic coverage, not full review or candidate approval.

The OpenAI circuit stays intact. Existing SQLite spend holds are never released.
No document, manifest, candidate, editor or model/selector modifications.
"""
import argparse
from dataclasses import replace
import json
from pathlib import Path
import sqlite3
import time

from reviewer_acoustic_cache import cached_receipts, request_index
from reviewer_campaign import SpendLedger, atomic_json, owner_lock
from reviewer_integral import union_seconds, windows
from reviewer_shadow import ShadowPolicy, source_binding
from reviewer_shadow_audio import extract_clip
from scripts.run_reviewer_campaign import (ProviderCircuitOpen, authorization, covered,
    execute_request_batches, previous_unknown, verify_audio)
from scripts.watch_reviewer_campaign_budget import project
from shadow_reference_import import digest

FAMILY = "google/gemini-2.5-flash-audio"


def validate_roster(snapshot, manifest):
    jobs = snapshot["jobs"]
    if (len(jobs) != 300 or len({j["job_id"] for j in jobs}) != 300
            or digest(jobs) != snapshot["snapshot_sha256"]
            or snapshot.get("campaign_id") != manifest.get("campaign_id")
            or len(manifest["songs"]) != 300
            or {j["job_id"] for j in jobs} != {r["job_id"] for r in manifest["songs"]}):
        raise ValueError("exact_300_campaign_required")
    songs = {j["job_id"]: j for j in jobs}
    for row in manifest["songs"]:
        song = songs[row["job_id"]]
        if row["source"] != source_binding(song) or row["windows"] != windows(song["duration_seconds"]):
            raise ValueError("frozen_source_or_window_plan_mismatch")
    return songs


def google_specs(root, manifest, row, song, cached, index, errors):
    folder = root / "campaign-300" / song["job_id"]
    folder.mkdir(mode=0o700, parents=True, exist_ok=True)
    policy = replace(ShadowPolicy(), max_calls_per_song=len(row["windows"]) * 2)
    for window in row["windows"]:
        if covered(cached["receipts"], FAMILY, window):
            continue
        if previous_unknown(index, song, "google", window):
            errors.append("unknown_completion_not_repeated"); continue
        clip = folder / (digest(window) + ".wav")
        if not clip.exists(): extract_clip(Path(row["audio_path"]), window, clip)
        yield {"identity": digest({"audio": {k: song[k] for k in ("job_id", "audio_sha256", "audio_revision")},
            "window": window, "provider": "google", "method": manifest["method_sha256"]}),
            "provider": "google", "window": window, "clip": clip, "folder": folder, "policy": policy}


def family_result(song, row, cached, *, errors=(), latency=0., new_attempts=0):
    receipts = [r for r in cached["receipts"] if r["family"] == FAMILY]
    missing = [w for w in row["windows"] if not covered(receipts, FAMILY, w)]
    seconds = union_seconds([(r["start"], r["end"]) for r in receipts])
    return {"job_id": song["job_id"], "source": source_binding(song),
        "family": FAMILY, "duration_seconds": song["duration_seconds"],
        "coverage_seconds": seconds, "missing_windows": missing, "audio_evidence": receipts,
        "status": "google_coverage_complete" if not missing else "google_coverage_partial" if seconds else "google_coverage_pending",
        "execution_errors": list(errors), "new_attempts": new_attempts, "latency_seconds": latency,
        "independent_families_required_for_full_review": 2, "full_review_complete": False,
        "candidate_generated": False, "documents_modified": False}


def run(root, snapshot_path, authorization_path, *, concurrency=2, execute=False):
    if type(concurrency) is not int or concurrency not in (2, 4):
        raise ValueError("google_concurrency_must_be_2_or_4")
    folder = root / "campaign-300"
    with owner_lock(folder):
        manifest = json.loads((folder / "manifest.json").read_text())
        songs = validate_roster(json.loads(snapshot_path.read_text()), manifest)
        rows = {r["job_id"]: r for r in manifest["songs"]}
        index = request_index(root, max_files=25000)
        report = {"schema": "google-only-campaign-coverage-v1", "campaign_id": manifest["campaign_id"],
            "method_sha256": manifest["method_sha256"], "songs": [], "openai_calls": 0,
            "full_reviews_completed": 0, "concurrency": concurrency, "executed": execute}
        output = folder / "google-only-coverage.json"
        hold_path = folder / "google-provider-circuit-hold.json"
        ledger = None
        if execute:
            auth = authorization(authorization_path, manifest)
            if auth["approved_usd"] != 20:
                raise ValueError("exact_usd20_campaign_authority_required")
            if hold_path.exists() and json.loads(hold_path.read_text()).get("status") == "open":
                return {"stop_reason": "google_provider_circuit_open", "new_calls": 0}
            ledger = SpendLedger(folder / "spend.sqlite", approved_usd=20, max_attempts=auth["max_attempts"])
        try:
            for jid in manifest["execution_order"]:
                song, row = songs[jid], rows[jid]
                cached = cached_receipts(song, index=index)
                start = time.monotonic(); errors = []; before = ledger.totals()["attempts"] if ledger else 0
                stop = False
                pending = any(not covered(cached["receipts"], FAMILY, w) for w in row["windows"])
                if execute and pending:
                    projection = project(manifest, index, ledger.totals(), approved_usd=20)
                    if projection["exceeds_budget"]:
                        ledger.hold_after_attempts(before)
                        report.update(stop_reason="projected_remaining_exceeds_authorized_balance", budget_projection=projection)
                        stop = True
                    else:
                        try:
                            verify_audio(Path(row["audio_path"]), song)
                            execute_request_batches(google_specs(root, manifest, row, song, cached, index, errors),
                                ledger, source_binding(song), errors, concurrency=concurrency)
                        except ProviderCircuitOpen as exc:
                            atomic_json(hold_path, exc.receipt)
                            report["stop_reason"] = "google_http_429_circuit_open"
                            errors.append("google_http_429_circuit_open"); stop = True
                        except sqlite3.IntegrityError:
                            # Never lift a shared phase/spend guard to work around
                            # an OpenAI outage. Preserve it and report the boundary.
                            report["stop_reason"] = "existing_sqlite_spend_hold"
                            errors.append("existing_sqlite_spend_hold"); stop = True
                        except Exception as exc:
                            errors.append("song_audio_execution_failed:" + type(exc).__name__)
                    index = request_index(root, max_files=25000)
                    cached = cached_receipts(song, index=index)
                after = ledger.totals()["attempts"] if ledger else 0
                report["songs"].append(family_result(song, row, cached, errors=errors,
                    latency=round(time.monotonic()-start, 3), new_attempts=after-before))
                report["counts"] = {state: sum(r["status"] == state for r in report["songs"])
                    for state in ("google_coverage_complete", "google_coverage_partial", "google_coverage_pending")}
                report["songs_remaining_unvisited"] = 300 - len(report["songs"])
                if ledger: report["spend"] = ledger.totals()
                atomic_json(output, report)
                if stop: break
        finally:
            if ledger: ledger.db.close()
        print(json.dumps({"counts": report.get("counts", {}), "stop_reason": report.get("stop_reason"),
            "songs_remaining_unvisited": report.get("songs_remaining_unvisited", 300),
            "openai_calls": 0, "report": str(output)}), flush=True)
        return report


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--snapshot", type=Path, required=True)
    parser.add_argument("--authorization", type=Path, required=True)
    parser.add_argument("--provider-concurrency", type=int, choices=[2,4], default=2)
    parser.add_argument("--execute", action="store_true")
    args = parser.parse_args()
    run(args.root, args.snapshot, args.authorization, concurrency=args.provider_concurrency, execute=args.execute)
