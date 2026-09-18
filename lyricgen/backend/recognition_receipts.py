"""Lossless storage contract for completed, already-existing Parakeet receipts.

This module does not recognize audio, group tokens into words, select text, or
authenticate provider origin.  It validates two stored JSON byte strings and
keeps those exact bytes in base64 so later code can replay the original record.

An explicit empty receipt list is valid. Exact duplicate packets are collapsed
in first-seen order; two different packets claiming the same result/receipt
identity are rejected. At most eight unique packets and four MiB of decoded raw
JSON bytes are accepted per list. These are storage limits, not quality gates.
"""
from __future__ import annotations

import base64
import binascii
from copy import deepcopy
import hashlib
import json
import math
import re
from typing import Any


SCHEMA = "parakeet-stored-receipt-v1"
FAMILY = "nvidia_parakeet"
MODEL_ID = "nvidia/parakeet-tdt-0.6b-v3"
REVISION = "541d1f99c6b0c3cd0b11a95167540bb8edefd82b"
MAX_RECEIPTS = 8
MAX_AGGREGATE_RAW_BYTES = 4 * 1024 * 1024
MAX_BASE64_FIELD_CHARS = 4 * ((MAX_AGGREGATE_RAW_BYTES + 2) // 3)
_SHA256 = re.compile(r"[0-9a-f]{64}")
_UNSET = object()


class ReceiptValidationError(ValueError):
    """Stored recognition bytes do not satisfy the preservation contract."""


def _fail(reason: str) -> None:
    raise ReceiptValidationError(reason)


def _sha256(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def _canonical_hash(value: Any) -> str:
    try:
        raw = json.dumps(
            value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError, RecursionError) as exc:
        raise ReceiptValidationError("receipt_packet_not_canonical_json") from exc
    return _sha256(raw)


def _strict_json(raw: bytes, label: str) -> dict:
    if not isinstance(raw, bytes):
        _fail(f"{label}_bytes_required")
    try:
        text = raw.decode("utf-8", errors="strict")
    except UnicodeDecodeError as exc:
        raise ReceiptValidationError(f"{label}_utf8_invalid") from exc

    def pairs(items: list[tuple[str, Any]]) -> dict:
        result: dict[str, Any] = {}
        for key, value in items:
            if key in result:
                _fail(f"{label}_duplicate_json_key")
            result[key] = value
        return result

    def constant(_: str) -> None:
        _fail(f"{label}_nonfinite_json_number")

    def finite_float(value: str) -> float:
        number = float(value)
        if not math.isfinite(number):
            _fail(f"{label}_nonfinite_json_number")
        return number

    try:
        value = json.loads(
            text,
            object_pairs_hook=pairs,
            parse_constant=constant,
            parse_float=finite_float,
        )
    except ReceiptValidationError:
        raise
    except (json.JSONDecodeError, RecursionError) as exc:
        raise ReceiptValidationError(f"{label}_json_invalid") from exc
    if not isinstance(value, dict):
        _fail(f"{label}_json_object_required")
    return value


def _sha_field(value: Any, reason: str) -> str:
    if not isinstance(value, str) or _SHA256.fullmatch(value) is None:
        _fail(reason)
    return value


def _number(value: Any, reason: str) -> float:
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        _fail(reason)
    try:
        number = float(value)
    except (OverflowError, ValueError):
        _fail(reason)
    if not math.isfinite(number):
        _fail(reason)
    return number


def _validate_result(result: dict, *, clip_sha256: str) -> None:
    if result.get("family") != FAMILY:
        _fail("result_family_invalid")
    if result.get("model_id") != MODEL_ID:
        _fail("result_model_invalid")
    if result.get("revision") != REVISION:
        _fail("result_revision_invalid")
    if result.get("conditioning_texts") != []:
        _fail("result_conditioning_invalid")
    if _sha_field(result.get("clip_sha256"), "result_clip_sha256_invalid") != clip_sha256:
        _fail("result_clip_sha256_mismatch")

    window = result.get("window")
    if not isinstance(window, list) or len(window) != 2:
        _fail("result_window_invalid")
    window_start = _number(window[0], "result_window_invalid")
    window_end = _number(window[1], "result_window_invalid")
    if window_start < 0 or window_end <= window_start:
        _fail("result_window_invalid")
    duration = window_end - window_start

    texts = result.get("text")
    spans = result.get("raw_token_spans_relative")
    if not isinstance(texts, list) or not isinstance(spans, list) or len(texts) != len(spans):
        _fail("result_token_batch_shape_invalid")
    if any(not isinstance(text, str) for text in texts):
        _fail("result_text_invalid")
    for text, pieces in zip(texts, spans):
        if not isinstance(pieces, list):
            _fail("result_token_batch_shape_invalid")
        reconstructed: list[str] = []
        previous_start = 0.0
        for index, piece in enumerate(pieces):
            if not isinstance(piece, dict) or not isinstance(piece.get("token"), str):
                _fail("result_token_piece_invalid")
            start = _number(piece.get("start"), "result_token_span_invalid")
            end = _number(piece.get("end"), "result_token_span_invalid")
            if start < 0 or end < start or end > duration + 1e-9:
                _fail("result_token_span_invalid")
            if index and start < previous_start:
                _fail("result_token_order_invalid")
            previous_start = start
            reconstructed.append(piece["token"])
        if "".join(reconstructed) != text:
            _fail("result_tokens_do_not_reconstruct_text")

    absolute = result.get("raw_token_spans_absolute")
    if absolute is not None:
        if not isinstance(absolute, list) or len(absolute) != len(spans):
            _fail("result_absolute_token_batch_shape_invalid")
        for relative_pieces, absolute_pieces in zip(spans, absolute):
            if not isinstance(absolute_pieces, list) or len(absolute_pieces) != len(relative_pieces):
                _fail("result_absolute_token_batch_shape_invalid")
            for relative, absolute_piece in zip(relative_pieces, absolute_pieces):
                if not isinstance(absolute_piece, dict) or absolute_piece.get("token") != relative["token"]:
                    _fail("result_absolute_token_piece_invalid")
                start = _number(absolute_piece.get("start"), "result_absolute_token_span_invalid")
                end = _number(absolute_piece.get("end"), "result_absolute_token_span_invalid")
                if not window_start <= start <= end <= window_end + 1e-9:
                    _fail("result_absolute_token_span_invalid")
                if not math.isclose(start, window_start + float(relative["start"]), abs_tol=1e-6):
                    _fail("result_absolute_token_span_mismatch")
                if not math.isclose(end, window_start + float(relative["end"]), abs_tol=1e-6):
                    _fail("result_absolute_token_span_mismatch")


def _validate_completed_receipt(receipt: dict, result_raw: bytes) -> None:
    if receipt.get("completed") is not True:
        _fail("receipt_not_completed")
    expected = _sha_field(receipt.get("result_sha256"), "receipt_result_sha256_invalid")
    if expected != _sha256(result_raw):
        _fail("receipt_result_sha256_mismatch")


def _packet_without_hash(packet: dict) -> dict:
    return {key: value for key, value in packet.items() if key != "packet_hash"}


def _jsonb_stable_window(window: list[Any]) -> list[Any]:
    """Match JSONB's negative-zero normalization without changing raw bytes."""
    return [
        0.0 if isinstance(value, float) and value == 0.0 else value
        for value in window
    ]


def make_receipt(
    raw_result_bytes: bytes,
    raw_receipt_bytes: bytes,
    *,
    parent_audio_sha256: str,
    audio_revision: int,
    clip_sha256: str,
) -> dict:
    """Validate stored JSON and return a packet preserving both byte strings."""
    if not isinstance(raw_result_bytes, bytes):
        _fail("result_bytes_required")
    if not isinstance(raw_receipt_bytes, bytes):
        _fail("receipt_bytes_required")
    if len(raw_result_bytes) + len(raw_receipt_bytes) > MAX_AGGREGATE_RAW_BYTES:
        _fail("receipt_raw_bytes_limit_exceeded")
    parent = _sha_field(parent_audio_sha256, "parent_audio_sha256_invalid")
    clip = _sha_field(clip_sha256, "clip_sha256_invalid")
    if type(audio_revision) is not int or audio_revision < 0:
        _fail("audio_revision_invalid")
    result = _strict_json(raw_result_bytes, "result")
    completed = _strict_json(raw_receipt_bytes, "receipt")
    _validate_result(result, clip_sha256=clip)
    _validate_completed_receipt(completed, raw_result_bytes)
    packet = {
        "schema": SCHEMA,
        "family": FAMILY,
        "model_id": MODEL_ID,
        "revision": REVISION,
        "parent_audio_sha256": parent,
        "audio_revision": audio_revision,
        "clip_sha256": clip,
        "window": _jsonb_stable_window(result["window"]),
        "result_sha256": _sha256(raw_result_bytes),
        "receipt_sha256": _sha256(raw_receipt_bytes),
        "raw_result_base64": base64.b64encode(raw_result_bytes).decode("ascii"),
        "raw_receipt_base64": base64.b64encode(raw_receipt_bytes).decode("ascii"),
    }
    packet["packet_hash"] = _canonical_hash(packet)
    return packet


def _decode_field(packet: dict, key: str) -> bytes:
    value = packet.get(key)
    if not isinstance(value, str):
        _fail(f"{key}_invalid")
    if len(value) > MAX_BASE64_FIELD_CHARS:
        _fail(f"{key}_size_limit_exceeded")
    try:
        return base64.b64decode(value.encode("ascii"), validate=True)
    except (UnicodeEncodeError, binascii.Error) as exc:
        raise ReceiptValidationError(f"{key}_invalid") from exc


def _validate_packet(packet: Any) -> tuple[dict, bytes, bytes]:
    if not isinstance(packet, dict) or packet.get("schema") != SCHEMA:
        _fail("receipt_packet_invalid")
    if set(packet) != {
        "schema", "family", "model_id", "revision", "parent_audio_sha256",
        "audio_revision", "clip_sha256", "window", "result_sha256",
        "receipt_sha256", "raw_result_base64", "raw_receipt_base64", "packet_hash",
    }:
        _fail("receipt_packet_fields_invalid")
    supplied_hash = _sha_field(packet.get("packet_hash"), "packet_hash_invalid")
    if supplied_hash != _canonical_hash(_packet_without_hash(packet)):
        _fail("packet_hash_mismatch")
    result_raw = _decode_field(packet, "raw_result_base64")
    receipt_raw = _decode_field(packet, "raw_receipt_base64")
    if len(result_raw) + len(receipt_raw) > MAX_AGGREGATE_RAW_BYTES:
        _fail("receipt_raw_bytes_limit_exceeded")
    if _sha256(result_raw) != packet.get("result_sha256"):
        _fail("packet_result_sha256_mismatch")
    if _sha256(receipt_raw) != packet.get("receipt_sha256"):
        _fail("packet_receipt_sha256_mismatch")
    rebuilt = make_receipt(
        result_raw,
        receipt_raw,
        parent_audio_sha256=packet.get("parent_audio_sha256"),
        audio_revision=packet.get("audio_revision"),
        clip_sha256=packet.get("clip_sha256"),
    )
    if rebuilt != packet:
        _fail("receipt_packet_inconsistent")
    return rebuilt, result_raw, receipt_raw


def validate_receipts(
    receipts: Any,
    *,
    audio_sha256: Any = _UNSET,
    audio_revision: Any = _UNSET,
) -> list[dict]:
    """Return validated copies; supplied bindings are authoritative and exact.

    Omitting both binding keywords performs structure/integrity validation for
    build time. Supplying either keyword activates authoritative binding and
    requires both to be present and valid; explicit ``None`` is rejected.
    """
    if not isinstance(receipts, list):
        _fail("recognition_receipts_list_required")
    if len(receipts) > MAX_RECEIPTS:
        _fail("recognition_receipts_count_exceeded")
    binding_requested = audio_sha256 is not _UNSET or audio_revision is not _UNSET
    if binding_requested:
        if audio_sha256 is _UNSET or audio_revision is _UNSET:
            _fail("authoritative_audio_binding_incomplete")
        authoritative_sha = _sha_field(audio_sha256, "authoritative_audio_sha256_invalid")
        if type(audio_revision) is not int or audio_revision < 0:
            _fail("authoritative_audio_revision_invalid")
    else:
        authoritative_sha = None

    output: list[dict] = []
    seen_hashes: set[str] = set()
    identity_hashes: dict[tuple[str, str], str] = {}
    aggregate = 0
    for raw_packet in receipts:
        packet, result_raw, receipt_raw = _validate_packet(raw_packet)
        aggregate += len(result_raw) + len(receipt_raw)
        if aggregate > MAX_AGGREGATE_RAW_BYTES:
            _fail("recognition_receipts_aggregate_size_exceeded")
        if binding_requested and (
            packet["parent_audio_sha256"] != authoritative_sha
            or packet["audio_revision"] != audio_revision
        ):
            _fail("recognition_receipt_audio_binding_mismatch")
        packet_hash = packet["packet_hash"]
        identity = (packet["result_sha256"], packet["receipt_sha256"])
        previous = identity_hashes.get(identity)
        if previous is not None and previous != packet_hash:
            _fail("conflicting_duplicate_recognition_receipt")
        identity_hashes[identity] = packet_hash
        if packet_hash in seen_hashes:
            continue
        seen_hashes.add(packet_hash)
        output.append(deepcopy(packet))
    return output


def decode_receipt(packet: Any) -> dict:
    """Return the exact stored result JSON object without derived word edits."""
    _, result_raw, _ = _validate_packet(packet)
    return deepcopy(_strict_json(result_raw, "result"))
