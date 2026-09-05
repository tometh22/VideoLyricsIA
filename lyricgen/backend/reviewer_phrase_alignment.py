"""Shadow-only phrase localization. CTC is a clock, never a text verifier.

Uses the production pinned Spanish aligner's primitives on the original mix.
No stem timestamp transfer, padding rule, database or network access here.
"""
from __future__ import annotations

import math
import subprocess
import time

from reviewer_shadow import tokens


def phrase_occurrences(text, hypothesis):
    """All lexical occurrences, not a claim about which one was sung here."""
    needle, haystack = tokens(text), tokens(hypothesis)
    if not needle:
        return []
    return [{"token_start": i, "token_end": i + len(needle)}
            for i in range(len(haystack) - len(needle) + 1)
            if haystack[i:i + len(needle)] == needle]


def extend_context(window, *, duration, continuity=False, truncated=False,
                   extension_used=0, maximum_extension=8.0, maximum_duration=24.0):
    """One bounded arm; never mutates a document or silently extends a line."""
    start, end = float(window["start"]), float(window["end"])
    if not all(math.isfinite(x) for x in (start, end, duration, extension_used)):
        raise ValueError("nonfinite_context")
    if not 0 <= start < end <= duration or extension_used < 0:
        raise ValueError("invalid_context")
    remaining = max(0.0, maximum_extension - extension_used)
    target = min(duration, start + maximum_duration, end + remaining)
    expanded = bool((continuity or truncated) and target > end)
    return {**window, "end": target if expanded else end,
            "offset_seconds": start, "expanded": expanded,
            "extension_used": extension_used + (target - end if expanded else 0),
            "extension_reason": "phrase_truncated" if truncated else
                "vocal_continuity" if continuity else "no_extension_evidence",
            "boundary_observed": None}


def align_phrase(audio_path, text, window):
    """Localize audio-supported text using existing CTC, with explicit errors.

    Output words are forced alignment hypotheses, NOT recognition or a
    phonetic endpoint certificate. Caller must separately select occurrences.
    ffmpeg decodes from the original signal's zero; local/global are explicit.
    """
    started = time.monotonic()
    result = {"tool": "production-spanish-ctc", "conditioning_text": text,
              "clock": "original_mix_decoded", "text_certified": False,
              "cross_signal_timestamp_transfer": False, "words": [],
              "window": dict(window), "provider_calls": 0}
    try:
        import numpy as np
        import torch
        import torchaudio.functional as AF
        import ctc_align as ctc

        start, end = float(window["start"]), float(window["end"])
        if not 0 <= start < end or not 1 <= end - start <= 24:
            raise ValueError("alignment_window_budget")
        if not tokens(text):
            raise ValueError("empty_phrase")
        torch.set_num_threads(2)
        model, dictionary, blank_id = ctc._load_model()
        result.update(model=ctc.MODEL_ID, model_revision=ctc.MODEL_REVISION)
        targets, words = ctc.build_targets([text], dictionary,
            model.config.vocab_size, word_sep_id=dictionary.get("|"))
        characters = sum(len(ctc.norm_word(w)) for w in text.split()) or 1
        if sum(n for line, _, n in words if line >= 0) / characters < .60:
            raise ValueError("unsupported_phoneme_or_character_inventory")
        # Decode before trim to avoid container seek/priming inconsistencies.
        decoded = subprocess.run(["ffmpeg", "-v", "error", "-nostdin", "-i",
            str(audio_path), "-af", f"atrim=start={start}:end={end},asetpts=PTS-STARTPTS",
            "-ac", "1", "-ar", str(ctc.SR), "-f", "f32le", "pipe:1"],
            check=True, capture_output=True, timeout=60)
        signal = np.frombuffer(decoded.stdout, dtype="<f4").copy()
        result["decoded_samples"] = len(signal)
        emission = ctc._emissions(model, torch.from_numpy(signal).unsqueeze(0),
                                   blank_id, ctc._star_delta())
        aligned, scores = AF.forced_align(emission.unsqueeze(0),
            torch.tensor(targets, dtype=torch.int32).unsqueeze(0), blank=blank_id)
        spans = [(s.start, s.end, float(s.score)) for s in
                 AF.merge_tokens(aligned[0], scores[0].exp(), blank=blank_id)]
        lines = ctc.spans_to_lines(spans, words, 1, ctc.FRAME / ctc.SR)
        if not lines[0]:
            result["status"] = "unaligned"
        else:
            _, _, aligned_words = lines[0]
            result["words"] = [{"word": w, "local_start": a, "local_end": b,
                "global_start": start + a, "global_end": start + b,
                "offset_seconds": start, "alignment_score": score}
                for w, a, b, score in aligned_words]
            result["status"] = "aligned_hypothesis" if aligned_words else "unaligned"
            result["boundary_near_clip_edge"] = bool(aligned_words and
                end - (start + aligned_words[-1][2]) < .25)
    except Exception as exc:
        result.update(status="tool_error", error_type=type(exc).__name__,
                      error_message=str(exc)[:500])
    result["latency_seconds"] = round(time.monotonic() - started, 3)
    return result
