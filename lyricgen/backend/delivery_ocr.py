"""Render-vs-manifest OCR comparison for Delivery QC.

OCR is evidence, never authority.  The model only sees extracted pixels (not
the expected text), so a match cannot be produced by copying the manifest.
Ambiguous output creates review suggestions and is never auto-applied.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import tempfile
import unicodedata
from typing import Any, Callable, Mapping, Sequence


def _fold(value: Any) -> str:
    # Preserve diacritics: JAMAS vs JAMÁS is exactly the class of final-frame
    # defect the label reports. Only case and whitespace are editorially free.
    text = unicodedata.normalize("NFC", str(value or "").casefold())
    return " ".join(re.findall(r"[^\W_]+", text, re.UNICODE))


def compare_ocr_observations(
    observations: Sequence[Mapping[str, Any]],
    *,
    metadata: Mapping[str, Any],
    segments: Sequence[Mapping[str, Any]],
    min_confidence: float = .92,
) -> list[dict[str, Any]]:
    """Compare independently transcribed frames to expected rendered content."""
    issues: list[dict[str, Any]] = []
    for row in observations:
        try:
            confidence = float(row.get("confidence") or 0)
            seconds = float(row.get("seconds") or 0)
        except (TypeError, ValueError):
            continue
        if confidence < min_confidence:
            continue
        kind = str(row.get("kind") or "lyric")
        actual = str(row.get("text") or "").strip()
        expected = ""
        code = "OCR_LYRIC_MISMATCH"
        summary = "El texto visible no coincide con la letra"
        if kind == "title":
            expected = str(metadata.get("title") or "").strip()
            code, summary = "OCR_TITLE_MISMATCH", "El título visible no coincide"
        elif kind == "artist":
            expected = str(metadata.get("artist") or "").strip()
            code, summary = "OCR_ARTIST_MISMATCH", "El artista visible no coincide"
        else:
            index = row.get("segment_index")
            try:
                segment = segments[int(index)]
            except (TypeError, ValueError, IndexError):
                continue
            expected = str(segment.get("text", segment.get("t", ""))).strip()
        if expected and actual and _fold(actual) != _fold(expected):
            digest = hashlib.sha256(f"{code}|{seconds:.3f}|{actual}|{expected}".encode()).hexdigest()[:16]
            issues.append({
                "issue_id": digest, "status": "OPEN", "severity": "WARN",
                "category": "rendered_text", "code": code, "summary": summary,
                "description": "Revisar el cuadro final; OCR es una señal independiente, no una corrección automática.",
                "frequency": "ISOLATED", "occurrence_count": 1,
                "seconds": [round(seconds, 3)], "timecodes": [],
                "actual": actual, "expected": expected,
                "confidence": round(confidence, 3), "auto_fixable": False,
                "detector": "final_frame_ocr",
                "evidence": [{"segment_index": row.get("segment_index")}],
            })
    return issues


def inspect_rendered_text(
    video_path: str,
    *,
    metadata: Mapping[str, Any],
    segments: Sequence[Mapping[str, Any]],
    ocr_callback: Callable[[list[dict[str, Any]]], Sequence[Mapping[str, Any]]] | None = None,
) -> dict[str, Any]:
    """Sample title/lyric frames and optionally run an injected OCR provider.

    Production provider wiring is deliberately injectable: when unavailable the
    report records an abstention instead of silently declaring PASS.
    """
    enabled = os.environ.get("DELIVERY_QC_OCR_ENABLED", "0").strip().lower() in {"1", "true", "yes", "on"}
    if not enabled:
        return {"observations": [], "issues": [], "abstentions": [{"detector": "final_frame_ocr", "reason": "disabled"}]}
    if not os.path.isfile(video_path):
        return {"observations": [], "issues": [], "abstentions": [{"detector": "final_frame_ocr", "reason": "asset_missing"}]}
    max_frames = max(1, min(int(os.environ.get("DELIVERY_QC_OCR_MAX_FRAMES", "24")), 60))
    samples: list[dict[str, Any]] = [{"kind": "title", "seconds": 1.0}]
    step = max(1, len(segments) // max(1, max_frames - 1))
    for index in range(0, len(segments), step):
        row = segments[index]
        try:
            start, end = float(row.get("start", row.get("s", 0))), float(row.get("end", row.get("e", 0)))
        except (TypeError, ValueError):
            continue
        samples.append({"kind": "lyric", "seconds": max(0, (start + end) / 2), "segment_index": index})
        if len(samples) >= max_frames:
            break
    if ocr_callback is None:
        ocr_callback = lambda rows: _gemini_ocr(video_path, rows)
    try:
        observations = list(ocr_callback(samples))
    except Exception as exc:
        return {"observations": [], "issues": [], "abstentions": [{"detector": "final_frame_ocr", "reason": f"provider_failed:{type(exc).__name__}"}]}
    return {
        "observations": observations,
        "issues": compare_ocr_observations(observations, metadata=metadata, segments=segments),
        "abstentions": [],
    }


def _gemini_ocr(video_path: str, samples: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Extract frames and transcribe pixels without exposing expected text."""
    from google import genai
    from pipeline import _call_with_timeout, _get_genai_client

    parts: list[Any] = []
    with tempfile.TemporaryDirectory(prefix="genly-delivery-ocr-") as folder:
        for index, sample in enumerate(samples):
            frame_path = os.path.join(folder, f"frame-{index:03d}.jpg")
            subprocess.run(
                [
                    "ffmpeg", "-v", "error", "-ss", f"{float(sample['seconds']):.3f}",
                    "-i", video_path, "-frames:v", "1", "-q:v", "2", "-y", frame_path,
                ],
                capture_output=True, timeout=20, check=True,
            )
            with open(frame_path, "rb") as handle:
                parts.append(genai.types.Part.from_text(text=f"FRAME_ID={index}"))
                parts.append(genai.types.Part.from_bytes(data=handle.read(), mime_type="image/jpeg"))
        parts.append(genai.types.Part.from_text(text=(
            "Transcribe ONLY the lyric/title/artist text visibly rendered in each frame. "
            "Do not infer missing words and do not identify objects. Return a JSON array "
            "with frame_id (integer), text (exact visible text), confidence (0..1), and "
            "kind (title, artist, lyric, or none). If no readable overlay exists, kind=none "
            "and text=''. Preserve accents exactly as visible."
        )))
        client = _get_genai_client()
        response = _call_with_timeout(
            lambda: client.models.generate_content(
                model=os.environ.get("DELIVERY_QC_OCR_MODEL", "gemini-2.5-flash"),
                contents=parts,
                config=genai.types.GenerateContentConfig(
                    temperature=0.0, max_output_tokens=2500,
                    response_mime_type="application/json",
                    thinking_config=genai.types.ThinkingConfig(thinking_budget=0),
                ),
            ),
            timeout_s=float(os.environ.get("DELIVERY_QC_TIMEOUT_SECONDS", "90")),
            label="DELIVERY-QC-OCR",
        )
    payload = json.loads((response.text or "[]").strip())
    if isinstance(payload, Mapping):
        payload = payload.get("frames") or []
    output: list[dict[str, Any]] = []
    for row in payload if isinstance(payload, list) else []:
        if not isinstance(row, Mapping):
            continue
        try:
            sample = samples[int(row.get("frame_id"))]
        except (TypeError, ValueError, IndexError):
            continue
        output.append({**sample, **dict(row)})
    return output
