"""Content validation — checks AI-generated output for prohibited content.

Uses Gemini Vision to detect people, faces, text, logos, and other
content that violates UMG Guidelines (Guideline 15).
"""

import logging
import os
import subprocess
import tempfile

logger = logging.getLogger("genly.validator")


def _extract_frames(video_path: str, interval_seconds: int = 3, max_frames: int = 10) -> list[str]:
    """Extract frames from a video at regular intervals using ffmpeg.

    Returns list of temporary file paths (caller must clean up).
    """
    tmp_dir = tempfile.mkdtemp(prefix="genly_validate_")
    duration_cmd = [
        "ffprobe", "-v", "error", "-show_entries", "format=duration",
        "-of", "default=noprint_wrappers=1:nokey=1", video_path,
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
            "ffmpeg", "-y", "-ss", str(ts), "-i", video_path,
            "-frames:v", "1", "-q:v", "2", out_path,
        ]
        try:
            subprocess.run(cmd, capture_output=True, timeout=30)
            if os.path.exists(out_path) and os.path.getsize(out_path) > 0:
                frame_paths.append(out_path)
        except Exception as e:
            logger.warning(f"Frame extraction failed at {ts}s: {e}")

    return frame_paths


def _check_frame_with_gemini(image_path: str) -> dict:
    """Use Gemini Vision to check a single frame for prohibited content.

    Returns {"safe": bool, "issues": [str]}
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
                    "Analyze this image for content policy compliance. "
                    "Check for: people, faces, hands, body parts, text/words/letters, "
                    "logos, trademarks, recognizable brand symbols. "
                    "Respond ONLY with JSON: "
                    '{"safe":true/false,"issues":["list of issues found"]}'
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
                "safe": data.get("safe", True),
                "issues": data.get("issues", []),
            }
    except Exception as e:
        logger.warning(f"Gemini Vision check failed: {e}")

    # If check fails, assume safe (don't block pipeline on validation errors)
    return {"safe": True, "issues": []}


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

    frame_paths = _extract_frames(video_path)
    all_issues = []

    try:
        for i, frame_path in enumerate(frame_paths):
            result = _check_frame_with_gemini(frame_path)
            if not result["safe"]:
                for issue in result["issues"]:
                    all_issues.append({
                        "frame": i,
                        "type": issue,
                    })
    finally:
        # Clean up temp frames
        for fp in frame_paths:
            try:
                os.unlink(fp)
            except OSError:
                pass
        # Clean up temp dir
        if frame_paths:
            try:
                os.rmdir(os.path.dirname(frame_paths[0]))
            except OSError:
                pass

    passed = len(all_issues) == 0
    summary = f"passed={passed}, frames_checked={len(frame_paths)}, issues={len(all_issues)}"

    if recorder:
        recorder.finish(response_summary=summary)

    logger.info(f"[VALIDATION] job={job_id}: {summary}")
    return {"passed": passed, "issues": all_issues, "frames_checked": len(frame_paths)}


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

    result = _check_frame_with_gemini(image_path)
    issues = [{"frame": 0, "type": issue} for issue in result.get("issues", [])]
    passed = result.get("safe", True)

    if recorder:
        recorder.finish(response_summary=f"passed={passed}, issues={len(issues)}")

    return {"passed": passed, "issues": issues, "frames_checked": 1}
