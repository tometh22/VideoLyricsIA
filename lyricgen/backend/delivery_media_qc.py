"""Technical inspection of the encoded delivery candidate.

The checker is intentionally conservative.  Objective container/stream errors
can block in enforce mode; black frames, freezes and loudness are observations
because they may be an artistic choice and never trigger an automatic repair.
"""
from __future__ import annotations

import json
import math
import os
import subprocess
from typing import Any, Callable, Mapping


Runner = Callable[..., subprocess.CompletedProcess]


def _number(value: Any) -> float | None:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


def _ratio(value: Any) -> float | None:
    text = str(value or "")
    if "/" in text:
        left, right = text.split("/", 1)
        denominator = _number(right)
        return (_number(left) or 0.0) / denominator if denominator else None
    return _number(value)


def inspect_delivery_media(
    path: str,
    *,
    expected_duration: float | None = None,
    expected: Mapping[str, Any] | None = None,
    runner: Runner = subprocess.run,
) -> dict[str, Any]:
    """Return objective media findings without mutating the asset."""
    issues: list[dict[str, Any]] = []
    abstentions: list[dict[str, str]] = []
    if not path or not os.path.isfile(path):
        return {
            "probe": {},
            "issues": [{
                "code": "MEDIA_ASSET_MISSING", "severity": "FAIL",
                "category": "technical", "summary": "No se encontró el video final",
                "description": "El preflight no pudo abrir el archivo renderizado.",
                "seconds": [0.0], "timecodes": ["00:00:00:00"],
                "status": "OPEN", "confidence": 1.0,
            }],
            "abstentions": [],
        }
    command = [
        "ffprobe", "-v", "error", "-show_streams", "-show_format",
        "-of", "json", path,
    ]
    try:
        completed = runner(command, capture_output=True, text=True, timeout=30, check=True)
        probe = json.loads(completed.stdout or "{}")
    except Exception as exc:
        return {
            "probe": {},
            "issues": [{
                "code": "MEDIA_PROBE_FAILED", "severity": "FAIL",
                "category": "technical", "summary": "No se pudo validar el archivo",
                "description": f"ffprobe falló ({type(exc).__name__}).",
                "seconds": [0.0], "timecodes": ["00:00:00:00"],
                "status": "OPEN", "confidence": 1.0,
            }],
            "abstentions": [],
        }

    streams = probe.get("streams") or []
    videos = [row for row in streams if row.get("codec_type") == "video"]
    audios = [row for row in streams if row.get("codec_type") == "audio"]
    if not videos:
        issues.append({
            "code": "MEDIA_VIDEO_STREAM_MISSING", "severity": "FAIL",
            "category": "technical", "summary": "Falta la pista de video",
            "description": "El entregable no contiene un stream de video.",
        })
    if not audios:
        issues.append({
            "code": "MEDIA_AUDIO_STREAM_MISSING", "severity": "FAIL",
            "category": "technical", "summary": "Falta la pista de audio",
            "description": "El entregable no contiene un stream de audio.",
        })

    fmt = probe.get("format") or {}
    duration = _number(fmt.get("duration"))
    if duration is None or duration <= 0:
        issues.append({
            "code": "MEDIA_DURATION_INVALID", "severity": "FAIL",
            "category": "technical", "summary": "Duración de video inválida",
            "description": "El contenedor no informa una duración positiva.",
        })
    elif expected_duration and abs(duration - expected_duration) > max(0.5, expected_duration * .01):
        issues.append({
            "code": "MEDIA_DURATION_MISMATCH", "severity": "FAIL",
            "category": "technical", "summary": "El video no dura lo mismo que el audio",
            "description": f"Video {duration:.3f}s; audio esperado {expected_duration:.3f}s.",
            "actual": duration, "expected": expected_duration,
        })

    spec = dict(expected or {})
    if videos:
        video = videos[0]
        actual = {
            "width": video.get("width"), "height": video.get("height"),
            "codec": video.get("codec_name"),
            "fps": _ratio(video.get("avg_frame_rate") or video.get("r_frame_rate")),
            "pix_fmt": video.get("pix_fmt"),
        }
        for key in ("width", "height", "codec", "pix_fmt"):
            if spec.get(key) is not None and str(actual.get(key)) != str(spec[key]):
                issues.append({
                    "code": f"MEDIA_{key.upper()}_MISMATCH", "severity": "WARN",
                    "category": "technical", "summary": f"Especificación {key} distinta",
                    "description": f"Render: {actual.get(key)}; esperado: {spec[key]}.",
                    "actual": actual.get(key), "expected": spec[key],
                })
        if spec.get("fps") is not None and actual["fps"] is not None:
            if abs(float(actual["fps"]) - float(spec["fps"])) > .02:
                issues.append({
                    "code": "MEDIA_FPS_MISMATCH", "severity": "WARN",
                    "category": "technical", "summary": "Frame rate distinto",
                    "description": f"Render: {actual['fps']:.3f}; esperado: {float(spec['fps']):.3f}.",
                    "actual": actual["fps"], "expected": spec["fps"],
                })
    else:
        actual = {}

    if os.environ.get("DELIVERY_QC_MEDIA_SCAN_ENABLED", "1").strip().lower() not in {"1", "true", "yes", "on"}:
        abstentions.append({"detector": "media_artifact_scan", "reason": "disabled"})
    # These detectors are deliberately left as an explicit abstention until
    # their label-specific thresholds are calibrated. ffprobe checks above are
    # deterministic and already catch broken exports.
    else:
        abstentions.append({
            "detector": "black_freeze_loudness_scan",
            "reason": "artistic_content_requires_calibrated_observe_only_thresholds",
        })

    for issue in issues:
        issue.setdefault("status", "OPEN")
        issue.setdefault("seconds", [0.0])
        issue.setdefault("timecodes", ["00:00:00:00"])
        issue.setdefault("confidence", 1.0)
        issue.setdefault("detector", "ffprobe")
    return {
        "probe": {"duration": duration, "video": actual, "audio_streams": len(audios)},
        "issues": issues,
        "abstentions": abstentions,
    }
