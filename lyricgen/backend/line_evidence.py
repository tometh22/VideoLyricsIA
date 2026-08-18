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
from copy import deepcopy

from evidence_contracts import (
    EvidenceRole,
    SCHEMA_VERSION,
    analytics_projection,
    build_line_evidence_contract,
    resolve_content_source,
)


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


def annotate_provider_evidence(
    segments: list[dict],
    *,
    source: str = "asr",
    content_source: str | None = None,
    timing_source: str | None = None,
    reference_text: str | None = None,
    reference_id: str | None = None,
    provider: str | None = None,
    model: str | None = None,
    model_revision: str | None = None,
    view: str | None = None,
    transformation: str | None = None,
    parent_audio_sha256: str | None = None,
    correlated_family: str | None = None,
) -> list[dict]:
    """Freeze provider evidence and attach immutable v6 provenance.

    The original function name, default arguments, return shape and legacy
    ``provider_evidence`` keys remain supported.  V6 additionally separates
    recognition from alignment and fails closed for reference/catalog text.
    """
    output: list[dict] = []
    for segment in segments or []:
        if not isinstance(segment, dict):
            continue
        item = deepcopy(segment)
        if not isinstance(item.get("provider_evidence"), dict):
            words = [deepcopy(word) for word in (item.get("words") or [])
                     if isinstance(word, dict)]
            scores = _scores(words)
            item["provider_evidence"] = {
                "source": str(content_source or item.get("content_source") or source),
                "text": str(item.get("text") or ""),
                "start": round(_f(item.get("start")), 3),
                "end": round(_f(item.get("end")), 3),
                "words": words,
                "word_count": len(words),
                "mean_score": round(statistics.fmean(scores), 4) if scores else None,
                "min_score": round(min(scores), 4) if scores else None,
                "recognition_score": item.get(
                    "recognition_score", item.get("asr_confidence")
                ),
            }
        evidence = deepcopy(item["provider_evidence"])
        resolved_source = resolve_content_source(
            item,
            trusted_source=(content_source if content_source is not None else source),
            reference_text=reference_text,
        )
        # Do not preserve a caller-controlled ASR-looking alias in the legacy
        # private snapshot.  Some older consumers still inspect this field, so
        # it must agree with the attested provenance decision as well.
        evidence["source"] = resolved_source
        # Build recognition identity from the frozen provider row, but timing
        # identity from the current segment (which may already have CTC data).
        contract = build_line_evidence_contract(
            item,
            provider_output=evidence,
            content_source=resolved_source,
            timing_source=timing_source,
            reference_text=reference_text,
            reference_id=reference_id,
            provider=provider,
            model=model,
            model_revision=model_revision,
            view=view,
            transformation=transformation,
            parent_audio_sha256=parent_audio_sha256,
            correlated_family=correlated_family,
        )
        frozen = contract.frozen_provider_output.to_dict()
        evidence.setdefault("schema", SCHEMA_VERSION)
        evidence.setdefault("frozen_provider_output", frozen)
        evidence.setdefault("raw_output_sha256", frozen["output_sha256"])
        evidence.setdefault("text_sha256", frozen["text_sha256"])
        evidence.setdefault(
            "content_role", contract.content_provenance.role.value,
        )
        evidence.setdefault(
            "correlated_family",
            contract.content_provenance.lineage.correlated_family,
        )
        item["provider_evidence"] = evidence
        item["evidence_schema"] = contract.schema
        item["content_provenance"] = contract.content_provenance.to_dict()
        item["timing_provenance"] = contract.timing_provenance.to_dict()
        item["recognition_score"] = contract.recognition_score
        item["alignment_score"] = contract.alignment_score
        item["provider_output_integrity"] = contract.provider_output_integrity

        if contract.reference_fingerprint:
            item["reference_fingerprint"] = contract.reference_fingerprint
        elif item.get("reference_fingerprint") is None:
            item.pop("reference_fingerprint", None)

        if contract.content_provenance.role is EvidenceRole.ASR_WITNESS:
            # Legacy aliases remain available, but derive exclusively from
            # the immutable raw ASR row—not from alignment or references.
            if contract.recognition_score is not None:
                item["content_asr_score"] = contract.recognition_score
            if contract.frozen_provider_output.min_recognition_score is not None:
                item["content_asr_min_score"] = (
                    contract.frozen_provider_output.min_recognition_score
                )
        else:
            # A catalogue/reference score is never allowed to masquerade as
            # recognition evidence, including contaminated legacy payloads.
            item.pop("content_asr_score", None)
            item.pop("content_asr_min_score", None)
        item["content_source"] = resolved_source
        output.append(item)
    return output


def freeze_result_provider_evidence(
    result: dict,
    *,
    source: str = "asr",
    provider: str | None = None,
    model: str | None = None,
    model_revision: str | None = None,
    view: str | None = None,
    transformation: str | None = None,
    parent_audio_sha256: str | None = None,
    correlated_family: str | None = None,
) -> dict:
    """Clone one pipeline result and freeze its current provider rows once.

    A result carrying reference lyrics is conservatively classified as a
    reference candidate unless it declares a more specific content source.
    This closes the legacy path where reconciled catalogue text inherited the
    default ``asr`` label and its alignment score became recognition evidence.
    """
    if not isinstance(result, dict):
        return result
    frozen = dict(result)
    reference_text = result.get("reference_lyrics")
    result_content_source = (
        str(result.get("lyrics_source") or "catalog_reference")
        if str(reference_text or "").strip()
        else source
    )
    frozen["segments"] = annotate_provider_evidence(
        result.get("segments") or [],
        source=source,
        content_source=result_content_source,
        timing_source=result.get("timing_source"),
        reference_text=reference_text,
        reference_id=result.get("reference_id"),
        provider=provider or result.get("provider"),
        model=model or result.get("model") or result.get("asr_model"),
        model_revision=(
            model_revision or result.get("model_revision")
            or result.get("asr_model_revision")
        ),
        view=view or result.get("audio_view"),
        transformation=transformation or result.get("audio_transformation"),
        parent_audio_sha256=(
            parent_audio_sha256 or result.get("parent_audio_sha256")
            or result.get("audio_sha256")
        ),
        correlated_family=correlated_family or result.get("correlated_family"),
    )
    return frozen


def evidence_analytics_projection(segment: dict) -> dict:
    """Public log/analytics projection guaranteed to contain no lyric text."""
    return analytics_projection(segment)


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

        ctc_score = segment.get(
            "alignment_score",
            segment.get("ctc_mean_score", segment.get("ctc_score")),
        )
        if ctc_score is not None and _f(ctc_score, 1.0) < low_ctc:
            reasons.append("low_ctc_timing_confidence")

        provenance = segment.get("content_provenance") or {}
        if provenance:
            asr_score = (
                segment.get("recognition_score")
                if provenance.get("role") == EvidenceRole.ASR_WITNESS.value
                else None
            )
        else:
            # Backwards compatibility for pre-v6 persisted documents.
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
