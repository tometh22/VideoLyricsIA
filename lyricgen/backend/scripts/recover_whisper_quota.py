"""Prepared recovery after EXPLICIT credit restoration; never runs by default.

Only a known canonical OpenAI 429 rejection may receive one durable quota retry.
Original reservations remain. No automatic retries, document writes or approval.
"""
import argparse
from dataclasses import asdict
import json
from pathlib import Path
import time

from reviewer_acoustic_cache import cached_receipts, request_index
from reviewer_campaign import SpendLedger, atomic_json, owner_lock
from reviewer_shadow import ShadowPolicy, source_binding
from reviewer_shadow_audio import BlindAudioTools, extract_clip, file_sha
from scripts.run_reviewer_campaign import authorization, covered, verify_audio
from scripts.watch_reviewer_campaign_budget import project
from shadow_reference_import import digest


def restoration_authorization(path, manifest):
    if path is None:
        raise ValueError("explicit_credit_restoration_receipt_required")
    receipt = json.loads(Path(path).read_text())
    expected = {"schema": "openai-whisper-credit-restoration-v1", "provider": "openai",
                "model": "whisper-1", "restoration_confirmed": True,
                **{k: manifest[k] for k in ("campaign_id", "roster_sha256", "method_sha256")}}
    if (any(receipt.get(k) != value for k, value in expected.items())
            or receipt.get("restoration_confirmed") is not True
            or not isinstance(receipt.get("human_approval_reference"), str)
            or not receipt["human_approval_reference"].strip()):
        raise ValueError("explicit_credit_restoration_receipt_required")
    return receipt


def known_quota_rejection(request, song, canonical_windows):
    return (request.get("tool_status") == "tool_error" and request.get("http_status") == 429
        and request.get("received_audio") is False and request.get("provider") == "openai"
        and request.get("model") == "whisper-1" and request.get("family") == "openai/whisper-1"
        and request.get("prompt_version") == "no-prompt-v1" and request.get("error_type") == "HTTPError"
        and request.get("view") == "mix" and request.get("conditioning_texts") == []
        and request.get("source") == source_binding(song)
        and request.get("window") in canonical_windows)


def recovery_plan(root, manifest, songs, index):
    rows = {r["job_id"]: r for r in manifest["songs"]}
    plan = {}
    for entry in index:
        request = entry.get("request", {})
        jid = request.get("source", {}).get("job_id")
        if jid not in songs or jid not in rows:
            continue
        song, row = songs[jid], rows[jid]
        expected = root / "campaign-300" / jid / "requests"
        if Path(entry["cache_path"]).resolve().parent != expected.resolve():
            continue  # No retry-of-retry or unrelated historical experiments.
        if not known_quota_rejection(request, song, row["windows"]):
            continue
        cached = cached_receipts(song, index=index)
        if covered(cached["receipts"], "openai/whisper-1", request["window"]):
            continue
        original = digest({"audio": {k: song[k] for k in ("job_id", "audio_sha256", "audio_revision")},
            "window": request["window"], "provider": "openai", "method": manifest["method_sha256"]})
        identity = digest({"quota_retry_of": original, "quota_retry_number": 1})
        plan[identity] = {"identity": identity, "original_identity": original,
            "job_id": jid, "window": request["window"], "failed_path": entry["cache_path"],
            "failed_evidence_sha256": entry["evidence_sha256"]}
    order = {jid: i for i, jid in enumerate(manifest["execution_order"])}
    return sorted(plan.values(), key=lambda x: (order[x["job_id"]], x["window"]["start"]))


def usable_success(request):
    response = request.get("response")
    return (request.get("tool_status") == "ok" and request.get("received_audio") is True
        and isinstance(response, dict)
        and (isinstance(response.get("words"), list) or isinstance(response.get("text"), str)))


def attempt_one(case, song, row, ledger, root, *, listener_factory=None):
    """Owner-thread execution. All retry states are terminal for this identity."""
    prior = ledger.db.execute("SELECT status FROM attempts WHERE id=?", (case["original_identity"],)).fetchone()
    if prior != ("tool_error",):
        return {"status": "blocked", "reason": "original_rejection_ledger_mismatch"}
    existing = ledger.db.execute("SELECT status FROM attempts WHERE id=?", (case["identity"],)).fetchone()
    if existing:
        return {"status": "not_repeated", "reason": existing[0]}
    path = Path(case["failed_path"])
    if file_sha(path) != case["failed_evidence_sha256"]:
        raise ValueError("failed_receipt_changed")
    failed = json.loads(path.read_text())
    if not known_quota_rejection(failed, song, row["windows"]):
        raise ValueError("known_canonical_quota_rejection_required")
    folder = root / "campaign-300" / song["job_id"]
    clip = folder / (digest(case["window"]) + ".wav")
    if not clip.exists(): extract_clip(Path(row["audio_path"]), case["window"], clip)
    if file_sha(clip) != failed.get("clip_sha256"):
        raise ValueError("retry_audio_clip_identity_mismatch")
    policy = ShadowPolicy(**failed["policy"])
    if asdict(policy) != failed["policy"]:
        raise ValueError("retry_policy_identity_mismatch")
    # The exact restored authorization permits one new reservation, not a
    # blanket release of the campaign's existing quota/budget hold.
    ledger.hold_after_attempts(ledger.totals()["attempts"] + 1)
    reserved, reason = ledger.reserve(case["identity"], "openai", case["window"]["end"] - case["window"]["start"])
    if not reserved:
        return {"status": "blocked", "reason": reason}
    directory = folder / "quota-retry-1" / "requests"
    listener = (listener_factory or BlindAudioTools)(directory, policy=policy)
    try:
        request = listener.listen(clip, provider="openai", view="mix",
            source=source_binding(song), window=case["window"])
    except Exception:
        return {"status": "unknown_completion", "reason": "unknown_quota_retry_not_repeated"}
    ledger.finish(case["identity"], request["tool_status"], directory, request=request)
    return {"status": request["tool_status"], "http_status": request.get("http_status"),
        "provider_error_code": request.get("provider_error_code"),
        "provider_error_type": request.get("provider_error_type"),
        "usable_success": usable_success(request), "latency_seconds": request.get("latency_seconds")}


def run(root, snapshot_path, authorization_path, restoration_path=None, *, execute=False):
    folder = root / "campaign-300"
    with owner_lock(folder):
        manifest = json.loads((folder / "manifest.json").read_text())
        snapshot = json.loads(snapshot_path.read_text())
        if (len(snapshot["jobs"]) != 300 or digest(snapshot["jobs"]) != snapshot["snapshot_sha256"]
                or {j["job_id"] for j in snapshot["jobs"]} != {r["job_id"] for r in manifest["songs"]}):
            raise ValueError("exact_campaign_snapshot_required")
        songs = {s["job_id"]: s for s in snapshot["jobs"]}
        rows = {r["job_id"]: r for r in manifest["songs"]}
        if any(source_binding(songs[jid]) != row["source"] for jid, row in rows.items()):
            raise ValueError("snapshot_manifest_revision_mismatch")
        index = request_index(root, max_files=25000)
        plan = recovery_plan(root, manifest, songs, index)
        if not execute:
            result = {"eligible_known_429": len(plan), "new_calls": 0, "executed": False}
            print(json.dumps(result)); return result
        restored = restoration_authorization(restoration_path, manifest)
        auth = authorization(authorization_path, manifest)
        if auth["approved_usd"] != 20:
            raise ValueError("exact_usd20_campaign_authority_required")
        ledger = SpendLedger(folder / "spend.sqlite", approved_usd=20, max_attempts=auth["max_attempts"])
        report = {"schema": "whisper-quota-recovery-v1", "restoration_receipt_sha256": digest(restored),
            "eligible_known_429": len(plan), "results": [], "automatic_apply_allowed": False,
            "documents_modified": False, "first_attempt_validated_before_expansion": False,
            "started_at_epoch": time.time()}
        report_path = folder / "whisper-quota-recovery.json"
        verified_audio = set()
        try:
            for case in plan:
                projection = project(manifest, request_index(root, max_files=25000), ledger.totals(), approved_usd=20)
                if projection["exceeds_budget"]:
                    report["stop_reason"] = "projected_remaining_exceeds_authorized_balance"; break
                song, row = songs[case["job_id"]], rows[case["job_id"]]
                if song["job_id"] not in verified_audio:
                    verify_audio(Path(row["audio_path"]), song)
                    verified_audio.add(song["job_id"])
                outcome = attempt_one(case, song, row, ledger, root)
                report["results"].append({**case, **outcome})
                if outcome.get("status") == "not_repeated":
                    # A previous quota retry is permanently spent, regardless
                    # of its outcome. Continue to an untouched canonical request.
                    atomic_json(report_path, report)
                    continue
                # Sequential first attempt: no later reservation exists until
                # a complete, successful ASR response validates restored credit.
                if not outcome.get("usable_success"):
                    report["stop_reason"] = "quota_retry_not_successful_no_expansion"
                    break
                report["first_attempt_validated_before_expansion"] = True
                report["spend"] = ledger.totals()
                atomic_json(report_path, report)
        finally:
            # Even success leaves the broad campaign hold in place. Only this
            # exact recovery path was authorized; no unrelated runner is enabled.
            ledger.hold_after_attempts(ledger.totals()["attempts"])
            report["spend"] = ledger.totals()
            atomic_json(report_path, report)
            ledger.db.close()
        print(json.dumps({"processed": len(report["results"]), "stop_reason": report.get("stop_reason"),
            "spend": report["spend"], "report": str(report_path)}), flush=True)
        return report


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--snapshot", type=Path, required=True)
    parser.add_argument("--authorization", type=Path, required=True)
    parser.add_argument("--restoration-receipt", type=Path)
    parser.add_argument("--execute", action="store_true")
    args = parser.parse_args()
    run(args.root, args.snapshot, args.authorization, args.restoration_receipt, execute=args.execute)
