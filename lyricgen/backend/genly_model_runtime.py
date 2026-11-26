"""Local full-song draft model with blind ASR and original-clock alignment.

Recognition and alignment are separate stages so large models can be unloaded
between them. The editable draft is a proposal: coverage never certifies lyric
accuracy, and missing/ambiguous observations preserve the original segment.
"""
from __future__ import annotations

from copy import deepcopy
from difflib import SequenceMatcher
import math
import os
import json
from pathlib import Path
import subprocess
import time

from local_genly_review import normalize_snapshot
from reviewer_shadow import assert_current, source_binding, tokens
from reviewer_shadow_audio import file_sha, probe
from shadow_reference_import import digest
from genly_model_lexical_policy import choose_lexical


SCHEMA = "genly-model-result-v1"


def cached_stem_for_audio(manifest_path, audio_sha256):
    if manifest_path is None or not Path(manifest_path).is_file():
        return None
    matches = [r for r in json.loads(Path(manifest_path).read_text()) if r.get("original_sha256") == audio_sha256]
    if not matches:
        return None
    if len({(r.get("stem_path"), r.get("stem_sha256")) for r in matches}) != 1:
        raise ValueError("conflicting_cached_stems")
    return matches[0]


def plan_windows(duration, *, window_seconds=24.0, overlap_seconds=4.0, max_windows=128):
    if not math.isfinite(duration) or duration <= 0:
        raise ValueError("invalid_duration")
    if not 1 <= window_seconds <= 24 or not 0 <= overlap_seconds < window_seconds:
        raise ValueError("invalid_window_configuration")
    if type(max_windows) is not int or not 1 <= max_windows <= 1024:
        raise ValueError("invalid_window_budget")
    windows, start = [], 0.0
    while start < duration and len(windows) < max_windows:
        end = min(duration, start + window_seconds)
        windows.append({"id": len(windows), "start": start, "end": end})
        if end == duration:
            break
        start += window_seconds - overlap_seconds
    for i, window in enumerate(windows):
        window["core_start"] = 0.0 if i == 0 else (windows[i - 1]["end"] + window["start"]) / 2
        window["core_end"] = window["end"] if i == len(windows) - 1 else (window["end"] + windows[i + 1]["start"]) / 2
    return windows


def extract_native_clip(path, window, destination):
    """Decode before trim so every crop uses the original decoded audio clock."""
    subprocess.run([
        "ffmpeg", "-v", "error", "-nostdin", "-i", str(path), "-af",
        f"atrim=start={window['start']}:end={window['end']},asetpts=PTS-STARTPTS",
        "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le", "-threads", "1", "-n", str(destination),
    ], check=True, capture_output=True, timeout=90)
    os.chmod(destination, 0o600)
    return file_sha(destination)


def verify_cached_vocal_clock(audio_path, stem_path):
    """Reuse the established waveform clock check on active stem anchors.

    Unlike fixed RMS-envelope positions, this checks three independently
    selected high-energy anchors, unique correlation peaks and clock drift.
    Crops use the same bounds only for a verified offset no larger than 40 ms;
    final CTC timestamps are always measured on the original mix.
    """
    from reviewer_shadow_audio import pcm
    from vocal_continuity import verify_stem_clock
    receipt = verify_stem_clock(pcm(audio_path, rate=16000), pcm(stem_path, rate=16000), 16000)
    admissible = receipt.get("verified") is True and abs(receipt["offset_s"]) <= .04
    return {"tool": "existing-active-waveform-stem-clock-v1", **receipt,
            "mix_stem_sync_verified": admissible, "automatic_offset_applied": False,
            "timestamp_transfer": False}


def recognize_song(snapshot, audio_path, output_dir, *, listener, ranking=(), stem=None,
                   window_seconds=24.0, overlap_seconds=4.0, max_windows=128,
                   duration_probe=probe, clip_extractor=extract_native_clip, sync_checker=None):
    song = normalize_snapshot(snapshot)
    if file_sha(audio_path) != song["audio_sha256"]:
        raise ValueError("audio_identity_changed")
    duration = duration_probe(audio_path)
    signals = [("original_mix", Path(audio_path))]
    stem_evidence = None
    if stem:
        stem_path = Path(stem["stem_path"])
        if stem.get("original_sha256") != song["audio_sha256"] or file_sha(stem_path) != stem.get("stem_sha256"):
            raise ValueError("stem_identity_mismatch")
        if sync_checker is None:
            sync_checker = verify_cached_vocal_clock
        stem_evidence = sync_checker(audio_path, stem_path)
        if stem_evidence.get("mix_stem_sync_verified") is not True:
            raise ValueError("stem_clock_not_verified")
        stem_evidence = {**stem_evidence, "original_sha256": song["audio_sha256"], "stem_sha256": stem["stem_sha256"]}
        signals.append(("vocals", stem_path))
    for segment in song["segments"]:
        a, b = segment["start"], segment["end"]
        if not all(type(v) in (float, int) and math.isfinite(v) for v in (a, b)) or not 0 <= a < b <= duration:
            raise ValueError("invalid_segment_clock")
    windows = plan_windows(duration, window_seconds=window_seconds,
                           overlap_seconds=overlap_seconds, max_windows=max_windows)
    # Risk ordering changes only work order; no low-risk windows are omitted.
    priority = {}
    for rank, item in enumerate(ranking):
        index = item.get("line_index")
        if type(index) is not int or not 0 <= index < len(song["segments"]):
            raise ValueError("invalid_ranking")
        segment = song["segments"][index]
        for window in windows:
            if segment["start"] < window["end"] and segment["end"] > window["start"]:
                priority.setdefault(window["id"], rank)
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(output_dir, 0o700)
    observations = []
    for window in sorted(windows, key=lambda w: (priority.get(w["id"], len(ranking)), w["id"])):
        for signal, signal_path in signals:
            clip = output_dir / f"{signal}-window-{window['id']:04d}.wav"
            if clip.exists():
                raise ValueError("clip_exists_choose_new_output_directory")
            observation = {"source": source_binding(song), "window": window,
                           "model_id": listener.model_id, "reference_conditioned": False,
                           "signal": signal, "clock": "original_mix_decoded"}
            try:
                observation["clip_sha256"] = clip_extractor(signal_path, window, clip)
                text = listener.listen(clip)
                if not isinstance(text, str) or len(text) > 4000:
                    raise ValueError("invalid_recognizer_output")
                observation.update(status="recognized", text=text.strip())
            except Exception as exc:
                observation.update(status="failed", error_type=type(exc).__name__)
            observations.append(observation)
    return {"schema": "genly-model-recognition-v1", "source": source_binding(song),
            "duration_seconds": duration, "windows": windows,
            "observations": sorted(observations, key=lambda o: o["window"]["id"]),
            "model_id": listener.model_id, "stem_evidence": stem_evidence, "hosted_provider_calls": 0,
            "audio_uploaded": False}


def _valid_alignment(alignment, text, window):
    if alignment.get("status") != "aligned_hypothesis" or alignment.get("clock") != "original_mix_decoded":
        return False
    words = alignment.get("words") or []
    if tokens(" ".join(str(w.get("word", "")) for w in words)) != tokens(text):
        return False
    previous = window["start"]
    for word in words:
        a, b = word.get("global_start"), word.get("global_end")
        if not all(type(v) in (float, int) and math.isfinite(v) for v in (a, b)) or not previous <= a < b <= window["end"]:
            return False
        previous = b
    return bool(words)


def _merge_intervals(intervals):
    merged = []
    for start, end in sorted(intervals):
        if merged and start <= merged[-1][1] + 1e-6:
            merged[-1][1] = max(merged[-1][1], end)
        else:
            merged.append([start, end])
    return merged


def _lexical_edits(before, after):
    left, right = tokens(before), tokens(after)
    return [{"operation": op, "original_token_range": [a, b], "candidate_token_range": [c, d],
             "before": left[a:b], "after": right[c:d]}
            for op, a, b, c, d in SequenceMatcher(None, left, right, autojunk=False).get_opcodes()
            if op != "equal"]


def _invalidate_evidence(segment):
    for key in list(segment):
        if key == "words" or key.startswith("ctc_") or key in {
            "alignment_score", "recognition_score", "provider_evidence", "provider_output_integrity",
            "content_asr_score", "content_asr_min_score", "content_source", "timing_provenance",
            "content_provenance", "evidence_schema",
        }:
            segment.pop(key, None)


def assemble_candidate(snapshot, audio_path, recognition, *, aligner, endpoint_measure=None,
                       activity_receipts=()):
    """Create a full editable draft; align ASR text, never editor text.

    Word midpoints choose one occurrence and one window core. Ambiguous lines
    preserve their text/timing; unassigned words remain findings, not silently
    inserted lyrics. pYIN observations never trim final consonants or extend a
    line automatically.
    """
    song = normalize_snapshot(snapshot)
    assert_current(recognition, song)
    if file_sha(audio_path) != song["audio_sha256"]:
        raise ValueError("audio_identity_changed")
    duration = recognition["duration_seconds"]
    if type(duration) not in (int, float) or not math.isfinite(duration) or duration <= 0:
        raise ValueError("invalid_recognition_duration")
    observations = recognition["observations"]
    from genly_model_acoustic_policy import classify_nonvocal_window
    aligned, intervals, alignment_intervals, word_pool = [], [], [], []
    seen = set()
    for observation in observations:
        assert_current(observation, song)
        window = observation["window"]
        clocks = [window.get(k) for k in ("start", "core_start", "core_end", "end")]
        if (not all(type(v) in (int, float) and math.isfinite(v) for v in clocks)
                or not 0 <= clocks[0] <= clocks[1] < clocks[2] <= clocks[3] <= duration
                or not 0 < clocks[3] - clocks[0] <= 24):
            raise ValueError("invalid_recognition_window")
        signal = observation.get("signal", "original_mix")
        if signal not in {"original_mix", "vocals"}:
            raise ValueError("unknown_audio_signal")
        if signal == "vocals" and (recognition.get("stem_evidence") or {}).get("mix_stem_sync_verified") is not True:
            raise ValueError("unverified_stem_recognition")
        key = (window["id"], signal)
        if key in seen:
            raise ValueError("duplicate_recognition_window")
        seen.add(key)
        if observation.get("reference_conditioned") is not False:
            raise ValueError("reference_conditioned_recognition")
        if observation.get("status") != "recognized":
            aligned.append({"window": window, "status": "recognition_failed"})
            continue
        intervals.append([window["start"], window["end"]])
        activity_decisions = [classify_nonvocal_window(receipt, {"start": window["start"], "end": window["end"]},
                              audio_sha256=song["audio_sha256"], audio_duration=duration) for receipt in activity_receipts]
        suppression = next((decision for decision in activity_decisions if decision["suppression_allowed"]), None)
        if suppression:
            aligned.append({"window": window, "signal": signal, "status": "nonvocal_supported",
                            "activity_decision": suppression, "recognition_retained_in_source_artifact": True})
            alignment_intervals.append([window["start"], window["end"]])
            continue
        text = observation["text"]
        if not tokens(text):
            aligned.append({"window": window, "status": "no_recognized_words"})
            alignment_intervals.append([window["start"], window["end"]])
            continue
        try:
            alignment = aligner(audio_path, text, {"start": window["start"], "end": window["end"]})
        except Exception as exc:
            alignment = {"status": "tool_error", "error_type": type(exc).__name__}
        record = {"window": window, "signal": signal, "alignment": alignment,
                  "activity_decisions": activity_decisions,
                  "status": "aligned" if _valid_alignment(alignment, text, window) else "invalid_alignment"}
        aligned.append(record)
        if record["status"] != "aligned":
            continue
        alignment_intervals.append([window["start"], window["end"]])
        for word in alignment["words"]:
            midpoint = (word["global_start"] + word["global_end"]) / 2
            if window["core_start"] <= midpoint < window["core_end"]:
                word_pool.append({**word, "window_id": window["id"], "signal": signal,
                                  "near_clip_edge": word["global_start"] < window["start"] + .08 or word["global_end"] > window["end"] - .08})
    word_pool.sort(key=lambda w: (w["global_start"], w["global_end"]))
    owned = {i: {} for i in range(len(song["segments"]))}
    ambiguous, unassigned = set(), []
    for word in word_pool:
        midpoint = (word["global_start"] + word["global_end"]) / 2
        owners = [i for i, line in enumerate(song["segments"])
                  if line["start"] - .25 <= midpoint <= line["end"] + .25]
        if len(owners) == 1:
            owned[owners[0]].setdefault(word["signal"], []).append(word)
        elif len(owners) > 1:
            ambiguous.update(owners)
        else:
            unassigned.append(word)
    candidates, findings = deepcopy(song["segments"]), []
    for i, current in enumerate(song["segments"]):
        base = {"line_index": i, "occurrence_id": f"{song['job_id']}:{i}",
                "original_text": current.get("text", ""), "original_start": current["start"],
                "original_end": current["end"], "correctness_certified": False}
        if current.get("locked") or current.get("operator_locked") or song.get("approved_at") or song.get("status") in {"done", "lyrics_approved"}:
            findings.append({**base, "status": "protected", "reason": "protected_source"})
            continue
        original = song.get("original_segments")
        if original is not None and (i >= len(original) or any(current.get(k) != original[i].get(k) for k in ("text", "start", "end"))):
            findings.append({**base, "status": "protected", "reason": "human_edited_source"})
            continue
        alternatives = {signal: " ".join(w["word"] for w in rows) for signal, rows in owned[i].items()}
        words = owned[i].get("original_mix") or owned[i].get("vocals") or []
        if i in ambiguous or not words or any(w["near_clip_edge"] for w in words):
            findings.append({**base, "status": "needs_review", "reason": "ambiguous_or_missing_acoustic_words", "original_timing_retained": True})
            continue
        recognized_text = " ".join(w["word"] for w in words)
        left, recognized_tokens = tokens(current.get("text", "")), tokens(recognized_text)
        # A recognizer with a completely different phrase does not establish
        # that it belongs to this line. Keep its evidence, preserve the draft.
        similarity = SequenceMatcher(None, left, recognized_tokens, autojunk=False).ratio()
        if similarity < .35 or not .5 <= len(recognized_tokens) / max(1, len(left)) <= 2:
            findings.append({**base, "status": "needs_review", "reason": "occurrence_text_uncertain",
                             "recognized_text": recognized_text, "alternatives": alternatives, "original_timing_retained": True})
            continue
        a, b = words[0]["global_start"], words[-1]["global_end"]
        # Do not introduce overlaps or large shifts while assigning occurrences
        # against an unverified editor timeline.
        previous_end = max([r["end"] for j, r in enumerate(song["segments"]) if j != i and r["start"] < current["start"]] or [0.0])
        next_start = min([r["start"] for j, r in enumerate(song["segments"]) if j != i and r["start"] >= current["start"]] or [duration])
        nonmonotonic = any(x["global_end"] > y["global_start"] for x, y in zip(words, words[1:]))
        if nonmonotonic or not previous_end <= a < b <= next_start or max(abs(a-current["start"]), abs(b-current["end"])) > 1.0:
            findings.append({**base, "status": "needs_review", "reason": "unstable_or_overlapping_timing",
                             "recognized_text": recognized_text, "alternatives": alternatives, "original_timing_retained": True})
            continue
        lexical = choose_lexical(current["text"], recognized_text, alternatives)
        proposed_text = lexical["text"]
        right = tokens(proposed_text)
        candidate = candidates[i]
        # CTC locates recognition hypotheses. It is not evidence that the
        # existing visual boundaries should be shortened, nor that a sustained
        # vowel ends at its final acoustic token. Preserve baseline timing.
        if left != right:
            _invalidate_evidence(candidate)
            candidate.update(text=proposed_text, word_alignment_status="hypotheses_only_after_text_change",
                             content_provenance="local_blind_asr_hypothesis", correctness_certified=False)
        finding = {**base, "status": "proposed" if left != right else "unchanged", "candidate_text": candidate["text"],
                   "candidate_start": candidate["start"], "candidate_end": candidate["end"],
                   "lexical_edits": _lexical_edits(current["text"], candidate["text"]),
                   "aligned_word_hypotheses": [{"word": w["word"], "start": w["global_start"], "end": w["global_end"]} for w in words],
                   "word_count": len(words), "original_timing_retained": True,
                   "window_ids": sorted({w["window_id"] for w in words}),
                   "supporting_signals": sorted(alternatives), "alternatives": alternatives,
                   "lexical_decision": lexical, "alignment_conditioning_text": recognized_text}
        if left == right and lexical["withheld_edits"]:
            finding.update(status="needs_review", reason="localized_edits_withheld")
        if endpoint_measure:
            # Localize pYIN to the phrase using aligned original-clock words.
            measurement_alignment = {"window": {"start": max(0., a-.3), "end": min(next_start, b+1.5)}, "words": words}
            try:
                finding["sustain_evidence"] = endpoint_measure(audio_path, measurement_alignment, next_occurrence_start=next_start)
            except Exception as exc:
                finding["sustain_evidence"] = {"status": "tool_error", "error_type": type(exc).__name__}
        findings.append(finding)
    covered, aligned_coverage = _merge_intervals(intervals), _merge_intervals(alignment_intervals)
    full = bool(covered and covered[0][0] == 0 and len(covered) == 1 and covered[0][1] >= duration-1e-6)
    all_aligned = bool(aligned_coverage and aligned_coverage[0][0] == 0 and len(aligned_coverage) == 1 and aligned_coverage[0][1] >= duration-1e-6)
    failed_alignment_windows = sum(record["status"] in {"recognition_failed", "invalid_alignment"} for record in aligned)
    implementation_hashes = {name: file_sha(Path(__file__).with_name(name)) for name in
                             ("genly_model_runtime.py", "genly_model_lexical_policy.py", "genly_model_acoustic_policy.py")}
    model_id = "genly-local-" + digest({"recognizer": recognition["model_id"], "algorithm": SCHEMA,
                                     "implementation_hashes": implementation_hashes,
                                     "lexical_policy": "corroborated-local-edits-v1",
                                     "timing_policy": "baseline-preserved-ctc-hypotheses-v1",
                                     "acoustic_policy": "genly-low-singing-activity-v1-experimental",
                                     "activity_policy_enabled": bool(activity_receipts),
                                     "alignment_models": sorted({str(r.get("alignment", {}).get("model", "injected")) for r in aligned})})
    return {"schema": SCHEMA, "source": source_binding(song), "status": "complete" if full and all_aligned and not failed_alignment_windows else "partial",
            "model_id": model_id, "candidate_segments": candidates, "findings": findings,
            "coverage": {"duration_seconds": duration, "analyzed_intervals": covered,
                         "aligned_intervals": aligned_coverage, "full_audio_covered": full,
                         "all_audio_aligned_or_empty": all_aligned, "total_lines": len(candidates),
                         "failed_alignment_windows": failed_alignment_windows,
                         "proposed_lines": sum(f["status"] == "proposed" for f in findings)},
            "provenance": {"recognizer_model_id": recognition["model_id"], "recognition_evidence_sha256": digest(recognition),
                           "implementation_hashes": implementation_hashes,
                           "singing_activity_enabled": bool(activity_receipts),
                           "clock": "original_mix_decoded", "selector_role": "prioritization_only",
                           "reference_conditioned_recognition": False, "model_family": "genly-local-audio-draft-v1"},
            "alignment_evidence": aligned, "unassigned_acoustic_words": unassigned,
            "quality_validated": False, "timing_verified": False, "automatic_apply_allowed": False,
            "hosted_provider_calls": 0, "audio_uploaded": False}


def attach_endpoint_extensions(report, snapshot, evidence):
    """Attach only separately validated extension proposals, never CTC trims."""
    song = normalize_snapshot(snapshot)
    assert_current(report, song)
    assert_current(evidence, song)
    if evidence.get("policy") != "vocal-continuity-v2-periodicity" or evidence.get("automatic_apply_allowed") is not False:
        raise ValueError("unverified_endpoint_policy")
    result = deepcopy(report)
    applied = []
    for item in evidence.get("candidates", []):
        index = item.get("line_index")
        if type(index) is not int or not 0 <= index < len(song["segments"]):
            raise ValueError("invalid_endpoint_line")
        assert_current(item, song)
        baseline, candidate = song["segments"][index], result["candidate_segments"][index]
        old, new = item.get("current_end"), item.get("proposed_end")
        if (not all(type(v) in (int, float) and math.isfinite(v) for v in (old, new))
                or old != baseline["end"] or not .5 <= new-old <= 3.0
                or candidate["text"] != baseline["text"] or candidate["end"] != old
                or (item.get("clock") or {}).get("verified") is not True
                or (item.get("periodicity_evidence") or {}).get("supported") is not True
                or baseline.get("locked") or baseline.get("operator_locked")
                or song.get("approved_at") or song.get("status") in {"done", "lyrics_approved"}):
            continue
        next_start = min([row["start"] for j, row in enumerate(song["segments"])
                          if j != index and row["start"] > baseline["start"]]
                         or [result["coverage"]["duration_seconds"]])
        if new > next_start or new > result["coverage"]["duration_seconds"]:
            continue
        candidate["end"] = new
        candidate["timing_provenance"] = "verified_vocal_continuity_extension_proposal"
        candidate["timing_verified"] = False
        applied.append(index)
        finding = next(f for f in result["findings"] if f["line_index"] == index)
        finding.update(status="proposed", candidate_end=new, original_timing_retained=False,
                       endpoint_extension=deepcopy(item))
    result["endpoint_evidence"] = deepcopy(evidence)
    result["provenance"]["endpoint_evidence_sha256"] = digest(evidence)
    result["coverage"]["proposed_lines"] = sum(f["status"] == "proposed" for f in result["findings"])
    result["provenance"]["endpoint_extension_lines"] = applied
    return result
