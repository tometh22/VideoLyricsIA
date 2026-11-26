"""Offline local-model review using Genly's existing human proposal contract.

Recognition never receives editor lyrics. The learned risk model orders work;
it does not certify the recognizer's output. This experimental path proposes
text only and never approves, publishes, or silently changes a song.
"""
from __future__ import annotations

from copy import deepcopy
import json
import math
import os
from pathlib import Path
import time

from operator_review_proposals import build_operator_review_proposal
from reviewer_shadow import assert_current, source_binding, tokens, validate_snapshot
from reviewer_shadow_audio import extract_clip, file_sha, probe
from shadow_reference_import import digest


def normalize_snapshot(snapshot: dict) -> dict:
    song = deepcopy(snapshot)
    if "segments_revision" not in song:
        song["segments_revision"] = song.get("revision")
    song.setdefault("segments_sha256", digest(song["segments"]))
    validate_snapshot(song)
    if not isinstance(song.get("audio_revision"), int) or not song.get("job_id"):
        raise ValueError("source_identity_missing")
    return song


def prepare_proposal(song: dict, observations: list[dict]) -> tuple[dict | None, dict]:
    """Convert bound acoustic observations into review-only editor suggestions."""
    song = normalize_snapshot(song)
    candidates, seen = [], set()
    skipped = {"protected": 0, "unchanged": 0, "invalid": 0}
    for item in observations:
        assert_current(item, song)
        index = item.get("line_index")
        if type(index) is not int or not 0 <= index < len(song["segments"]) or index in seen:
            raise ValueError("invalid_or_duplicate_line_index")
        seen.add(index)
        current = song["segments"][index]
        if (current.get("locked") or current.get("operator_locked") or song.get("approved_at")
                or song.get("status") in {"lyrics_approved", "done"}):
            skipped["protected"] += 1
            continue
        original = song.get("original_segments")
        if original is not None and (index >= len(original) or
                any(current.get(k) != original[index].get(k) for k in ("text", "start", "end"))):
            skipped["protected"] += 1
            continue
        text = item.get("text")
        if (item.get("status") != "recognized" or item.get("reference_conditioned") is not False
                or not item.get("model_id") or not item.get("clip_sha256")
                or not isinstance(text, str) or not text.strip() or len(text) > 1000):
            skipped["invalid"] += 1
            continue
        window = item.get("window") or {}
        if window.get("start") != current["start"] or window.get("end") != current["end"]:
            raise ValueError("recognition_window_does_not_match_line")
        if tokens(text) == tokens(current.get("text", "")):
            skipped["unchanged"] += 1
            continue
        # Preserve layout and future presentation metadata. Evidence depending
        # on the old lyric must not travel with a replacement transcription.
        stale_fields = {"words", "review", "alignment_score", "recognition_score",
                        "line_signals_v2", "gap_recovered", "repetition_group",
                        "acoustic_corrected", "reference_fingerprint"}
        stale_prefixes = ("ctc_", "content_", "provider_", "evidence_", "timing_", "live_lexical_")
        proposed = {key: deepcopy(value) for key, value in current.items()
                    if key not in stale_fields and not key.startswith(stale_prefixes)}
        proposed["text"] = text.strip()
        proposed["word_alignment_status"] = "not_certified_after_text_change"
        candidates.append({
            "kind": "operator_review_candidate", "id": digest(item),
            "suggestion_type": "text", "start": current["start"], "end": current["end"],
            "current_segments": [current], "proposed_segments": [proposed],
            "confidence": "low", "reasons": ["local_asr_disagreement_requires_listening"],
            "source_families": ["whisper"],
            "selector_policy": "local-genly-experimental-human-review-v1",
        })
    proposal, telemetry = build_operator_review_proposal(song["segments"], text_candidates=candidates)
    if proposal:
        proposal["local_genly"] = {
            "source": source_binding(song), "evidence_sha256": digest(observations),
            "model_ids": sorted({o["model_id"] for o in observations if o.get("model_id")}),
            "correctness_certified": False, "timing_verified": False,
        }
    return proposal, {**telemetry, "skipped": skipped}


def verify_local_adapter(model_path: Path, adapter_path: Path) -> dict:
    """Only load a measured local adapter with a matching training receipt."""
    config = json.loads((adapter_path / "adapter_config.json").read_text())
    base = Path(config.get("base_model_name_or_path") or "")
    receipt = json.loads((adapter_path.parent / "report.json").read_text())
    if (not base.is_absolute() or base.resolve() != model_path.resolve()
            or receipt.get("snapshot_revision") != model_path.name):
        raise ValueError("adapter_base_checkpoint_mismatch")
    if (receipt.get("status") not in {"smoke_completed", "trained_local_candidate"} or not receipt.get("finite_gradients")
            or receipt.get("adapter_sha256") != file_sha(adapter_path / "adapter_model.safetensors")):
        raise ValueError("adapter_training_receipt_mismatch")
    if receipt["status"] == "trained_local_candidate":
        if (receipt.get("schema") != "genly-local-asr-training-v1"
                or receipt.get("base_weights_sha256") != file_sha(model_path / "model.safetensors")
                or receipt.get("base_config_sha256") != file_sha(model_path / "config.json")
                or receipt.get("adapter_config_sha256") != file_sha(adapter_path / "adapter_config.json")
                or not receipt.get("selected_validation") or not receipt.get("baseline_validation")
                or not isinstance(receipt.get("selected_step"), int) or receipt["selected_step"] <= 0):
            raise ValueError("adapter_training_provenance_mismatch")
    return receipt


class LocalWhisper:
    """Explicit local checkpoint; never calls hosted providers or downloads weights."""
    def __init__(self, model_path: Path, *, adapter_path: Path | None = None, device: str = "cpu"):
        import torch
        from transformers import WhisperForConditionalGeneration, WhisperProcessor
        if not model_path.is_dir():
            raise ValueError("local_model_directory_required")
        if device not in {"cpu", "mps"}:
            raise ValueError("unsupported_local_device")
        if device == "mps" and not torch.backends.mps.is_available():
            raise ValueError("mps_unavailable")
        weights = sorted(model_path.glob("*.safetensors")) or sorted(model_path.glob("pytorch_model*.bin"))
        if not weights:
            raise ValueError("local_weights_missing")
        if adapter_path:
            verify_local_adapter(model_path, adapter_path)
        identity = {"weights": [file_sha(p) for p in weights],
                    "configuration": {p.name: file_sha(p) for p in sorted(model_path.glob("*.json"))}}
        self.processor = WhisperProcessor.from_pretrained(str(model_path), local_files_only=True)
        self.model = WhisperForConditionalGeneration.from_pretrained(str(model_path), local_files_only=True)
        if adapter_path:
            from peft import PeftModel
            identity["adapter"] = file_sha(adapter_path / "adapter_model.safetensors")
            identity["adapter_config"] = file_sha(adapter_path / "adapter_config.json")
            self.model = PeftModel.from_pretrained(self.model, str(adapter_path), local_files_only=True)
        self.model.to(device).eval()
        self.device = device
        self.model_id = "local-whisper-" + digest(identity)

    def listen(self, clip: Path) -> str:
        import soundfile as sf
        import torch
        audio, sample_rate = sf.read(clip, dtype="float32")
        if sample_rate != 16000 or audio.ndim != 1:
            raise ValueError("expected_mono_16khz")
        inputs = self.processor(audio, sampling_rate=16000, return_tensors="pt", return_attention_mask=True)
        with torch.inference_mode():
            ids = self.model.generate(inputs.input_features.to(self.device),
                                      attention_mask=inputs.attention_mask.to(self.device),
                                      language="es", task="transcribe",
                                      max_new_tokens=192, do_sample=False)
        return self.processor.batch_decode(ids, skip_special_tokens=True)[0].strip()


def review(song: dict, audio_path: Path, output: Path, *, listener, ranking: list[dict],
           max_lines: int = 6) -> dict:
    song = normalize_snapshot(song)
    if not 1 <= max_lines <= 64:
        raise ValueError("max_lines must be 1..64")
    if file_sha(audio_path) != song["audio_sha256"]:
        raise ValueError("audio_identity_changed")
    duration = probe(audio_path)
    for row in song["segments"]:
        start, end = float(row["start"]), float(row["end"])
        if not all(math.isfinite(v) for v in (start, end)) or not 0 <= start < end <= duration:
            raise ValueError("invalid_segment_clock")
    output.mkdir(parents=True, mode=0o700, exist_ok=True)
    output.chmod(0o700)
    observations, skipped, processed = [], [], set()
    started = time.monotonic()
    for item in ranking:
        index = item["line_index"]
        if type(index) is not int or not 0 <= index < len(song["segments"]) or index in processed:
            raise ValueError("invalid_ranking")
        processed.add(index)
        row = song["segments"][index]
        if row.get("locked") or row.get("operator_locked"):
            skipped.append({"line_index": index, "reason": "locked"})
            continue
        if row["end"] - row["start"] > 24:
            skipped.append({"line_index": index, "reason": "line_exceeds_clip_budget"})
            continue
        if len(observations) >= max_lines:
            break
        window = {"start": row["start"], "end": row["end"]}
        clip = output / (digest([source_binding(song), index, window]) + ".wav")
        # Do not trust a previously existing cache file without a stored hash.
        if clip.exists():
            raise ValueError("clip_exists_choose_new_output_directory")
        clip_sha = extract_clip(audio_path, window, clip)
        observation = {"source": source_binding(song), "line_index": index,
                       "window": window, "clip_sha256": clip_sha,
                       "model_id": listener.model_id, "reference_conditioned": False}
        try:
            observation.update(status="recognized", text=listener.listen(clip))
        except Exception as exc:
            observation.update(status="failed", error_type=type(exc).__name__)
        observations.append(observation)
    proposal, telemetry = prepare_proposal(song, observations)
    complete = len(observations) == len(song["segments"]) and all(
        item["status"] == "recognized" for item in observations)
    return {"schema": "local-genly-review-v1", "source": source_binding(song),
            "status": "complete" if complete else "partial", "model_id": listener.model_id,
            "observations": observations, "ranking": ranking, "skipped": skipped,
            "proposal": proposal, "telemetry": telemetry,
            "elapsed_seconds": round(time.monotonic() - started, 3),
            "coverage": {"processed_lines": len(observations), "total_lines": len(song["segments"]),
                         "recognized_lines": sum(o["status"] == "recognized" for o in observations)},
            "timing_verified": False, "automatic_apply_allowed": False,
            "hosted_provider_calls": 0, "audio_uploaded": False}


def publish_review(db, song: dict, report: dict) -> dict:
    """Opt-in bridge to the existing editor apply/dismiss flow, never auto-apply.

    Caller owns authentication/tenant authorization and transaction. No CLI
    enables publication. This uses the same revision/audio checks as other
    operator proposals and never replaces a pending batch.
    """
    song = normalize_snapshot(song)
    assert_current(report, song)
    proposal, telemetry = prepare_proposal(song, report["observations"])
    if proposal is None:
        return {"published": False, "reason": "no_proposals", "telemetry": telemetry}
    from database import EditorDocument, Job
    from editor import persist_operator_review_proposal_if_current
    from transcription_quality import segments_hash
    job = db.query(Job).filter(Job.job_id == song["job_id"]).with_for_update().first()
    if job is None or job.approved_at or job.status in {"lyrics_approved", "done"}:
        return {"published": False, "reason": "job_missing_or_approved"}
    document = db.query(EditorDocument).filter(EditorDocument.job_id == song["job_id"]).with_for_update().first()
    if document is None or document.quality_proposal:
        return {"published": False, "reason": "document_missing_or_pending_proposal"}
    published = persist_operator_review_proposal_if_current(
        db, job_id=song["job_id"], expected_revision=song["segments_revision"],
        expected_segments_hash=segments_hash(song["segments"]),
        expected_audio_revision=song["audio_revision"], expected_audio_sha256=song["audio_sha256"],
        proposal=proposal)
    return {"published": bool(published), "telemetry": telemetry}


def quality_ranking(segments: list[dict]) -> dict | None:
    """Optional CPU-only diagnostics for the existing revision-bound quality UI.

    Scores are historical editorial risk, never candidate correctness. Loading
    errors fail open for the normal pipeline, but remain visible as unavailable.
    """
    if os.environ.get("GENLY_LOCAL_REVIEW_RISK_ENABLED", "0").lower() not in {"1", "true", "yes", "on"}:
        return None
    path = os.environ.get("GENLY_LOCAL_REVIEW_RISK_MODEL_PATH", "")
    try:
        from local_correction_selector import load_artifact, rank_segments
        artifact = load_artifact(path, expected_sha256=os.environ.get("GENLY_LOCAL_REVIEW_RISK_SHA256") or None)
        ranking = rank_segments(artifact, segments)
        return {"available": True, "model_id": artifact["model_id"], "ranking": ranking,
                "capability": "rank_editorial_review_risk", "experimental": True,
                "mutated_segments": False, "candidate_correctness_estimated": False}
    except Exception as exc:
        return {"available": False, "reason": "local_risk_artifact_unavailable",
                "error_type": type(exc).__name__, "mutated_segments": False}
