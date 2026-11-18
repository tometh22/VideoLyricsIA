"""Deterministic technical check for a background asset (no AI, no network).

**Bordes negros / letterbox** — "La imagen no tiene la resolución adecuada…
no se vean los bordes negros", re-filed as "Se sigue viendo el borde superior e
inferior en negro" (IKV, We Fight Against The Rainbow, pedidos 21-oct-2026).
The render fills the master with ``scale=…:force_original_aspect_ratio=
increase,crop``, so bars in the final cut only exist when the generator baked
them INTO the background. The effective aspect ratio of the picture (frame
minus persistent black bands) is a number; measuring it is cheaper and more
reliable than asking a model, and it costs nothing against the Vertex quota.

Not here on purpose: a pixel-statistics detector for burned-in corner text
("Sacar el 16:9 que aparece arriba a izquierda", Gilda). It was prototyped
(still + sharp pixels in a corner of a moving scene) and does NOT separate the
real case from accepted backgrounds: most UMG backgrounds are stills or slow
zooms, so "still and sharp" describes the whole picture. That case is covered
by the Vision ``overlay`` category instead (content_validator).

Contract — this module must never be the reason a job fails:

* every error (ffmpeg missing, unreadable file, odd geometry) is an
  ``abstention``, never a finding. A detector that could not look says so; it
  does not guess, and it never turns into an inconclusive verdict upstream
  (the 429→fail-closed incident of 2026-08-20 is the lesson).
* findings are reported with their metrics so the operator — and whoever
  calibrates the thresholds next — can see why it fired.
* the caller decides observe vs. enforce; nothing here blocks.
"""
from __future__ import annotations

import json
import os
import subprocess
from typing import Any, Callable

Runner = Callable[..., subprocess.CompletedProcess]

# Analysis raster. 320 px wide keeps a 16:9 frame at 320x180: a 2.5 % bar is
# still 4-5 rows, and 8 grayscale frames are ~460 KB — nothing for a worker.
ANALYSIS_WIDTH = 320
MAX_FRAMES = 8

# A row/column belongs to a black band when 98 % of its pixels are at or below
# this luma (8-bit, full range). Pure letterbox encodes at 16 (limited range)
# or 0; 28 leaves room for compression noise and still rejects a dark-but-lit
# night sky, whose stars/gradient push p98 well above it.
BLACK_LUMA_P98 = 28
# Letterbox is symmetric: opposite bands of at least 2.5 % each (27 px at
# 1080p, far above 1-4 px encoder edge artefacts) are enough. A band on ONE
# side only must be twice as tall: a dark table front or floor at the bottom of
# a still reads exactly like a 4 % bar (accepted job f517564b42d8, 18-nov).
MIN_BAND_FRACTION = 0.025
MIN_SINGLE_BAND_FRACTION = 0.05
# Content right inside the band must be visibly brighter than the band itself:
# real letterbox is a hard edge; a scene that fades to black is not.
MIN_EDGE_CONTRAST = 10.0


def _abstain(reason: str, **metrics: Any) -> dict[str, Any]:
    return {"ran": False, "abstained": reason, "findings": [], "metrics": metrics}


def _probe(path: str, runner: Runner) -> dict[str, Any]:
    completed = runner(
        ["ffprobe", "-v", "error", "-select_streams", "v:0",
         "-show_entries", "stream=width,height:format=duration",
         "-of", "json", path],
        capture_output=True, text=True, timeout=30, check=True,
    )
    data = json.loads(completed.stdout or "{}")
    stream = (data.get("streams") or [{}])[0]
    try:
        duration = float((data.get("format") or {}).get("duration") or 0)
    except (TypeError, ValueError):
        duration = 0.0
    return {
        "width": int(stream.get("width") or 0),
        "height": int(stream.get("height") or 0),
        "duration": duration,
    }


def _frames(path: str, probe: dict[str, Any], runner: Runner):
    """Return a (n, h, w) uint8 array of evenly spaced grayscale frames."""
    import numpy as np

    width, height = probe["width"], probe["height"]
    out_w = ANALYSIS_WIDTH
    out_h = max(2, int(round(height * out_w / width / 2.0)) * 2)
    duration = probe["duration"]
    if duration and duration > 0.5:
        count = MAX_FRAMES
        # Avoid the very first/last frames: fades and encoder warm-up.
        start, end = duration * 0.05, duration * 0.95
        stamps = [start + (end - start) * i / (count - 1) for i in range(count)]
    else:
        stamps = [0.0]
    frames = []
    for ts in stamps:
        completed = runner(
            ["ffmpeg", "-v", "error", "-ss", f"{ts:.3f}", "-i", path,
             "-frames:v", "1", "-vf", f"scale={out_w}:{out_h},format=gray",
             "-f", "rawvideo", "-"],
            capture_output=True, timeout=30, check=True,
        )
        raw = completed.stdout or b""
        if len(raw) < out_w * out_h:
            continue
        frames.append(np.frombuffer(raw[: out_w * out_h], dtype=np.uint8).reshape(out_h, out_w))
    if not frames:
        return None
    return np.stack(frames)


def _band(lines_p98, *, from_end: bool) -> int:
    """Length of the run of black lines starting at one edge."""
    seq = range(len(lines_p98) - 1, -1, -1) if from_end else range(len(lines_p98))
    run = 0
    for index in seq:
        if lines_p98[index] > BLACK_LUMA_P98:
            break
        run += 1
    return run


def detect_letterbox(frames) -> dict[str, Any]:
    """Black bands present in EVERY sampled frame, with a hard inner edge."""
    import numpy as np

    n, h, w = frames.shape
    sides = {"top": [], "bottom": [], "left": [], "right": []}
    contrast = {"top": [], "bottom": [], "left": [], "right": []}
    for frame in frames.astype(np.float32):
        rows_p98 = np.percentile(frame, 98, axis=1)
        cols_p98 = np.percentile(frame, 98, axis=0)
        rows_mean = frame.mean(axis=1)
        cols_mean = frame.mean(axis=0)
        bands = {
            "top": _band(rows_p98, from_end=False),
            "bottom": _band(rows_p98, from_end=True),
            "left": _band(cols_p98, from_end=False),
            "right": _band(cols_p98, from_end=True),
        }
        for side, size in bands.items():
            sides[side].append(size)
            extent = h if side in {"top", "bottom"} else w
            if size <= 0 or size >= extent - 2:
                contrast[side].append(0.0)
                continue
            means = rows_mean if side in {"top", "bottom"} else cols_mean
            inner = means[size: size + 3] if side in {"top", "left"} else means[extent - size - 3: extent - size]
            outer = means[:size] if side in {"top", "left"} else means[extent - size:]
            contrast[side].append(float(inner.mean() - outer.mean()))
    # A band counts only if it is in every frame: a dark moment is not a bar.
    persistent = {side: int(min(values)) for side, values in sides.items()}
    edge = {side: float(min(values)) for side, values in contrast.items()}
    fractions = {
        side: persistent[side] / float(h if side in {"top", "bottom"} else w)
        for side in persistent
    }
    candidates = {
        side for side in ("top", "bottom", "left", "right")
        if fractions[side] >= MIN_BAND_FRACTION and edge[side] >= MIN_EDGE_CONTRAST
    }
    opposite = {"top": "bottom", "bottom": "top", "left": "right", "right": "left"}
    flagged = [
        side for side in ("top", "bottom", "left", "right")
        if side in candidates and (
            opposite[side] in candidates
            or fractions[side] >= MIN_SINGLE_BAND_FRACTION
        )
    ]
    content_h = h - (persistent["top"] if "top" in flagged else 0) - (
        persistent["bottom"] if "bottom" in flagged else 0)
    content_w = w - (persistent["left"] if "left" in flagged else 0) - (
        persistent["right"] if "right" in flagged else 0)
    effective_aspect = round(content_w / float(max(1, content_h)), 3)
    return {
        "flagged_sides": flagged,
        "band_fraction": {side: round(value, 4) for side, value in fractions.items()},
        "edge_contrast": {side: round(value, 1) for side, value in edge.items()},
        "effective_aspect": effective_aspect,
        "frame_aspect": round(w / float(h), 3),
    }


def analyze_background(
    path: str,
    *,
    target_aspect: float | None = 16 / 9,
    runner: Runner = subprocess.run,
) -> dict[str, Any]:
    """Run every deterministic background check; never raises.

    Returns ``{"ran", "abstained", "findings", "metrics"}``. Each finding is
    ``{"category", "code", "reason", "evidence"}`` with category
    ``letterbox`` — the same shape the Vision categories use, so the validator
    can treat them uniformly.
    """
    if not path or not os.path.isfile(path):
        return _abstain("asset_missing")
    try:
        probe = _probe(path, runner)
        if probe["width"] <= 0 or probe["height"] <= 0:
            return _abstain("no_video_stream")
        frames = _frames(path, probe, runner)
    except Exception as exc:  # noqa: BLE001 — fail-open by contract
        return _abstain(f"probe_failed:{type(exc).__name__}")
    if frames is None:
        return _abstain("no_frames_decoded", **probe)

    findings: list[dict[str, Any]] = []
    metrics: dict[str, Any] = {
        "source_width": probe["width"], "source_height": probe["height"],
        "source_aspect": round(probe["width"] / float(probe["height"]), 3),
        "frames_analyzed": int(frames.shape[0]),
    }
    try:
        letterbox = detect_letterbox(frames)
        metrics["letterbox"] = letterbox
        if letterbox["flagged_sides"]:
            sides = ", ".join(letterbox["flagged_sides"])
            target = f" (master {target_aspect:.2f}:1)" if target_aspect else ""
            findings.append({
                "category": "letterbox",
                "code": "BG_LETTERBOX",
                "reason": (
                    f"Bandas negras fijas en {sides}: la imagen útil es "
                    f"{letterbox['effective_aspect']:.2f}:1{target}."
                ),
                "evidence": letterbox,
            })
    except Exception as exc:  # noqa: BLE001
        metrics["letterbox"] = {"abstained": f"error:{type(exc).__name__}"}
    return {"ran": True, "abstained": None, "findings": findings, "metrics": metrics}
