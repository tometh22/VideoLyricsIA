"""Bounded existing-quality-worker integration. Rollout remains default off."""
import os
from pathlib import Path

from reviewer_assist import enabled, prepare
from reviewer_candidate import build_candidate
from reviewer_phrase_alignment import align_phrase
from reviewer_shadow import ShadowPolicy, plan_windows, review_window, source_binding
from reviewer_shadow_audio import BlindAudioTools, extract_clip, file_sha, private_write, probe
from shadow_reference_import import digest


def run_snapshot(job_id, snapshot, mix_path, stem_path):
    if not enabled():
        return None, {"enabled": False}
    if snapshot.get("approved_at") or snapshot.get("status") in {"lyrics_approved", "done"}:
        return None, {"enabled": True, "reason": "human_approval_preserved", "provider_calls": 0}
    from reviewer_assist_scope import inference_enabled
    if not inference_enabled(snapshot.get("campaign_id")):
        return None, {"enabled": True, "reason": "inference_disabled_or_campaign_out_of_scope", "provider_calls": 0}
    if os.environ.get("QUALITY_OPERATOR_SUGGESTIONS_ENABLED", "0").strip().lower() not in {"1", "true", "yes", "on"}:
        return None, {"enabled": True, "reason": "text_suggestion_rollout_disabled", "provider_calls": 0}
    cache_base = os.environ.get("REVIEWER_ASSIST_CACHE_DIR")
    if not cache_base:
        return None, {"enabled": True, "reason": "persistent_cache_directory_required", "provider_calls": 0}
    if not stem_path:
        return None, {"enabled": True, "reason": "stem_unavailable", "provider_calls": 0}
    if file_sha(mix_path) != snapshot["audio_sha256"]:
        return None, {"enabled": True, "reason": "source_audio_hash_mismatch", "provider_calls": 0}
    song = {"job_id": job_id, "campaign_id": snapshot.get("campaign_id"), "segments": snapshot["segments"],
        "segments_revision": snapshot["revision"], "segments_sha256": digest(snapshot["segments"]),
        "audio_revision": snapshot["audio_revision"], "audio_sha256": snapshot["audio_sha256"],
        "duration_seconds": probe(mix_path)}
    root = Path(cache_base) / digest(source_binding(song))
    root.mkdir(parents=True, mode=0o700, exist_ok=True)
    policy = ShadowPolicy()
    listener = BlindAudioTools(root / "requests", policy=policy)
    decisions = []
    # Existing unsafe windows determine work; no calls on every lyric line.
    unsafe = snapshot.get("quality", {}).get("unsafe_windows", [])
    indices = {i for i, row in enumerate(song["segments"]) if any(
        float(w.get("start", 0)) < float(row["end"]) and
        float(w.get("end", 0)) > float(row["start"]) for w in unsafe if isinstance(w, dict))}
    windows = []
    original_keys = {digest([r.get("text"), r.get("start"), r.get("end")])
                     for r in snapshot.get("original_segments", snapshot["segments"])}
    for i in sorted(indices):
        row = song["segments"][i]
        if row.get("locked") or row.get("operator_locked"):
            continue
        if digest([row.get("text"), row.get("start"), row.get("end")]) not in original_keys:
            continue
        start = max(0., float(row["start"]) - policy.context_seconds)
        end = min(song["duration_seconds"], float(row["end"]) + policy.context_seconds,
                  start + policy.max_clip_seconds)
        windows.append({"line_index": i, "start": start, "end": end,
                        "offset_seconds": start, "sample_role": "existing_quality_flag"})
        if len(windows) == policy.max_windows_per_song:
            break
    for window in windows:
        row = song["segments"][window["line_index"]]
        if row.get("locked") or row.get("operator_locked"):
            continue
        evidence = []
        for view, provider, audio in [("mix", "openai", mix_path), ("stem", "google", stem_path)]:
            clip = root / f"{window['line_index']}-{view}.wav"
            if not clip.exists():
                extract_clip(audio, window, clip)
            response = listener.listen(clip, provider=provider, view=view,
                source=source_binding(song), window=window)
            evidence.append({**response, "kind": "minimal_text_patch_request"})
        decisions.append(review_window(song, window, evidence=evidence,
            commit=os.environ.get("RAILWAY_GIT_COMMIT_SHA", "unknown")))
    prepared = prepare(song, decisions)
    hypotheses = (snapshot.get("machine_evidence") or {}).get("hypotheses_by_family", [])
    candidate = build_candidate(song, decisions, hypotheses=hypotheses if isinstance(hypotheses, list) else [])
    candidate["realignments"] = []
    for change in candidate["changes"]:
        if change["field"] == "text":
            i = change["line_index"]
            recognition_window = next(d["window"] for d in decisions if d["window"]["line_index"] == i)
            start = max(0., song["segments"][i]["start"] - 1.)
            end = (song["segments"][i+1]["start"] if i+1 < len(song["segments"])
                   else song["duration_seconds"])
            window = {"start": start, "end": min(end, start+24.), "offset_seconds": start}
            candidate["realignments"].append({"line_index": i,
                "recognition_window": recognition_window, "display_timing_changed": False,
                **align_phrase(mix_path, candidate["segments"][i]["text"], window)})
    artifact = root / f"candidate-{candidate['id']}.json"
    if not artifact.exists():
        private_write(artifact, candidate)
    if prepared["proposal"]:
        prepared["proposal"]["reviewer_assist"]["candidate"] = {
            "segments": candidate["segments"], "baseline_sha256": candidate["baseline_sha256"],
            "candidate_sha256": candidate["candidate_sha256"], "approved": False,
            "unchanged_lines_certified": False}
    return prepared["proposal"], {"enabled": True, "provider_calls": listener.calls,
        "processed_windows": len(decisions), "generated": prepared["telemetry"]["proposal_count"],
        "tool_errors": sum(len(d["tool_errors"]) for d in decisions),
        "automatic_apply_allowed": False}
