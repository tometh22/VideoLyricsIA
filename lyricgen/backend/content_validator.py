"""Content validation for generated background assets.

Gemini Vision is used as a classifier, not as the policy authority.  It
reports independent detections for people, atmospheric effects and commercial
brands; the caller then decides which dimensions are allowed for this render.
Keeping detection separate from enforcement prevents an opt-in for one visual
element from accidentally disabling the other safety gates.
"""

import hashlib
import logging
import os
import subprocess
import tempfile
import threading
from functools import lru_cache

logger = logging.getLogger("genly.validator")

GEMINI_VALIDATOR_VERSION = "gemini-2.5-flash-vision-v2"
LOCAL_VALIDATOR_VERSION = "opencv-yunet-face-v1"
_YUNET_MODEL_SHA256 = "8f2383e4dd3cfbb4553ea8718107fc0423210dc964f9f4280604804ed2552fa4"
_LOCAL_DETECTOR_LOCK = threading.Lock()


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
    interval_seconds: float = 1,
    max_frames: int = 48,
) -> tuple[list[str], str, int]:
    """Extract frames from a video at regular intervals using ffmpeg.

    Returns (frame_paths, tmp_dir, planned_frames). The caller is responsible for cleaning
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
        # Unknown duration is not a safe basis for sampling: assuming 60s used
        # to approve only the beginning of a longer asset.
        return [], tmp_dir, 1

    # Cover the WHOLE asset.  The old implementation stopped after the first
    # 28 seconds (10 frames at 3-second intervals), so a person in a later
    # scene of a long UMG video was invisible to the gate.  Short clips retain
    # the dense interval; long assets are sampled uniformly end-to-end.
    usable_start = min(1.0, max(duration * 0.05, 0.0))
    usable_end = max(usable_start, duration - min(0.5, duration * 0.02))
    dense_count = max(1, int(max(usable_end - usable_start, 0.0) / interval_seconds) + 1)
    sample_count = min(max_frames, dense_count)
    if sample_count <= 1:
        timestamps = [usable_start]
    else:
        step = (usable_end - usable_start) / (sample_count - 1)
        timestamps = [usable_start + (step * i) for i in range(sample_count)]

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

    return frame_paths, tmp_dir, len(timestamps)


def _extract_frames_with_opencv(
    video_path: str,
    interval_seconds: float = 0.25,
    max_frames: int = 192,
) -> tuple[list[str], str, int]:
    """Extract uniformly distributed samples with one local decoder.

    Spawning one ffmpeg process per dense sample made the independent
    Universal pass unnecessarily expensive. OpenCV keeps one decoder open,
    still covers the complete asset, and returns an explicit planned count so
    a partial decode remains a fail-closed result.
    """
    tmp_dir = tempfile.mkdtemp(prefix="genly_validate_local_")
    try:
        cv2, _detector = _local_detector_assets()
        capture = cv2.VideoCapture(_safe_ffmpeg_path(video_path))
        if not capture.isOpened():
            return [], tmp_dir, 1
        try:
            fps = float(capture.get(cv2.CAP_PROP_FPS) or 0.0)
            frame_count = float(capture.get(cv2.CAP_PROP_FRAME_COUNT) or 0.0)
            duration = frame_count / fps if fps > 0 and frame_count > 0 else 0.0
            if duration <= 0:
                return [], tmp_dir, 1

            usable_start = min(1.0, max(duration * 0.05, 0.0))
            usable_end = max(
                usable_start,
                duration - min(0.5, duration * 0.02),
            )
            dense_count = max(
                1,
                int(max(usable_end - usable_start, 0.0) / interval_seconds) + 1,
            )
            sample_count = min(max_frames, dense_count)
            if sample_count <= 1:
                timestamps = [usable_start]
            else:
                step = (usable_end - usable_start) / (sample_count - 1)
                timestamps = [usable_start + (step * i) for i in range(sample_count)]

            paths: list[str] = []
            for index, timestamp in enumerate(timestamps):
                capture.set(cv2.CAP_PROP_POS_MSEC, timestamp * 1000.0)
                ok, frame = capture.read()
                if not ok or frame is None or not getattr(frame, "size", 0):
                    continue
                out_path = os.path.join(tmp_dir, f"frame_{index:03d}.jpg")
                if cv2.imwrite(out_path, frame):
                    paths.append(out_path)
            return paths, tmp_dir, len(timestamps)
        finally:
            capture.release()
    except LocalDetectorCheckError:
        return [], tmp_dir, 1
    except Exception as exc:
        logger.warning("OpenCV frame extraction failed: %s", exc)
        return [], tmp_dir, 1


class ValidatorCheckError(Exception):
    """Raised when the Vision check could not produce a verdict (network,
    auth, malformed response). Distinct from "checked-and-flagged" so the
    caller can fail-closed instead of silently passing."""


class LocalDetectorCheckError(Exception):
    """Raised when the independent local face/person detector cannot run."""


@lru_cache(maxsize=1)
def _local_detector_assets():
    """Load OpenCV's provider-free YuNet face detector once per worker.

    YuNet is materially more selective than Haar/HOG on stylized backgrounds:
    lamps, dogs and building windows produced too many false positives during
    the retrospective Universal audit. Gemini remains the high-recall semantic
    detector for people/figures; this independent pass is deliberately focused
    on the client's highest-risk failure mode: a visible face.
    """
    try:
        import cv2

        model_path = os.path.join(
            os.path.dirname(__file__),
            "assets", "models", "face_detection_yunet_2023mar.onnx",
        )
        if not os.path.isfile(model_path):
            raise RuntimeError("bundled YuNet face model is unavailable")
        with open(model_path, "rb") as model_file:
            model_sha256 = hashlib.sha256(model_file.read()).hexdigest()
        if model_sha256 != _YUNET_MODEL_SHA256:
            raise RuntimeError("bundled YuNet face model checksum is invalid")
        detector = cv2.FaceDetectorYN.create(
            model=model_path,
            config="",
            input_size=(320, 320),
            # Calibrated against the complete historical Universal set:
            # 0.80 still confused rain droplets (0.817) and a full moon
            # (0.803) with a face; real visible faces in the same set scored
            # 0.861+. Keep the threshold explicit and regression-tested.
            score_threshold=0.85,
            nms_threshold=0.3,
            top_k=5000,
        )
        return cv2, detector
    except Exception as exc:  # pragma: no cover - exact import error is platform-specific
        raise LocalDetectorCheckError(str(exc)) from exc


def _check_frame_with_local_detector(image_path: str) -> dict:
    """Detect visible faces without a network/provider call."""
    try:
        cv2, detector = _local_detector_assets()
        image = cv2.imread(image_path)
        if image is None or not getattr(image, "size", 0):
            raise RuntimeError("OpenCV could not decode sampled frame")

        height, width = image.shape[:2]
        max_width = 1280
        if width > max_width:
            scale = max_width / float(width)
            image = cv2.resize(
                image,
                (max_width, max(1, int(round(height * scale)))),
                interpolation=cv2.INTER_AREA,
            )
        height, width = image.shape[:2]
        # FaceDetectorYN mutates its configured input size; serialize calls in
        # case an API process validates two previews concurrently.
        with _LOCAL_DETECTOR_LOCK:
            detector.setInputSize((width, height))
            _status, faces = detector.detect(image)
        face_count = 0 if faces is None else len(faces)
        evidence: list[str] = []
        if face_count:
            min_score = min(float(face[-1]) for face in faces)
            evidence.append(
                f"visible_face:{face_count}:min_score={min_score:.3f}"
            )

        return {"people": bool(evidence), "evidence": evidence}
    except LocalDetectorCheckError:
        raise
    except Exception as exc:
        raise LocalDetectorCheckError(str(exc)) from exc


def _cleanup_extracted_frames(frame_paths: list[str], tmp_dir: str | None) -> None:
    for frame_path in frame_paths:
        try:
            os.unlink(frame_path)
        except OSError:
            pass
    if tmp_dir:
        try:
            os.rmdir(tmp_dir)
        except OSError:
            pass


def _check_frame_with_gemini(image_path: str) -> dict:
    """Classify policy-relevant content in one frame with Gemini Vision.

    Returns ``safe`` for backwards compatibility plus independent
    ``detections`` and categorized ``issues``.  ``safe`` only reflects the
    immutable people/brand baseline; atmospheric permission is applied by
    :func:`_evaluate_frame_result`.
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
        # Audit 2026-05-26: timeout wrapper. Validator runs Gemini Vision
        # per frame (~5 frames per UMG render); a Vertex hang here would
        # block the validation step indefinitely. find_orphan_polling_jobs
        # wouldn't catch it for 10 min because Vision doesn't record an
        # AIProvenance row at call start. 45s is generous (single-image
        # Vision call p99 ~5s) but bounds the worst case so the validator
        # can fall through to its outer except → raise → pipeline marks
        # validation_failed instead of hanging.
        from pipeline import _call_with_timeout
        _prompt_text = (
            "You are classifying a frame from a music-video background. "
            "Report each category independently; do not decide which category "
            "the customer is allowed to use. Classify visible evidence, not "
            "inferred activity or objects that suggest somebody may have been "
            "nearby.\n\n"
            "Set detections.people=true if the frame contains ANY human "
            "presence, including a person, face, head, body, limb, hand, figure, "
            "silhouette, reflection, audience, crowd, statue/mannequin that "
            "looks human, or a distant/blurred/partial human shape. Human "
            "presence is forbidden regardless of size, prominence, identity, "
            "focus, realism, or whether the face is recognizable.\n\n"
            "Do NOT set detections.people=true for inanimate equipment or other "
            "indirect signs of activity alone: tripods, cameras, microphones, "
            "instruments, furniture, vehicles, clothing laid on a surface, "
            "footprints, tools, doors, paths, or recently disturbed objects are "
            "not people. They count only when visible human anatomy or a clearly "
            "human-shaped figure/silhouette/statue/mannequin is also present.\n\n"
            "Set detections.atmospherics=true if smoke, fog, haze, mist, "
            "steam, vapor, a smoke machine, or a smoke-like atmospheric cloud "
            "is visibly present. Ordinary clouds in the sky, rain, dust, lens "
            "flare, bokeh and solid objects are not atmospherics.\n\n"
            "Set detections.brand=true for these legal/IP risks:\n"
            "    - Text matching a globally famous COMMERCIAL brand "
            "(Nike, Coca-Cola, McDonald's, Apple, Pepsi, Adidas, "
            "Starbucks, Microsoft, Google, Amazon, Disney) shown "
            "prominently as the focus of the frame.\n"
            "    - A clearly-rendered logo of a globally famous "
            "commercial brand shown prominently.\n\n"
            "The following non-human elements are acceptable:\n"
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
            "  - Abstract glowing shapes, particles, ordinary weather effects, "
            "lighting effects, lens flares. (Still report smoke/fog/haze/mist/"
            "steam/vapor separately as atmospherics.)\n"
            "  - Generic pattern textures, abstract graphic "
            "elements.\n\n"
            "When a visible shape itself may be human, set people=true. Do not "
            "infer a person solely because an object could be operated by one. "
            "Respond ONLY with JSON using exactly this schema: "
            '{"detections":{"people":true/false,"atmospherics":true/false,'
            '"brand":true/false},"issues":[{"category":"people|atmospherics|brand",'
            '"reason":"specific visible evidence"}]}'
        )
        response = _call_with_timeout(
            lambda: client.models.generate_content(
                model="gemini-2.5-flash",
                contents=[
                    genai.types.Part.from_bytes(data=image_bytes, mime_type="image/jpeg"),
                    _prompt_text,
                ],
                config=genai.types.GenerateContentConfig(
                    temperature=0.1,
                    max_output_tokens=300,
                    thinking_config=genai.types.ThinkingConfig(thinking_budget=0),
                ),
            ),
            timeout_s=45.0,
            label="VALIDATOR",
        )
        text = response.text.strip()
        json_match = re.search(r'\{.*\}', text, re.DOTALL)
        if json_match:
            data = _json.loads(json_match.group())
            detections = data.get("detections")
            if not isinstance(detections, dict):
                raise ValidatorCheckError("Gemini Vision 'detections' must be a JSON object")
            categories = ("people", "atmospherics", "brand")
            if any(type(detections.get(category)) is not bool for category in categories):
                raise ValidatorCheckError(
                    "Gemini Vision detections must contain boolean people, atmospherics and brand"
                )
            issues = data.get("issues", [])
            if not isinstance(issues, list) or not all(
                isinstance(item, dict)
                and item.get("category") in categories
                and isinstance(item.get("reason"), str)
                and bool(item.get("reason", "").strip())
                for item in issues
            ):
                raise ValidatorCheckError(
                    "Gemini Vision 'issues' must contain categorized reasons"
                )
            # A categorized issue is positive evidence even if the model also
            # emitted a contradictory false boolean.  Promote it here rather
            # than trusting the more permissive half of an inconsistent LLM
            # response.  This preserves the validator's fail-closed contract
            # for people, brands and atmospheric effects independently.
            for issue in issues:
                if issue.get("reason", "").strip():
                    detections[issue["category"]] = True
            return {
                "safe": not detections["people"] and not detections["brand"],
                "detections": {category: detections[category] for category in categories},
                "issues": issues,
            }
    except Exception as e:
        logger.warning(f"Gemini Vision check failed: {e}")
        raise ValidatorCheckError(str(e)) from e

    raise ValidatorCheckError("Gemini Vision response did not contain a JSON verdict")


def _evaluate_frame_result(
    result: dict,
    *,
    allow_people: bool,
    allow_atmospherics: bool,
    enforce_atmospherics: bool,
) -> dict:
    """Apply render policy to a classifier result.

    Test doubles and in-flight callers may still return the legacy
    ``{"safe": bool, "issues": [str]}`` shape.  That shape remains supported
    fail-closed; it cannot be used to bypass an unsafe result when people are
    allowed because it does not identify which category was detected.
    """
    detections = result.get("detections")
    if not isinstance(detections, dict):
        legacy_safe = result.get("safe") is True
        legacy_issues = [str(item) for item in result.get("issues", [])]
        return {
            # Legacy responses cannot attribute an issue to an independently
            # configurable category. Any issue therefore blocks; allowing a
            # human must never accidentally allow a brand (or vice versa).
            "passed": legacy_safe and not legacy_issues,
            "issues": legacy_issues,
            "detections": {"people": False, "atmospherics": False, "brand": False},
            "observations": [],
        }

    normalized = {
        "people": detections.get("people") is True,
        "atmospherics": detections.get("atmospherics") is True,
        "brand": detections.get("brand") is True,
    }
    reasons_by_category: dict[str, list[str]] = {
        "people": [], "atmospherics": [], "brand": [],
    }
    for item in result.get("issues", []):
        if isinstance(item, dict) and item.get("category") in reasons_by_category:
            reason = str(item.get("reason") or "").strip()
            if reason:
                reasons_by_category[item["category"]].append(reason)
        elif isinstance(item, str):
            # Defensive compatibility with a partially migrated response.
            reasons_by_category["brand"].append(item)

    # Test doubles and partially migrated callers may bypass the strict JSON
    # parser above. Apply the same contradiction rule at this boundary too.
    for category, reasons in reasons_by_category.items():
        if reasons:
            normalized[category] = True

    blocked_categories = []
    if normalized["people"] and not allow_people:
        blocked_categories.append("people")
    if normalized["brand"]:
        blocked_categories.append("brand")
    if (
        normalized["atmospherics"]
        and not allow_atmospherics
        and enforce_atmospherics
    ):
        blocked_categories.append("atmospherics")

    blocked_issues = []
    for category in blocked_categories:
        reasons = reasons_by_category[category] or [f"Detected prohibited {category}"]
        blocked_issues.extend(f"{category}: {reason}" for reason in reasons)

    observations = []
    if normalized["atmospherics"] and not allow_atmospherics:
        reasons = reasons_by_category["atmospherics"] or ["Atmospheric effect detected"]
        observations.extend(f"atmospherics: {reason}" for reason in reasons)

    return {
        "passed": not blocked_categories,
        "issues": blocked_issues,
        "detections": normalized,
        "observations": observations,
    }


def validate_video(
    video_path: str,
    job_id: str = None,
    *,
    allow_people: bool = False,
    allow_atmospherics: bool = True,
    observe_atmospherics: bool = False,
    enforce_atmospherics: bool = False,
    require_secondary: bool = False,
) -> dict:
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
        prompt=(
            "Classify frames for people, commercial brands and atmospheric effects; "
            f"allow_people={allow_people}, allow_atmospherics={allow_atmospherics}, "
            f"observe_atmospherics={observe_atmospherics}, "
            f"enforce_atmospherics={enforce_atmospherics}"
        ),
        input_data_types=["video_frames"],
    ) if job_id else None

    frame_paths, tmp_dir, planned_frames = _extract_frames(video_path)
    secondary_paths: list[str] = []
    secondary_tmp_dir: str | None = None
    secondary_planned = 0
    all_issues = []
    check_errors = 0
    frames_checked = 0
    secondary_errors = 0
    secondary_frames_checked = 0
    secondary_extraction_errors = 0
    detected = {"people": False, "atmospherics": False, "brand": False}
    observations = []

    try:
        # Universal gets a provider-independent, denser 4 fps pass first.
        # It catches faces that appear between Gemini's semantic samples and
        # prevents a single network model from being the sole approval signal.
        if require_secondary:
            secondary_paths, secondary_tmp_dir, secondary_planned = _extract_frames_with_opencv(
                video_path, interval_seconds=0.25, max_frames=192
            )
            secondary_extraction_errors = max(
                0, secondary_planned - len(secondary_paths)
            )
            for i, frame_path in enumerate(secondary_paths):
                try:
                    local_result = _check_frame_with_local_detector(frame_path)
                except LocalDetectorCheckError as exc:
                    secondary_errors += 1
                    logger.warning(
                        "[VALIDATION] local detector frame %d error: %s", i, exc
                    )
                    break
                secondary_frames_checked += 1
                if local_result.get("people") is True:
                    detected["people"] = True
                    evidence = ", ".join(local_result.get("evidence") or [])
                    all_issues.append({
                        "frame": i,
                        "type": (
                            "people: independent local face/person detector "
                            f"reported visible human evidence ({evidence or 'unspecified'})"
                        ),
                        "validator": LOCAL_VALIDATOR_VERSION,
                    })
                    break

            secondary_complete = bool(
                secondary_planned > 0
                and secondary_extraction_errors == 0
                and secondary_frames_checked == secondary_planned
                and secondary_errors == 0
            )
            if not secondary_complete and not all_issues:
                all_issues.append({
                    "frame": -1,
                    "type": (
                        "Independent local validator did not inspect every dense "
                        f"sample ({secondary_frames_checked}/{secondary_planned} checked, "
                        f"{secondary_extraction_errors} extraction errors, "
                        f"{secondary_errors} detector errors). Treating as failed "
                        "per fail-closed Universal policy."
                    ),
                    "validator": LOCAL_VALIDATOR_VERSION,
                })
        else:
            secondary_complete = True

        # A decisive/incomplete local verdict already blocks the candidate;
        # avoid spending paid Gemini calls on an asset that cannot be approved.
        if not all_issues:
            for i, frame_path in enumerate(frame_paths):
                try:
                    result = _check_frame_with_gemini(frame_path)
                except ValidatorCheckError as e:
                    check_errors += 1
                    logger.warning("[VALIDATION] frame %d check error: %s", i, e)
                    break
                frames_checked += 1
                verdict = _evaluate_frame_result(
                    result,
                    allow_people=allow_people,
                    allow_atmospherics=allow_atmospherics,
                    enforce_atmospherics=enforce_atmospherics,
                )
                for category, present in verdict["detections"].items():
                    detected[category] = detected[category] or present
                if observe_atmospherics:
                    observations.extend(
                        {"frame": i, "type": issue}
                        for issue in verdict["observations"]
                    )
                if not verdict["passed"]:
                    for issue in verdict["issues"]:
                        all_issues.append({"frame": i, "type": issue})
                    break
    finally:
        _cleanup_extracted_frames(frame_paths, tmp_dir)
        _cleanup_extracted_frames(secondary_paths, secondary_tmp_dir)

    # Fail closed on ANY partial extraction/check failure. Previously one
    # successful safe frame plus nine Vision timeouts passed the asset. A
    # decisive policy finding is different: once a sampled frame visibly
    # contains a blocked category, checking later frames cannot turn the asset
    # safe, so the intentional early exit is a complete negative verdict.
    expected_frames = planned_frames
    extraction_errors = max(0, planned_frames - len(frame_paths))
    primary_complete = (
        expected_frames > 0
        and extraction_errors == 0
        and frames_checked == expected_frames
        and check_errors == 0
    )
    complete_verdict = primary_complete and secondary_complete
    decisive_policy_failure = bool(all_issues)
    passed = complete_verdict and not decisive_policy_failure
    summary = (
        f"passed={passed}, frames_checked={frames_checked}, "
        f"extraction_errors={extraction_errors}, check_errors={check_errors}, "
        f"secondary_frames_checked={secondary_frames_checked}, "
        f"secondary_extraction_errors={secondary_extraction_errors}, "
        f"secondary_errors={secondary_errors}, issues={len(all_issues)}"
    )

    if not complete_verdict and not decisive_policy_failure:
        # Surface a synthetic issue so the operator sees why the job was
        # blocked instead of "validation failed: 0 issues".
        all_issues.append({
            "frame": -1,
            "type": (
                "Validator did not produce a verdict for every sampled frame "
                f"({frames_checked}/{expected_frames} checked, {extraction_errors} extraction errors, "
                f"{check_errors} check errors). Treating as failed per "
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
        "frames_planned": planned_frames,
        "extraction_errors": extraction_errors,
        "check_errors": check_errors,
        "secondary_required": bool(require_secondary),
        "secondary_frames_checked": secondary_frames_checked,
        "secondary_frames_planned": secondary_planned,
        "secondary_extraction_errors": secondary_extraction_errors,
        "secondary_errors": secondary_errors,
        "validator_stack": [
            GEMINI_VALIDATOR_VERSION,
            *([LOCAL_VALIDATOR_VERSION] if require_secondary else []),
        ],
        "detections": detected,
        "observations": observations,
        "shadow_atmospherics_detected": bool(
            observe_atmospherics and detected["atmospherics"] and not enforce_atmospherics
        ),
    }


def validate_image(
    image_path: str,
    job_id: str = None,
    *,
    allow_people: bool = False,
    allow_atmospherics: bool = True,
    observe_atmospherics: bool = False,
    enforce_atmospherics: bool = False,
    require_secondary: bool = False,
) -> dict:
    """Validate a single image for prohibited content."""
    from provenance import record_ai_call

    recorder = record_ai_call(
        job_id=job_id or "unknown",
        step="output_validation",
        tool_name="gemini-2.5-flash-vision",
        tool_provider="google_vertex",
        prompt=(
            "Classify image for people, commercial brands and atmospheric effects; "
            f"allow_people={allow_people}, allow_atmospherics={allow_atmospherics}, "
            f"observe_atmospherics={observe_atmospherics}, "
            f"enforce_atmospherics={enforce_atmospherics}"
        ),
        input_data_types=["image"],
    ) if job_id else None

    local_result = {"people": False, "evidence": []}
    if require_secondary:
        try:
            local_result = _check_frame_with_local_detector(image_path)
        except LocalDetectorCheckError as e:
            logger.warning("[VALIDATION] local image detector error: %s", e)
            if recorder:
                recorder.finish(response_summary=f"passed=False, secondary_error={e}")
            return {
                "passed": False,
                "issues": [{
                    "frame": 0,
                    "type": (
                        "Independent local validator could not inspect the image "
                        f"({e}). Treating as failed per fail-closed Universal policy."
                    ),
                    "validator": LOCAL_VALIDATOR_VERSION,
                }],
                "frames_checked": 0,
                "frames_planned": 1,
                "extraction_errors": 0,
                "check_errors": 0,
                "secondary_required": True,
                "secondary_frames_checked": 0,
                "secondary_frames_planned": 1,
                "secondary_extraction_errors": 0,
                "secondary_errors": 1,
                "validator_stack": [GEMINI_VALIDATOR_VERSION, LOCAL_VALIDATOR_VERSION],
            }
        if local_result.get("people") is True:
            evidence = ", ".join(local_result.get("evidence") or [])
            if recorder:
                recorder.finish(response_summary="passed=False, local_people=True")
            return {
                "passed": False,
                "issues": [{
                    "frame": 0,
                    "type": (
                        "people: independent local face/person detector reported "
                        f"visible human evidence ({evidence or 'unspecified'})"
                    ),
                    "validator": LOCAL_VALIDATOR_VERSION,
                }],
                "frames_checked": 0,
                "frames_planned": 1,
                "extraction_errors": 0,
                "check_errors": 0,
                "secondary_required": True,
                "secondary_frames_checked": 1,
                "secondary_frames_planned": 1,
                "secondary_extraction_errors": 0,
                "secondary_errors": 0,
                "validator_stack": [GEMINI_VALIDATOR_VERSION, LOCAL_VALIDATOR_VERSION],
                "detections": {
                    "people": True, "atmospherics": False, "brand": False,
                },
            }

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
            "frames_planned": 1,
            "extraction_errors": 0,
            "check_errors": 1,
            "secondary_required": bool(require_secondary),
            "secondary_frames_checked": 1 if require_secondary else 0,
            "secondary_frames_planned": 1 if require_secondary else 0,
            "secondary_extraction_errors": 0,
            "secondary_errors": 0,
            "validator_stack": [
                GEMINI_VALIDATOR_VERSION,
                *([LOCAL_VALIDATOR_VERSION] if require_secondary else []),
            ],
        }
    verdict = _evaluate_frame_result(
        result,
        allow_people=allow_people,
        allow_atmospherics=allow_atmospherics,
        enforce_atmospherics=enforce_atmospherics,
    )
    issues = [{"frame": 0, "type": issue} for issue in verdict["issues"]]
    observations = (
        [{"frame": 0, "type": issue} for issue in verdict["observations"]]
        if observe_atmospherics else []
    )
    passed = verdict["passed"]

    if recorder:
        recorder.finish(response_summary=f"passed={passed}, issues={len(issues)}")

    return {
        "passed": passed,
        "issues": issues,
        "frames_checked": 1,
        "frames_planned": 1,
        "extraction_errors": 0,
        "check_errors": 0,
        "secondary_required": bool(require_secondary),
        "secondary_frames_checked": 1 if require_secondary else 0,
        "secondary_frames_planned": 1 if require_secondary else 0,
        "secondary_extraction_errors": 0,
        "secondary_errors": 0,
        "validator_stack": [
            GEMINI_VALIDATOR_VERSION,
            *([LOCAL_VALIDATOR_VERSION] if require_secondary else []),
        ],
        "detections": {
            **verdict["detections"],
            "people": bool(
                verdict["detections"].get("people") or local_result.get("people")
            ),
        },
        "observations": observations,
        "shadow_atmospherics_detected": bool(
            observe_atmospherics
            and verdict["detections"]["atmospherics"]
            and not enforce_atmospherics
        ),
    }
