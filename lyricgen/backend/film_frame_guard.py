"""Conservative detector and local repair for film-frame artefacts in Veo.

This guard intentionally does *not* look for arbitrary dark rectangles.  A
rectangle is reported only when the clip also has a screen-fixed, near-black
frame on all four outer edges.  That compound signature matches the incident
where Veo rendered a physical film frame (border + sprocket/placeholder) and
avoids treating windows, mirrors, signs, cars, or naturally dark footage as UI.

Repair is a centre-preserving uniform zoom: it removes the literal outer film
frame and its edge block without synthesising pixels.  It never asks Veo for
another generation.  ``BG_FILM_FRAME_GUARD_MODE=shadow`` keeps detection in
observation-only mode; the default is ``repair``.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
import glob
import logging
import math
import os
import subprocess
import tempfile

import numpy as np
from PIL import Image


logger = logging.getLogger("genly.film_frame_guard")

_SAMPLE_WIDTH = 640
_MAX_SAMPLES = 8
_BLACK_MAX = 24
_EDGE_BLACK_MIN = 0.90
_CROP_MARGIN = 0.0075
_MAX_CROP_PER_SIDE = 0.105


@dataclass(frozen=True)
class FilmFrameArtifact:
    """Detection coordinates in the sampled-frame coordinate system."""

    x: int
    y: int
    width: int
    height: int
    frame_width: int
    frame_height: int
    fill_ratio: float
    area_ratio: float
    edge_black_ratios: tuple[float, float, float, float]
    samples: int

    def log_fields(self) -> str:
        return (
            f"bbox={self.x},{self.y},{self.width},{self.height} "
            f"sample={self.frame_width}x{self.frame_height} "
            f"fill={self.fill_ratio:.3f} area={self.area_ratio:.4f} "
            f"edges={','.join(f'{v:.3f}' for v in self.edge_black_ratios)} "
            f"frames={self.samples}"
        )


def _components(mask: np.ndarray):
    """Yield 8-connected component stats as (x, y, w, h, area).

    The sampled mask is at most 640 px wide and usually contains <5% true
    pixels, so this small dependency-free flood fill is fast enough and avoids
    shipping OpenCV in the production image.
    """

    height, width = mask.shape
    seen = np.zeros(mask.shape, dtype=np.uint8)
    for start_y, start_x in np.argwhere(mask):
        start_y = int(start_y)
        start_x = int(start_x)
        if seen[start_y, start_x]:
            continue
        queue = deque([(start_x, start_y)])
        seen[start_y, start_x] = 1
        min_x = max_x = start_x
        min_y = max_y = start_y
        area = 0
        while queue:
            x, y = queue.popleft()
            area += 1
            min_x = min(min_x, x)
            max_x = max(max_x, x)
            min_y = min(min_y, y)
            max_y = max(max_y, y)
            for ny in range(max(0, y - 1), min(height, y + 2)):
                for nx in range(max(0, x - 1), min(width, x + 2)):
                    if mask[ny, nx] and not seen[ny, nx]:
                        seen[ny, nx] = 1
                        queue.append((nx, ny))
        yield min_x, min_y, max_x - min_x + 1, max_y - min_y + 1, area


def detect_from_grayscale_frames(
    frames: list[np.ndarray],
) -> FilmFrameArtifact | None:
    """Detect the compound physical-film signature in sampled gray frames.

    Every candidate pixel must remain near-black in *all* sampled frames.  The
    detector then requires:

    * a near-black outer edge on all four sides (literal frame shell), and
    * a separate, dense rectangular component close to a vertical edge
      (sprocket/placeholder).

    A dark rectangle by itself is deliberately insufficient.
    """

    if len(frames) < 3:
        return None
    shape = frames[0].shape
    if len(shape) != 2 or any(frame.shape != shape for frame in frames):
        return None
    stack = np.stack(frames).astype(np.uint8, copy=False)
    stable_black = np.max(stack, axis=0) <= _BLACK_MAX
    height, width = stable_black.shape
    if width < 80 or height < 45:
        return None

    edge_ratios = (
        float(stable_black[0, :].mean()),
        float(stable_black[-1, :].mean()),
        float(stable_black[:, 0].mean()),
        float(stable_black[:, -1].mean()),
    )
    if min(edge_ratios) < _EDGE_BLACK_MIN:
        return None

    candidates = []
    frame_area = width * height
    for x, y, box_w, box_h, area in _components(stable_black):
        # The outer frame itself touches an image edge; the offending inner
        # sprocket/placeholder does not, although it sits very close to one.
        if x == 0 or y == 0 or x + box_w == width or y + box_h == height:
            continue
        width_ratio = box_w / width
        height_ratio = box_h / height
        area_ratio = area / frame_area
        fill_ratio = area / (box_w * box_h)
        side_margin = min(x, width - (x + box_w)) / width
        if not (0.025 <= width_ratio <= 0.14):
            continue
        if not (0.06 <= height_ratio <= 0.30):
            continue
        if not (0.004 <= area_ratio <= 0.03):
            continue
        if fill_ratio < 0.85:
            continue
        if side_margin > 0.035:
            continue
        if y / height < 0.04 or (y + box_h) / height > 0.96:
            continue
        candidates.append((area_ratio, x, y, box_w, box_h, fill_ratio))

    if not candidates:
        return None
    area_ratio, x, y, box_w, box_h, fill_ratio = max(candidates)
    return FilmFrameArtifact(
        x=x,
        y=y,
        width=box_w,
        height=box_h,
        frame_width=width,
        frame_height=height,
        fill_ratio=fill_ratio,
        area_ratio=area_ratio,
        edge_black_ratios=edge_ratios,
        samples=len(frames),
    )


def _extract_grayscale_samples(video_path: str) -> list[np.ndarray]:
    temp_dir = tempfile.mkdtemp(prefix="genly_film_frame_")
    pattern = os.path.join(temp_dir, "frame_%03d.png")
    try:
        result = subprocess.run(
            [
                "ffmpeg", "-y", "-loglevel", "error", "-i", video_path,
                "-vf", f"fps=1,scale={_SAMPLE_WIDTH}:-2",
                "-frames:v", str(_MAX_SAMPLES), pattern,
            ],
            capture_output=True,
            text=True,
            timeout=90,
        )
        if result.returncode != 0:
            logger.warning(
                "[BG][FILM-FRAME] sample extraction failed: %s",
                (result.stderr or "")[-300:],
            )
            return []
        frames = []
        for path in sorted(glob.glob(os.path.join(temp_dir, "frame_*.png"))):
            with Image.open(path) as image:
                frames.append(np.asarray(image.convert("L"), dtype=np.uint8))
        return frames
    except Exception as exc:
        logger.warning("[BG][FILM-FRAME] sample extraction skipped: %s", exc)
        return []
    finally:
        for path in glob.glob(os.path.join(temp_dir, "*")):
            try:
                os.unlink(path)
            except OSError:
                pass
        try:
            os.rmdir(temp_dir)
        except OSError:
            pass


def observe_film_frame_artifact(
    video_path: str,
    *,
    job_id: str | None = None,
) -> FilmFrameArtifact | None:
    """Inspect and log a finding without changing the clip or calling Veo."""

    frames = _extract_grayscale_samples(video_path)
    finding = detect_from_grayscale_frames(frames)
    if finding:
        logger.warning(
            "[BG][FILM-FRAME][SHADOW] job=%s %s",
            job_id or "unknown",
            finding.log_fields(),
        )
    return finding


def _crop_fraction(finding: FilmFrameArtifact) -> float:
    """Return the symmetric crop needed to clear the edge-side component."""

    if finding.x + finding.width / 2 <= finding.frame_width / 2:
        component_extent = (finding.x + finding.width) / finding.frame_width
    else:
        component_extent = (finding.frame_width - finding.x) / finding.frame_width
    return min(_MAX_CROP_PER_SIDE, component_extent + _CROP_MARGIN)


def _video_dimensions(video_path: str) -> tuple[int, int]:
    result = subprocess.run(
        [
            "ffprobe", "-v", "error", "-select_streams", "v:0",
            "-show_entries", "stream=width,height", "-of", "csv=p=0:s=x",
            video_path,
        ],
        capture_output=True,
        text=True,
        timeout=20,
    )
    if result.returncode != 0:
        raise RuntimeError((result.stderr or "ffprobe failed")[-300:])
    width_text, height_text = result.stdout.strip().split("x", 1)
    width, height = int(width_text), int(height_text)
    if width < 80 or height < 45:
        raise RuntimeError(f"invalid video dimensions: {width}x{height}")
    return width, height


def _repair_with_uniform_zoom(
    video_path: str,
    finding: FilmFrameArtifact,
) -> float:
    """Atomically replace ``video_path`` with a centre-preserving zoom.

    The original is left untouched unless FFmpeg completes successfully.  A
    uniform scale followed by a centre crop preserves the source aspect ratio
    and resolution; unlike inpainting/delogo it cannot create a smeared patch.
    """

    width, height = _video_dimensions(video_path)
    crop_fraction = _crop_fraction(finding)
    remaining = 1.0 - 2.0 * crop_fraction
    if remaining <= 0.75:
        raise RuntimeError(f"unsafe crop fraction: {crop_fraction:.4f}")

    # FFmpeg's -2 keeps the derived height even for H.264.  Round the scaled
    # width upward so the centre crop can always produce the original size.
    scaled_width = int(math.ceil((width / remaining) / 2.0) * 2)
    video_filter = (
        f"scale={scaled_width}:-2:flags=lanczos,"
        f"crop={width}:{height}:(iw-{width})/2:(ih-{height})/2,setsar=1"
    )

    output_dir = os.path.dirname(os.path.abspath(video_path))
    fd, repaired_path = tempfile.mkstemp(
        prefix=".film_frame_repaired_", suffix=".mp4", dir=output_dir,
    )
    os.close(fd)
    try:
        result = subprocess.run(
            [
                "ffmpeg", "-y", "-loglevel", "error", "-i", video_path,
                "-map", "0:v:0", "-map", "0:a?", "-vf", video_filter,
                "-c:v", "libx264", "-preset", "fast", "-crf", "18",
                "-c:a", "copy", "-movflags", "+faststart", repaired_path,
            ],
            capture_output=True,
            text=True,
            timeout=180,
        )
        if result.returncode != 0 or os.path.getsize(repaired_path) == 0:
            raise RuntimeError((result.stderr or "ffmpeg repair failed")[-500:])
        os.replace(repaired_path, video_path)
    finally:
        if os.path.exists(repaired_path):
            try:
                os.unlink(repaired_path)
            except OSError:
                pass
    return crop_fraction


def process_film_frame_artifact(
    video_path: str,
    *,
    job_id: str | None = None,
) -> FilmFrameArtifact | None:
    """Detect and optionally repair the one known physical-frame signature.

    Modes:
      * ``repair`` (default): local uniform zoom; no Veo request.
      * ``shadow``: log only.
      * ``off``: skip even sample extraction.

    Detection or repair failures are deliberately non-fatal.  On a failed
    repair the original file remains byte-for-byte untouched.
    """

    mode = os.environ.get("BG_FILM_FRAME_GUARD_MODE", "repair").strip().lower()
    if mode in {"off", "disabled", "0"}:
        return None

    finding = detect_from_grayscale_frames(_extract_grayscale_samples(video_path))
    if not finding:
        return None
    if mode != "repair":
        logger.warning(
            "[BG][FILM-FRAME][SHADOW] job=%s mode=%s %s",
            job_id or "unknown",
            mode,
            finding.log_fields(),
        )
        return finding

    try:
        crop_fraction = _repair_with_uniform_zoom(video_path, finding)
        logger.warning(
            "[BG][FILM-FRAME][REPAIRED] job=%s crop_per_side=%.3f %s",
            job_id or "unknown",
            crop_fraction,
            finding.log_fields(),
        )
    except Exception as exc:
        logger.exception(
            "[BG][FILM-FRAME][REPAIR-FAILED] job=%s original_preserved=true "
            "%s error=%s",
            job_id or "unknown",
            finding.log_fields(),
            exc,
        )
    return finding
