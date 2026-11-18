"""Content validation for generated background assets.

Gemini Vision is used as a classifier, not as the policy authority.  It
reports independent detections for people, malformed anatomy, atmospheric
effects, commercial brands and readable text; the caller then decides which dimensions are allowed
for this render.  Keeping detection separate from enforcement prevents an opt-in
for one visual element from accidentally disabling the other safety gates.

`text` (2026-09-03) es la categoría más nueva y arranca en OBSERVACIÓN. El riel
del prompt pedía "no text, no words, no letters" desde siempre, pero el gate no
lo hacía cumplir: `brand` sólo mira marcas famosas y las palabras genéricas de
cartelería y los "invented / gibberish text strings" estaban listados como
aceptables — que son justo el artefacto típico de un generador de video. Ahora
se detecta y se registra, pero sólo bloquea con `enforce_text=True`: antes de
ponerlo en bloqueo hay que medir su tasa de falsos positivos sobre escenas
urbanas legítimas, porque este repo ya tuvo un incidente de sobre-bloqueo del
validador (jul-2026).

QC extendido (2026-11-18, pedidos UMG de imagen): `weapon`, `gore`,
`body_parts` y `overlay` por una llamada Vision APARTE (el prompt base no se
toca), más `letterbox` medido sin IA en background_technical_qc. Gobernado por
BACKGROUND_EXTENDED_QC_MODE (default observe: se registra y se muestra en
Delivery QC, no rechaza). Ver el bloque "Extended background QC".
"""

import logging
import os
import re
import subprocess
import tempfile
import time

logger = logging.getLogger("genly.validator")

# Vertex answers a burst of frame checks with 429 RESOURCE_EXHAUSTED (per-minute
# quota) far more often than it answers with an actual verdict failure. Without
# a retry that transient quota blip becomes an "incomplete verdict", which the
# fail-closed policy converts into a hard rejection of a background the caller
# may have already certified twice (incident 2026-08-20, job 2861e17a44d4: one
# 429 on frame 6 of 8 discarded a Veo background the same job had passed at
# 8/8 an hour earlier). Retry the RETRYABLE errors — quota, unavailability,
# transport timeouts — and keep failing closed on everything else.
_VISION_MAX_ATTEMPTS = int(os.environ.get("VALIDATOR_VISION_MAX_ATTEMPTS", "3"))
_VISION_RETRY_BACKOFF_S = float(
    os.environ.get("VALIDATOR_VISION_RETRY_BACKOFF_S", "3")
)
_RETRYABLE_VISION_ERROR_RE = re.compile(
    r"429|RESOURCE_EXHAUSTED|rate.?limit|quota|"
    r"503|UNAVAILABLE|500|INTERNAL|502|504|DEADLINE_EXCEEDED|timed? ?out",
    re.IGNORECASE,
)


def _is_retryable_vision_error(exc: Exception) -> bool:
    """True for infrastructure errors that a later attempt may answer.

    Deliberately matched on the message: the Vertex SDK surfaces quota and
    availability failures as generic exceptions carrying the HTTP status in
    their text, so there is no exception class to key off.
    """
    return bool(_RETRYABLE_VISION_ERROR_RE.search(str(exc)))


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
    interval_seconds: int = 1,
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


class ValidatorCheckError(Exception):
    """Raised when the Vision check could not produce a verdict (network,
    auth, malformed response). Distinct from "checked-and-flagged" so the
    caller can fail-closed instead of silently passing."""


def _check_frame_with_gemini(image_path: str) -> dict:
    """Classify policy-relevant content in one frame with Gemini Vision.

    Returns ``safe`` for backwards compatibility plus independent
    ``detections`` and categorized ``issues``.  ``safe`` only reflects the
    people/brand/anatomy baseline; atmospheric permission is applied by
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
            "Set detections.people=true ONLY when a human is IDENTIFIABLE or "
            "PROMINENT. Exactly two cases qualify:\n"
            "  (a) A human FACE is recognizable: facial features are rendered "
            "clearly enough that the individual could be identified, even if "
            "the face is partial, in profile, reflected, or belongs to a "
            "realistic statue or mannequin.\n"
            "  (b) A person is the SUBJECT of the shot: in close-up or the "
            "foreground, occupying a significant portion of the frame, or "
            "clearly the focal point of the composition — this applies even "
            "when the face is hidden, turned away, or in silhouette.\n\n"
            "Set detections.people=false for INCIDENTAL human presence in a "
            "wide, aerial or distant view: small, far-away, blurred or "
            "motion-blurred figures, background crowds, pedestrians seen from "
            "above or across a street, passengers inside vehicles, and any "
            "figure whose face cannot be identified and who is not the focus "
            "of the shot. An aerial view of a city avenue with tiny people and "
            "cars is ACCEPTABLE and must not be reported.\n\n"
            "Do NOT set detections.people=true for inanimate equipment or other "
            "indirect signs of activity alone: tripods, cameras, microphones, "
            "instruments, furniture, vehicles, clothing laid on a surface, "
            "footprints, tools, doors, paths, or recently disturbed objects are "
            "not people.\n\n"
            "Set detections.anatomy=true for clearly malformed human anatomy: "
            "disconnected or floating limbs, extra or fused limbs, or body parts "
            "emerging implausibly from objects or the ground without a coherent "
            "body. Report this independently of people, even without a face. "
            "Do not flag normal cropping, natural occlusion, ordinary poses, "
            "disability, prostheses, empty clothing, or distant ambiguous shapes. "
            "Normal people, faces, hands and feet are NOT anatomical defects. "
            "If perspective, occlusion or cropping plausibly explains a shape, "
            "set anatomy=false. Only report unmistakable visible defects, including "
            "realistic detached mannequin limbs used as human body parts. "
            "Describe the specific visible defect, not an inferred injury.\n\n"
            "Set detections.atmospherics=true if smoke, fog, haze, mist, "
            "steam, vapor, a smoke machine, or a smoke-like atmospheric cloud "
            "is visibly present. Ordinary clouds in the sky, rain, dust, lens "
            "flare, bokeh and solid objects are not atmospherics.\n\n"
            # Categoría nueva (2026-09-03). El riel del prompt pedía "no text,
            # no words, no letters" desde siempre, pero el gate de salida NO lo
            # hacía cumplir: `brand` sólo mira marcas comerciales famosas, y más
            # abajo se listan como ACEPTABLES las palabras genéricas de
            # cartelería y los "invented / gibberish / stylized text strings" —
            # que son justo el artefacto típico de un generador de video. Un
            # fondo con garabatos de IA pasaba el gate.
            #
            # Se reporta por separado de `brand` porque son riesgos distintos:
            # `brand` es IP y bloquea siempre; `text` es un requisito de entrega
            # del sello y arranca en observación para medir su tasa de falsos
            # positivos antes de bloquear (este repo ya tuvo un incidente de
            # sobre-bloqueo del validador, jul-2026).
            "Set detections.text=true when ANY legible writing is visible in the "
            "frame: letters, numbers, words, captions or subtitles, in any "
            "language or alphabet — INCLUDING invented, garbled, misspelled or "
            "AI-hallucinated glyphs that still read as writing. Report it "
            "wherever it appears: signage, billboards, posters, banners, shop "
            "windows, screens, painted walls, packaging, clothing or vehicles.\n"
            "Set detections.text=false when the surface exists but carries no "
            "readable content: blank, unmarked, weathered, peeling, smudged, "
            "heavily blurred or too distant to resolve. A blank cardboard sign "
            "or an empty billboard is ACCEPTABLE and must not be reported. "
            "Abstract patterns, textures and shapes that merely resemble writing "
            "without resolving into characters are also acceptable.\n\n"
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
            "When in doubt about a human shape, decide by the two people rules "
            "above: report it only if a face is recognizable or the person is "
            "the subject of the shot. Do not infer a person solely because an "
            "object could be operated by one. "
            "Respond ONLY with JSON using exactly this schema: "
            '{"detections":{"people":true/false,"atmospherics":true/false,'
            '"brand":true/false,"text":true/false,"anatomy":true/false},'
            '"issues":[{"category":"people|atmospherics|brand|text|anatomy",'
            '"reason":"specific visible evidence"}]}'
        )
        response = None
        for _attempt in range(1, max(1, _VISION_MAX_ATTEMPTS) + 1):
            try:
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
                break
            except Exception as _vision_error:  # noqa: BLE001
                if (
                    _attempt >= max(1, _VISION_MAX_ATTEMPTS)
                    or not _is_retryable_vision_error(_vision_error)
                ):
                    raise
                _delay = _VISION_RETRY_BACKOFF_S * (2 ** (_attempt - 1))
                logger.warning(
                    "[VALIDATION] transient Vision error on attempt %d/%d "
                    "(%s) — retrying in %.0fs",
                    _attempt, _VISION_MAX_ATTEMPTS, _vision_error, _delay,
                )
                time.sleep(_delay)
        text = response.text.strip()
        json_match = re.search(r'\{.*\}', text, re.DOTALL)
        if json_match:
            data = _json.loads(json_match.group())
            detections = data.get("detections")
            if not isinstance(detections, dict):
                raise ValidatorCheckError("Gemini Vision 'detections' must be a JSON object")
            categories = ("people", "atmospherics", "brand", "text", "anatomy")
            if any(type(detections.get(category)) is not bool for category in categories):
                raise ValidatorCheckError(
                    "Gemini Vision detections must contain boolean people, atmospherics, brand, text and anatomy"
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
                "safe": not any(detections[c] for c in ("people", "brand", "anatomy")),
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
    enforce_text: bool = False,
    anatomy_only: bool = False,
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
            "detections": {"people": False, "atmospherics": False,
                           "brand": False, "text": False, "anatomy": False},
            "observations": [],
        }

    normalized = {
        "people": detections.get("people") is True,
        "atmospherics": detections.get("atmospherics") is True,
        "brand": detections.get("brand") is True,
        "text": detections.get("text") is True,
        "anatomy": detections.get("anatomy") is True,
    }
    reasons_by_category: dict[str, list[str]] = {
        "people": [], "atmospherics": [], "brand": [], "text": [], "anatomy": [],
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
    if normalized["anatomy"]:
        blocked_categories.append("anatomy")
    if normalized["brand"]:
        blocked_categories.append("brand")
    if (
        normalized["atmospherics"]
        and not allow_atmospherics
        and enforce_atmospherics
    ):
        blocked_categories.append("atmospherics")
    # `text` sigue el mismo camino que atmospherics: se mide siempre, bloquea
    # sólo cuando se lo pide explícitamente. Arranca en observación a propósito
    # — pasarlo a bloqueo sin conocer su tasa de falsos positivos sobre escenas
    # urbanas legítimas repetiría el sobre-bloqueo de jul-2026.
    if normalized["text"] and enforce_text:
        blocked_categories.append("text")

    observed_categories = []
    if anatomy_only:
        observed_categories = [c for c in blocked_categories if c != "anatomy"]
        blocked_categories = [c for c in blocked_categories if c == "anatomy"]

    blocked_issues = []
    for category in blocked_categories:
        reasons = reasons_by_category[category] or [f"Detected prohibited {category}"]
        blocked_issues.extend(f"{category}: {reason}" for reason in reasons)

    observations = [
        f"{c}: {reason}" for c in observed_categories
        for reason in (reasons_by_category[c] or [f"Detected {c}"])
    ]
    if normalized["atmospherics"] and not allow_atmospherics:
        reasons = reasons_by_category["atmospherics"] or ["Atmospheric effect detected"]
        observations.extend(f"atmospherics: {reason}" for reason in reasons)
    if normalized["text"] and not enforce_text:
        reasons = reasons_by_category["text"] or ["Readable text detected"]
        observations.extend(f"text: {reason}" for reason in reasons)

    return {
        "passed": not blocked_categories,
        "issues": blocked_issues,
        "detections": normalized,
        "observations": observations,
    }


# ── Extended background QC (pedidos UMG sep-nov 2026) ─────────────────────
# 20 change requests were about the BACKGROUND, not the lyric: guns ("Sacar el
# arma", Amor Suicida / Virus Anti-Amor / Niño Jefe), blood and raw meat
# ("que no se vea la carne ni el trapo con sangre"), body parts without a face
# ("sacar las piernas", "sin partes de cuerpo humano"), a burned-in "16:9"
# label and black letterbox bars. None is covered by the base categories:
# ``people`` is deliberately limited to identifiable/prominent humans (jul-2026
# over-block) and ``text`` lists stylised strings as acceptable.
#
# Why a SEPARATE Vision call and not four more fields in the base prompt: the
# base prompt gates production (people/brand/anatomy reject an asset). A
# combined prompt was measured on 2026-11-18 and moved those gating answers on
# identical frames (76 paired frames: people 0→2, brand 0→2, anatomy 2→0).
# Keeping the base request byte-identical means zero drift on what already
# blocks; the extended call is additive, capped to a few frames, and anything
# it cannot answer (429, timeout, malformed JSON) is "not evaluated" —
# fail-OPEN, never an inconclusive verdict (2026-08-20 incident).
#
# Rollout (BACKGROUND_EXTENDED_QC_MODE):
#   off     — nothing new runs; results keep their previous shape.
#   observe — DEFAULT. The extended call and the deterministic letterbox scan
#             run; findings are stored as observations and shown in Delivery QC
#             before publishing, but never reject a background.
#   enforce — categories in BACKGROUND_EXTENDED_QC_ENFORCE (default
#             weapon,gore,overlay,letterbox: the ones with ≤5 % flags on the
#             accepted sample) reject the asset like malformed anatomy does,
#             i.e. even under BACKGROUND_VALIDATION_ENFORCEMENT=observe, and
#             route it through the existing regenerate-once → safe-gradient
#             recovery. body_parts (10 % on accepted backgrounds: statues,
#             gloves, a framed photo) stays observe-only by default.
EXTENDED_QC_MODE_ENV = "BACKGROUND_EXTENDED_QC_MODE"
EXTENDED_QC_ENFORCE_ENV = "BACKGROUND_EXTENDED_QC_ENFORCE"
EXTENDED_QC_MAX_FRAMES_ENV = "BACKGROUND_EXTENDED_QC_MAX_FRAMES"
EXTENDED_VISION_CATEGORIES = ("weapon", "gore", "body_parts", "overlay")
TECHNICAL_CATEGORIES = ("letterbox",)
EXTENDED_CATEGORIES = EXTENDED_VISION_CATEGORIES + TECHNICAL_CATEGORIES
_DEFAULT_EXTENDED_ENFORCE = ("weapon", "gore", "overlay", "letterbox")

EXTENDED_PROMPT = (
    "You are classifying a frame from a music-video background. Report each "
    "category independently and classify only visible evidence.\n\n"
    "Set detections.weapon=true when a firearm (pistol, revolver, rifle, "
    "shotgun, machine gun), ammunition, a grenade, or a knife, dagger, machete "
    "or sword presented as a weapon is visible — held, lying on a surface, "
    "stuck in something or hanging on a wall, toy or replica included. Kitchen "
    "utensils in normal culinary use, tools and scissors are not weapons.\n"
    "Set detections.gore=true for visible blood, blood-stained cloth or "
    "bandages, wounds, raw or butchered meat, carcasses or exposed flesh. Red "
    "paint, wine, coffee, rust, red light or red fabric are not gore unless "
    "they clearly read as blood.\n"
    "Set detections.body_parts=true when any part of a real human body is "
    "visible, even without a face and even if it is not the subject: legs, "
    "feet, hands, arms, torso, bare skin, or limbs emerging from objects, "
    "walls or clothing, including realistic mannequin parts. Do not report "
    "empty clothing, shoes or gloves with no body inside, statues or "
    "sculptures that read as art objects, people inside photos or posters, or "
    "figures too small or blurred to resolve.\n"
    "Set detections.overlay=true when a graphic or text is laid OVER the "
    "picture instead of belonging to the scene: a watermark, an aspect-ratio "
    "or format label such as '16:9' or '4K', a timecode, a channel or "
    "stock-footage logo, user-interface elements, captions or subtitles. Text "
    "that is physically part of the scene (signs, posters, packaging) is not "
    "overlay.\n\n"
    "Respond ONLY with JSON using exactly this schema: "
    '{"detections":{"weapon":true/false,"gore":true/false,'
    '"body_parts":true/false,"overlay":true/false},'
    '"issues":[{"category":"weapon|gore|body_parts|overlay",'
    '"reason":"specific visible evidence"}]}'
)


def extended_qc_mode() -> str:
    """``off`` | ``observe`` | ``enforce``; unknown values fail SAFE to observe.

    Observe never rejects an asset, so a typo must neither switch the evidence
    off nor start blocking."""
    value = os.environ.get(EXTENDED_QC_MODE_ENV, "observe").strip().lower()
    return value if value in {"off", "observe", "enforce"} else "observe"


def extended_enforced_categories() -> frozenset:
    """Extended categories that reject an asset right now (empty unless enforce)."""
    if extended_qc_mode() != "enforce":
        return frozenset()
    raw = os.environ.get(EXTENDED_QC_ENFORCE_ENV)
    names = (
        [item.strip().lower() for item in raw.split(",")]
        if raw is not None else list(_DEFAULT_EXTENDED_ENFORCE)
    )
    return frozenset(name for name in names if name in EXTENDED_CATEGORIES)


def _extended_max_frames() -> int:
    try:
        return max(1, min(48, int(os.environ.get(EXTENDED_QC_MAX_FRAMES_ENV, "8"))))
    except ValueError:
        return 8


def _spread(items: list, limit: int) -> list:
    """``limit`` items evenly spread over ``items``, endpoints included."""
    if len(items) <= limit:
        return list(items)
    if limit == 1:
        return [items[0]]
    picks = sorted({round(k * (len(items) - 1) / (limit - 1)) for k in range(limit)})
    return [items[i] for i in picks]


def _check_frame_extended(image_path: str) -> dict | None:
    """One extended-category Vision call; ``None`` when it could not answer.

    Fail-open by contract: quota, transport, auth or parse errors return None
    (recorded as a frame not evaluated). One retry only for retryable errors —
    the quota is shared with production and the base gate has priority.
    """
    try:
        from pipeline import _get_genai_client, _call_with_timeout
        from google import genai
        import json as _json

        client = _get_genai_client()
        with open(image_path, "rb") as f:
            image_bytes = f.read()
        response = None
        for attempt in (1, 2):
            try:
                response = _call_with_timeout(
                    lambda: client.models.generate_content(
                        model="gemini-2.5-flash",
                        contents=[
                            genai.types.Part.from_bytes(data=image_bytes, mime_type="image/jpeg"),
                            EXTENDED_PROMPT,
                        ],
                        config=genai.types.GenerateContentConfig(
                            temperature=0.1,
                            max_output_tokens=400,
                            thinking_config=genai.types.ThinkingConfig(thinking_budget=0),
                        ),
                    ),
                    timeout_s=45.0,
                    label="VALIDATOR_EXTENDED",
                )
                break
            except Exception as error:  # noqa: BLE001
                if attempt == 2 or not _is_retryable_vision_error(error):
                    raise
                time.sleep(_VISION_RETRY_BACKOFF_S)
        match = re.search(r"\{.*\}", (response.text or "").strip(), re.DOTALL)
        data = _json.loads(match.group()) if match else {}
        detections = data.get("detections")
        if not isinstance(detections, dict):
            return None
        evaluated = [c for c in EXTENDED_VISION_CATEGORIES if type(detections.get(c)) is bool]
        normalized = {c: detections[c] is True for c in evaluated}
        issues = []
        for item in data.get("issues") or []:
            if (
                isinstance(item, dict)
                and item.get("category") in EXTENDED_VISION_CATEGORIES
                and str(item.get("reason") or "").strip()
            ):
                issues.append({"category": item["category"], "reason": str(item["reason"]).strip()})
                # Same contradiction rule as the base parser: a categorized
                # reason is positive evidence.
                normalized[item["category"]] = True
                if item["category"] not in evaluated:
                    evaluated.append(item["category"])
        return {"detections": normalized, "issues": issues, "evaluated": evaluated}
    except Exception as exc:  # noqa: BLE001 — fail-open by contract
        logger.warning("[VALIDATION] extended check not evaluated: %s", exc)
        return None


def _technical_scan(path: str) -> dict:
    """Deterministic letterbox scan; an error is an abstention, never a finding."""
    try:
        from background_technical_qc import analyze_background
        analysis = analyze_background(path)
    except Exception as exc:  # noqa: BLE001
        analysis = {"ran": False, "abstained": f"error:{type(exc).__name__}",
                    "findings": [], "metrics": {}}
    return {
        "ran": bool(analysis.get("ran")),
        "abstained": analysis.get("abstained"),
        "findings": [
            {k: f.get(k) for k in ("category", "code", "reason", "evidence")}
            for f in analysis.get("findings") or []
        ],
        "metrics": analysis.get("metrics") or {},
    }


def _apply_extended(
    result: dict,
    *,
    asset_path: str,
    frame_paths: list[str],
    allow_people: bool,
    technical: dict | None = None,
) -> dict:
    """Run the extended checks and fold them into a validator ``result``.

    Only ADDS information: observations always; issues + ``passed=False`` +
    ``hard_violation`` only for categories enforced right now. Never touches
    ``inconclusive`` and never runs when the base verdict already failed (the
    asset is going to be discarded; do not spend shared quota on it).
    """
    mode = extended_qc_mode()
    if mode == "off":
        return result
    enforced = extended_enforced_categories()
    result.setdefault("hard_violation", False)
    detections = result.setdefault("detections", {})
    for category in EXTENDED_CATEGORIES:
        detections.setdefault(category, False)
    summary = {
        "mode": mode, "enforced": sorted(enforced),
        "frames_planned": 0, "frames_evaluated": 0, "frames_unevaluated": 0,
        "technical": None,
    }
    result["extended_qc"] = summary
    global_observe = (
        os.getenv("BACKGROUND_VALIDATION_ENFORCEMENT", "block").strip().lower() == "observe"
    )
    base_rejects = result.get("passed") is not True and (
        result.get("inconclusive")
        or not global_observe
        or detections.get("anatomy") is True
    )
    if base_rejects:
        # Under the global observe kill-switch a people/brand failure is
        # waived later by the pipeline, so the asset may still ship: the
        # extended evidence is still wanted there (Amor Suicida's gun sat
        # behind an observed "brand" verdict).
        summary["skipped"] = "base_verdict_rejects_asset"
        return result

    findings: list[tuple[int, str, str]] = []
    technical = technical if technical is not None else _technical_scan(asset_path)
    summary["technical"] = technical
    for finding in technical.get("findings") or []:
        findings.append((-1, finding["category"], finding["reason"]))

    frames = _spread(list(enumerate(frame_paths)), _extended_max_frames())
    summary["frames_planned"] = len(frames)
    for index, frame_path in frames:
        answer = _check_frame_extended(frame_path)
        if answer is None:
            summary["frames_unevaluated"] += 1
            continue
        summary["frames_evaluated"] += 1
        reasons = {c: [] for c in EXTENDED_VISION_CATEGORIES}
        for issue in answer["issues"]:
            reasons[issue["category"]].append(issue["reason"])
        for category, present in answer["detections"].items():
            if present:
                findings.append((index, category, (reasons[category] or [f"Detected {category}"])[0]))
        if any(c in enforced for _i, c, _r in findings):
            break  # decisive: later frames cannot make the asset acceptable

    issues, observations = [], []
    for index, category, reason in findings:
        # A body part is a person for policy purposes: when the operator's
        # prompt allows people it is neither a violation nor worth a note.
        if category == "body_parts" and allow_people:
            continue
        detections[category] = True
        row = {"frame": index, "type": f"{category}: {reason}"}
        (issues if category in enforced else observations).append(row)
    result.setdefault("observations", []).extend(observations)
    if issues:
        result["passed"] = False
        result["hard_violation"] = True
        result.setdefault("issues", []).extend(issues)
    return result


def validate_video(
    video_path: str,
    job_id: str = None,
    *,
    allow_people: bool = False,
    allow_atmospherics: bool = True,
    observe_atmospherics: bool = False,
    enforce_atmospherics: bool = False,
) -> dict:
    """Validate a video for prohibited content.

    Extracts frames and checks each with Gemini Vision.

    Returns {"passed": bool, "issues": [{"frame": int, "type": str}]}
    """
    from provenance import record_ai_call

    # Deterministic letterbox scan first: when it is enforced and fires, the
    # asset is rejected before a single Vision frame is paid for.
    technical = _technical_scan(video_path) if extended_qc_mode() != "off" else None
    if technical and any(
        f["category"] in extended_enforced_categories() for f in technical["findings"]
    ):
        logger.info("[VALIDATION] job=%s: technical rejection before Vision", job_id)
        return _apply_extended(
            {
                "passed": True, "inconclusive": False, "issues": [],
                "frames_checked": 0, "frames_planned": 0, "extraction_errors": 0,
                "check_errors": 0,
                "detections": {"people": False, "atmospherics": False, "brand": False,
                               "text": False, "anatomy": False},
                "observations": [], "shadow_atmospherics_detected": False,
            },
            asset_path=video_path, frame_paths=[], allow_people=allow_people,
            technical=technical,
        )

    recorder = record_ai_call(
        job_id=job_id or "unknown",
        step="output_validation",
        tool_name="gemini-2.5-flash-vision",
        tool_provider="google_vertex",
        prompt=(
            "Classify frames for people, malformed anatomy, commercial brands and atmospheric effects; "
            f"allow_people={allow_people}, allow_atmospherics={allow_atmospherics}, "
            f"observe_atmospherics={observe_atmospherics}, "
            f"enforce_atmospherics={enforce_atmospherics}"
        ),
        input_data_types=["video_frames"],
    ) if job_id else None

    frame_paths, tmp_dir, planned_frames = _extract_frames(video_path)
    all_issues = []
    check_errors = 0
    frames_checked = 0
    detected = {"people": False, "atmospherics": False, "brand": False,
                "text": False, "anatomy": False}
    observations = []

    try:
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
                anatomy_only=os.getenv("BACKGROUND_VALIDATION_ENFORCEMENT", "block").strip().lower() == "observe",
            )
            for category, present in verdict["detections"].items():
                detected[category] = detected[category] or present
            if verdict["observations"]:
                observations.extend(
                    {"frame": i, "type": issue}
                    for issue in verdict["observations"]
                )
            if not verdict["passed"]:
                for issue in verdict["issues"]:
                    all_issues.append({"frame": i, "type": issue})
                break
    except BaseException:
        _cleanup_frames(frame_paths, tmp_dir)
        raise

    # Fail closed on ANY partial extraction/check failure. Previously one
    # successful safe frame plus nine Vision timeouts passed the asset. A
    # decisive policy finding is different: once a sampled frame visibly
    # contains a blocked category, checking later frames cannot turn the asset
    # safe, so the intentional early exit is a complete negative verdict.
    expected_frames = planned_frames
    extraction_errors = max(0, planned_frames - len(frame_paths))
    complete_verdict = (
        expected_frames > 0
        and extraction_errors == 0
        and frames_checked == expected_frames
        and check_errors == 0
    )
    decisive_policy_failure = bool(all_issues)
    passed = complete_verdict and not decisive_policy_failure
    summary = (
        f"passed={passed}, frames_checked={frames_checked}, "
        f"extraction_errors={extraction_errors}, check_errors={check_errors}, issues={len(all_issues)}"
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
    result = {
        "passed": passed,
        # "We could not finish looking" is NOT "we saw something forbidden".
        # Both fail closed, but only the decisive verdict is evidence about the
        # asset — a caller re-checking an ALREADY-certified background needs to
        # tell them apart before it throws that background away.
        "inconclusive": (not complete_verdict) and not decisive_policy_failure,
        "issues": all_issues,
        "frames_checked": frames_checked,
        "frames_planned": planned_frames,
        "extraction_errors": extraction_errors,
        "check_errors": check_errors,
        "detections": detected,
        "observations": observations,
        "shadow_atmospherics_detected": bool(
            observe_atmospherics and detected["atmospherics"] and not enforce_atmospherics
        ),
    }
    try:
        # Additive and fail-open; the frames already extracted for the base
        # gate are reused, so no extra decode.
        return _apply_extended(
            result, asset_path=video_path, frame_paths=frame_paths,
            allow_people=allow_people, technical=technical,
        )
    finally:
        _cleanup_frames(frame_paths, tmp_dir)


def _cleanup_frames(frame_paths: list[str], tmp_dir: str | None) -> None:
    """Remove temp frames + dir even when _extract_frames produced zero usable
    frames (mkdtemp leaks otherwise)."""
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


def validate_image(
    image_path: str,
    job_id: str = None,
    *,
    allow_people: bool = False,
    allow_atmospherics: bool = True,
    observe_atmospherics: bool = False,
    enforce_atmospherics: bool = False,
) -> dict:
    """Validate a single image for prohibited content."""
    from provenance import record_ai_call

    recorder = record_ai_call(
        job_id=job_id or "unknown",
        step="output_validation",
        # A single-image verdict is one Gemini request. Keep it distinct from
        # the video-scan key, whose list rate represents as many as 48 frame
        # requests, so calibration can preserve their relative cost.
        tool_name="gemini-2.5-flash-vision-image",
        tool_provider="google_vertex",
        prompt=(
            "Classify image for people, malformed anatomy, commercial brands and atmospheric effects; "
            f"allow_people={allow_people}, allow_atmospherics={allow_atmospherics}, "
            f"observe_atmospherics={observe_atmospherics}, "
            f"enforce_atmospherics={enforce_atmospherics}"
        ),
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
            # No verdict was produced — see validate_video's note.
            "inconclusive": True,
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
    verdict = _evaluate_frame_result(
        result,
        allow_people=allow_people,
        allow_atmospherics=allow_atmospherics,
        enforce_atmospherics=enforce_atmospherics,
        anatomy_only=os.getenv("BACKGROUND_VALIDATION_ENFORCEMENT", "block").strip().lower() == "observe",
    )
    issues = [{"frame": 0, "type": issue} for issue in verdict["issues"]]
    observations = (
        [{"frame": 0, "type": issue} for issue in verdict["observations"]]
        if verdict["observations"] else []
    )
    passed = verdict["passed"]

    if recorder:
        recorder.finish(response_summary=f"passed={passed}, issues={len(issues)}")

    return _apply_extended(
        {
            "passed": passed,
            # The classifier answered: pass or fail, the verdict is about the asset.
            "inconclusive": False,
            "issues": issues,
            "frames_checked": 1,
            "check_errors": 0,
            "detections": verdict["detections"],
            "observations": observations,
            "shadow_atmospherics_detected": bool(
                observe_atmospherics
                and verdict["detections"]["atmospherics"]
                and not enforce_atmospherics
            ),
        },
        asset_path=image_path, frame_paths=[image_path], allow_people=allow_people,
    )
