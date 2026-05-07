"""Content validation — checks AI-generated output for prohibited content.

Uses Gemini Vision to detect people, faces, text, logos, and other
content that violates UMG Guidelines (Guideline 15).
"""

import logging
import os
import subprocess
import tempfile

logger = logging.getLogger("genly.validator")


def _safe_ffmpeg_path(path: str) -> str:
    """Make a user-controlled path safe to pass as an ffmpeg/ffprobe input.

    ffmpeg interprets any argument starting with `-` as a flag — so a file
    literally named `-vf scale=-1:720` (or an attacker-uploaded path that
    starts with `-`) would inject options into the command line. We force
    a leading `./` for relative paths so the dash can't appear in argv[0]
    of the path arg, and we resolve to an absolute path when possible.
    """
    if not path:
        return path
    if os.path.isabs(path):
        return path
    if path.startswith("-"):
        return os.path.join(".", path)
    return path


def _extract_frames(
    video_path: str,
    interval_seconds: int = 3,
    max_frames: int = 10,
) -> tuple[list[str], str]:
    """Extract frames from a video at regular intervals using ffmpeg.

    Returns (frame_paths, tmp_dir). The caller is responsible for cleaning
    up tmp_dir (and the frames inside it) regardless of whether any frames
    were successfully extracted — returning the dir explicitly here closes
    the leak where ffprobe/ffmpeg failures left mkdtemp orphans in /tmp on
    long-running workers.
    """
    tmp_dir = tempfile.mkdtemp(prefix="genly_validate_")
    safe_video_path = _safe_ffmpeg_path(video_path)
    # `safe_video_path` already prepends `./` for relative paths beginning
    # with `-`; that is the most portable defense across ffmpeg/ffprobe
    # versions (some accept GNU `--`, some don't).
    duration_cmd = [
        "ffprobe", "-v", "error", "-show_entries", "format=duration",
        "-of", "default=noprint_wrappers=1:nokey=1",
        safe_video_path,
    ]
    try:
        result = subprocess.run(duration_cmd, capture_output=True, text=True, timeout=30)
        duration = float(result.stdout.strip())
    except Exception:
        duration = 60.0

    # Calculate frame timestamps
    timestamps = []
    t = 1.0  # skip first second (often black)
    while t < duration and len(timestamps) < max_frames:
        timestamps.append(t)
        t += interval_seconds

    frame_paths = []
    for i, ts in enumerate(timestamps):
        out_path = os.path.join(tmp_dir, f"frame_{i:03d}.jpg")
        cmd = [
            "ffmpeg", "-y", "-ss", str(ts), "-i", safe_video_path,
            "-frames:v", "1", "-q:v", "2", out_path,
        ]
        try:
            subprocess.run(cmd, capture_output=True, timeout=30)
            if os.path.exists(out_path) and os.path.getsize(out_path) > 0:
                frame_paths.append(out_path)
        except Exception as e:
            logger.warning(f"Frame extraction failed at {ts}s: {e}")

    return frame_paths, tmp_dir


class ValidatorCheckError(Exception):
    """Raised when the Vision check could not produce a verdict (network,
    auth, malformed response). Distinct from "checked-and-flagged" so the
    caller can fail-closed instead of silently passing."""


def _check_frame_with_gemini(image_path: str) -> dict:
    """Use Gemini Vision to check a single frame for prohibited content.

    Returns {"safe": bool, "issues": [str]} on success.
    Raises ValidatorCheckError if the verdict cannot be produced (caller
    must decide policy: pre-fix this fell through to "safe" silently,
    which let any Vision outage approve every job — Guideline 15 risk).
    """
    from pipeline import _get_genai_client
    from google import genai
    import json as _json
    import re

    client = _get_genai_client()

    with open(image_path, "rb") as f:
        image_bytes = f.read()

    try:
        response = client.models.generate_content(
            model="gemini-2.5-flash",
            contents=[
                genai.types.Part.from_bytes(data=image_bytes, mime_type="image/jpeg"),
                (
                    "You are auditing a frame from a music-video background "
                    "for risks where AI image generation typically fails. "
                    "Be CONSERVATIVE — flag only flagrant, prominent "
                    "violations. Music-video backgrounds routinely include "
                    "incidental signage, urban scenery, and stylized text "
                    "as part of the aesthetic; that is acceptable.\n\n"
                    "FLAG (safe=false) ONLY if ALL of these are true at once:\n"
                    "  (a) the issue is in the foreground or central to the "
                    "frame (NOT background scenery, NOT distant signage, "
                    "NOT a small element in a wider shot),\n"
                    "  (b) the issue is clearly readable / recognizable "
                    "without effort (NOT blurred, NOT partial, NOT tiny),\n"
                    "  (c) the issue falls into one of these categories:\n"
                    "    - A LARGE, FOREGROUND, IDENTIFIABLE human face "
                    "(eyes/nose/mouth clearly visible on a recognizable "
                    "specific individual).\n"
                    "    - Visible hands or individual fingers as the "
                    "subject of the frame.\n"
                    "    - Text matching a globally famous COMMERCIAL brand "
                    "(Nike, Coca-Cola, McDonald's, Apple, Pepsi, Adidas, "
                    "Starbucks, Microsoft, Google, Amazon, Disney) shown "
                    "prominently as the focus of the frame.\n"
                    "    - A clearly-rendered logo of a globally famous "
                    "commercial brand shown prominently.\n\n"
                    "DO NOT FLAG (safe=true) any of these — they are "
                    "acceptable in music-video backgrounds:\n"
                    "  - Names of bands, artists, songs, albums, venues, "
                    "stadiums, sports clubs, cities, countries, places, "
                    "or events. These are NOT commercial brands for "
                    "this purpose.\n"
                    "  - Generic words on signage (BAR, HOTEL, CAFE, OPEN, "
                    "SALE, etc.) even if real-looking.\n"
                    "  - Background signage, billboards, marquees, neon "
                    "signs that are part of the urban/scenic backdrop.\n"
                    "  - Distant, small, blurred, motion-blurred, rain-"
                    "distorted, or partially obscured text or logos.\n"
                    "  - Invented / gibberish / stylized text strings.\n"
                    "  - Silhouettes, audiences, distant crowds — even "
                    "with small partial faces.\n"
                    "  - Abstract glowing shapes, smoke, particles, "
                    "weather effects, lighting effects, lens flares.\n"
                    "  - Generic pattern textures, abstract graphic "
                    "elements.\n\n"
                    "Rule of thumb: would a typical viewer say 'this video "
                    "is selling Nike / showing a real celebrity'? If no, "
                    "mark safe. When in doubt, mark safe. "
                    "Respond ONLY with JSON: "
                    '{"safe":true/false,"issues":["specific reason"]}'
                ),
            ],
            config=genai.types.GenerateContentConfig(
                temperature=0.1,
                max_output_tokens=300,
                thinking_config=genai.types.ThinkingConfig(thinking_budget=0),
            ),
        )
        text = response.text.strip()
        json_match = re.search(r'\{.*\}', text, re.DOTALL)
        if json_match:
            data = _json.loads(json_match.group())
            return {
                "safe": bool(data.get("safe", True)),
                "issues": list(data.get("issues", [])),
            }
    except Exception as e:
        logger.warning(f"Gemini Vision check failed: {e}")
        raise ValidatorCheckError(str(e)) from e

    raise ValidatorCheckError("Gemini Vision response did not contain a JSON verdict")


def validate_video(video_path: str, job_id: str = None) -> dict:
    """Validate a video for prohibited content.

    Extracts frames and checks each with Gemini Vision.

    Returns {"passed": bool, "issues": [{"frame": int, "type": str}]}
    """
    from provenance import record_ai_call

    recorder = record_ai_call(
        job_id=job_id or "unknown",
        step="output_validation",
        tool_name="gemini-2.5-flash-vision",
        tool_provider="google_vertex",
        prompt="Content policy validation: check frames for people, faces, text, logos",
        input_data_types=["video_frames"],
    ) if job_id else None

    frame_paths, tmp_dir = _extract_frames(video_path)
    all_issues = []
    check_errors = 0
    frames_checked = 0

    try:
        for i, frame_path in enumerate(frame_paths):
            try:
                result = _check_frame_with_gemini(frame_path)
            except ValidatorCheckError as e:
                check_errors += 1
                logger.warning("[VALIDATION] frame %d check error: %s", i, e)
                continue
            frames_checked += 1
            if not result["safe"]:
                for issue in result["issues"]:
                    all_issues.append({"frame": i, "type": issue})
    finally:
        # Clean up temp frames + dir even when _extract_frames produced
        # zero usable frames (mkdtemp leaks otherwise).
        for fp in frame_paths:
            try:
                os.unlink(fp)
            except OSError:
                pass
        if tmp_dir:
            try:
                os.rmdir(tmp_dir)
            except OSError:
                pass

    # Fail-closed: if no frames could be successfully checked, refuse to
    # approve. Pre-fix this returned passed=True, letting any Vision outage
    # silently bypass the UMG Guideline 15 gate.
    has_verdict = frames_checked > 0
    passed = has_verdict and len(all_issues) == 0
    summary = (
        f"passed={passed}, frames_checked={frames_checked}, "
        f"check_errors={check_errors}, issues={len(all_issues)}"
    )

    if not has_verdict:
        # Surface a synthetic issue so the operator sees why the job was
        # blocked instead of "validation failed: 0 issues".
        all_issues.append({
            "frame": -1,
            "type": (
                "Validator could not produce a verdict for any frame "
                f"({check_errors} check errors). Treating as failed per "
                "fail-closed policy."
            ),
        })

    if recorder:
        recorder.finish(response_summary=summary)

    logger.info(f"[VALIDATION] job={job_id}: {summary}")
    return {
        "passed": passed,
        "issues": all_issues,
        "frames_checked": frames_checked,
        "check_errors": check_errors,
    }


def validate_image(image_path: str, job_id: str = None) -> dict:
    """Validate a single image for prohibited content."""
    from provenance import record_ai_call

    recorder = record_ai_call(
        job_id=job_id or "unknown",
        step="output_validation",
        tool_name="gemini-2.5-flash-vision",
        tool_provider="google_vertex",
        prompt="Content policy validation: check image for people, faces, text, logos",
        input_data_types=["image"],
    ) if job_id else None

    try:
        result = _check_frame_with_gemini(image_path)
    except ValidatorCheckError as e:
        logger.warning("[VALIDATION] image check error: %s", e)
        if recorder:
            recorder.finish(response_summary=f"passed=False, check_error={e}")
        return {
            "passed": False,
            "issues": [{
                "frame": 0,
                "type": (
                    "Validator could not produce a verdict (Vision API "
                    f"error: {e}). Treating as failed per fail-closed policy."
                ),
            }],
            "frames_checked": 0,
            "check_errors": 1,
        }
    issues = [{"frame": 0, "type": issue} for issue in result.get("issues", [])]
    passed = result.get("safe", True)

    if recorder:
        recorder.finish(response_summary=f"passed={passed}, issues={len(issues)}")

    return {"passed": passed, "issues": issues, "frames_checked": 1, "check_errors": 0}
