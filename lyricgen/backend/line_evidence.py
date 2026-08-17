"""Line-level provenance and conservative evidence risks.

CTC aligns supplied text; it is not an independent witness that the supplied
words were sung.  This module keeps provider confidence separate from CTC
timing confidence and emits review signals without rewriting lyric content.
"""
from __future__ import annotations

import math
import os
import re
import statistics
import unicodedata


def _f(value, default: float = 0.0) -> float:
    try:
        number = float(value)
        return number if math.isfinite(number) else default
    except (TypeError, ValueError):
        return default


def tokens(text: str) -> list[str]:
    value = unicodedata.normalize("NFKD", str(text or "").casefold())
    value = "".join(ch for ch in value if not unicodedata.combining(ch))
    return re.findall(r"[^\W_]+", value, flags=re.UNICODE)


def canonical_content_sequence(events: list[dict]) -> list[str]:
    """Canonical text sequence used by both attester and verifier."""
    return [
        " ".join(tokens(event.get("text") or ""))
        for event in (events or []) if isinstance(event, dict)
    ]


def _scores(words: list[dict]) -> list[float]:
    values = []
    for word in words or []:
        if not isinstance(word, dict):
            continue
        raw = word.get("score", word.get("probability"))
        value = _f(raw, float("nan"))
        if math.isfinite(value) and 0.0 <= value <= 1.0:
            values.append(value)
    return values


def annotate_provider_evidence(segments: list[dict], *, source: str = "asr") -> list[dict]:
    """Freeze raw ASR evidence before a timing model replaces word stamps."""
    output: list[dict] = []
    for segment in segments or []:
        if not isinstance(segment, dict):
            continue
        item = dict(segment)
        if not isinstance(item.get("provider_evidence"), dict):
            words = [dict(word) for word in (item.get("words") or [])
                     if isinstance(word, dict)]
            scores = _scores(words)
            item["provider_evidence"] = {
                "source": str(item.get("content_source") or source),
                "text": str(item.get("text") or ""),
                "start": round(_f(item.get("start")), 3),
                "end": round(_f(item.get("end")), 3),
                "words": words,
                "word_count": len(words),
                "mean_score": round(statistics.fmean(scores), 4) if scores else None,
                "min_score": round(min(scores), 4) if scores else None,
            }
        evidence = item["provider_evidence"]
        if item.get("content_asr_score") is None and evidence.get("mean_score") is not None:
            item["content_asr_score"] = evidence["mean_score"]
        if item.get("content_asr_min_score") is None and evidence.get("min_score") is not None:
            item["content_asr_min_score"] = evidence["min_score"]
        item.setdefault("content_source", evidence.get("source") or source)
        output.append(item)
    return output


def freeze_result_provider_evidence(result: dict, *, source: str = "asr") -> dict:
    """Clone one pipeline result and freeze its current provider rows once."""
    if not isinstance(result, dict):
        return result
    frozen = dict(result)
    frozen["segments"] = annotate_provider_evidence(
        result.get("segments") or [], source=source,
    )
    return frozen


def evidence_issues(segments: list[dict]) -> list[dict]:
    """Return conservative, explainable reasons that require human/acoustic review."""
    issues: list[dict] = []
    low_asr = _f(os.environ.get("QUALITY_LINE_ASR_SCORE_MIN", ".40"), .40)
    low_ctc = _f(os.environ.get("QUALITY_LINE_CTC_SCORE_MIN", ".20"), .20)
    items = [segment for segment in (segments or []) if isinstance(segment, dict)]
    for index, segment in enumerate(items):
        start = _f(segment.get("start"))
        end = max(start, _f(segment.get("end"), start))
        provider = segment.get("provider_evidence") or {}
        source_start = _f(
            segment.get("source_start", provider.get("start")), start,
        )
        source_end = _f(segment.get("source_end", provider.get("end")), end)
        base = {
            "segment_index": index,
            "start": start,
            "end": end,
            "source_start": min(start, source_start),
            "source_end": max(end, source_end),
        }
        reasons: list[str] = []

        if int(segment.get("collapsed_repetition") or 0) >= 3 or segment.get(
            "provider_timing_collapsed"
        ):
            reasons.append("provider_timing_collapsed")

        ctc_score = segment.get("ctc_mean_score", segment.get("ctc_score"))
        if ctc_score is not None and _f(ctc_score, 1.0) < low_ctc:
            reasons.append("low_ctc_timing_confidence")

        asr_score = segment.get("content_asr_score")
        if asr_score is not None and _f(asr_score, 1.0) < low_asr:
            reasons.append("low_asr_content_confidence")

        text_count = len(tokens(segment.get("text") or ""))
        provider_count = int(provider.get("word_count") or 0)
        ctc_count = len(segment.get("words") or [])
        observed_count = ctc_count or provider_count
        if text_count and observed_count and abs(text_count - observed_count) > max(
            1, round(text_count * .40)
        ):
            reasons.append("text_word_cardinality_mismatch")

        # A short final utterance separated from the previous lyric and weak
        # in either source is a classic applause/instrument hallucination.
        # It is not deleted: it becomes a bounded acoustic review candidate.
        previous_end = _f(items[index - 1].get("end")) if index else 0.0
        isolated_tail = (
            index == len(items) - 1 and index > 0 and start - previous_end >= 3.0
            and 1 <= text_count <= 4
        )
        weak_content = asr_score is not None and _f(asr_score, 1.0) < low_asr
        weak_timing = ctc_score is not None and _f(ctc_score, 1.0) < max(.25, low_ctc)
        if isolated_tail and (weak_content or weak_timing):
            reasons.append("isolated_tail_low_support")

        if reasons:
            inferred_analysis_end = base["source_end"]
            if "provider_timing_collapsed" in reasons:
                # Reserve three seconds of context on each side inside the
                # quality worker's hard 45s clip. Never trust a following ASR
                # row to bound the refrain: that row may itself be a ghost.
                inferred_analysis_end = start + 39.0
            analysis_end = max(base["source_end"], inferred_analysis_end)
            issues.append({**base, "end": analysis_end, "reasons": sorted(set(reasons))})
    return issues
