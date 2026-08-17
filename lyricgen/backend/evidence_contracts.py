"""Immutable v6 contracts for lyric evidence and provider provenance.

The objects in this module intentionally contain no lyric text.  Raw provider
rows may remain in the tenant-scoped transcription document for backwards
compatibility, while logs and analytics must use :func:`analytics_projection`.

Content recognition and timing alignment are different claims:

* catalogue/reference text is a content *candidate*, never an ASR witness;
* CTC/forced alignment is a timing witness, never recognition evidence;
* transformed views of one recording/model share one correlated family.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
import hashlib
import hmac
import json
import math
import os
import re
import unicodedata
from typing import Any, Mapping, Sequence


SCHEMA_VERSION = "lyrics-evidence-v6"
FINGERPRINT_VERSION = "hmac-sha256:v1"
PROVENANCE_ATTESTATION_VERSION = "content-provenance-hmac:v2"
MIN_HMAC_SECRET_BYTES = 32
MIN_HMAC_SECRET_DISTINCT_BYTES = 12


def strong_hmac_secret_bytes(value: Any) -> bytes | None:
    """Validate enough key material for privacy-preserving HMAC identities.

    Length alone would accept ``"a" * 32``.  The diversity floor is not a
    randomness proof, but it rejects placeholders/repeated secrets while
    remaining compatible with 32-byte random values encoded as text, hex or
    base64.  Invalid configuration always abstains.
    """
    raw = str(value or "").encode("utf-8")
    if len(raw) < MIN_HMAC_SECRET_BYTES:
        return None
    if len(set(raw)) < MIN_HMAC_SECRET_DISTINCT_BYTES:
        return None
    return raw


def _hmac_key() -> tuple[str, bytes] | None:
    """Return the versioned privacy key without ever inventing a weak fallback."""
    secret = strong_hmac_secret_bytes(
        os.environ.get("QUALITY_CONTENT_FINGERPRINT_HMAC_KEY")
        or os.environ.get("QUALITY_CONTENT_ATTESTATION_KEY")
        or os.environ.get("QUALITY_LEARNING_HMAC_KEY")
        or ""
    )
    if secret is None:
        return None
    key_id = str(
        os.environ.get("QUALITY_CONTENT_FINGERPRINT_HMAC_KEY_ID")
        or os.environ.get("QUALITY_LEARNING_HMAC_KEY_ID")
        or "v1"
    ).strip().lower()
    if not re.fullmatch(r"[a-z0-9_.-]{1,32}", key_id):
        return None
    return key_id, secret


def _versioned_hmac(namespace: str, value: Any) -> str | None:
    key = _hmac_key()
    if key is None:
        return None
    key_id, secret = key
    payload = namespace.encode("utf-8") + b"\x00" + _canonical_json(value)
    digest = hmac.new(secret, payload, hashlib.sha256).hexdigest()
    return f"{FINGERPRINT_VERSION}:{key_id}:{digest}"


def _canonical_json(value: Any) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        default=str,
    ).encode("utf-8")


class SourceKind(str, Enum):
    ASR = "asr"
    REFERENCE = "reference"
    CATALOG = "catalog"
    OPERATOR = "operator"
    ALIGNMENT = "alignment"
    UNKNOWN = "unknown"


class EvidenceRole(str, Enum):
    ASR_WITNESS = "asr_witness"
    CONTENT_CANDIDATE = "content_candidate"
    TIMING_WITNESS = "timing_witness"
    DIAGNOSTIC = "diagnostic"


def _normalized(value: Any) -> str:
    text = unicodedata.normalize("NFKD", str(value or "").casefold())
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    return re.sub(r"[^a-z0-9._:-]+", "_", text).strip("_")


def _finite_score(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(number) or not 0.0 <= number <= 1.0:
        return None
    return round(number, 6)


def _canonical_sha256(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value)).hexdigest()


def _text_sha256(value: Any) -> str:
    return hashlib.sha256(str(value or "").encode("utf-8")).hexdigest()


_REFERENCE_MARKERS = {
    "reference", "anchor", "supplied", "official", "operator_reference",
    "user_reference", "lyrics_reference",
}
_CATALOG_MARKERS = {
    "catalog", "catalogue", "lrclib", "genius", "musixmatch", "gemini",
    "lyrics_db", "lyrics_api",
}
_OPERATOR_MARKERS = {"operator", "editor", "human", "approved"}
_ALIGNMENT_MARKERS = {
    "ctc", "align", "alignment", "forced_align", "forced_alignment",
    "anchor_ctc", "whisperx_alignment",
}
_ASR_MARKERS = {
    "asr", "whisper", "whisperx", "speech_to_text", "speech2text",
    "openai_transcription", "scribe", "deepgram", "transcribe",
}


def classify_source(source: Any) -> SourceKind:
    """Classify a source fail-closed, with reference markers taking priority."""
    value = _normalized(source)
    markers = {part for part in re.split(r"[._:-]+", value) if part}
    if any(marker in value for marker in _CATALOG_MARKERS):
        return SourceKind.CATALOG
    if any(marker in value for marker in _REFERENCE_MARKERS):
        return SourceKind.REFERENCE
    if any(marker in value for marker in _OPERATOR_MARKERS):
        return SourceKind.OPERATOR
    if any(marker in value for marker in _ALIGNMENT_MARKERS):
        return SourceKind.ALIGNMENT
    if value in _ASR_MARKERS or markers.intersection(_ASR_MARKERS):
        return SourceKind.ASR
    return SourceKind.UNKNOWN


def is_reference_source(source: Any) -> bool:
    return classify_source(source) in {
        SourceKind.REFERENCE, SourceKind.CATALOG, SourceKind.OPERATOR,
    }


def reference_fingerprint(
    text: Any,
    *,
    source: Any = "reference",
    reference_id: Any = None,
) -> str | None:
    """Return a keyed, versioned identity for reference content.

    Plain SHA-256 is intentionally forbidden: catalogue lyrics are low entropy
    and therefore dictionary-reversible.  Missing/invalid key configuration
    abstains instead of silently weakening privacy.
    """
    return _versioned_hmac("reference-content", {
        "source": _normalized(source),
        "reference_id": str(reference_id or ""),
        "text": str(text or ""),
    })


def privacy_fingerprint(namespace: str, value: Any) -> str | None:
    """Public helper for non-reversible analytical identities."""
    return _versioned_hmac(str(namespace or "unknown"), value)


def _lineage_rows(segment: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    raw = segment.get("evidence_lineage") or []
    if isinstance(raw, Mapping):
        return [raw]
    if isinstance(raw, Sequence) and not isinstance(raw, (str, bytes)):
        return [item for item in raw if isinstance(item, Mapping)]
    return []


@dataclass(frozen=True)
class ModelViewLineage:
    provider: str
    model: str
    model_revision: str
    view: str
    transformation: str
    parent_audio_sha256: str | None
    correlated_family: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "provider": self.provider,
            "model": self.model,
            "model_revision": self.model_revision,
            "view": self.view,
            "transformation": self.transformation,
            "parent_audio_sha256": self.parent_audio_sha256,
            "correlated_family": self.correlated_family,
        }


def model_view_lineage(
    segment: Mapping[str, Any] | None = None,
    *,
    source: Any = "unknown",
    provider: Any = None,
    model: Any = None,
    model_revision: Any = None,
    view: Any = None,
    transformation: Any = None,
    parent_audio_sha256: Any = None,
    correlated_family: Any = None,
) -> ModelViewLineage:
    """Build lineage whose family is stable across correlated audio views."""
    item = segment or {}
    rows = _lineage_rows(item)
    row = rows[0] if rows else {}
    provider_value = _normalized(
        provider or row.get("provider") or item.get("provider") or source
    ) or "unknown"
    model_value = _normalized(
        model or row.get("model") or item.get("model")
        or item.get("asr_model") or item.get("ctc_model")
    ) or "unknown"
    revision_value = _normalized(
        model_revision or row.get("model_revision") or row.get("revision")
        or item.get("model_revision") or item.get("ctc_model_revision")
    ) or "unknown"
    view_value = _normalized(
        view or row.get("view") or item.get("audio_view") or "mix"
    ) or "mix"
    transform_value = _normalized(
        transformation or row.get("transformation")
        or item.get("audio_transformation") or "original"
    ) or "original"
    parent_hash = str(
        parent_audio_sha256 or row.get("parent_audio_sha256")
        or item.get("parent_audio_sha256") or item.get("audio_sha256") or ""
    ).strip().lower() or None
    explicit_family = _normalized(
        correlated_family or row.get("correlated_family") or row.get("family")
        or item.get("correlated_family")
    )
    # Deliberately exclude view/transformation: stem, mix, residual and slow
    # variants from the same parent/model are correlated, not independent votes.
    family = explicit_family or privacy_fingerprint(
        "correlated-model-audio-family",
        {
            "provider": provider_value,
            "model": model_value,
            "model_revision": revision_value,
            "parent_audio_sha256": parent_hash or "unknown-parent",
        },
    ) or "unknown"
    return ModelViewLineage(
        provider=provider_value,
        model=model_value,
        model_revision=revision_value,
        view=view_value,
        transformation=transform_value,
        parent_audio_sha256=parent_hash,
        correlated_family=family,
    )


@dataclass(frozen=True)
class FrozenProviderOutput:
    """Non-plaintext identity and score summary of one raw provider row."""

    output_sha256: str
    text_sha256: str
    output_fingerprint: str | None
    text_fingerprint: str | None
    text_length: int
    token_count: int
    start: float
    end: float
    word_count: int
    mean_recognition_score: float | None
    min_recognition_score: float | None

    def to_dict(self) -> dict[str, Any]:
        return {
            "output_sha256": self.output_sha256,
            "text_sha256": self.text_sha256,
            "output_fingerprint": self.output_fingerprint,
            "text_fingerprint": self.text_fingerprint,
            "text_length": self.text_length,
            "token_count": self.token_count,
            "start": self.start,
            "end": self.end,
            "word_count": self.word_count,
            "mean_recognition_score": self.mean_recognition_score,
            "min_recognition_score": self.min_recognition_score,
        }


def freeze_provider_output(provider_output: Mapping[str, Any]) -> FrozenProviderOutput:
    words = [
        dict(word) for word in (provider_output.get("words") or [])
        if isinstance(word, Mapping)
    ]
    scores = []
    for word in words:
        score = _finite_score(word.get("score", word.get("probability")))
        if score is not None:
            scores.append(score)
    explicit_mean = None
    for key in ("mean_recognition_score", "recognition_score", "mean_score", "asr_confidence"):
        explicit_mean = _finite_score(provider_output.get(key))
        if explicit_mean is not None:
            break
    explicit_min = None
    for key in ("min_recognition_score", "min_score"):
        explicit_min = _finite_score(provider_output.get(key))
        if explicit_min is not None:
            break
    text = str(provider_output.get("text") or "")
    try:
        start = round(float(provider_output.get("start") or 0.0), 3)
    except (TypeError, ValueError):
        start = 0.0
    try:
        end = round(float(provider_output.get("end") or start), 3)
    except (TypeError, ValueError):
        end = start
    raw = {
        "text": text,
        "start": start,
        "end": end,
        "words": words,
        "recognition_score": explicit_mean,
        "min_recognition_score": explicit_min,
    }
    return FrozenProviderOutput(
        output_sha256=_canonical_sha256(raw),
        text_sha256=_text_sha256(text),
        output_fingerprint=privacy_fingerprint("provider-output", raw),
        text_fingerprint=privacy_fingerprint("provider-text", text),
        text_length=len(text),
        token_count=len(re.findall(r"[^\W_]+", text, flags=re.UNICODE)),
        start=start,
        end=end,
        word_count=len(words),
        mean_recognition_score=(
            round(sum(scores) / len(scores), 6) if scores else explicit_mean
        ),
        min_recognition_score=min(scores) if scores else explicit_min,
    )


def content_provenance_attestation(
    *, source: Any, source_kind: SourceKind | str, role: EvidenceRole | str,
    raw_output_sha256: str, reference_fingerprint_value: str | None,
    lineage: Mapping[str, Any] | None,
) -> str | None:
    """Attest provenance, provider output identity, and its full lineage."""
    key = _hmac_key()
    if key is None:
        return None
    key_id, secret = key
    payload = _canonical_json({
        "schema": SCHEMA_VERSION,
        "source": str(source),
        "source_kind": (
            source_kind.value if isinstance(source_kind, SourceKind)
            else str(source_kind)
        ),
        "role": role.value if isinstance(role, EvidenceRole) else str(role),
        "raw_output_sha256": str(raw_output_sha256),
        "reference_fingerprint": reference_fingerprint_value,
        "lineage": dict(lineage or {}),
    })
    digest = hmac.new(
        secret, b"content-provenance\x00" + payload, hashlib.sha256,
    ).hexdigest()
    return f"{PROVENANCE_ATTESTATION_VERSION}:{key_id}:{digest}"


def verify_content_provenance_attestation(value: Mapping[str, Any]) -> bool:
    """Verify a previously frozen provenance row; malformed data abstains."""
    token = str(value.get("attestation") or "")
    key = _hmac_key()
    if key is None:
        return False
    key_id, _secret = key
    expected = content_provenance_attestation(
        source=value.get("source"),
        source_kind=str(value.get("source_kind") or ""),
        role=str(value.get("role") or ""),
        raw_output_sha256=str(value.get("raw_output_sha256") or ""),
        reference_fingerprint_value=(
            str(value.get("reference_fingerprint"))
            if value.get("reference_fingerprint") else None
        ),
        lineage=(
            value.get("lineage")
            if isinstance(value.get("lineage"), Mapping) else None
        ),
    )
    return bool(
        expected
        and token.startswith(f"{PROVENANCE_ATTESTATION_VERSION}:{key_id}:")
        and hmac.compare_digest(token, expected)
    )


@dataclass(frozen=True)
class ContentProvenance:
    source: str
    source_kind: SourceKind
    role: EvidenceRole
    lineage: ModelViewLineage
    raw_output_sha256: str
    reference_fingerprint: str | None
    attestation: str | None

    @property
    def is_asr_witness(self) -> bool:
        return self.role is EvidenceRole.ASR_WITNESS

    def to_dict(self) -> dict[str, Any]:
        return {
            "source": self.source,
            "source_kind": self.source_kind.value,
            "role": self.role.value,
            "is_asr_witness": self.is_asr_witness,
            "lineage": self.lineage.to_dict(),
            "raw_output_sha256": self.raw_output_sha256,
            "reference_fingerprint": self.reference_fingerprint,
            "attestation": self.attestation,
            "attested": bool(self.attestation),
        }


@dataclass(frozen=True)
class TimingProvenance:
    source: str
    source_kind: SourceKind
    role: EvidenceRole
    lineage: ModelViewLineage

    def to_dict(self) -> dict[str, Any]:
        return {
            "source": self.source,
            "source_kind": self.source_kind.value,
            "role": self.role.value,
            "lineage": self.lineage.to_dict(),
        }


@dataclass(frozen=True)
class LineEvidenceContract:
    schema: str
    content_provenance: ContentProvenance
    timing_provenance: TimingProvenance
    recognition_score: float | None
    alignment_score: float | None
    reference_fingerprint: str | None
    frozen_provider_output: FrozenProviderOutput
    provider_output_integrity: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema": self.schema,
            "content_provenance": self.content_provenance.to_dict(),
            "timing_provenance": self.timing_provenance.to_dict(),
            "recognition_score": self.recognition_score,
            "alignment_score": self.alignment_score,
            "reference_fingerprint": self.reference_fingerprint,
            "frozen_provider_output": self.frozen_provider_output.to_dict(),
            "provider_output_integrity": self.provider_output_integrity,
        }


def _existing_reference_fingerprint(value: Any) -> str | None:
    raw = str(value or "").strip().lower()
    if re.fullmatch(
        rf"{re.escape(FINGERPRINT_VERSION)}:[a-z0-9_.-]{{1,32}}:[0-9a-f]{{64}}",
        raw,
    ):
        return raw
    return None


def resolve_content_source(
    segment: Mapping[str, Any], *, trusted_source: Any = None,
    reference_text: Any = None,
) -> str:
    """Resolve provenance without accepting an upward claim from the payload.

    A trusted adapter may label its own output.  Existing payload provenance is
    reusable only when its HMAC verifies.  A reference signal always dominates
    an ASR-looking label because accepting the inverse would let supplied lyrics
    manufacture their own recognition confidence.
    """
    trusted = str(trusted_source or "").strip()
    if str(reference_text or "").strip():
        return (
            trusted if is_reference_source(trusted)
            else "catalog_reference"
        )
    existing = segment.get("content_provenance")
    if isinstance(existing, Mapping) and verify_content_provenance_attestation(existing):
        return str(existing.get("source") or "unknown")
    # Downward declarations are safe to honor: they can remove ASR authority,
    # never create it.  ASR claims in the payload are deliberately ignored.
    for candidate in (
        segment.get("content_source"),
        (existing or {}).get("source") if isinstance(existing, Mapping) else None,
        (segment.get("provider_evidence") or {}).get("source")
        if isinstance(segment.get("provider_evidence"), Mapping) else None,
    ):
        if is_reference_source(candidate):
            return str(candidate)
    return trusted or "unknown"


def build_line_evidence_contract(
    segment: Mapping[str, Any],
    *,
    provider_output: Mapping[str, Any] | None = None,
    content_source: Any = None,
    timing_source: Any = None,
    reference_text: Any = None,
    reference_id: Any = None,
    provider: Any = None,
    model: Any = None,
    model_revision: Any = None,
    view: Any = None,
    transformation: Any = None,
    parent_audio_sha256: Any = None,
    correlated_family: Any = None,
) -> LineEvidenceContract:
    raw = provider_output or segment
    source = resolve_content_source(
        segment,
        trusted_source=content_source,
        reference_text=reference_text,
    )
    kind = classify_source(source)
    frozen = freeze_provider_output(raw)
    expected_output_hash = None
    if raw.get("schema") == SCHEMA_VERSION or isinstance(
        raw.get("frozen_provider_output"), Mapping
    ):
        expected_output_hash = _safe_sha256(
            raw.get("raw_output_sha256")
            or (raw.get("frozen_provider_output") or {}).get("output_sha256")
        )
    integrity_valid = (
        expected_output_hash is None
        or expected_output_hash == frozen.output_sha256
    )
    lineage = model_view_lineage(
        segment,
        source=source,
        provider=provider,
        model=model,
        model_revision=model_revision,
        view=view,
        transformation=transformation,
        parent_audio_sha256=parent_audio_sha256,
        correlated_family=correlated_family,
    )
    reference_kind = kind in {
        SourceKind.REFERENCE, SourceKind.CATALOG, SourceKind.OPERATOR,
    }
    fingerprint = _existing_reference_fingerprint(
        segment.get("reference_fingerprint") or raw.get("reference_fingerprint")
    )
    if reference_kind and fingerprint is None:
        fingerprint = reference_fingerprint(
            reference_text if reference_text is not None else raw.get("text"),
            source=source,
            reference_id=reference_id,
        )
    role = (
        EvidenceRole.ASR_WITNESS
        if kind is SourceKind.ASR and integrity_valid
        else EvidenceRole.DIAGNOSTIC
        if kind is SourceKind.ASR
        else EvidenceRole.CONTENT_CANDIDATE
    )
    recognition = frozen.mean_recognition_score if role is EvidenceRole.ASR_WITNESS else None
    provenance_attestation = content_provenance_attestation(
        source=source,
        source_kind=kind,
        role=role,
        raw_output_sha256=frozen.output_sha256,
        reference_fingerprint_value=fingerprint,
        lineage=lineage.to_dict(),
    )

    timing_name = str(
        timing_source or segment.get("timing_source")
        or ("ctc_alignment" if any(
            segment.get(key) is not None
            for key in ("alignment_score", "ctc_mean_score", "ctc_score", "ctc_confidence")
        ) else "provider_timestamps")
    )
    timing_kind = classify_source(timing_name)
    alignment = None
    for key in ("alignment_score", "ctc_mean_score", "ctc_score", "ctc_confidence"):
        alignment = _finite_score(segment.get(key))
        if alignment is not None:
            break
    timing_lineage = model_view_lineage(
        segment,
        source=timing_name,
        provider=segment.get("timing_provider") or provider,
        model=segment.get("timing_model") or segment.get("ctc_model") or model,
        model_revision=(
            segment.get("timing_model_revision")
            or segment.get("ctc_model_revision") or model_revision
        ),
        view=segment.get("timing_view") or view,
        transformation=segment.get("timing_transformation") or transformation,
        parent_audio_sha256=parent_audio_sha256,
        correlated_family=segment.get("timing_correlated_family"),
    )
    return LineEvidenceContract(
        schema=SCHEMA_VERSION,
        content_provenance=ContentProvenance(
            source=source,
            source_kind=kind,
            role=role,
            lineage=lineage,
            raw_output_sha256=frozen.output_sha256,
            reference_fingerprint=fingerprint,
            attestation=provenance_attestation,
        ),
        timing_provenance=TimingProvenance(
            source=timing_name,
            source_kind=timing_kind,
            role=EvidenceRole.TIMING_WITNESS,
            lineage=timing_lineage,
        ),
        recognition_score=recognition,
        alignment_score=alignment,
        reference_fingerprint=fingerprint,
        frozen_provider_output=frozen,
        provider_output_integrity=integrity_valid,
    )


_IDENTIFIER_ALLOWLISTS = {
    "schema": {SCHEMA_VERSION},
    "source_kind": {item.value for item in SourceKind},
    "role": {item.value for item in EvidenceRole},
    "source": {
        "unknown", "asr", "reference", "catalog", "operator",
        "operator_reference", "catalog_reference", "user_reference",
        "lyrics_reference", "provider_timestamps", "ctc_alignment",
        "forced_alignment", "anchor_ctc", "whisperx_alignment",
        "whisper", "whisperx", "whisperx_primary", "whisper_raw",
        "whisper_lrclib", "whisper_lrclib_rec", "whisper_gemini_rec",
        "whisperx_reconciled", "whisperx_lrclib", "whisper_align",
        "forced_align", "cleanup_anchored", "synced_scaffold", "ctc_align",
        "openai_transcription", "gemini", "gemini_audio",
    },
    "provider": {
        "unknown", "asr", "openai", "whisper", "whisperx", "gemini",
        "gemini_audio", "ctc", "cureau", "facebook", "meta", "demucs",
    },
    "model": {
        "unknown", "whisper", "whisperx", "whisper-1",
        "gpt-4o-transcribe", "gpt-4o-mini-transcribe", "gemini",
        "gemini-2.5-flash", "xls-r", "xls-r-phone-event-v1",
        "wav2vec2", "wav2vec2-xls-r-300m", "demucs",
    },
    "view": {"unknown", "mix", "stem", "residual", "slow", "slowed_stem"},
    "transformation": {
        "unknown", "original", "vocal_separation", "residual",
        "tempo_slow", "slow", "none",
    },
}


def _safe_identifier(field: str, value: Any) -> str:
    normalized = _normalized(value) or "unknown"
    if normalized in _IDENTIFIER_ALLOWLISTS.get(field, set()):
        return normalized
    if field == "model_revision" and re.fullmatch(
        r"(?:[0-9a-f]{7,64}|v?[0-9]+(?:\.[0-9]+){0,3})", normalized,
    ):
        return normalized
    if field == "correlated_family" and (
        normalized == "source_audio_demucs"
        or re.fullmatch(r"family-v6-[0-9a-f]{24}", normalized)
    ):
        return normalized
    return "unknown"


def _safe_sha256(value: Any) -> str | None:
    raw = str(value or "").strip().lower()
    return raw if re.fullmatch(r"[0-9a-f]{64}", raw) else None


def _safe_number(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return round(number, 6) if math.isfinite(number) else None


def _safe_count(value: Any) -> int | None:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return None
    return max(0, number)


def _safe_lineage_projection(value: Any) -> dict[str, Any]:
    row = value if isinstance(value, Mapping) else {}
    parent_hash = _safe_sha256(row.get("parent_audio_sha256"))
    family = str(row.get("correlated_family") or "").strip()
    # Re-key even a token-shaped family supplied by a payload. Accepting an
    # arbitrary ``hmac-sha256:v1:*`` string verbatim would create a covert
    # analytics channel and would not prove that our key produced it.
    family_fingerprint = (
        privacy_fingerprint("correlated-family", family) if family else None
    )
    return {
        "provider": _safe_identifier("provider", row.get("provider")),
        "model": _safe_identifier("model", row.get("model")),
        "model_revision": _safe_identifier(
            "model_revision", row.get("model_revision"),
        ),
        "view": _safe_identifier("view", row.get("view")),
        "transformation": _safe_identifier(
            "transformation", row.get("transformation"),
        ),
        # A raw audio digest is a stable cross-tenant identifier.  Analytics
        # receives only a keyed, versioned pseudonym and abstains without a
        # configured privacy key.
        "parent_audio_fingerprint": privacy_fingerprint(
            "parent-audio-sha256", parent_hash,
        ) if parent_hash else None,
        "correlated_family_fingerprint": family_fingerprint,
    }


def _safe_provenance_projection(value: Any) -> dict[str, Any]:
    row = value if isinstance(value, Mapping) else {}
    return {
        "source": _safe_identifier("source", row.get("source")),
        "source_kind": _safe_identifier("source_kind", row.get("source_kind")),
        "role": _safe_identifier("role", row.get("role")),
        "is_asr_witness": bool(
            row.get("is_asr_witness") is True
            and row.get("role") == EvidenceRole.ASR_WITNESS.value
            and row.get("source_kind") == SourceKind.ASR.value
        ),
        "lineage": _safe_lineage_projection(row.get("lineage")),
        "reference_fingerprint": _existing_reference_fingerprint(
            row.get("reference_fingerprint")
        ),
        "attested": verify_content_provenance_attestation(row),
    }


def analytics_projection(value: LineEvidenceContract | Mapping[str, Any]) -> dict[str, Any]:
    """Return the only v6 representation safe for logs/global analytics.

    This is deliberately an allow-list.  It never serializes lyric text,
    reference text, words, tokens, provider payloads, or user-supplied IDs.
    """
    if isinstance(value, LineEvidenceContract):
        contract = value.to_dict()
    else:
        contract = {
            "schema": value.get("evidence_schema") or value.get("schema") or SCHEMA_VERSION,
            "content_provenance": value.get("content_provenance") or {},
            "timing_provenance": value.get("timing_provenance") or {},
            "recognition_score": value.get("recognition_score"),
            "alignment_score": value.get("alignment_score"),
            "reference_fingerprint": value.get("reference_fingerprint"),
            "provider_output_integrity": value.get("provider_output_integrity"),
            "frozen_provider_output": (
                (value.get("provider_evidence") or {}).get("frozen_provider_output")
                or value.get("frozen_provider_output") or {}
            ),
        }
    frozen = contract.get("frozen_provider_output") or {}
    return {
        "schema": _safe_identifier(
            "schema", contract.get("schema") or SCHEMA_VERSION,
        ),
        "content_provenance": _safe_provenance_projection(
            contract.get("content_provenance")
        ),
        "timing_provenance": _safe_provenance_projection(
            contract.get("timing_provenance")
        ),
        "recognition_score": _finite_score(contract.get("recognition_score")),
        "alignment_score": _finite_score(contract.get("alignment_score")),
        "reference_fingerprint": _existing_reference_fingerprint(
            contract.get("reference_fingerprint")
        ),
        "provider_output_integrity": bool(
            contract.get("provider_output_integrity")
        ),
        "frozen_provider_output": {
            "output_fingerprint": _existing_reference_fingerprint(
                frozen.get("output_fingerprint")
            ),
            "text_fingerprint": _existing_reference_fingerprint(
                frozen.get("text_fingerprint")
            ),
            "text_length": _safe_count(frozen.get("text_length")),
            "token_count": _safe_count(frozen.get("token_count")),
            "start": _safe_number(frozen.get("start")),
            "end": _safe_number(frozen.get("end")),
            "word_count": _safe_count(frozen.get("word_count")),
            "mean_recognition_score": _finite_score(
                frozen.get("mean_recognition_score")
            ),
            "min_recognition_score": _finite_score(
                frozen.get("min_recognition_score")
            ),
        },
    }
