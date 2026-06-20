"""Full processing pipeline: Whisper → Video → Short → Thumbnail."""

import gc
import hashlib
import json
import os
import math
import random
import re
import logging
logger = logging.getLogger("genly.pipeline")
import subprocess
import tempfile
import threading
import traceback

from dotenv import load_dotenv
load_dotenv(os.path.join(os.path.dirname(__file__), ".env"))

import librosa
import numpy as np
from PIL import Image as _PILImage
if not hasattr(_PILImage, "ANTIALIAS"):
    _PILImage.ANTIALIAS = _PILImage.LANCZOS
from moviepy.config import change_settings

# Auto-detect ImageMagick binary (v7 uses "magick", v6 uses "convert")
for _candidate in [
    "/opt/homebrew/bin/magick",
    "/usr/local/bin/magick",
    "/usr/bin/magick",
    "/opt/homebrew/bin/convert",
    "/usr/local/bin/convert",
    "/usr/bin/convert",
]:
    if os.path.exists(_candidate):
        change_settings({"IMAGEMAGICK_BINARY": _candidate})
        break

from moviepy.editor import (
    AudioFileClip,
    ColorClip,
    CompositeVideoClip,
    TextClip,
    VideoClip,
    VideoFileClip,
    concatenate_videoclips,
)
from PIL import Image, ImageDraw, ImageFont

from jobs import update_job, get_job_model
import storage
from render_spec import FPS_RATIONAL, RenderSpec
from subprocess_utils import run_checked, SubprocessExecutionError  # noqa: F401 — exported for upstream catches

ASSETS_DIR = os.path.join(os.path.dirname(__file__), "..", "assets")
OUTPUTS_DIR = os.path.join(os.path.dirname(__file__), "..", "outputs")
BACKGROUNDS_DIR = os.path.join(ASSETS_DIR, "backgrounds")


_DELIVERABLE_FILENAMES = {
    "video": "lyric_video.mp4",
    "short": "short.mp4",
    "thumbnail": "thumbnail.jpg",
    "umg_master": "umg_master.mov",
    "umg_short": "umg_short.mov",
}


# Deliverables whose absence from R2 means the user can't actually use the
# job — if any of these fail to upload, the job has to surface an error
# instead of pretending to be done. umg_master/umg_short are lazy-generated
# on first /download, so their absence from R2 here is fine.
_CRITICAL_DELIVERABLES = ("video", "short", "thumbnail")


def _upload_deliverables_to_r2(job_id: str, job_dir: str, files: dict) -> dict:
    """Upload each produced deliverable to R2 and delete the local copy on
    success. Returns {file_type: s3_key}.

    Audit 2026-05-26 (systemic-jobs-pipeline) — three behaviors changed:

    1. **Per-file heartbeat.** A multi-GB upload (UMG ProRes 5GB on slow
       upstream) can run 10-20 min without emitting any update_job, while
       the reaper's `find_stalled_renders` watchdog fires at 20 min. We
       now `heartbeat(job_id)` before each upload so the reaper sees the
       worker alive.

    2. **Atomic merge of s3_keys.** Caller used to do
       `update_job(s3_keys=this_dict)`, which REPLACES the JSONB column.
       That race-loses against `prores.prewarm_prores` writing
       `s3_keys[umg_master]=key` concurrently (incident jobs.py:670-675).
       We now write each key via `merge_s3_keys` inside this function as
       it succeeds — caller no longer needs to do the s3_keys update.

    3. **Critical-deliverable failure raises.** Old behavior was "log and
       continue", so the job ended up `done` with `s3_keys={}` and
       `/download/{id}/video` returning 404. Now if a critical deliverable
       (video / short / thumbnail) fails to upload, we raise — the
       pipeline's outer except catches it and marks the job `error` with
       a clear message, instead of pretending success and 404-ing later.
       Non-critical (umg_master, umg_short) keep the old "log and skip"
       behavior because they're lazy-regenerated on first /download.
    """
    if not storage.is_enabled():
        return {}
    from jobs import merge_s3_keys, heartbeat
    # We need a SQLAlchemy session, but this function runs in the worker
    # context with no request-scoped session available. Create one here
    # just to look up the tenant_id, then close it. get_job_model (not
    # get_job) because this is an intentional unscoped internal read —
    # the worker owns the job regardless of tenant. See get_job()'s
    # tenant-isolation contract.
    from database import SessionLocal
    _db = SessionLocal()
    try:
        _row = get_job_model(_db, job_id)
        tenant_id = (_row.tenant_id if _row is not None else None) or "default"
    finally:
        _db.close()
    out: dict = {}
    failed_critical: list[str] = []
    for file_type, _url in files.items():
        ftype_short = file_type.replace("_url", "")
        key_name = _DELIVERABLE_FILENAMES.get(ftype_short)
        if not key_name:
            continue
        local = os.path.join(job_dir, key_name)
        if not os.path.exists(local):
            # Missing local file is a soft failure for non-critical
            # deliverables (e.g. UMG profile didn't request a short).
            # For critical it means an upstream step misfired — surface
            # it as a critical failure so the job errors out instead of
            # advertising a 404 download.
            if ftype_short in _CRITICAL_DELIVERABLES:
                failed_critical.append(f"{ftype_short} (local file missing)")
            continue
        # Heartbeat before the upload — multi-GB uploads can spend tens of
        # minutes on the wire without any other update_job. Without this,
        # find_stalled_renders (20 min last_progress_at threshold) reaps
        # the job mid-upload and the operator sees "error" on a video that
        # was actually finishing upload.
        try:
            heartbeat(job_id)
        except Exception:
            pass  # heartbeat is best-effort; never block an upload
        try:
            key = storage.upload_master(local, tenant_id, job_id, key_name)
            if key:
                out[ftype_short] = key
                # Atomic merge into job.s3_keys so a concurrent
                # prores.prewarm_prores writing umg_master/umg_short
                # doesn't get clobbered when the caller eventually
                # snapshots files. merge_s3_keys takes FOR UPDATE.
                try:
                    merge_s3_keys(job_id, ftype_short, key)
                except Exception as e:
                    logger.warning("[R2] merge_s3_keys failed for %s: %s", ftype_short, e)
                # Upload confirmed — delete the local copy so the disk doesn't
                # fill up (a HD ProRes master is ~5 GB, a 240 GB NVMe fills
                # after ~50 UMG deliveries).
                try:
                    os.unlink(local)
                except OSError as e:
                    logger.error("[R2] Could not remove local %s: %s", local, e)
            else:
                # upload_master returned None without raising — treat as failure.
                logger.error("[R2] Upload returned no key for %s", key_name)
                if ftype_short in _CRITICAL_DELIVERABLES:
                    failed_critical.append(ftype_short)
        except Exception as e:
            logger.error("[R2] Upload failed for %s: %s", key_name, e)
            if ftype_short in _CRITICAL_DELIVERABLES:
                failed_critical.append(f"{ftype_short} ({type(e).__name__})")
    if failed_critical:
        # Bubble up so the pipeline's outer except marks the job `error`
        # instead of leaving it `done` with a half-uploaded set of files.
        raise RuntimeError(
            "R2 upload failed for critical deliverables: "
            + ", ".join(failed_critical)
        )
    return out


def _write_edit_audit(action: str, detail: dict) -> None:
    """Insert an audit_log row from the worker context.

    main.py:request_edit writes job.edit_request when the operator hits
    /edit; this helper closes the loop with job.edit_completed (success)
    or job.edit_failed (raised exception). user_id is left NULL because
    the worker has no request user — the original requester is already
    captured on the job.edit_request row. Best-effort: a failure here
    must never mask the real error from the pipeline.
    """
    try:
        from database import SessionLocal, AuditLog
        _db = SessionLocal()
        try:
            _db.add(AuditLog(user_id=None, action=action, detail=detail))
            _db.commit()
        finally:
            _db.close()
    except Exception as e:
        logger.error("[EDIT] audit log write failed (%s): %s", action, e)


def _snapshot_previous_deliverables(
    prior_s3_keys: dict | None,
    version_n: int,
) -> dict | None:
    """Server-side-copy each deliverable in `prior_s3_keys` to `{key}.v{N}`
    so the about-to-overwrite re-render preserves the prior cut.

    Called from run_edit_pipeline right before _upload_deliverables_to_r2.
    Returns a dict suitable to append to job.previous_versions:
        {"version": N, "archived_at": iso, "keys": {file_type: archived_key}}
    or None if there's nothing to archive (no prior keys / storage off).

    Tolerant of partial failures: a copy that fails is logged and skipped,
    not raised — the re-render must still succeed and overwrite the
    survivor; losing a rollback path for one file type is better than
    aborting the whole edit.
    """
    if not prior_s3_keys or not isinstance(prior_s3_keys, dict):
        return None
    if not storage.is_enabled():
        return None
    from datetime import datetime, timezone
    archived: dict = {}
    for file_type, src_key in prior_s3_keys.items():
        if not src_key or not isinstance(src_key, str):
            continue
        dst_key = f"{src_key}.v{version_n}"
        try:
            ok = storage.copy_object(src_key, dst_key)
            if ok:
                archived[file_type] = dst_key
        except Exception as e:
            logger.error("[EDIT] snapshot copy failed for %s (%s): %s", file_type, src_key, e)
    if not archived:
        return None
    return {
        "version": version_n,
        "archived_at": datetime.now(timezone.utc).isoformat(),
        "keys": archived,
    }


def _call_with_timeout(fn, timeout_s: float, label: str = ""):
    """Run `fn()` in a worker thread and raise `TimeoutError` if it doesn't
    return within `timeout_s` seconds.

    Audit 2026-05-26 (systemic-jobs-pipeline). The `google-genai` SDK and
    `google-cloud-aiplatform` Vertex client both default to NO timeout on
    `generate_content` / `generate_images`. When Vertex degrades, the call
    hangs the worker thread indefinitely — `find_stalled_renders` (20 min
    threshold) reaps the job mid-call and the operator sees "the render
    just got stuck at progress=22".

    Pattern previously inlined in `_fetch_lyrics_via_gemini_search`
    (pipeline.py:4017-4041). Extracted here so every Gemini/Imagen call
    can adopt the same defense in one line.

    Note: `concurrent.futures.ThreadPoolExecutor` does NOT actually kill
    the underlying request on timeout — the network call keeps running
    in its thread until the SDK eventually unblocks. That's fine for our
    case: the wrapper unblocks the WORKER (so the pipeline can fall
    through to whatever fallback the caller has), and the orphan thread
    dies with the worker on the next WORKER_MAX_JOBS recycle. We trade a
    small amount of thread leakage for protection against full worker
    deadlock.
    """
    import concurrent.futures as _cf
    # Tier 4 (H3): do NOT use the executor as a context manager. `with ... as _ex`
    # calls executor.shutdown(wait=True) on __exit__, which BLOCKS until the
    # orphaned (still-hung) task finishes — re-blocking the worker on the very
    # timeout this is meant to escape (so the docstring's "unblocks the WORKER"
    # was previously FALSE). Manage it manually and shutdown(wait=False) so a
    # timeout returns control immediately; the orphan dies on the next recycle.
    _ex = _cf.ThreadPoolExecutor(max_workers=1)
    fut = _ex.submit(fn)
    try:
        _res = fut.result(timeout=timeout_s)
        _ex.shutdown(wait=False)
        return _res
    except _cf.TimeoutError as exc:
        _ex.shutdown(wait=False)  # abandon the orphan — never wait on it
        tag = f"[{label}] " if label else ""
        logger.warning("%sGenAI call exceeded %.0fs timeout — raising (orphan abandoned)", tag, timeout_s)
        raise TimeoutError(
            f"{label or 'GenAI'} call exceeded {timeout_s:.0f}s timeout"
        ) from exc
    except Exception:
        _ex.shutdown(wait=False)  # fn() raised — don't block on shutdown either
        raise


def _ffprobe_duration(path: str) -> float | None:
    """Return media duration in seconds, or None if ffprobe fails."""
    import subprocess
    try:
        r = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "default=noprint_wrappers=1:nokey=1", path],
            capture_output=True, text=True, timeout=30,
        )
        return float((r.stdout or "").strip())
    except Exception:
        return None


def _verify_deliverables(job_dir: str, files: dict, audio_duration: float) -> None:
    """Sanity-check every deliverable BEFORE the R2 upload.

    Catches the silent-failure family:
    - ffmpeg exited 0 but produced an empty/truncated file (disk full, OOM
      mid-flush)
    - moviepy crashed mid-render and left the prior pass's leftover file
    - duration mismatch (audio offset bug, encoder cut early)
    - codec mismatch (caller forgot to pass the right RenderSpec)

    Raises RuntimeError on any failure so the outer try/except in
    run_pipeline marks the job 'error' with a clear message instead of
    uploading garbage to R2 + shipping it to UMG.
    """
    import os as _os

    expected = {
        "video_url":      ("lyric_video.mp4", "h264", audio_duration),
        "short_url":      ("short.mp4",        "h264", None),  # short is a fixed clip, not full audio
        "thumbnail_url":  ("thumbnail.jpg",   None,   None),
        # umg_master is generated lazily at download time via ffmpeg from
        # the MP4 above (see /download/{id}/umg_master). It does NOT
        # exist on disk after the pipeline finishes, so we don't verify
        # it here — the download endpoint validates the .mov post-
        # transcode using _validate_umg_master.
    }
    for url_key, (filename, expected_codec, expected_dur) in expected.items():
        if url_key not in files:
            continue
        path = _os.path.join(job_dir, filename)
        if not _os.path.exists(path):
            raise RuntimeError(f"verify: {filename} missing on disk after render")
        size = _os.path.getsize(path)
        if size < 1024:
            raise RuntimeError(f"verify: {filename} is {size} bytes (truncated / empty)")

        if expected_codec:
            # ffprobe codec check
            import subprocess
            r = subprocess.run(
                ["ffprobe", "-v", "error", "-select_streams", "v:0",
                 "-show_entries", "stream=codec_name",
                 "-of", "default=noprint_wrappers=1:nokey=1", path],
                capture_output=True, text=True, timeout=30,
            )
            codec = (r.stdout or "").strip()
            if codec != expected_codec:
                raise RuntimeError(
                    f"verify: {filename} codec is {codec!r}, expected {expected_codec!r}"
                )

        if expected_dur is not None:
            actual_dur = _ffprobe_duration(path)
            if actual_dur is None:
                raise RuntimeError(f"verify: {filename} ffprobe could not read duration")
            # ±2s tolerance — encoder rounding + container overhead
            if abs(actual_dur - expected_dur) > 2.0:
                raise RuntimeError(
                    f"verify: {filename} duration {actual_dur:.1f}s differs from "
                    f"audio {expected_dur:.1f}s by > 2s"
                )

    logger.info("[VERIFY] all %s deliverables passed sanity checks "
                "(umg_master, if requested, is generated lazily at download)", len(files))


def _cleanup_local_intermediates(job_dir: str) -> None:
    """Drop intermediate render artefacts that are not deliverables. Keeps the
    directory + any leftover deliverable that R2 upload missed, so the job
    can still be recovered."""
    leftovers = ("bg_generated.mp4", "bg_gradient_fallback.mp4")
    for name in leftovers:
        path = os.path.join(job_dir, name)
        if os.path.exists(path):
            try:
                os.unlink(path)
            except OSError:
                pass
    # Also drop any per-spec looped backgrounds (bg_looped_*.mp4)
    try:
        for entry in os.listdir(job_dir):
            if entry.startswith("bg_looped_") and entry.endswith(".mp4"):
                try:
                    os.unlink(os.path.join(job_dir, entry))
                except OSError:
                    pass
    except OSError:
        pass


def _cleanup_job_dir_on_failure(job_dir: str) -> None:
    """Remove the ENTIRE local job dir when a render FAILS (Tier 4 / C5).

    Unlike _cleanup_local_intermediates (success path: keeps any leftover
    deliverable for recovery), a failed render has no good deliverables —
    leaving its multi-GB intermediates (bg_generated.mp4, bg_looped_*, a
    partial lyric_video.mp4 or a ~5 GB umg_master.mov) accumulates across
    failures until the disk fills and the NEXT render fails mid-flush (the C5
    cascade). Removing everything is safe ONLY when the input is R2-recoverable
    (an RQ retry re-downloads it via input_r2_key and re-renders from scratch) —
    so the CALLER must gate this on input_r2_key being set, and fall back to
    _cleanup_local_intermediates (which preserves the local input) when it
    isn't. Best-effort — never raises. Kill-switch CLEANUP_FAILED_JOB_DIR=false
    keeps the old leak-but-debuggable behaviour for incident triage.
    """
    if os.environ.get("CLEANUP_FAILED_JOB_DIR", "true").strip().lower() == "false":
        return
    if not job_dir or not os.path.isdir(job_dir):
        return
    try:
        import shutil
        shutil.rmtree(job_dir, ignore_errors=True)
        logger.info("[PIPELINE] cleaned up failed job dir (freed disk): %s", job_dir)
    except Exception:
        pass


# Auto-trim "hanging text" used to live here — applied a text-length
# cap to segment ends so LRCLib's "end pinned to next line" convention
# didn't leave text on screen during instrumental fills. Removed
# 2026-05-14 because it ran at /transcribe time and silently modified
# segments BEFORE the operator opened the editor, leading to confusing
# "my edits didn't save" reports. The same V2 formula still exists in
# the frontend (LyricsEditor.jsx) where the operator can apply it
# per-segment or bulk via explicit ✂ buttons — that's the only way
# the trim is allowed to run now.


def _get_persisted_segments(job_id: str) -> list[dict] | None:
    """Return job_row.segments_json if it's a non-empty list, else None.

    Used by run_pipeline's "preserve user edits across retries" branch.
    Opens its own short-lived DB session so the caller doesn't have to
    pass one in (matching the rest of pipeline.py's update_job pattern).
    Best-effort: any exception returns None and the caller falls back to
    a fresh Whisper transcription — we never want a DB hiccup to
    silently produce a worse video.
    """
    try:
        from database import SessionLocal, Job
        with SessionLocal() as db:
            row = db.query(Job).filter(Job.job_id == job_id).first()
            if row is None:
                return None
            segs = row.segments_json
            if not segs or not isinstance(segs, list) or len(segs) == 0:
                return None
            return segs
    except Exception as e:  # pragma: no cover
        logger.error("[PIPELINE] _get_persisted_segments(%s) failed: %s", job_id, e)
        return None


def _best_effort_lyrics_hint(artist: str, song_title: str) -> str | None:
    """Fetch the reference lyrics from the Gemini-grounded search cache
    (or fresh search) to use as Whisper's `prompt` parameter.

    Why this exists: `/transcribe` already does this on the upload path,
    biasing Whisper toward the song's actual vocabulary. The pipeline
    path (run_pipeline → transcribe) used to skip the hint, so the same
    audio produced WORSE transcriptions on retry than on first upload.
    The user surfaced this as "el upload fresco siempre acierta, el
    retry alucina" — root cause was just this missing hint.

    Best-effort: returns None on any error so the caller transcribes
    without bias rather than crashing.
    """
    if not artist or not song_title:
        return None
    try:
        from database import SessionLocal
        with SessionLocal() as db:
            return _fetch_lyrics_via_gemini_search(
                artist, song_title, job_id=None, db=db,
            )
    except Exception as e:  # pragma: no cover
        logger.error("[PIPELINE] lyrics_hint fetch failed: %s", e)
        return None


def _validate_bg_cache_key(bg_cache_key, *, job_id, artist, song_title, style,
                           movement_style, effect, custom_colors, genre,
                           concept, background_hint, bg_verbatim, match_lyrics):
    """Valida que el bg_cache_key del cliente corresponda a ESTE job.

    Audit adversarial 2026-06-09: el key viene del CLIENTE y se usaba sin
    validar — un key stale (el operador cambió params después del preview)
    o directamente ajeno (otro tenant, request crafteado) servía CUALQUIER
    fondo de bg_cache/ como si fuera de este job. Acá el servidor recomputa
    el hash desde los params reales del job con la MISMA función que usó el
    preview (bg_preview.compute_bg_cache_key) y descarta el key si no
    coincide. Peor caso de un falso mismatch = generación fresh (correcta,
    solo más lenta y ~$0.80-3.20 de Veo) — nunca un fondo equivocado.

    Returns:
        El key si coincide; None si no (o si la validación misma falla).
    """
    try:
        from bg_preview import compute_bg_cache_key
        expected = compute_bg_cache_key({
            "artist": artist or "",
            "song_title": song_title or "",
            "style": style or "",
            "movement_style": movement_style or "",
            "effect": effect or "",
            "custom_colors": custom_colors or "",
            "genre": genre or "",
            "concept": concept or "",
            "background_hint": background_hint or "",
            "bg_verbatim": bool(bg_verbatim),
            # El preview siempre hashea con background_mode="veo"
            # (App.jsx previewEntry lo hardcodea) y animate_image solo es
            # true con custom file — caso que nunca llega al fast path
            # (_animate_user_image lo excluye en el caller).
            "background_mode": "veo",
            "animate_image": False,
            "match_lyrics": bool(match_lyrics),
        })
        if bg_cache_key != expected:
            logger.warning(
                "[BG] bg_cache_key DESCARTADO job=%s: cliente=%s esperado=%s "
                "(params del job no coinciden con los del preview) — fondo fresh",
                job_id, bg_cache_key, expected,
            )
            return None
        return bg_cache_key
    except Exception as exc:
        # Best-effort: si el recompute falla, mejor generar fresh que
        # arriesgar un fondo ajeno.
        logger.warning("[BG] bg_cache_key validation error job=%s: %s — fondo fresh",
                       job_id, exc)
        return None


def _seed_image_digest(image_path, job_id=None):
    """sha256-16 del contenido de la imagen semilla de image-to-video.

    Entra al cache key de Veo (audit adversarial 2026-06-09): sin esto, el
    hash era prompt+model+params y omitía la imagen — mismo namespace
    (artista|tema) + mismo prompt con DOS imágenes distintas servía el clip
    cacheado de la otra (caso real: re-render de la misma canción cambiando
    la foto recibía la animación de la foto anterior). Si la imagen no se
    puede leer, devolvemos un marcador único por job para no envenenar el
    cache compartido.
    """
    import hashlib as _ih
    try:
        with open(image_path, "rb") as _imf:
            return _ih.sha256(_imf.read()).hexdigest()[:16]
    except OSError:
        return f"unreadable:{job_id or 'nojob'}"


def run_pipeline(job_id: str, mp3_path: str, artist: str, style: str,
                 language: str = None, segments_override: list[dict] = None,
                 delivery_profile: str = "youtube", umg_spec: dict | None = None,
                 background_path: str = None,
                 input_r2_key: str | None = None,
                 bg_r2_key: str | None = None,
                 variation_source_path: str | None = None,
                 variation_source_r2_key: str | None = None,
                 variation_parent_asset_id: int | None = None,
                 genre: str = "",
                 font: str = "",
                 concept: str = "",
                 movement_style: str = "",
                 animate_image: bool = False,
                 song_title: str = "",
                 text_case: str = "upper",
                 font_scale: float = 1.0,
                 lyric_transition: str = "cut",
                 text_motion: str = "none",
                 lyrics_animation: str = "none",
                 line_transition: str = "none",
                 # Lyric text colors 2026-05-25. Hex #RRGGBB; cadena vacía
                 # = blanco default. Para karaoke: lyric_color = palabra no
                 # cantada, lyric_sung_color = palabra cantada. Para otras
                 # animaciones: lyric_color = único color del texto.
                 lyric_color: str = "",
                 lyric_sung_color: str = "",
                 match_lyrics: bool = True,
                 text_contrast: str = "medium",
                 # Background_hint llega solo desde el flow de variantes
                 # (POST /jobs/{id}/variant). En el upload normal viene
                 # vacío y el prompt Gemini se arma 100% desde concept +
                 # genre + lyrics. Cuando viene set, _ensure_background
                 # lo inyecta como [OPERATOR OVERRIDE] en el user_content
                 # de Gemini, misma mecánica que /edit (PR #116).
                 background_hint: str | None = None,
                 # bg_verbatim: cuando es True y hay background_hint, el texto
                 # del operador va DIRECTO a Veo sin que Gemini lo reescriba
                 # ("usar mi prompt tal cual"). Default False = comportamiento
                 # actual (Gemini refina el hint como [OPERATOR OVERRIDE]).
                 bg_verbatim: bool = False,
                 # custom_colors: paleta personalizada (hex/nombres, coma-sep)
                 # cuando style=="custom". Va al prompt de Veo como COLOR
                 # DIRECTION + al gradiente fallback.
                 custom_colors: str = "",
                 # effect: overlay animado componible sobre cualquier fondo
                 # (snow/rain/stars/bokeh/light). "" = ninguno. Se compone en el
                 # render (libass filter_complex o moviepy) vía fx_compositor.
                 effect: str = "",
                 # Capa C 2026-05-24: hash determinístico (sha256-12) de los
                 # params del background. Si está set Y `bg_cache/{key}.mp4`
                 # existe en R2, la pipeline lo descarga ANTES de llamar a
                 # _ensure_background — ahorra los ~60-180s de Veo/Imagen y
                 # ~$0.80-3.20 de cuota. Ver bg_preview.py para el hash y el
                 # path en R2. None = no chequear cache (fallback al flow
                 # tradicional de Veo/Imagen inline).
                 bg_cache_key: str | None = None,
                 # Add-on premium "Escenas" (multi-escena). Cuando es True el
                 # fondo se arma como un CONJUNTO de escenas (detect→biblia→
                 # plan→N clips Veo→stitch xfade) en vez de un loop único. El
                 # endpoint ya validó has_scenes_access antes de setearlo, así
                 # que acá se confía. Cualquier fallo cae al fondo único (cero
                 # regresión). Default False = comportamiento histórico.
                 enable_scenes: bool = False,
                 # Title-card customization (Full Rotor v1). Defaults reproduce
                 # the historical look. title_size clamps 0.5-2.0; the font ids
                 # resolve via _resolve_font ("" → ExtraBold artist / lyric song);
                 # template ∈ auto|centered|lower_third|badge.
                 title_template: str = "auto",
                 title_size: float = 1.0,
                 title_artist_font: str = "",
                 title_song_font: str = "",
                 # UI v1.1 (2026-05-30): explicit line break for the song
                 # title. Empty string = automatic shrink-then-wrap (default,
                 # historical). When set, contains the operator-chosen line
                 # break(s) joined with "\n" — e.g. "Donde Estan\nCorazón".
                 # Threaded into title_card_lines via song_lines kwarg.
                 title_song_break: str = ""):
    """Run the full pipeline for a job. Called synchronously.

    delivery_profile:
        "youtube" — YouTube MP4 + short + thumbnail (default).
        "umg"     — UMG ProRes master only (no short/thumbnail).
        "both"    — YouTube bundle + UMG master, sharing bg/font.

    background_path:
        If provided, skip AI background generation and use the human-provided
        asset instead (UMG Guideline 10 compliance).

    input_r2_key / bg_r2_key:
        When the API and worker run in separate containers (e.g. Railway), the
        local mp3_path / background_path written by the API are NOT visible
        to the worker. The API uploads the input MP3 (and any custom
        background) to R2 and passes the keys here; we download them locally
        before processing, restoring the same file paths the rest of the
        pipeline expects.

    variation_source_path / variation_source_r2_key / variation_parent_asset_id:
        Set when the user picked a library asset in "variation" mode. We
        materialize the source video, extract a representative frame, and
        feed it to Veo as image-to-video input — Veo then generates a
        brand-new clip visually derived from the original. This is how UMG
        gets a unique video off a library asset without needing a real
        video-to-video model (Veo 3.1 only supports image-to-video).
    """
    # Observability 2026-06-10: toda línea de log de este job lleva job_id.
    from observability import set_job_log_context
    set_job_log_context(job_id)
    job_dir = os.path.join(OUTPUTS_DIR, job_id)
    os.makedirs(job_dir, exist_ok=True)

    # Drop any ProRes .mov left from a PRIOR render of this same job_id
    # (e.g. a /retry re-render: retry_job clears s3_keys but reuses job_dir,
    # and _cleanup_local_intermediates does not touch the .mov). A fresh
    # full render invalidates the lazy ProRes derivative; leaving the old
    # .mov on disk would let the post-render prewarm short-circuit on
    # os.path.exists and re-publish the pre-render cut. Best-effort.
    for _stale_mov in ("umg_master.mov", "umg_short.mov"):
        _sp = os.path.join(job_dir, _stale_mov)
        if os.path.exists(_sp):
            try:
                os.unlink(_sp)
                logger.info("[PIPELINE] job=%s dropped stale %s before re-render", job_id, _stale_mov)
            except OSError as _e:
                logger.warning("[PIPELINE] could not drop stale %s: %s", _sp, _e)

    # Telemetría 2026-05-23 — text_motion + lyric_transition deprecados.
    # main.py coerce upstream a defaults, pero un job que estuviera en cola
    # antes del deploy puede traer valores no-default; loguear UNA VEZ por
    # job (warn) y resetear. Sirve para medir cuánta cola legacy queda.
    if text_motion not in (None, "", "none"):
        logger.warning(
            "[DEPRECATED] job=%s recibió text_motion=%r — campo deprecado, "
            "se ignora. Reemplazo: lyrics_animation (pop/glow).",
            job_id, text_motion,
        )
        text_motion = "none"
    if lyric_transition not in (None, "", "cut"):
        logger.warning(
            "[DEPRECATED] job=%s recibió lyric_transition=%r — campo deprecado, "
            "se ignora. Reemplazo: line_transition (slide/wipe/dissolve_blur).",
            job_id, lyric_transition,
        )
        lyric_transition = "cut"

    # Worker just claimed this job — flip the user-facing status from
    # "queued" (sitting in RQ) to "processing" (a worker is actively on it).
    # This makes the queue visible in the dashboard: jobs piling up in RQ
    # show as "queued" until a worker picks them, at which point they
    # immediately go "processing". Idempotent — if the job already says
    # processing (e.g. on retry), the update is a no-op.
    update_job(job_id, status="processing", current_step="starting", progress=1)

    # Materialize R2-stored inputs onto local disk so moviepy/ffmpeg/whisper
    # can open them. No-op when running on a single host (no R2 keys passed).
    #
    # The /retry endpoint (main.py:retry_job) calls enqueue_pipeline with
    # mp3_path=None because the audio only lives in R2 — the caller has
    # no local path to hand us. Detect that and derive one from the R2
    # key's basename. Without this, os.path.exists(None) raises:
    #   TypeError: stat: path should be string, bytes, os.PathLike or
    #   integer, not NoneType
    # which RQ surfaces to pipeline_failure_callback and the user gets
    # "El render falló tras reintentos" — same row, same retry loop.
    # Discovered 2026-05-11 22:43 UTC when admin retried two jobs that
    # had been reaped from a 4K render stall.
    if mp3_path is None and input_r2_key:
        mp3_path = os.path.join(job_dir, os.path.basename(input_r2_key))
    if input_r2_key and not os.path.exists(mp3_path):
        if not storage.download_object(input_r2_key, mp3_path):
            update_job(
                job_id, status="error",
                error=f"Failed to fetch input from R2: {input_r2_key}",
            )
            return
    # Mismo derive que mp3_path: /retry pasa bg_r2_key (preserved_bg_r2_key)
    # con background_path=None porque el archivo solo vive en R2. Sin esta
    # línea la condición de abajo nunca se cumplía y el "fondo preservado"
    # del retry se descartaba en silencio — el pipeline regeneraba con Veo
    # pisando el fondo aprobado/subido (audit 2026-06-11: afectaba tanto
    # fondos IA cacheados como imágenes del usuario). El basename conserva
    # la extensión, de la que depende _is_still más abajo.
    if background_path is None and bg_r2_key:
        background_path = os.path.join(job_dir, os.path.basename(bg_r2_key))
    if bg_r2_key and background_path and not os.path.exists(background_path):
        if not storage.download_object(bg_r2_key, background_path):
            update_job(
                job_id, status="error",
                error=f"Failed to fetch background from R2: {bg_r2_key}",
            )
            return

    # Variation mode: materialize the source library video locally and
    # extract a frame to use as the Veo image-to-video seed. The source
    # video itself is NOT used as the final background — we only borrow
    # one frame so Veo can derive a visually similar but distinct clip.
    variation_seed_image = None
    if variation_source_path:
        if variation_source_r2_key and not os.path.exists(variation_source_path):
            os.makedirs(os.path.dirname(variation_source_path) or ".", exist_ok=True)
            if not storage.download_object(variation_source_r2_key, variation_source_path):
                update_job(
                    job_id, status="error",
                    error=f"Failed to fetch variation source from R2: {variation_source_r2_key}",
                )
                return
        if not os.path.exists(variation_source_path):
            update_job(
                job_id, status="error",
                error=f"Variation source not found locally: {variation_source_path}",
            )
            return
        variation_seed_image = os.path.join(job_dir, "variation_seed.png")
        try:
            _extract_frame_from_video(variation_source_path, variation_seed_image)
        except Exception as e:
            update_job(
                job_id, status="error",
                error=f"Failed to extract frame for variation: {e}",
            )
            return
        # Hand the extracted frame to the existing image-to-video branch.
        # `_animate_user_image` (computed below) only fires when the
        # background file is a JPG/PNG — our extracted frame is a PNG, so
        # the existing logic will pass it to Veo as the image-to-video
        # seed and produce a brand-new clip derived from it.
        background_path = variation_seed_image
        animate_image = True
        logger.info("[BG] variation: seeded Veo image-to-video from frame of "
                    "%s (parent asset id=%s)",
                    os.path.basename(variation_source_path), variation_parent_asset_id)

    wants_youtube = delivery_profile in ("youtube", "both")
    wants_umg = delivery_profile in ("umg", "both")

    try:
        # Step 1 — Whisper transcription (or reuse persisted segments).
        # Precedence:
        #   1. Caller-passed segments_override (e.g. /generate after the
        #      wizard's lyrics editor)
        #   2. Job row's segments_json (e.g. /retry path — preserves the
        #      user's previous corrections instead of re-running Whisper
        #      and clobbering them, which is the bug observed on 2026-05-
        #      11 when admin retried after a deploy and lost their lyric
        #      edits)
        #   3. Fresh Whisper transcription (first-ever processing of this
        #      audio). Pass lyrics_hint sourced from artist+song so
        #      Whisper biases toward the right vocabulary — same trick
        #      /transcribe uses on the upload path, restored here so the
        #      retry path matches its quality.
        update_job(job_id, current_step="whisper", progress=5)
        _persist_segments = True
        if segments_override:
            segments = segments_override
            logger.info("[WHISPER] Using %s caller-supplied segments", len(segments))
        else:
            # Re-fetch the job row in case the caller (retry) didn't
            # pass us segments but the row has them from a previous
            # generate. update_job above already opened a session so we
            # do this in a fresh, short-lived one.
            persisted = _get_persisted_segments(job_id)
            if persisted:
                segments = persisted
                _persist_segments = False  # don't rewrite identical data
                logger.info("[WHISPER] Reusing %s persisted segments "
                            "(skip Whisper — preserves user corrections)", len(segments))
            else:
                lyrics_hint = _best_effort_lyrics_hint(artist, song_title)
                segments = transcribe(
                    mp3_path, language=language, lyrics_hint=lyrics_hint,
                    job_id=job_id,
                )
        # Persist segments so edit re-renders can skip re-transcription.
        # Skip when we just read them from the same row — pointless write.
        if _persist_segments:
            update_job(job_id, segments_json=segments, progress=20)
        else:
            update_job(job_id, progress=20)

        # Step 1.5 — Background (AI-generated or human-provided)
        update_job(job_id, current_step="background", progress=22)
        # Decide if the operator's upload is a still image they want
        # animated by Veo image-to-video (vs. used as-is via Ken Burns).
        # Path requires: animate_image flag set + background_path is a
        # JPG/PNG (NOT an MP4 — those are already video).
        _is_still = (background_path and
                     background_path.lower().endswith((".jpg", ".jpeg", ".png")))
        _animate_user_image = bool(animate_image and _is_still)

        # P0 fix 2026-06-19: _scenes_active se usa SIEMPRE en el render
        # (bg_prelooped=_scenes_active) pero antes sólo se inicializaba dentro de
        # la rama de fondo IA (el else de abajo). Con un fondo humano/library la
        # variable quedaba sin asignar → UnboundLocalError tumbaba TODO job de
        # fondo no-IA (incl. los golden renders). Inicializar acá, incondicional.
        _scenes_active = False

        if background_path and not _animate_user_image:
            # Human-provided background — skip AI generation (UMG Guideline 10)
            from provenance import record_ai_call
            recorder = record_ai_call(
                job_id=job_id,
                step="background_human",
                tool_name="human-provided",
                tool_provider="user_upload",
                prompt="User-uploaded background asset (no AI generation)",
                input_data_types=["user_uploaded_file"],
            )
            recorder.finish(
                response_summary="human_provided_background",
                output_artifact=background_path,
            )
            bg_image_path = background_path
            logger.info("[BG] Using human-provided background: %s", background_path)
        else:
            lyrics_text = " ".join(seg["text"] for seg in segments)
            # Prefer the structured title the operator set on the job; fall
            # back to filename parsing for legacy rows / batch uploads. The
            # cache key downstream uses (artist|title) as a namespace so
            # different songs don't share a Veo background.
            if song_title:
                _song_title = song_title
            else:
                _basename = os.path.splitext(os.path.basename(mp3_path))[0]
                if " - " in _basename:
                    _song_title = _basename.split(" - ", 1)[1]
                elif "_" in _basename:
                    _song_title = _basename.split("_", 1)[0]
                else:
                    _song_title = _basename
            for _sfx in ["(Official Video)", "(Official Audio)", "(Lyric Video)",
                         "(Official Music Video)", "(En Vivo)", "(Live)", "(Lyrics)"]:
                _song_title = _song_title.replace(_sfx, "").strip()
            if _animate_user_image:
                logger.info("[BG] image-to-video: animating user-supplied %s via Veo",
                            os.path.basename(background_path))

            # Capa C 2026-05-24 — bg_cache fast path. Si el operador hizo
            # pre-gen (POST /generate-preview) mientras editaba lyrics, el
            # video del fondo ya está en R2 bajo bg_cache/{key}.mp4. Lo
            # descargamos a job_dir y skip Veo/Imagen — ~60-180s y $0.80-3.20
            # de cuota ahorrados. Si el cache no existe (operador cambió
            # opciones después del preview, o el preview falló, o el TTL
            # de 24h del cache lo limpió), seguimos con el flow normal.
            bg_image_path = None
            if bg_cache_key and not _animate_user_image:
                bg_cache_key = _validate_bg_cache_key(
                    bg_cache_key, job_id=job_id, artist=artist,
                    song_title=song_title, style=style,
                    movement_style=movement_style, effect=effect,
                    custom_colors=custom_colors, genre=genre, concept=concept,
                    background_hint=background_hint, bg_verbatim=bg_verbatim,
                    match_lyrics=match_lyrics,
                )
            if bg_cache_key and not _animate_user_image:
                try:
                    from bg_preview import cache_check, cache_download
                    if cache_check(bg_cache_key):
                        cached_path = os.path.join(job_dir, f"bg_cached_{bg_cache_key}.mp4")
                        if cache_download(bg_cache_key, cached_path):
                            logger.info("[BG] cache HIT key=%s — reusando %s, skip Veo/Imagen",
                                        bg_cache_key, os.path.basename(cached_path))
                            bg_image_path = cached_path
                        else:
                            logger.warning("[BG] cache_check OK pero download falló key=%s — fallback",
                                           bg_cache_key)
                except Exception as e:
                    logger.warning("[BG] cache lookup error key=%s: %s — fallback", bg_cache_key, e)

            if bg_image_path is None:
                # Fix urgente 2026-05-25: pasar audio_duration al Ken Burns
                # render para evitar el palindrome loop trabado en audios >60s.
                # Best-effort: si el cómputo falla, ensure_background cae al
                # default de 60s (comportamiento previo).
                try:
                    _audio_dur_for_kb = _audio_duration(mp3_path)
                except Exception:
                    _audio_dur_for_kb = None
            # Add-on premium "Escenas": si está activo (y no es un fondo a partir
            # de imagen del usuario), armamos el timeline multi-escena. El
            # resultado entra al render como un fondo ya del largo completo
            # (_scenes_active → bg_prelooped=True). Si algo falla, log + caemos al
            # fondo único de _ensure_background (no rompemos el job).
            _scenes_active = False
            if bg_image_path is None and enable_scenes and not _animate_user_image:
                try:
                    update_job(job_id, current_step="scenes", progress=22)
                    _scene_timeline, _scene_plan = _generate_scene_background(
                        segments, _audio_dur_for_kb or _audio_duration(mp3_path),
                        job_dir, style_hint=style, lyrics_text=lyrics_text,
                        artist=artist, song_title=_song_title, genre=genre,
                        concept=concept, movement_style=movement_style,
                        custom_colors=custom_colors,
                        # El prompt del operador ("Mi prompt") moldea TODA la
                        # biblia → multi-escena respeta auto/letra/prompt igual
                        # que el fondo único. bg_verbatim ="usá mi texto tal cual".
                        background_hint=background_hint, bg_verbatim=bg_verbatim,
                        allow_people=_compute_allow_people(job_id),
                        job_id=job_id,
                    )
                    bg_image_path = _scene_timeline
                    _scenes_active = True
                    update_job(job_id, scene_plan=_scene_plan)
                    logger.info("[SCENES] timeline multi-escena listo para job=%s", job_id)
                except Exception as e:  # noqa: BLE001
                    logger.error("[SCENES] multi-escena falló para job=%s (%s) — "
                                 "fallback a fondo único", job_id, e)
                    _scenes_active = False
            if bg_image_path is None:
                bg_image_path = _ensure_background(
                    style, job_dir,
                    lyrics_text=lyrics_text, artist=artist, job_id=job_id,
                    song_title=_song_title, genre=genre, concept=concept,
                    movement_style=movement_style,
                    image_to_video_path=(background_path if _animate_user_image else None),
                    match_lyrics=match_lyrics,
                    background_hint=background_hint,
                    bg_verbatim=bg_verbatim,
                    custom_colors=custom_colors,
                    effect=effect,
                    allow_people=_compute_allow_people(job_id),
                    audio_duration=_audio_dur_for_kb,
                )
            # Image-to-video fallback: if Veo failed to produce an MP4 (None
            # or non-existent path) AND the operator wanted to animate their
            # image, fall back to using the still image with Ken Burns.
            if _animate_user_image and (not bg_image_path or not os.path.exists(bg_image_path)):
                logger.warning("[BG] image-to-video failed, falling back to Ken Burns on %s",
                               background_path)
                bg_image_path = background_path
        update_job(job_id, progress=40)

        # Persist render params so edit/variant re-renders + reaper-recovery
        # retries can override individual fields without losing the rest.
        #
        # CRITICAL: MERGE over the existing render_params, do NOT replace the
        # column wholesale. /variant and /edit write background_hint into
        # render_params BEFORE the worker runs; an earlier version of this
        # block did update_job(render_params={...}) with a dict that omitted
        # background_hint, and update_job does a plain setattr (replace) — so
        # the operator's hint was wiped after the very first render and
        # vanished on every later retry. That is the Amanda Pujó "Ser Anti"
        # alley regression (2026-05-20): the hint rendered once, the job was
        # reaped during the Railway outage, and the retry had no hint left to
        # forward so Gemini reverted to its default alley cliché.
        _new_rp = {
            "font": font,
            "text_case": text_case,
            "font_scale": font_scale,
            "lyric_transition": lyric_transition,
            "text_motion": text_motion,
            "lyrics_animation": lyrics_animation,
            "line_transition": line_transition,
            "style": style,
            "genre": genre,
            "concept": concept,
            "movement_style": movement_style,
            "effect": effect,
            "match_lyrics": match_lyrics,
            # Title-card customization (Full Rotor v1). Safe defaults, always
            # persisted so future edits/retries inherit the operator's choice.
            "title_template": title_template,
            "title_size": title_size,
            "title_artist_font": title_artist_font,
            "title_song_font": title_song_font,
            # UI v1.1 (2026-05-30): manual song split. Empty string => auto
            # (legacy). When set, the operator picked their own line break in
            # the wizard and we persist it so retries/edits respect it.
            "title_song_break": title_song_break,
        }
        # Only persist background_hint / bg_verbatim when this run actually
        # received them — otherwise a hint-less typography edit would null out
        # values a prior background edit had stored (merge-not-replace).
        if background_hint:
            _new_rp["background_hint"] = background_hint
        if bg_verbatim:
            _new_rp["bg_verbatim"] = True
        if custom_colors:
            _new_rp["custom_colors"] = custom_colors
        # Escenas (multi-escena): persistimos sólo cuando está ON (mismo
        # criterio que bg_verbatim) — así un edit de tipografía que no manda
        # el flag no apaga un job que ya era multi-escena, y retry/variant lo
        # heredan vía la whitelist de render_params en main.py.
        if enable_scenes:
            _new_rp["enable_scenes"] = True
        # U5 (audit 2026-05-25): merge atómico vía jobs.merge_render_params.
        # El read-then-write fuera de un row lock dejaba race: 2 callers
        # concurrentes (worker + /edit endpoint, o 2 /edit calls) podían
        # pisarse mutuamente. merge_render_params hace SELECT FOR UPDATE
        # + read + write en UNA tx.
        try:
            from jobs import merge_render_params
            merge_render_params(job_id, _new_rp)
        except Exception as _rp_exc:
            logger.warning("[BG] merge_render_params failed: %s", _rp_exc)

        # Cache the FINAL background to R2 so a typography/lyrics/metadata
        # edit can re-use it without another Veo call ($0.80 saved per edit)
        # and /retry can preserve it.
        #
        # Audit 2026-06-11: antes la condición era `not background_path`
        # ("los fondos humanos ya tienen su key en bg_r2_key") — pero esa
        # key nunca llegaba a bg_r2_key_cached, así que los jobs con fondo
        # subido por el usuario tenían los edits rápidos bloqueados con 400
        # y el retry les pisaba la imagen. Ahora:
        #   - Fondo humano usado tal cual (bg_image_path == background_path):
        #     reutilizamos bg_r2_key como cached (sin re-subir nada).
        #   - Cualquier otro caso (Veo, Imagen, imagen del usuario ANIMADA
        #     con i2v): subimos el resultado final — así un edit sobre una
        #     imagen animada reusa el clip animado, no la foto fija.
        if bg_image_path and os.path.exists(bg_image_path):
            import storage as _storage
            if background_path and bg_image_path == background_path:
                if bg_r2_key:
                    update_job(job_id, bg_r2_key_cached=bg_r2_key)
                    logger.info("[EDIT] Cached human-provided background key: %s", bg_r2_key)
            elif _storage.is_enabled():
                try:
                    _bg_ext = os.path.splitext(bg_image_path)[1] or ".mp4"
                    _bg_cache_key = _storage.upload_file(
                        bg_image_path,
                        f"backgrounds/{job_id}/bg_cached{_bg_ext}",
                    )
                    if _bg_cache_key:
                        update_job(job_id, bg_r2_key_cached=_bg_cache_key)
                        logger.info("[EDIT] Cached background to R2: %s", _bg_cache_key)
                except Exception as _e:
                    logger.warning("[EDIT] Warning: background cache upload failed: %s", _e)

        files = {}
        # La elección de tipografía se hace UNA vez acá (operador o pick
        # del pool) y fluye a video + short + persiste para re-renders.
        # Fix incidente UMG Chile 2026-06-11 — ver _pick_concrete_font.
        chosen_font = _pick_concrete_font(font, job_id, job_dir, deterministic=wants_umg)
        if chosen_font:
            logger.info("[FONT] Selected: %s", os.path.basename(chosen_font))
        bg_source = bg_image_path

        # Step 1b — Pre-render content validation (UMG Guideline 15).
        # We validate the BACKGROUND ASSET BEFORE the expensive render so
        # we don't burn 5+ minutes of CPU only to throw the result away.
        # Two paths:
        #   - Operator/AI supplied a specific bg → validate it; if it
        #     fails, mark validation_failed (we can't auto-substitute).
        #   - No bg supplied → cycle through library candidates, picking
        #     the first one that passes (up to 3 attempts).
        if wants_youtube:
            update_job(job_id, current_step="validation", progress=38)

            # Tenant-gated content validation.
            #
            # The validator enforces UMG Guideline 15 (no recognizable
            # faces / hands / logos as subject). That's a *UMG* contract
            # constraint — non-UMG tenants don't need it by default.
            #
            # Default behavior:
            #   - tenant in UMG_TENANTS → validator runs.
            #     Override OFF via render_params.bypass_content_validation=True
            #     ("Asumir el riesgo" toggle).
            #   - tenant NOT in UMG_TENANTS → validator skipped.
            #     Override ON via render_params.force_content_validation=True
            #     ("Activar verificación" toggle, opt-in).
            #
            # Both flags are safe to send regardless of tenant: each only
            # has effect when it pushes against its tenant's default.
            # UMG_TENANTS lives at module scope so _compute_allow_people()
            # and this block share the same source of truth.
            _tenant_id = None
            _bypass_validation = False
            _force_validation = False
            try:
                from database import SessionLocal as _SL, Job as _Job
                with _SL() as _db:
                    _row = _db.query(_Job).filter(_Job.job_id == job_id).first()
                    if _row:
                        _tenant_id = _row.tenant_id
                        if isinstance(_row.render_params, dict):
                            _bypass_validation = bool(
                                _row.render_params.get("bypass_content_validation")
                            )
                            _force_validation = bool(
                                _row.render_params.get("force_content_validation")
                            )
            except Exception as e:
                logger.warning("[VALIDATION] could not read tenant/flags, defaulting to UMG-enforce: %s", e)
                _tenant_id = "umg"  # conservative fallback: validate

            _is_umg = _tenant_id in UMG_TENANTS
            # XOR-like semantics: tenant default ⊕ operator override
            _should_validate = (
                (_is_umg and not _bypass_validation)
                or (not _is_umg and _force_validation)
            )

            if not _should_validate:
                from datetime import datetime as _dt
                _reason = (
                    "operator_override_via_bypass" if _is_umg and _bypass_validation
                    else "non_umg_tenant_default"
                )
                logger.warning(
                    "[VALIDATION] SKIPPED for job %s tenant=%s is_umg=%s "
                    "bypass=%s force=%s reason=%s",
                    job_id, _tenant_id, _is_umg, _bypass_validation,
                    _force_validation, _reason,
                )
                update_job(job_id, validation_result={
                    "passed": True,
                    "issues": [],
                    "bypassed": True,
                    "bypassed_at": _dt.utcnow().isoformat() + "Z",
                    "bypassed_reason": _reason,
                    "tenant_id": _tenant_id,
                })
            elif bg_image_path:
                from content_validator import validate_video, validate_image
                ext = os.path.splitext(bg_image_path)[1].lower()
                _validate_fn = (
                    validate_video if ext in (".mp4", ".mov", ".webm")
                    else validate_image
                )
                pre_validation = _validate_fn(bg_image_path, job_id=job_id)
                update_job(job_id, validation_result=pre_validation)
                if not pre_validation["passed"]:
                    update_job(
                        job_id,
                        status="validation_failed",
                        error=f"Content policy violation detected: {pre_validation['issues']}",
                    )
                    logger.warning("[VALIDATION] FAILED for job %s: %s", job_id, pre_validation['issues'])
                    return
            else:
                clean_bg, rejection_log = _select_validated_background(job_id)
                if not clean_bg:
                    update_job(
                        job_id,
                        status="validation_failed",
                        error=(
                            "No clean background found after retries. "
                            f"Rejections: {rejection_log}"
                        ),
                    )
                    logger.warning("[VALIDATION] FAILED for job %s: no clean bg after retries", job_id)
                    return
                bg_image_path = clean_bg
                update_job(job_id, validation_result={
                    "passed": True, "issues": [], "rejections": rejection_log,
                })

        # Step 2 — Render the source MP4 (H.264 yuv420p aac mp4).
        # Always rendered when ANY delivery profile is requested. The UMG
        # ProRes is generated lazily at download time from this MP4
        # (see _transcode_to_prores + /download/{id}/umg_master) so we
        # avoid the dual-render moviepy-palindrome hang.
        #
        # WHEN UMG IS REQUESTED, render the MP4 at the EXACT UMG target
        # dimensions and fps (still cheap codec). The lazy ProRes
        # transcode then becomes a pure codec/audio/container swap — no
        # ffmpeg scale, no fps interpolation, no chroma stretch. This
        # is what makes the master pass UMG manual QC for any of the
        # 4 frame sizes × 8 fps the spec sheet allows.
        if wants_youtube or wants_umg:
            if wants_umg and not umg_spec:
                raise RuntimeError("UMG delivery requested without umg_spec")
            update_job(job_id, current_step="video", progress=40)
            # The word-level animations (karaoke fill + word_reveal) need
            # per-word timing to stay in sync. Derive it once via forced-align
            # (gated to those animations; no-op/fallback otherwise) and cache it
            # into segments_json so re-renders don't re-pay. Isolated from the
            # transcription pipeline by design.
            if lyrics_animation in ("karaoke", "word_reveal"):
                import karaoke_align
                _enriched = karaoke_align.enrich_segments_with_word_timings(segments, mp3_path)
                if _enriched is not segments:
                    segments = _enriched
                    update_job(job_id, segments_json=segments)
            intermediate_spec = (
                RenderSpec.umg_intermediate_master(umg_spec) if wants_umg
                else None  # generate_lyric_video defaults to youtube_default
            )
            _, chosen_font, bg_source = generate_lyric_video(
                mp3_path, segments, style, job_dir, artist, bg_image_path,
                font=chosen_font, spec=intermediate_spec,
                song_title=song_title,
                text_case=text_case,
                font_scale=font_scale,
                lyric_transition=lyric_transition,
                text_motion=text_motion,
                lyrics_animation=lyrics_animation,
                line_transition=line_transition,
                text_contrast=text_contrast,
                effect=effect, custom_colors=custom_colors,
                lyric_color=lyric_color, lyric_sung_color=lyric_sung_color,
                title_template=title_template, title_size=title_size,
                title_artist_font=title_artist_font, title_song_font=title_song_font,
                title_song_break=title_song_break,
                # Multi-escena: el fondo ya es un timeline del largo completo.
                bg_prelooped=_scenes_active,
            )
            files["video_url"] = f"/download/{job_id}/video"
            update_job(job_id, progress=55)

        # Lazy ProRes — register the URLs so the UI shows the
        # "Master ProRes" + "Short ProRes" download buttons. The
        # actual .mov files are generated on the first GET
        # /download/{id}/umg_master or /download/{id}/umg_short
        # from the existing MP4 / short.mp4 via ffmpeg (no moviepy
        # involvement).
        if wants_umg:
            files["umg_master_url"] = f"/download/{job_id}/umg_master"
            files["umg_short_url"] = f"/download/{job_id}/umg_short"

        # Step 3 — Short (1080×1920 vertical). Same fps as the master
        # when UMG-bound so the lazy ProRes short is also a pure recode.
        if wants_youtube or wants_umg:
            update_job(job_id, current_step="short", progress=75)
            short_fps = float(umg_spec["fps"]) if wants_umg else 24
            generate_short(
                mp3_path, segments, job_dir, bg_source=bg_source,
                style=style, font=chosen_font, fps=short_fps,
                text_case=text_case, font_scale=font_scale,
                lyric_color=lyric_color, lyric_sung_color=lyric_sung_color,
                text_contrast=text_contrast, effect=effect, custom_colors=custom_colors,
                lyrics_animation=lyrics_animation, line_transition=line_transition,
            )
            files["short_url"] = f"/download/{job_id}/short"
            update_job(job_id, progress=85)

            # Step 4 — Thumbnail (uses raw background, not lyric video)
            update_job(job_id, current_step="thumbnail", progress=90)
            generate_thumbnail(
                artist, mp3_path, job_dir, bg_source=bg_source,
                song_title=song_title,
            )
            files["thumbnail_url"] = f"/download/{job_id}/thumbnail"

        # Content validation already happened pre-render (Step 1b) so the
        # background here is guaranteed clean. No post-render check needed.

        # Sanity-check every deliverable before uploading to R2. Catches
        # silent failures (truncated files, codec mismatches, duration
        # drift) so we mark the job as error here instead of shipping
        # garbage to UMG.
        try:
            audio_dur_for_verify = _audio_duration(mp3_path)
        except Exception:
            audio_dur_for_verify = None
        if audio_dur_for_verify is None:
            # _audio_duration uses mutagen/wave; fall back to ffprobe for any format
            audio_dur_for_verify = _ffprobe_duration(mp3_path)
        _verify_deliverables(job_dir, files, audio_dur_for_verify)

        # Post-render upload to cloud storage. No-op if R2 env not set.
        # _upload_deliverables_to_r2 now persists each successful key
        # atomically via merge_s3_keys (audit 2026-05-26) — caller no
        # longer needs to do its own update_job(s3_keys=...) because that
        # would REPLACE any concurrent prores prewarm key. Critical
        # deliverable failures raise — caught by the outer except below.
        _upload_deliverables_to_r2(job_id, job_dir, files)

        # Drop intermediate files (looped backgrounds, gradient fallbacks).
        # Deliverables are already removed above when R2 was used.
        _cleanup_local_intermediates(job_dir)

        # Robust env-var read: tolerate accidental whitespace or quotes that
        # Railway / .env files sometimes leave around the value. We default to
        # review-required so the safest behaviour applies if the var is missing.
        _require_review_raw = os.environ.get("REQUIRE_REVIEW")
        if _require_review_raw is None:
            _require_review = True
        else:
            _normalized = _require_review_raw.strip().strip('"').strip("'").lower()
            _require_review = _normalized in ("true", "1", "yes", "y", "on")
        final_status = "pending_review" if _require_review else "done"
        logger.info("[PIPELINE] job=%s REQUIRE_REVIEW=%r -> require_review=%s final_status=%s",
                    job_id, _require_review_raw, _require_review, final_status)

        update_job(job_id, status=final_status, progress=100, files=files)

        # Send job completion email to the user (best-effort, never blocks render).
        try:
            import emails as _emails
            from database import SessionLocal as _SessionLocal, Job as _Job
            from database import User as _User, UserSettings as _UserSettings
            _ndb = _SessionLocal()
            try:
                _job_row = _ndb.query(_Job).filter(_Job.job_id == job_id).first()
                if _job_row and _job_row.user_id:
                    _usr = _ndb.query(_User).filter(_User.id == _job_row.user_id).first()
                    if _usr and _usr.email:
                        _settings = _ndb.query(_UserSettings).filter(
                            _UserSettings.user_id == _usr.id
                        ).first()
                        _prefs = (_settings.settings_json or {}) if _settings else {}
                        if _prefs.get("notif_jobs", False):
                            threading.Thread(
                                target=_emails.send_job_completed,
                                kwargs={
                                    "email": _usr.email,
                                    "username": _usr.username,
                                    "artist": artist or "",
                                    "filename": os.path.basename(mp3_path),
                                    "job_id": job_id,
                                },
                                daemon=True,
                            ).start()
            finally:
                _ndb.close()
        except Exception as _email_err:
            logger.warning("[PIPELINE] job completion email skipped: %s", _email_err)

        # G4: pre-warm the ProRes deliverables in a background worker
        # job. When UMG eventually clicks "Master ProRes" the .mov is
        # already on R2 (302 instant) instead of paying 60-120 s of
        # ffmpeg in the request thread. Best-effort — never fail the
        # main render because the prewarm couldn't be enqueued.
        if wants_umg and final_status in ("done", "pending_review"):
            try:
                from queue_jobs import enqueue_prores_prewarm
                enqueue_prores_prewarm(job_id, "umg_master")
                enqueue_prores_prewarm(job_id, "umg_short")
            except Exception as e:  # pragma: no cover
                logger.warning("[PIPELINE] prores prewarm enqueue skipped: %s", e)

        # PR feat/waveform-precompute 2026-05-27: pre-compute the timeline
        # waveform now, while the worker still has the input MP3 in
        # context, so the FIRST time the operator opens the editor on this
        # job (post-approval lyrics fix) /jobs/:id/waveform is a cache
        # hit (~200ms instead of the 5-30s cold-cache cost from
        # downloading the MP3 + running librosa.load). Best-effort —
        # never fail the main render because the waveform precompute
        # had a hiccup, the on-demand endpoint will recompute on first
        # operator request as a fallback.
        if final_status in ("done", "pending_review"):
            try:
                # Resolve input_r2_key from the row (the local `mp3_path`
                # used during render is ephemeral; the helper downloads
                # the canonical R2 copy). Cheap session, closed immediately.
                from database import SessionLocal
                from jobs import get_job_model
                from waveform_compute import compute_and_cache_waveform
                _db = SessionLocal()
                try:
                    _row = get_job_model(_db, job_id)
                    _input_key = _row.input_r2_key if _row else None
                finally:
                    _db.close()
                if _input_key:
                    payload = compute_and_cache_waveform(job_id, _input_key)
                    if payload is None:
                        logger.warning(
                            "[WAVEFORM] precompute returned None for %s — operator's "
                            "first open will recompute on-demand",
                            job_id,
                        )
            except Exception as e:  # pragma: no cover
                logger.warning("[WAVEFORM] precompute skipped for %s: %s", job_id, e)
    except Exception as exc:
        traceback.print_exc()
        from error_taxonomy import classify_error
        update_job(
            job_id, status="error", error=str(exc),
            error_category=classify_error(str(exc)),
        )
        # Surface render failures to Sentry. The worker runs outside
        # the FastAPI request loop so the framework's auto-capture
        # doesn't fire — without this explicit hook, ffmpeg hangs,
        # OOMs, Veo 429-storms, etc. would be invisible. Wrapped to
        # never let observability break the failure path.
        try:
            import sentry_sdk
            with sentry_sdk.push_scope() as _scope:
                _scope.set_tag("event", "pipeline.failed")
                _scope.set_tag("job_id", job_id)
                _scope.set_tag("artist", artist or "?")
                sentry_sdk.capture_exception(exc)
        except Exception:
            pass
        # Tier 4 (C5): free the disk on failure. Without this, a failed render's
        # multi-GB intermediates pile up until the disk fills and the NEXT
        # render fails mid-flush. When R2 is enabled the input can be re-fetched
        # (input_r2_key), so rmtree the whole dir — an RQ retry re-downloads +
        # re-renders. When it ISN'T, the input lives only locally inside job_dir,
        # so preserve it (just free the heavy bg intermediates) — else the retry
        # would fail for lack of its own input. (Adversarial-review guard.)
        if input_r2_key:
            _cleanup_job_dir_on_failure(job_dir)
        else:
            _cleanup_local_intermediates(job_dir)


# ---------------------------------------------------------------------------
# Step 1 — Whisper transcription
# ---------------------------------------------------------------------------

# YouTube-uploader chatter we don't want in the lyrics. Tight-and-narrow:
# every entry must be a multi-word phrase or unambiguous YouTuber jargon
# that essentially never shows up in song lyrics. The previous broader
# list killed legit content on UMG videos that open with dialogue/intros
# (Karol G "Si Antes Te Hubiera Conocido (Official Video)" had
# "¡Gracias! ¡Qué linda! ¡Gracias!" filtered as spam — that's the artist
# thanking the audience in the video, not channel chatter, and the
# operator wanted it transcribed).
_SPAM_PATTERNS = [
    "suscribete al canal", "subscribe to my channel",
    "thanks for watching", "thanks for listening",
    "link in description", "link in the description",
    "link en la descripcion", "link en descripcion",
    "all rights reserved", "todos los derechos reservados",
    "escucha en spotify", "available on spotify",
    "apple music", "deezer", "amazon music",
    "music by", "produced by", "lyrics by",
]


# Whisper-1 has a small set of "training-data hallucinations" that fire
# during silence / quiet music intros — phrases lifted directly from
# subtitle datasets (Amara.org credits, "♪ music ♪" tags) that the
# model emits as a sequence completion when there's no real speech.
# These are NOT YouTube uploader chatter (above) — they're outputs that
# come straight from training data leakage. Tight match because we want
# zero false positives on real lyrics.
#
# Phrases here are matched PUNCTUATION-INSENSITIVELY (both sides folded
# to lowercase ASCII words separated by single spaces). Whisper does not
# always transcribe the dot in "Amara.org" — the Lamento Boliviano
# regression (2026-06-01) emitted "…comunidad de Amara org", which the
# old literal "amara.org" needles never matched. Store needles already
# in folded form: no dots, no colons, single spaces.
_WHISPER_HALLUCINATION_PHRASES = [
    "subtitulos realizados por la comunidad",  # …de Amara org / Amara.org / (nothing)
    "realizados por la comunidad de amara",
    "subtitled by the amara org community",
    "subtitles by the amara org community",
    "subtitling by the amara org community",
    "transcribed by amara",
    "amara org",  # folded "amara.org" — also catches "Visit amara.org"
    "subtitles created by",
    "subtitles by",
    "subtitulado por",
    "transcripcion por",
]

# Patterns where the punctuation IS the signal — folding them would
# leave a bare word ("music") that appears in real lyrics. Matched
# literally against the lowercased raw text instead.
_WHISPER_HALLUCINATION_LITERALS = [
    "♪ music ♪",
    "[ music ]",
    "[music]",
]


def _fold_for_hallucination_match(text: str) -> str:
    """Lowercase ASCII fold + replace every non-alphanumeric run with a
    single space. "Subtítulos … de Amara.org", "…de Amara org" and
    "…de Amara. org" all fold to the same string."""
    import unicodedata as _u
    s = _u.normalize("NFD", text or "").encode("ascii", "ignore").decode("ascii").lower()
    return " ".join(re.sub(r"[^a-z0-9]+", " ", s).split())


def _is_whisper_hallucination(text: str) -> bool:
    """True if the segment text matches a known Whisper training-data
    hallucination. Match is case-, accent- and punctuation-insensitive
    for word phrases; symbol patterns ("[music]") match literally."""
    if not text:
        return False
    raw = " ".join(text.lower().split())
    for needle in _WHISPER_HALLUCINATION_LITERALS:
        if needle in raw:
            return True
    s = _fold_for_hallucination_match(text)
    for needle in _WHISPER_HALLUCINATION_PHRASES:
        if needle in s:
            return True
    return False


def _is_single_word_loop(
    text: str, min_repeats: int = 8, seg_duration: float | None = None
) -> bool:
    """True if `text` is essentially the same short word repeated many
    times — Whisper-1's classic outro/sustained-vocal failure mode
    ("oh, oh, oh, oh, …" × 100). We detect by checking that, after
    normalising, ≥ 90 % of tokens are the same single word AND the
    repeat count is ≥ `min_repeats`.

    This catches the AIRBAG / River Plate case (110 "oh"s in a 30 s
    segment) without flagging real lyrics: a chorus like "oh-oh-oh I
    love you oh-oh" stays mixed enough that the dominant token never
    reaches 90 % concentration.

    `seg_duration` (seconds): when provided, the loop is NOT treated as a
    hallucination if the average pace is ≥ 0.5 s/token. Real musical
    vocalisations ("uh uh uh" ad-libs) run at 0.7–0.9 s/syllable; Whisper
    hallucinations run at 0.1–0.3 s/token (the model streams tokens faster
    than the audio supports). This avoids dropping genuine "uh × 9–12"
    blocks that Whisper transcribes correctly for songs where the artist
    sustains an ad-lib section.
    """
    if not text:
        return False
    tokens = [n for n in (_normalize_token(w) for w in text.split()) if n]
    if len(tokens) < min_repeats:
        return False
    from collections import Counter as _C
    counts = _C(tokens)
    top_token, top_count = counts.most_common(1)[0]
    # Require the dominant token to be short (≤ 4 chars) so we don't
    # collapse a verse that legitimately repeats a longer word.
    if len(top_token) > 4:
        return False
    if top_count / len(tokens) < 0.9 or top_count < min_repeats:
        return False
    # Duration guard: if pace is ≥ 0.5 s/token the content is musically
    # paced — keep it. Hallucination loops pack tokens at 0.1–0.3 s each.
    if seg_duration is not None and len(tokens) > 0:
        if seg_duration / len(tokens) >= 0.5:
            return False
    return True


def _collapse_consecutive_duplicates(
    segments: list[dict], *, with_counts: bool = False,
):
    """Collapse streaks of consecutive identical-text segments into
    one — but only when the streak looks like a Whisper hallucination
    loop, not a legitimate repeated chorus.

    Bug B1 from the 2026-05-18 audit (Una Vez Más — Viejas Locas):
    the previous version always collapsed any consecutive duplicate
    streak. That was correct for Whisper's "¡Karol!" hallucination
    (174 false repetitions) but destroyed the song's outro fadeout
    chorus, which legitimately repeats 4–6 times.

    Heuristic (conservative on the "keep separately" side):

      - streak length ≤ CHORUS_MAX_REPS (6) AND text length >
        CHORUS_MIN_TEXT_LEN (4)  →  keep all segments separate
        (legit chorus pattern: a few repetitions of a normal line)

      - streak length > CHORUS_MAX_REPS  →  collapse
        (a chorus rarely repeats more than 6 times in a row; many
        more is the Karol-style loop signature)

      - text length ≤ CHORUS_MIN_TEXT_LEN (e.g. "no", "oh") AND
        streak length ≥ 4  →  collapse
        (single-word/very-short repeated phrases are the classic
        Whisper outro filler; collapse to a chant span)

      - otherwise → keep all separate (default conservative bias)

    Text comparison is case-insensitive and whitespace-trimmed —
    Whisper occasionally varies capitalisation across consecutive
    segments ("¡Karol!" / "¡KAROL!"), and a chorus shouldn't get
    accidentally preserved because of that.

    Returns the collapsed segments. When `with_counts=True`, returns
    `(segments, collapsed_groups, collapsed_total)` so callers can log
    how many merges happened.
    """
    CHORUS_MAX_REPS = 6
    CHORUS_MIN_TEXT_LEN = 4

    if not segments:
        return ([], 0, 0) if with_counts else []

    def _norm(text: str) -> str:
        return (text or "").lower().strip()

    # First pass: group consecutive identical-text streaks.
    streaks: list[list[dict]] = []
    for seg in segments:
        if streaks and _norm(streaks[-1][-1]["text"]) == _norm(seg["text"]):
            streaks[-1].append(seg)
        else:
            streaks.append([seg])

    # Second pass: apply heuristic per streak.
    out: list[dict] = []
    collapsed_groups = 0
    collapsed_total = 0
    for streak in streaks:
        if len(streak) == 1:
            out.append({**streak[0]})
            continue
        text = _norm(streak[0]["text"])
        should_collapse = (
            len(streak) > CHORUS_MAX_REPS
            or (len(text) <= CHORUS_MIN_TEXT_LEN and len(streak) >= 4)
        )
        if should_collapse:
            merged = {**streak[0]}
            merged["end"] = max(s["end"] for s in streak)
            out.append(merged)
            collapsed_groups += 1
            collapsed_total += len(streak) - 1
        else:
            for s in streak:
                out.append({**s})

    if with_counts:
        return out, collapsed_groups, collapsed_total
    return out


def _filter_whisper_hallucinations(segments: list[dict]) -> tuple[list[dict], int]:
    """Drop segments whose text is a known Whisper hallucination phrase
    OR a single-word loop (e.g. "oh, oh, oh, …" outro fills). The
    single-word filter runs BEFORE _detect_hallucination in the caller
    so a legitimate transcription with a loopy outro doesn't get its
    whole timeline thrown out by the recovery branch.

    Returns (filtered_segments, dropped_count) for logging.
    """
    if not segments:
        return segments, 0
    out = []
    for s in segments:
        text = s.get("text") or ""
        if _is_whisper_hallucination(text):
            continue
        dur = float(s.get("end", 0)) - float(s.get("start", 0))
        if _is_single_word_loop(text, seg_duration=dur):
            logger.info("[WHISPER] dropping single-word loop (%.1fs, %.2fs/tok): %r",
                        dur, dur / max(len(text.split()), 1), text[:60])
            continue
        out.append(s)
    return out, len(segments) - len(out)


# ── Post-reconcile cleanup ────────────────────────────────────────────────────
# Logic lives in post_reconcile.py (lightweight, no heavy deps) so it can be
# unit-tested in isolation. The alias below preserves the internal call-site.

def _post_reconcile_cleanup(segments: list[dict]) -> list[dict]:
    from post_reconcile import post_reconcile_cleanup
    return post_reconcile_cleanup(segments)


def _filter_intro_song_overlap(
    intro_segs: list[dict],
    song_segs: list[dict],
    threshold: float = 0.7,
) -> tuple[list[dict], int]:
    """Drop intro Whisper segments that fuzzy-match the song's opening
    lines. When a user uploads a track with an instrumental intro,
    Whisper run on the prefix slice often hallucinates the first verse
    (using the lrclib `lyrics_hint` as a noisy prior) at start≈0. We
    then concatenate that hallucinated segment in front of the
    LRCLIB-aligned song segments — producing a phantom "first line at
    0:00.0" in the editor while the real first line sits at the offset.

    Heuristic: only drop intro segs whose start is before the song's
    first sung line AND whose normalised text fuzzy-matches one of the
    first few song segments (≥ `threshold`). Intro segs that sit fully
    inside the instrumental window but transcribe genuinely different
    text (e.g. a spoken-word preamble) survive.
    """
    if not intro_segs or not song_segs:
        return intro_segs, 0
    from difflib import SequenceMatcher

    def _norm(t: str) -> str:
        return _normalize_token(" ".join((t or "").split()))

    song_heads = [_norm(s.get("text") or "") for s in song_segs[:4]]
    song_first_start = float(song_segs[0].get("start", 0.0))
    kept: list[dict] = []
    dropped = 0
    for s in intro_segs:
        t_norm = _norm(s.get("text") or "")
        if not t_norm:
            kept.append(s)
            continue
        if float(s.get("start", 0.0)) >= song_first_start:
            kept.append(s)
            continue
        is_dup = any(
            sh and SequenceMatcher(None, t_norm, sh).ratio() >= threshold
            for sh in song_heads
        )
        if is_dup:
            dropped += 1
            continue
        kept.append(s)
    return kept, dropped


_WHISPER_MODELS: dict = {}
_WHISPER_LOCK = None


def _fix_lrc_first_line_at_zero(
    segments: list[dict],
    audio_duration: float | None = None,
) -> tuple[list[dict], float | None]:
    """Auto-correct the lrclib "first line at [00:00.00]" quirk.

    A non-trivial fraction of community-curated LRCs anchor line 1 to
    song time 0 even when there's a long instrumental intro before the
    first vocal. Trusting the LRC then shows the first lyric pinned to
    0:00 in the editor / video while the actual vocal entry sits ~15 s
    later — exactly the bug the operator hit on Intoxicados — "No Tengo
    Ganas".

    We can't ground-truth the vocal entry without a VAD pass, but the
    LRC's OWN cadence betrays the quirk: the gap between line 1 and
    line 2 is dramatically larger than the median gap between
    subsequent verse / chorus lines. When all three of these hold we
    relocate line 1 to ``line2.start - median_gap`` (the spot a normal
    cadence would put it):

      - segments[0].start <= 1.0
      - gap(line1, line2) > 2 × median(gaps in lines 2..6)
      - gap(line1, line2) > 8 s in absolute terms

    The thresholds are conservative — a song with a normal 4-second
    intro on line 1 won't false-positive (gap to line 2 is ~8 s but
    the ratio against the median typically isn't > 2×).

    Returns (segments_with_first_fixed, suggested_new_start_or_None).
    The second value is logged by the caller for observability.
    """
    if len(segments) < 4:
        return segments, None
    first = segments[0]
    second = segments[1]
    first_start = float(first.get("start", 0.0))
    second_start = float(second.get("start", 0.0))
    if first_start > 1.0:
        return segments, None
    gaps: list[float] = []
    for i in range(1, min(len(segments) - 1, 6)):
        gaps.append(float(segments[i + 1]["start"]) - float(segments[i]["start"]))
    if not gaps:
        return segments, None
    gaps.sort()
    median_gap = gaps[len(gaps) // 2]
    first_gap = second_start - first_start
    if median_gap <= 0:
        return segments, None
    if first_gap < median_gap * 2 or first_gap < 8.0:
        return segments, None
    suggested = max(0.0, second_start - median_gap)
    seg_dur = max(0.5, float(first.get("end", suggested)) - first_start)
    new_end = suggested + seg_dur
    if audio_duration:
        new_end = min(float(audio_duration), new_end)
    # Don't let the new line bleed into line 2.
    if new_end > second_start - 0.05:
        new_end = max(suggested + 0.5, second_start - 0.05)
    fixed_first = {**first, "start": suggested, "end": new_end}
    return [fixed_first] + list(segments[1:]), suggested


def _get_whisper_model(name: str = "turbo"):
    """Load a Whisper model once per process and reuse. Thread-safe. Supports
    multiple sizes cached side-by-side so we can fall back turbo -> large-v3
    without re-loading the first one."""
    global _WHISPER_LOCK
    import whisper
    import threading as _t
    if _WHISPER_LOCK is None:
        _WHISPER_LOCK = _t.Lock()
    with _WHISPER_LOCK:
        if name not in _WHISPER_MODELS:
            logger.info("[WHISPER] Loading model '%s' (one-time)", name)
            _WHISPER_MODELS[name] = whisper.load_model(name)
    return _WHISPER_MODELS[name]


_WHISPER_API_MAX_BYTES = 24 * 1024 * 1024  # 25 MB ceiling, with 1 MB headroom


def _compress_for_whisper(input_path: str) -> str:
    """If `input_path` exceeds Whisper-API's 25 MB ceiling (typical for
    UMG-style WAV uploads), produce a temp 128 kbps mono MP3 alongside
    it and return the new path. Otherwise return the original path
    unchanged. Caller is responsible for cleaning up the temp file when
    `_compress_for_whisper(p) != p`.

    Why mono 128 kbps: Whisper's transcription accuracy is bounded well
    above this bitrate — extra fidelity doesn't help. Stereo→mono cuts
    size in half with zero impact on Whisper. A 4-min track lands around
    4 MB, comfortably under the cap.
    """
    try:
        sz = os.path.getsize(input_path)
    except OSError:
        return input_path
    if sz <= _WHISPER_API_MAX_BYTES:
        return input_path
    import subprocess as _sp
    out = input_path + ".whisper.mp3"
    try:
        _sp.run(
            ["ffmpeg", "-y", "-i", input_path,
             "-ac", "1", "-b:a", "128k", "-loglevel", "error", out],
            check=True, timeout=120,
            capture_output=True, text=True,
        )
    except (_sp.CalledProcessError, _sp.TimeoutExpired,
            FileNotFoundError, OSError) as e:
        # Previously this swallowed the error and returned the original
        # 30-50 MB file. The Whisper API would then 413 / 400 and the
        # operator saw a generic "Error al procesar" with no diagnostic.
        # Surface the real cause via the pipeline catch-all (which sets
        # job.error and tags Sentry) instead.
        stderr = (getattr(e, "stderr", "") or "") if isinstance(
            e, _sp.CalledProcessError
        ) else ""
        raise RuntimeError(
            f"audio_compression_failed: ffmpeg no pudo transcodificar "
            f"{os.path.basename(input_path)} para Whisper API "
            f"(tamaño {sz/1e6:.1f} MB > {_WHISPER_API_MAX_BYTES/1e6:.0f} MB). "
            f"Detalle: {(stderr or str(e))[-500:]}"
        ) from e
    if not os.path.exists(out) or os.path.getsize(out) == 0:
        raise RuntimeError(
            f"audio_compression_failed: ffmpeg returned 0 but produced "
            f"an empty/missing output at {out!r}"
        )
    new_sz = os.path.getsize(out)
    logger.info("[WHISPER-API] compressed %.1f MB -> %.1f MB for API limit",
                sz / 1e6, new_sz / 1e6)
    return out


def _transcribe_via_openai_api(mp3_path: str, language: str | None = None,
                                lyrics_hint: str | None = None,
                                job_id: str | None = None,
                                return_words: bool = False) -> list[dict]:
    """Transcribe by calling OpenAI's Whisper API. Returns the same segments
    structure as the local Whisper path. Used in production where loading
    the local model would consume too much worker RAM (~3 GB) and risks OOM.

    Cost: ~$0.006 per minute of audio (~$0.02 per song).

    `lyrics_hint`: if provided, the FIRST ~200 tokens of this string are
    used as Whisper's `prompt` parameter — orienting the model's
    vocabulary toward the actual lyrics it should expect. This is the
    documented Whisper-API mechanism for biasing transcription
    (https://platform.openai.com/docs/guides/speech-to-text/prompting).
    Significantly reduces hallucination loops on tracks where Whisper
    otherwise drifts (e.g. confusing artist-name ad-libs for the lyric
    line). Only the last 224 tokens are read by Whisper and only the
    first ~30 s of audio benefits from it; on longer tracks the help is
    most impactful at the song's start where the model establishes its
    interpretation.

    Why whisper-1 and not gpt-4o-transcribe (better text quality):
        gpt-4o-transcribe and gpt-4o-mini-transcribe only return plain
        text — no segment timestamps. This pipeline renders lyrics
        synchronized to the audio, so segment-level start/end times are
        non-negotiable. whisper-1 (whisper-large-v2) is the only OpenAI
        transcription model that returns verbose_json with segment
        timestamps as of 2026-04.
    """
    from openai import OpenAI

    client = OpenAI()  # picks up OPENAI_API_KEY from env
    logger.info("[WHISPER-API] transcribing %s via OpenAI (whisper-1)", os.path.basename(mp3_path))

    # Build the initial prompt. When the caller has reference lyrics
    # (typically from lrclib plain), we ship the first ~200 tokens of
    # them so Whisper expects that vocabulary. Otherwise fall back to a
    # generic "song lyrics" hint.
    if lyrics_hint and lyrics_hint.strip():
        # ~200 tokens ≈ 800 chars for Spanish/English. Whisper truncates
        # silently if longer; this just keeps logs cleaner.
        prompt_text = lyrics_hint.strip()[:800]
        logger.info("[WHISPER-API] initial_prompt primed with %s chars from reference lyrics",
                    len(prompt_text))
    else:
        prompt_text = ("Letras de canción:" if (language or "").startswith("es")
                       else "Song lyrics:")

    granularities = ["word", "segment"] if return_words else ["segment"]
    kwargs = {
        "model": "whisper-1",
        "response_format": "verbose_json",
        "timestamp_granularities": granularities,
        "prompt": prompt_text,
        # temperature=0 gives the most confident output; we lower the
        # default 0.0 ladder so it doesn't sample alternative
        # interpretations on tricky words.
        "temperature": 0.0,
    }
    if language:
        kwargs["language"] = language

    # Whisper-API rejects > 25 MB. UMG uploads lossless WAV (often 30-50
    # MB for a 3-min track). Transcode-compress only when over the cap;
    # the compressed copy is just for the API call — original audio is
    # untouched and used by the rest of the render pipeline.
    api_path = _compress_for_whisper(mp3_path)
    cleanup_compressed = api_path != mp3_path
    # Provenance record for the cost dashboard. job_id is optional — paths
    # that call transcribe() without a job_id (one-off scripts, tests)
    # skip the recording, and the cost panel just under-reports those
    # outliers. The OpenAI call itself is the same either way.
    recorder = None
    if job_id:
        from provenance import record_ai_call
        recorder = record_ai_call(
            job_id=job_id,
            step="whisper_transcribe",
            tool_name="whisper-1",
            tool_provider="openai",
            prompt=prompt_text[:500],
            input_data_types=["audio_file"],
        )
    # Retry loop with exponential backoff + jitter for transient failures
    # (rate-limits, connection drops). Before this loop, a single 429 from
    # OpenAI bubbled straight to the user as 503 with no retry — the message
    # claimed "Reintentamos en unos segundos" but actually didn't.
    # Incident 2026-05-14: Agus + admin transcribiendo en paralelo →
    # cascade 503. Now: 5 attempts over ~30s before surrendering.
    from fastapi import HTTPException
    try:
        from openai import RateLimitError, APIConnectionError, APIError
    except ImportError:
        RateLimitError = APIConnectionError = APIError = ()

    import random
    import time as _time_retry
    _MAX_RETRIES = int(os.environ.get("WHISPER_MAX_RETRIES", "5"))
    response = None
    last_exc = None
    try:
        for attempt in range(_MAX_RETRIES):
            try:
                with open(api_path, "rb") as f:
                    kwargs["file"] = f
                    response = client.audio.transcriptions.create(**kwargs)
                if attempt > 0:
                    logger.info("[WHISPER-API] succeeded on attempt %s/%s", attempt + 1, _MAX_RETRIES)
                break
            except Exception as exc:
                last_exc = exc
                # OpenAI SDK raises RateLimitError for BOTH transient
                # rate-limits AND persistent quota exhaustion (insufficient_quota,
                # account billing issues). The two have different `code` strings
                # inside the error body — retrying insufficient_quota wastes
                # ~32s and gives the user a delayed error instead of an instant
                # actionable message. Incident 2026-05-14: balance hit $0,
                # users saw 30s+ retries that all failed. Detect & bail fast.
                if isinstance(exc, RateLimitError):
                    quota_keywords = (
                        "insufficient_quota",
                        "exceeded your current quota",
                        "billing",
                        "payment",
                    )
                    msg_lower = str(exc).lower()
                    is_quota_exhaustion = any(k in msg_lower for k in quota_keywords)
                    if is_quota_exhaustion:
                        logger.error(
                            "[WHISPER-API][INSUFFICIENT_QUOTA] OpenAI rejected with "
                            "quota/billing error: %s; NOT retrying. "
                            "Recarga creditos en https://platform.openai.com/settings/organization/billing",
                            exc,
                        )
                        # Bail immediately — no retry helps a quota issue.
                        break
                # Retryable transients: rate-limit + connection drops.
                if isinstance(exc, (RateLimitError, APIConnectionError)):
                    if attempt < _MAX_RETRIES - 1:
                        # 2^attempt + jitter: 1-2s, 2-3s, 4-5s, 8-9s, 16-17s
                        sleep_s = (2 ** attempt) + random.uniform(0, 1)
                        kind = "rate-limit" if isinstance(exc, RateLimitError) else "connection"
                        logger.warning(
                            "[WHISPER-API] transient %s error on attempt %s/%s: %s; "
                            "sleeping %.1fs then retrying",
                            kind, attempt + 1, _MAX_RETRIES, exc, sleep_s,
                        )
                        _time_retry.sleep(sleep_s)
                        continue
                    # Last attempt failed — fall through to raise below.
                # Non-retryable (APIError, OSError, etc.) — bail immediately.
                break
        else:
            # for/else: only reached if loop completed without break (shouldn't happen)
            pass

        if response is None:
            # Translate the final exception to HTTPException for the UI.
            if isinstance(last_exc, RateLimitError):
                quota_keywords = (
                    "insufficient_quota",
                    "exceeded your current quota",
                    "billing",
                    "payment",
                )
                msg_lower = str(last_exc).lower()
                if any(k in msg_lower for k in quota_keywords):
                    # Quota exhaustion — different message, NO Retry-After
                    # (the frontend retry would just hit the same wall).
                    # Status 503 stays so the frontend's error handling treats
                    # it as a server-side issue, but no auto-retry kicks in.
                    raise HTTPException(
                        status_code=503,
                        detail=(
                            "Servicio AI temporalmente sin cuota. "
                            "El administrador fue notificado."
                        ),
                    ) from last_exc
                raise HTTPException(
                    status_code=503,
                    detail=(
                        f"Servicio de transcripción saturado tras {_MAX_RETRIES} reintentos. "
                        "Reintentá en un minuto."
                    ),
                    headers={"Retry-After": "60"},
                ) from last_exc
            if isinstance(last_exc, APIConnectionError):
                raise HTTPException(
                    status_code=502,
                    detail="No pudimos contactar el servicio de transcripción. Reintentá en unos segundos.",
                ) from last_exc
            if isinstance(last_exc, APIError):
                raise HTTPException(
                    status_code=502,
                    detail=f"Servicio de transcripción no disponible: {last_exc!s}",
                ) from last_exc
            # Unknown exception type — re-raise the original.
            if last_exc is not None:
                raise last_exc
    finally:
        if cleanup_compressed:
            try:
                os.unlink(api_path)
            except OSError:
                pass

    if recorder is not None:
        # Mark the provenance row finished so the dashboard counts this
        # call and the reaper does not mistake it for an in-flight orphan.
        try:
            recorder.finish(response_summary=f"whisper_transcribe_ok")
        except Exception:
            pass

    raw_segments = response.segments or []
    raw_words = (getattr(response, "words", None) or []) if return_words else []
    import re as _re

    # Word granularity returns a flat top-level word list, not per-segment.
    # Walk both lists in parallel to bucket each word into the segment
    # whose [start, end) covers its start time. Cursor is monotonic so
    # this is O(W + S).
    word_cursor = 0

    def _words_for_segment(seg) -> list[dict]:
        nonlocal word_cursor
        if not raw_words:
            return []
        s_start = float(seg.start)
        s_end = float(seg.end)
        bucket: list[dict] = []
        while word_cursor < len(raw_words):
            w = raw_words[word_cursor]
            w_start = float(getattr(w, "start", 0.0))
            if w_start >= s_end:
                break
            word_cursor += 1
            if w_start < s_start:
                continue
            bucket.append({
                "word": (getattr(w, "word", "") or "").strip(),
                "start": w_start,
                "end": float(getattr(w, "end", w_start)),
            })
        return bucket

    segments: list[dict] = []
    for seg in raw_segments:
        text = (seg.text or "").strip()
        seg_words = _words_for_segment(seg) if return_words else []
        if not text or len(text) < 3:
            continue
        # Same filters as local path so behavior matches.
        if _re.search(r'[一-鿿぀-ゟ゠-ヿ가-힯]', text):
            logger.info("[WHISPER-API] Filtered non-latin: %s", text[:60])
            continue
        if any(spam in text.lower() for spam in _SPAM_PATTERNS):
            logger.info("[WHISPER-API] Filtered spam: %s", text[:60])
            continue
        # Only drop segments that Whisper is VERY sure aren't speech. The
        # previous 0.7 threshold was tossing legitimate lyric lines on
        # tracks with dense crowd noise / heavy mix (Karol G's "Yo Me
        # Caso Contigo" interlude, audience cheering on live cuts). The
        # operator can prune obvious non-lyrics in the editor; better to
        # surface borderline content than to silently drop it.
        if (seg.no_speech_prob or 0) > 0.92:
            logger.info("[WHISPER-API] Filtered very-low-confidence: %s", text[:60])
            continue
        out_seg = {
            "start": float(seg.start),
            "end": float(seg.end),
            "text": text,
        }
        if return_words:
            out_seg["words"] = seg_words
        segments.append(out_seg)

    # Whisper hallucinates loops in two distinct shapes:
    #   1. SAME LINE REPEATED across consecutive segments — easy: dedupe
    #      by exact match, keep first 2 (preserves legit chorus repeats).
    #   2. SAME PHRASE REPEATED INSIDE a single segment's text — happens
    #      on long sustained / instrumental passages where Whisper emits
    #      one large segment whose text is "X and X and X and X and …".
    #      Need to detect intra-segment repetition and truncate.
    #
    # Both cases observed in production on "El Plan de la Mariposa - El
    # Riesgo" (5/5/2026): segment 60-150s contained the line "que podía
    # reflexionar sobre lo que estaba haciendo y" repeated ~5 times within
    # the same segment.
    import re as _re_loops

    def _truncate_intra_loop(text: str) -> tuple[str, float]:
        """If text contains a phrase that repeats 3+ times consecutively,
        truncate to the first 2 occurrences. Phrase = 4–14 word window —
        the upper bound matters because Whisper hallucinations sometimes
        loop on a clause that's 8–12 words long.

        Returns (truncated_text, ratio_kept) so the caller can shrink the
        segment's end timestamp proportionally — without that adjustment,
        the truncated text would stay on screen during the instrumental
        passage Whisper was hallucinating over, giving a "stuck subtitle"
        feel. Ratio = 1.0 when nothing changes.
        """
        words = text.split()
        total = len(words)
        if total < 12:
            return text, 1.0
        for window in range(14, 3, -1):  # try longer windows first
            if total < window * 3:
                continue
            for start in range(total - window * 3 + 1):
                phrase = words[start:start + window]
                count = 1
                pos = start + window
                while pos + window <= total and words[pos:pos + window] == phrase:
                    count += 1
                    pos += window
                if count >= 3:
                    cut = start + window * 2
                    truncated = " ".join(words[:cut])
                    truncated = truncated.rstrip(",.;: ") + "…"
                    ratio = cut / total
                    return truncated, ratio
        return text, 1.0

    cleaned: list[dict] = []
    intra_truncated = 0
    for seg in segments:
        original = seg["text"]
        new_text, ratio = _truncate_intra_loop(original)
        if new_text != original:
            intra_truncated += 1
            duration = seg["end"] - seg["start"]
            seg = {
                **seg,
                "text": new_text,
                # Shrink end so the subtitle leaves the screen when the
                # legitimate spoken phrase ends, not when Whisper's
                # hallucination tail would have ended.
                "end": seg["start"] + duration * ratio,
            }
        cleaned.append(seg)
    if intra_truncated:
        logger.info("[WHISPER-API] Truncated intra-segment loops in %s segment(s)", intra_truncated)
    segments = cleaned

    # Collapse consecutive-identical-text segments, but only when the
    # streak looks like a Whisper hallucination loop — not a legitimate
    # repeated chorus. See _collapse_consecutive_duplicates' docstring
    # for the heuristic.
    segments, collapsed_groups, collapsed_total = (
        _collapse_consecutive_duplicates(segments, with_counts=True)
    )
    if collapsed_total:
        logger.info("[WHISPER-API] Merged %s consecutive duplicate segments into %s chant/loop spans",
                    collapsed_total, collapsed_groups)

    GAP = 0.05
    for i in range(len(segments) - 1):
        if segments[i]["end"] > segments[i + 1]["start"] - GAP:
            segments[i]["end"] = segments[i + 1]["start"] - GAP

    # Drop training-data-leak phrases AND single-word "oh oh oh"
    # outro loops BEFORE returning, so the caller's hallucination
    # detector sees a clean timeline. Without this, a 100-"oh"
    # outro would trip the "implausible mega-segment" detector and
    # the caller would discard the entire (otherwise good) Whisper
    # output, replacing it with reference lyrics distributed at
    # synthetic timestamps — losing the accurate timing of the
    # legitimate verses.
    segments, _dropped_loops = _filter_whisper_hallucinations(segments)
    if _dropped_loops:
        logger.info("[WHISPER-API] filtered %s hallucination/loop segment(s)", _dropped_loops)

    logger.info("[WHISPER-API] %s segments", len(segments))
    return segments


def transcribe(mp3_path: str, language: str = None,
               lyrics_hint: str | None = None,
               job_id: str | None = None,
               return_words: bool = False) -> list[dict]:
    """Transcribe an audio file to lyric segments.

    Backend selection:
        - If OPENAI_API_KEY is set, route to the OpenAI Whisper API. This is
          the production path: no local model, no OOM risk on 1-2 GB workers.
          Errors propagate — no silent fallback to the 1.5 GB local model
          that frequently OOMs on small instances.
        - If OPENAI_API_KEY is not set, fall back to the local Whisper-turbo
          model. Works for development on machines with enough RAM.

    `lyrics_hint`: optional reference text (e.g. lrclib plain lyrics) used
    as Whisper-API's `prompt` parameter to bias transcription toward the
    expected vocabulary. See _transcribe_via_openai_api for details. Local
    Whisper path ignores it (could be added later via `initial_prompt=`).
    """
    has_key = bool(os.environ.get("OPENAI_API_KEY", "").strip())
    logger.info("[transcribe] OPENAI_API_KEY=%s", 'set' if has_key else 'EMPTY')
    if has_key:
        return _transcribe_via_openai_api(
            mp3_path, language=language, lyrics_hint=lyrics_hint,
            job_id=job_id, return_words=return_words,
        )

    # --- local Whisper path ---
    audio_path = mp3_path

    model = _get_whisper_model("turbo")

    kwargs = dict(
        word_timestamps=True,
        initial_prompt="Lyrics:",
        condition_on_previous_text=False,
    )
    if language:
        kwargs["language"] = language
        logger.info("[WHISPER] Forced language: %s", language)

    result = model.transcribe(audio_path, **kwargs)

    import re as _re

    segments = []
    for seg in result["segments"]:
        text = seg["text"].strip()
        if not text or len(text) < 3:
            continue
        # Filter non-latin characters (Demucs artifacts like "Lil怎麼樣")
        if _re.search(r'[\u4e00-\u9fff\u3040-\u309f\u30a0-\u30ff\uac00-\ud7af]', text):
            logger.info("[WHISPER] Filtered non-latin artifact: %s", text[:60])
            continue
        # Filter spam/non-lyrics
        if any(spam in text.lower() for spam in _SPAM_PATTERNS):
            logger.info("[WHISPER] Filtered spam: %s", text[:60])
            continue
        # Filter high no_speech_prob segments (likely hallucinations)
        if seg.get("no_speech_prob", 0) > 0.7:
            logger.info("[WHISPER] Filtered low-confidence (no_speech=%.2f): %s",
                        seg['no_speech_prob'], text[:60])
            continue
        words = seg.get("words", [])
        if words:
            start = words[0]["start"]
            end = words[-1]["end"]
        else:
            start = seg["start"]
            end = seg["end"]
        out_seg = {"start": start, "end": end, "text": text}
        if return_words and words:
            out_seg["words"] = [
                {"word": (w.get("word") or "").strip(),
                 "start": float(w.get("start", start)),
                 "end": float(w.get("end", end))}
                for w in words if (w.get("word") or "").strip()
            ]
        segments.append(out_seg)

    # Safety net: retry if first segment starts very late
    if segments and segments[0]["start"] > 30:
        logger.warning("[WHISPER] WARNING: first seg at %.1fs, retrying", segments[0]['start'])
        kwargs2 = dict(kwargs, initial_prompt="Song lyrics transcription:", no_speech_threshold=0.4)
        result2 = model.transcribe(mp3_path, **kwargs2)
        segments2 = []
        for seg in result2["segments"]:
            text = seg["text"].strip()
            if not text or len(text) < 3:
                continue
            words = seg.get("words", [])
            if words:
                out_seg = {"start": words[0]["start"], "end": words[-1]["end"], "text": text}
            else:
                out_seg = {"start": seg["start"], "end": seg["end"], "text": text}
            if return_words and words:
                out_seg["words"] = [
                    {"word": (w.get("word") or "").strip(),
                     "start": float(w.get("start", out_seg["start"])),
                     "end": float(w.get("end", out_seg["end"]))}
                    for w in words if (w.get("word") or "").strip()
                ]
            segments2.append(out_seg)
        if segments2 and segments2[0]["start"] < segments[0]["start"]:
            segments = segments2

    # Quality fallback: if turbo produced a sparse/low-confidence result, retry
    # with large-v3 (slower but much more accurate, especially for noisy vocals
    # or heavy accents). Gated by WHISPER_FALLBACK_ENABLED to save RAM on small
    # machines that cannot hold both models at once.
    if os.environ.get("WHISPER_FALLBACK_ENABLED", "1") != "0":
        if len(segments) < 5:
            logger.info("[WHISPER] Only %s segments with turbo; falling back to large-v3",
                        len(segments))
            try:
                large = _get_whisper_model("large-v3")
                result3 = large.transcribe(audio_path, **kwargs)
                segments3 = []
                for seg in result3["segments"]:
                    text = seg["text"].strip()
                    if not text or len(text) < 3:
                        continue
                    if any(spam in text.lower() for spam in _SPAM_PATTERNS):
                        continue
                    words = seg.get("words", [])
                    if words:
                        out_seg = {"start": words[0]["start"],
                                   "end": words[-1]["end"], "text": text}
                    else:
                        out_seg = {"start": seg["start"],
                                   "end": seg["end"], "text": text}
                    if return_words and words:
                        out_seg["words"] = [
                            {"word": (w.get("word") or "").strip(),
                             "start": float(w.get("start", out_seg["start"])),
                             "end": float(w.get("end", out_seg["end"]))}
                            for w in words if (w.get("word") or "").strip()
                        ]
                    segments3.append(out_seg)
                if len(segments3) > len(segments):
                    logger.info("[WHISPER] large-v3 produced %s segments (turbo: %s); using large-v3",
                                len(segments3), len(segments))
                    segments = segments3
            except Exception as e:
                logger.warning("[WHISPER] large-v3 fallback failed: %s; keeping turbo", e)

    for i, seg in enumerate(segments[:5]):
        logger.info("[WHISPER] seg %s: %.2f-%.2f  %s", i, seg['start'], seg['end'], seg['text'][:60])

    GAP = 0.05
    for i in range(len(segments) - 1):
        if segments[i]["end"] > segments[i + 1]["start"] - GAP:
            segments[i]["end"] = segments[i + 1]["start"] - GAP

    # Same outro-loop filter as the API path — see notes there.
    segments, _dropped_loops = _filter_whisper_hallucinations(segments)
    if _dropped_loops:
        logger.info("[WHISPER] filtered %s hallucination/loop segment(s)", _dropped_loops)

    return segments


# ---------------------------------------------------------------------------
# Lyrics reference fetcher — used by /transcribe to show reference text in UI.
#
# The reference is fed to LyricsEditor.findSuggestion which fuzzy-matches each
# Whisper segment to a reference line and surfaces a one-click correction.
# Quality of suggestions is bounded by quality of the reference, so we lean
# on Gemini 2.5 Flash with the google_search grounding tool — Google's
# grounded LLM aggregates from public lyric sites with cleaner provenance
# than direct API integration with any single commercial lyrics provider
# (Genius prohibits commercial use without a license; UMG cannot ride that).
# ---------------------------------------------------------------------------

# Domains we *prefer* in grounding sources. Soft signal only — Vertex AI
# Search wraps grounding URIs in a redirect host (vertexaisearch.cloud
# .google.com/...) so the original target host is often hidden until the
# redirect is followed. We log it for observability but do NOT reject on
# absence; the lyrics-shape validation downstream handles hallucination.
_LYRIC_DOMAINS = {
    "genius.com", "azlyrics.com", "letras.com", "letras.mus.br",
    "lyrics.com", "musixmatch.com", "songlyrics.com", "metrolyrics.com",
    "lyricfind.com", "songmeanings.com",
}


def _truthy_env(val: str) -> bool:
    """Robust truthy parser — same shape as REQUIRE_REVIEW (commit 06a42e7)."""
    return (val or "").strip().strip('"').strip("'").lower() in (
        "1", "true", "yes", "on", "y", "t",
    )


def _lyrics_cache_key(artist: str, song: str) -> str:
    import hashlib
    return hashlib.sha1(
        f"{artist.lower().strip()}|{song.lower().strip()}".encode()
    ).hexdigest()[:16]


def _lrclib_cache_key(artist: str, song: str) -> str:
    """Stable namespaced key for lrclib results in the LyricsCache table.
    Same Postgres table the Gemini path uses; the `lrclib:` prefix keeps
    the two namespaces independent (Gemini stores plain text in `lyrics`,
    lrclib stores a JSON-encoded dict)."""
    import hashlib as _h
    h = _h.sha1(f"{artist.strip().lower()}|{song.strip().lower()}".encode())
    return f"lrclib:{h.hexdigest()[:16]}"


def _fetch_lrclib(artist: str, song: str, db=None,
                  audio_duration: float | None = None) -> dict | None:
    """Look up a song on lrclib.net's public API. Returns:
        {"plain": str|None, "synced": str|None, "duration": float|None}
    or None if the request failed or the song wasn't found.

    `audio_duration` (optional): when provided, the /search candidate picker
    prefers the lrclib record whose duration matches the uploaded audio,
    so a multi-version title (radio edit / extended / cover) doesn't grab
    the wrong-length lyrics. Default None → duration-agnostic (back-compat).

    lrclib.net is an open, free, no-auth lyrics database (similar shape to
    MusicBrainz for lyrics). Public API, no anti-bot, generous rate limits.
    Crucially: covers Latin / reggaeton / pop catalogues that Gemini-grounded
    search refuses to answer for due to RECITATION blocking on UMG-owned
    songs (Karol G, Bad Bunny, J Balvin, etc.). It also frequently has
    *synced* lyrics with line-level timestamps — when present, those let us
    skip Whisper transcription entirely and avoid hallucination loops.

    `db` (optional): a SQLAlchemy session. When provided, the function
    consults the LyricsCache table first and writes successful fetches
    back so that future calls don't depend on lrclib.net's uptime. This
    is what saves us when Railway's outbound to lrclib gets a transient
    timeout — once we've fetched a song once, we never re-fetch it.

    Best-effort — never raises.
    """
    if not artist or not song:
        return None
    import requests as _req
    import json as _json

    cache_key = _lrclib_cache_key(artist, song)

    # Cache lookup. Skip entirely on db=None (e.g. the smoke scripts).
    if db is not None:
        try:
            from database import LyricsCache
            row = db.query(LyricsCache).filter(
                LyricsCache.cache_key == cache_key
            ).first()
            if row and row.lyrics:
                cached = _json.loads(row.lyrics)
                if cached.get("plain") or cached.get("synced"):
                    logger.info("[LYRICS] lrclib cache hit %s (%s plain chars, synced=%s)",
                                cache_key,
                                len((cached.get('plain') or '')),
                                'yes' if cached.get('synced') else 'no')
                    return cached
        except Exception as e:
            logger.error("[LYRICS] lrclib cache read failed: %s", e)
    # Two attempts: lrclib reads can spike >10s under load. Total budget
    # is ~25s in the worst case, well within the user-perceived bound
    # for /transcribe (Whisper is the long pole anyway).
    last_err: Exception | None = None
    r = None
    for attempt in range(2):
        try:
            r = _req.get(
                "https://lrclib.net/api/get",
                params={"artist_name": artist, "track_name": song},
                timeout=20,
                headers={"User-Agent": "GenLyAI/1.0 (+https://app.genly.pro)"},
            )
            break
        except Exception as e:  # transient network / timeout
            last_err = e
            if attempt == 0:
                logger.warning("[LYRICS] lrclib attempt 1 failed (%s: %s); retrying once",
                               e.__class__.__name__, str(e)[:80])
                continue
            logger.error("[LYRICS] lrclib fetch failed after retry: %s", e)
            return None
    if r is None:
        result = None
    elif r.status_code != 200:
        logger.warning("[LYRICS] lrclib /get %s for %r - %r", r.status_code, artist, song)
        result = None
    else:
        try:
            result = _parse_lrclib_record(r.json())
        except Exception as e:
            logger.error("[LYRICS] lrclib /get parse failed: %s", e)
            result = None

    # Fallback: si /api/get no devolvió un record útil (404, transient,
    # null fields), intentar /api/search. Es fuzzy: busca con keywords
    # combinados y devuelve hasta N candidates. Pickeamos el que mejor
    # matchee artist+song con preferencia para syncedLyrics. Caso real
    # motivador: Noches Sin Sueño (Rata Blanca) — /api/get devolvió 404
    # transient en staging, /api/search habría devuelto 4 candidates
    # válidos con synced perfecto, evitando el bug del Gemini fallback.
    if result is None:
        candidates = _try_lrclib_search(artist, song)
        if candidates:
            best = _pick_best_lrclib_candidate(candidates, artist, song, audio_duration)
            if best is not None:
                logger.info("[LYRICS] lrclib /get failed but /search rescued candidate id=%s "
                            "(%r - %r, synced=%s)",
                            best.get('id'), best.get('artistName'), best.get('trackName'),
                            'yes' if best.get('syncedLyrics') else 'no')
                try:
                    result = _parse_lrclib_record(best)
                except Exception as e:
                    logger.error("[LYRICS] lrclib /search parse failed: %s", e)
                    result = None

    # ─── Coverage boost (opt-in via LRCLIB_COVERAGE_BOOST=1) ─────────
    # Dos mejoras opt-in para subir el hit-rate global de lrclib:
    #
    # 1. Upgrade plain→synced: si /get devolvió un record con plain pero
    #    sin synced, otro upload del mismo song puede tener synced.
    #    Pickeamos el mejor synced candidate de /search y reemplazamos.
    #    Motivo: lrclib es contributor-driven; mismo song aparece varias
    #    veces con cobertura distinta.
    #
    # 2. Variant-retry: si todo lo de arriba falló (result is None),
    #    probamos /search con variaciones del query (accent-fold,
    #    strip parens/Live/Remix, primer-token del artist). El picker
    #    sigue scoreando contra el (artist, song) original, así un mal
    #    variant no nos hace pickear un match débil.
    if _lrclib_coverage_enabled():
        # (1) Upgrade plain→synced
        if result is not None and not result.get("synced"):
            candidates = _try_lrclib_search(artist, song)
            synced_candidates = [c for c in (candidates or []) if c.get("syncedLyrics")]
            if synced_candidates:
                upgrade = _pick_best_lrclib_candidate(synced_candidates, artist, song, audio_duration)
                if upgrade is not None:
                    try:
                        upgraded = _parse_lrclib_record(upgrade)
                    except Exception as e:
                        logger.error("[LYRICS] lrclib synced-upgrade parse failed: %s", e)
                        upgraded = None
                    if upgraded and upgraded.get("synced"):
                        logger.info(
                            "[LYRICS] lrclib /get returned plain-only; "
                            "/search upgraded to synced (id=%s)",
                            upgrade.get("id"))
                        result = upgraded

        # (2) Variant-retry cuando todo lo anterior dio None
        if result is None:
            for v_artist, v_song in _lrclib_query_variants(artist, song):
                v_candidates = _try_lrclib_search(v_artist, v_song)
                if not v_candidates:
                    continue
                # Score against ORIGINAL artist/song para evitar
                # aceptar matches débiles que la variante haya inflado
                best_v = _pick_best_lrclib_candidate(v_candidates, artist, song, audio_duration)
                if best_v is None:
                    continue
                try:
                    parsed_v = _parse_lrclib_record(best_v)
                except Exception as e:
                    logger.error("[LYRICS] lrclib variant /search parse failed: %s", e)
                    continue
                if parsed_v:
                    logger.info(
                        "[LYRICS] lrclib variant-retry hit (%r,%r) → id=%s synced=%s",
                        v_artist, v_song, best_v.get("id"),
                        "yes" if best_v.get("syncedLyrics") else "no")
                    result = parsed_v
                    break

    if result is None:
        return None

    # Write-through cache. Once stored, this song never depends on
    # lrclib.net uptime again — important for Railway outbound flakes.
    if db is not None:
        try:
            from database import LyricsCache
            payload = _json.dumps(result, ensure_ascii=False)
            row = db.query(LyricsCache).filter(
                LyricsCache.cache_key == cache_key
            ).first()
            if row:
                row.lyrics = payload
            else:
                db.add(LyricsCache(
                    cache_key=cache_key,
                    artist=artist,
                    title=song,
                    lyrics=payload,
                    fetched_by_model="lrclib",
                ))
            db.commit()
            logger.info("[LYRICS] lrclib cached %s (%s bytes)", cache_key, len(payload))
        except Exception as e:
            logger.error("[LYRICS] lrclib cache write failed: %s", e)
    return result


def _parse_lrclib_record(data: dict) -> dict | None:
    """Parsea un dict crudo de lrclib (de /api/get o de un item de
    /api/search) al shape `{plain, synced, duration}` que usa el rest
    del pipeline. Devuelve None si el record no tiene ni plain ni synced.

    Extraído del cuerpo de `_fetch_lrclib` para que el fallback a
    /api/search pueda reusar la misma lógica (incluido el derive de
    plain desde synced cuando lrclib solo expone synced).
    """
    plain = (data.get("plainLyrics") or "").strip() or None
    synced = (data.get("syncedLyrics") or "").strip() or None
    if not plain and not synced:
        return None
    # Some lrclib records expose only `syncedLyrics` (different bots
    # populate the two columns independently). The downstream auto-
    # recover code in /transcribe gates on `if plain:` so when plain
    # is missing the recovery branch is unreachable. Derive plain from
    # synced by stripping the `[mm:ss.xx]` timestamps so the recovery
    # path always has a usable reference.
    if not plain and synced:
        import re as _re
        ts_re = _re.compile(r"^\s*(?:\[\d+:\d+(?:[.:]\d+)?\]\s*)+")
        derived: list[str] = []
        for line in synced.splitlines():
            stripped = ts_re.sub("", line).strip()
            if stripped:
                derived.append(stripped)
        if derived:
            plain = "\n".join(derived)
            logger.info("[LYRICS] lrclib derived plain from synced (%s chars, %s lines)",
                        len(plain), len(derived))
    return {
        "plain": plain,
        "synced": synced,
        "duration": data.get("duration"),
    }


def _strip_accents(s: str) -> str:
    """Quitar diacríticos (NFKD + filter combining). Para matching más
    laxo entre 'Babasónicos' y 'Babasonicos', 'Mil Horas' y 'mil horas',
    etc. No toca la ñ (es una letra, no un diacrítico)."""
    import unicodedata as _u
    if not s:
        return s
    out = []
    for c in _u.normalize("NFKD", s):
        if _u.combining(c):
            continue
        out.append(c)
    return "".join(out)


def _strip_song_noise(title: str) -> str:
    """Quitar paréntesis, corchetes, 'feat. X', '(Live)', '(Remix)',
    '- Remastered 2009', etc. del título de la canción para mejorar el
    match contra registros base de lrclib.

    Ejemplos:
      'Aunque a nadie ya le importe (Remix)' → 'Aunque a nadie ya le importe'
      'Un Pacto Live In Buenos Aires 2001'   → 'Un Pacto'
      'Despacito (feat. Daddy Yankee)'       → 'Despacito'
      'Bohemian Rhapsody - Remastered 2011'  → 'Bohemian Rhapsody'
    """
    import re as _re
    if not title:
        return title
    s = title
    # paréntesis y corchetes con contenido
    s = _re.sub(r"\s*[\(\[][^)\]]*[\)\]]\s*", " ", s)
    # ' - <suffix>' al final (remastered, live, etc.)
    s = _re.sub(r"\s+-\s+.+$", "", s)
    # ' feat. X' / 'ft. X' / 'with X'
    s = _re.sub(r"\s+(?:feat\.?|ft\.?|with)\s+.+$", "", s, flags=_re.I)
    # palabras de variante al final sin guión: live, remix, acoustic, demo, edit, version, mix
    s = _re.sub(r"\s+(?:live|remix|acoustic|demo|edit|version|mix|remastered|en\s+vivo)\b.*$", "", s, flags=_re.I)
    # años sueltos al final (e.g. "Pacto 2001")
    s = _re.sub(r"\s+(?:19|20)\d{2}\s*$", "", s)
    # whitespace dedupe
    s = _re.sub(r"\s{2,}", " ", s).strip()
    return s or title  # nunca devolver string vacío


def _lrclib_coverage_enabled() -> bool:
    """Multi-artist split + variant-retry + diacritic-fold for lrclib search.

    Default ON since 2026-06-01. Incident (job c57c32846d75): the cumbia
    cover "Luz De Dia" credited to artist "Coti, Angela Leiva, La K´onga"
    shipped raw whisperX (timing_source=whisperx, scattered timing, missed
    outro) because lrclib was queried with the FULL multi-artist string and
    found nothing — even though lrclib has the original "Coti - Luz de Día"
    (169 s, synced) that a per-artist query hits exactly.

    Kept behind an env var as a kill-switch: set LRCLIB_COVERAGE_BOOST=0
    (or false/no/off) to revert to the old exact-match-only behavior
    without a redeploy.
    """
    import os as _os
    return _os.environ.get("LRCLIB_COVERAGE_BOOST", "1").strip().lower() not in (
        "0", "false", "no", "off",
    )


def _split_multi_artist(artist: str) -> list[str]:
    """Split a multi-artist credit into individual artists.

    "Coti, Angela Leiva, La K´onga" → ["Coti", "Angela Leiva", "La K´onga"].
    lrclib indexes most songs under a single primary artist, so collab /
    cover credits never match the full string. Splits on commas,
    ampersands, slashes and feat./ft./featuring (NOT bare "x" — too prone
    to false splits inside real names). Returns [] when there's only one
    artist (nothing to split)."""
    import re as _re
    parts = [
        a.strip() for a in _re.split(
            r"\s*(?:,|&|/|\bfeat\b\.?|\bft\b\.?|\bfeaturing\b)\s*",
            (artist or "").strip(),
            flags=_re.IGNORECASE,
        )
        if a.strip()
    ]
    return parts if len(parts) > 1 else []


def _lrclib_query_variants(artist: str, song: str):
    """Yield (artist, song) variations to try cuando el exact-match falla.
    Orden: más-probable-a-mejor primero. Dedupe contra (artist, song)
    original y entre variantes."""
    seen = set()
    base_artist = (artist or "").strip()
    base_song = (song or "").strip()

    def _emit(a: str, s: str):
        if not a or not s:
            return None
        key = (a.strip().lower(), s.strip().lower())
        if key in seen:
            return None
        seen.add(key)
        return (a.strip(), s.strip())

    # Variant 1: song con noise stripped
    clean_song = _strip_song_noise(base_song)
    v = _emit(base_artist, clean_song)
    if v: yield v

    # Variant 2: accent-fold both (común: lrclib normaliza unicode)
    v = _emit(_strip_accents(base_artist), _strip_accents(clean_song))
    if v: yield v

    # Variant 3: artist como primer token (e.g. "Bersuit" en lugar de "Bersuit Vergarabat"),
    # cuando el artist tiene más de 1 palabra
    parts = base_artist.split()
    if len(parts) > 1:
        v = _emit(parts[0], clean_song)
        if v: yield v
        # también con accent-fold
        v = _emit(_strip_accents(parts[0]), _strip_accents(clean_song))
        if v: yield v

    # Variant 4: multi-artist credits ("Coti, Angela Leiva, La K´onga" —
    # cumbia / collab covers). Try each individual artist (+ accent-fold)
    # so the original single-artist record on lrclib is found. The picker
    # still scores against the ORIGINAL (artist, song), so a per-artist
    # query that surfaces an unrelated song is rejected. (Incident
    # 2026-06-01, job c57c32846d75.)
    for ind in _split_multi_artist(base_artist):
        v = _emit(ind, clean_song)
        if v: yield v
        v = _emit(_strip_accents(ind), _strip_accents(clean_song))
        if v: yield v


def _try_lrclib_search(artist: str, song: str) -> list:
    """GET /api/search?q=<artist> <song>. Endpoint fuzzy de lrclib.net
    que devuelve hasta N candidates (cada uno con el mismo shape que
    /api/get: id, trackName, artistName, plainLyrics, syncedLyrics,
    duration, instrumental).

    Best-effort: cualquier error (network, parsing, status != 200)
    devuelve [] sin raise. El caller decide qué hacer si no hay
    resultados (típicamente: caer al Gemini fallback original).
    """
    if not artist or not song:
        return []
    import requests as _req
    q = f"{artist} {song}".strip()
    # lrclib.net read-timeouts / connection hiccups son transitorios y
    # frecuentes. El retry sólo cubre EXCEPCIONES de red (ReadTimeout/
    # ConnectionError); un 5xx devuelve [] sin reintento (no suele recuperarse
    # en 1.5s y caemos a Gemini igual). Un retry con backoff corto absorbe la
    # mayoría de los timeouts sin colgar el pipeline. lrclib es best-effort
    # (caemos a Gemini si falla), así que el log es WARNING — no ERROR — para
    # no inundar Sentry con ruido manejado.
    last_err = None
    for _attempt in range(2):
        try:
            r = _req.get(
                "https://lrclib.net/api/search",
                params={"q": q},
                timeout=8.0,
                headers={"User-Agent": "GenLyAI/1.0 (+https://app.genly.pro)"},
            )
            if r.status_code != 200:
                logger.warning("[LYRICS] lrclib /search %s for q=%r", r.status_code, q)
                return []
            data = r.json()
            if not isinstance(data, list):
                return []
            return data
        except Exception as e:
            last_err = e
            if _attempt == 0:
                import time as _t
                _t.sleep(1.5)
    logger.warning("[LYRICS] lrclib /search failed (best-effort, falling back): %s", last_err)
    return []


def _pick_best_lrclib_candidate(candidates: list, artist: str,
                                 song: str,
                                 audio_duration: float | None = None) -> dict | None:
    """Scorea cada candidate de /api/search contra el (artist, song)
    pedido. Devuelve el de mayor score si supera el threshold 0.5,
    sino None.

    Scoring:
      - Artist match exacto: +0.5; substring: +0.3; else 0.
      - Song match exacto: +0.3; substring: +0.2; else 0.
      - Bonus +0.2 si el candidate tiene syncedLyrics (preferimos
        synced sobre plain para output con timestamps exactos).
      - Duration guard (cuando `audio_duration` está disponible): +0.25 si
        la duración del candidate matchea el audio (±3 s), +0.05 si está
        cerca (±30 s), −0.35 si difiere mucho. lrclib suele tener varios
        uploads del mismo título a distinta duración (radio edit /
        extendido / el original vs un cover); sin esto el picker elegiría
        por orden de resultados y podría agarrar el "Luz de Día" de 261 s
        para una cumbia de 169 s. Es preferencia, no rechazo duro: un
        match de otra duración igual pasa el threshold si es lo único que
        hay (mejor tener el texto correcto — reconcile usa los wordstamps
        propios, no los timestamps de lrclib).
      - Threshold 0.5: requiere mínimo artist+song match O synced+song
        match razonable. Evita aceptar matches débiles que generarían
        output peor que el Gemini fallback existente.

    `audio_duration` es opcional y default None → scoring idéntico al
    original (back-compat con callers/tests que no lo pasan).
    """
    if not candidates:
        return None

    # Diacritic-fold así "Babasonicos" matchea "Babasónicos" exactamente y
    # no sólo por substring. Default ON (kill-switch LRCLIB_COVERAGE_BOOST=0).
    _fold = _lrclib_coverage_enabled()

    def _norm(s: str) -> str:
        base = (s or "").lower().strip()
        return _strip_accents(base) if _fold else base

    artist_n = _norm(artist)
    song_n = _norm(song)
    if not artist_n or not song_n:
        return None

    best = None
    best_score = 0.0
    for c in candidates:
        if not isinstance(c, dict):
            continue
        c_artist = _norm(c.get("artistName"))
        c_song = _norm(c.get("trackName"))
        a_score = (
            0.5 if c_artist == artist_n
            else 0.3 if artist_n and (artist_n in c_artist or c_artist in artist_n)
            else 0.0
        )
        s_score = (
            0.3 if c_song == song_n
            else 0.2 if song_n and (song_n in c_song or c_song in song_n)
            else 0.0
        )
        sync_score = 0.2 if c.get("syncedLyrics") else 0.0
        dur_score = 0.0
        if audio_duration and c.get("duration"):
            try:
                diff = abs(float(c["duration"]) - float(audio_duration))
                dur_score = 0.25 if diff <= 3.0 else 0.05 if diff <= 30.0 else -0.35
            except (TypeError, ValueError):
                dur_score = 0.0
        score = a_score + s_score + sync_score + dur_score
        if score > best_score:
            best_score = score
            best = c
    if best_score >= 0.5:
        return best
    return None


def _fetch_lrclib_with_swap_retry(artist: str, song: str, db=None,
                                  audio_duration: float | None = None) -> tuple:
    """`_fetch_lrclib` con retry en orden invertido cuando el directo falla.

    Devuelve `(result, meta)` donde `meta = {swapped: bool, artist_used,
    song_used}`. El caller usa `meta["swapped"]` para persistir el orden
    corregido en `job.artist`/`job.song_title` y que el editor muestre
    metadata limpia al usuario.

    Origin (incidente 2026-05-24): un upload llegó con `artist='Legalícenla',
    title='Viejas Locas'` (los campos invertidos por el parser del frontend
    que asume convención `Title_Artist` para archivos con underscore — la
    mayoría de los usuarios nombran `Artist_Title`). lrclib falló silen-
    ciosamente, el pipeline cayó a whisperX puro sin texto de referencia,
    y el output fue ASR degradado con líneas partidas a mitad de frase y
    palabras inventadas ("se cacachó", "hierbar", "Le realicen la..."
    en lugar del estribillo "Legalícenla").

    Costo: una sola llamada extra a lrclib, gated en que el directo
    devolvió None. Despreciable contra la calidad recuperada cuando
    la metadata viene invertida (caso real, no hipotético).

    Defensa: si artist o song están vacíos, o son textualmente iguales,
    no intenta swap (evita ruido en logs).
    """
    result = _fetch_lrclib(artist, song, db, audio_duration)
    meta = {"swapped": False, "artist_used": artist, "song_used": song}
    if result is not None:
        return result, meta
    if not artist or not song:
        return None, meta
    if artist.strip().lower() == song.strip().lower():
        return None, meta
    logger.info("[LYRICS] lrclib miss for (%r,%r) — trying swapped order (%r,%r)",
                artist, song, song, artist)
    swap_result = _fetch_lrclib(song, artist, db, audio_duration)
    if swap_result is None:
        return None, meta
    logger.warning(
        "[LYRICS] lrclib hit on SWAPPED order — direct (%r,%r) missed, swap (%r,%r) "
        "succeeded. Upload metadata was likely inverted; auto-corrected for this job.",
        artist, song, song, artist)
    return swap_result, {"swapped": True, "artist_used": song, "song_used": artist}


_LRC_LINE = None  # lazy-compiled regex


def _lrc_to_segments(lrc: str, audio_duration: float | None = None,
                     time_offset: float = 0.0) -> list[dict]:
    """Parse LRC-format synced lyrics into Whisper-shape segments.

    LRC line format: ``[mm:ss.xx] Text``. Empty-text lines (e.g. ``[00:06.00]``
    in the Karol G example) are gap markers that bound the previous segment
    but don't produce a segment of their own. Each emitted segment's `end`
    is set to the next line's `start` minus a tiny gap (50 ms), so subtitles
    leave the screen exactly when the next line should appear. Tail segment
    ends at audio_duration when known, otherwise +8 s after its start.

    `time_offset` shifts ALL timestamps by the given seconds — used when the
    user uploads a version of the song with extra audio at the start (e.g.
    "Official Video" with a dialogue intro that the studio LRC doesn't
    account for). The caller computes the offset by comparing user audio
    duration against lrclib's reported duration.
    """
    import re as _re
    global _LRC_LINE
    if _LRC_LINE is None:
        _LRC_LINE = _re.compile(r"^\s*\[(\d+):(\d+)(?:[.:](\d+))?\]\s*(.*)$")

    raw: list[dict] = []
    for line in (lrc or "").splitlines():
        m = _LRC_LINE.match(line)
        if not m:
            continue
        mm, ss, frac, text = m.group(1), m.group(2), m.group(3), m.group(4)
        try:
            start = int(mm) * 60 + int(ss)
            if frac:
                start += int(frac) / (10 ** len(frac))
        except ValueError:
            continue
        raw.append({"start": float(start), "text": (text or "").strip()})
    if not raw:
        return []
    raw.sort(key=lambda r: r["start"])

    segments: list[dict] = []
    n = len(raw)
    GAP = 0.05
    for i, item in enumerate(raw):
        if not item["text"]:
            continue  # gap marker — used only to bound the previous line
        # Find the next entry with a strictly greater start to set our end.
        j = i + 1
        while j < n and raw[j]["start"] <= item["start"]:
            j += 1
        if j < n:
            end = raw[j]["start"] - GAP
        elif audio_duration:
            end = min(float(audio_duration), item["start"] + 8.0)
        else:
            end = item["start"] + 5.0
        # Defensive: keep at least 0.5 s on screen
        if end < item["start"] + 0.5:
            end = item["start"] + 0.5
        segments.append({
            "start": item["start"] + time_offset,
            "end": end + time_offset,
            "text": item["text"],
        })
    return segments


def _detect_hallucination(segments: list[dict],
                           audio_duration: float | None,
                           language: str | None = None) -> tuple[bool, str]:
    """Whisper hallucination smoke test. Returns (is_hallucinated, reason).

    Three independent signals trigger; ANY one is enough:
      - segment count is implausibly low for the audio duration,
      - any single segment is both very long (>15 s) and very wordy
        (>40 words) — the classic instrumental-passage trap,
      - any segment shows 3+ near-duplicate phrase windows by token-set
        Jaccard ≥ 0.75 — synonym loops ("reflexionar" ↔ "pensar") that
        the exact-match `_truncate_intra_loop` lets through.

    The detector is the GATE for the auto-recover branch: when this fires
    AND the caller has lrclib plain lyrics, we replace Whisper's output
    with synthesized segments (see `_synthesize_segments_from_plain`).

    `language` is the ISO code the caller passed to Whisper (or None when
    auto-detected). Spanish songs typically pack lines with longer pauses
    between (instrumental fills, sustained vocals) and produce 6-7
    segments per minute legitimately; English at 8/min was an empirically
    valid floor for an English-tuned threshold and is too strict for
    Spanish — audited 2026-05-15 on Noches Sin Sueño (Rata Blanca):
    Whisper with language='es' returned 23 segments in 371s of audio
    (= 3.7/min), legitimately good text + timing, but the old floor of
    max(8, 4*minutes) = max(8, 24) = 24 flagged it as hallucinated and
    pushed it into the synthesizer path with bad timestamps.
    """
    if not segments:
        return True, "empty segment list"

    # Signal 1 — segment count vs audio duration.
    if audio_duration and audio_duration > 30:
        minutes = audio_duration / 60.0
        is_spanish = (language or "").lower().startswith("es")
        if is_spanish:
            # Empirically validated 2026-05-15 — Spanish songs naturally
            # sit at 3-4 segs/min, not 4+.
            floor = max(6, int(minutes * 3.5))
        else:
            floor = max(8, int(minutes * 4))
        if len(segments) < floor:
            return True, (f"low count: {len(segments)} segments for "
                          f"{audio_duration:.0f}s audio (floor={floor}, "
                          f"lang={language or 'auto'})")

    # Signal 2 — instrumental-passage mega-segment.
    for s in segments:
        dur = float(s.get("end", 0)) - float(s.get("start", 0))
        words = len((s.get("text") or "").split())
        if dur > 15.0 and words > 40:
            return True, (f"implausible segment: {dur:.1f}s × {words} "
                          f"words — text={(s.get('text') or '')[:60]!r}")
        # Signal 2b — sparse-and-long mega-segment. Whisper sometimes maps
        # an entire song to ONE tiny phrase held for minutes (incident "El
        # Arbol": a single 346s segment reading "Música de presentación")
        # instead of transcribing. Signal 1 is OFF when we're called
        # per-segment (audio_duration=None, as _fill_gaps_with_reference
        # does), and Signal 2 needs >40 words, so a 3-word/346s segment slips
        # through and gets kept — discarding the reference lyrics we already
        # have. Real speech, even slow sung lines, runs ~0.5 words/s; a
        # density below 0.1 w/s over a long span is acoustically impossible
        # and means Whisper bailed. Flagging it routes the caller into the
        # reference-lyrics recovery path (_synthesize_segments_from_plain).
        if dur > 30.0 and words / dur < 0.1:
            return True, (f"sparse mega-segment: {dur:.1f}s × {words} "
                          f"words ({words / dur:.3f} w/s) — "
                          f"text={(s.get('text') or '')[:60]!r}")

    # Signal 3 — fuzzy intra-loop (token-set Jaccard ≥ 0.75).
    for s in segments:
        if _has_fuzzy_intra_loop(s.get("text") or ""):
            return True, ("fuzzy intra-loop in segment "
                          f"{(s.get('text') or '')[:60]!r}")

    # Signal 4 — segment whose first and second halves carry the same
    # content. Catches the Whisper failure mode where the model emits
    # the SAME phrase exactly twice in one segment
    # ("¿Qué podía reflexionar sobre lo que estaba haciendo? ¿Qué
    # podía reflexionar sobre lo que estaba haciendo?") — only 2
    # repetitions so the fuzzy-loop check (3+) doesn't catch it.
    # Variety guards (each half needs >= 4 unique normalised tokens)
    # keep simple repetitive choruses like "la la la la" from being
    # false-flagged.
    for s in segments:
        text = s.get("text") or ""
        words = [n for n in (_normalize_token(w) for w in text.split()) if n]
        n = len(words)
        if n < 12:
            continue
        half = n // 2
        first_half = set(words[:half])
        second_half = set(words[half:half * 2])
        if len(first_half) < 4 or len(second_half) < 4:
            continue
        inter = len(first_half & second_half)
        union = len(first_half | second_half)
        if union > 0 and (inter / union) >= 0.85:
            dur = float(s.get("end", 0)) - float(s.get("start", 0))
            return True, (f"duplicate halves in {dur:.1f}s segment "
                          f"({n} words): {text[:60]!r}")

    return False, ""


def _normalize_token(s: str) -> str:
    """Lowercase + strip combining diacritics + drop non-alphanumeric.
    Without this, "haciendo," and "haciendo" or "podía" and "podia"
    register as distinct tokens, breaking the Jaccard fuzzy-loop check
    on real Whisper output (which carries Spanish accents and clause
    punctuation). The normalisation matches the behaviour Whisper users
    intuitively expect when reasoning about repetition."""
    import unicodedata as _u
    s = (s or "").lower()
    s = _u.normalize("NFD", s)
    return "".join(c for c in s if c.isalnum() and not _u.combining(c))


def _has_fuzzy_intra_loop(text: str) -> bool:
    """Detect 3+ near-duplicate consecutive word-windows in a segment.
    Two windows count as the same loop when their token-set Jaccard is
    ≥ 0.75 — catches synonym swaps ("reflexionar" ↔ "pensar") that the
    exact-equality intra-loop truncator misses.

    Window sizes 4..14 (longer first), same shape as the existing
    `_truncate_intra_loop`, but only used as a SIGNAL here, not a fix.

    Tokens are normalised (lowercase + accent fold + punctuation strip)
    before comparison. Earlier versions used `.lower()` only and missed
    real-world Whisper hallucinations like "que podía reflexionar sobre
    lo que estaba haciendo, que podía pensar sobre lo que estaba
    haciendo …" because "haciendo," and "haciendo" tokenised
    differently.
    """
    raw = text.split()
    words = [n for n in (_normalize_token(w) for w in raw) if n]
    total = len(words)
    if total < 12:
        return False
    for window in range(14, 3, -1):
        if total < window * 3:
            continue
        for start in range(total - window * 3 + 1):
            phrase_set = set(words[start:start + window])
            if not phrase_set:
                continue
            count = 1
            pos = start + window
            while pos + window <= total:
                next_set = set(words[pos:pos + window])
                if not next_set:
                    break
                inter = len(phrase_set & next_set)
                union = len(phrase_set | next_set)
                if union == 0 or (inter / union) < 0.75:
                    break
                count += 1
                pos += window
            if count >= 3:
                return True
    return False


# Section markers we strip when distributing lrclib plain lyrics — they're
# scaffolding metadata, not lines a singer actually performs.
_PLAIN_SECTION_MARKER = re.compile(
    r"^\s*[\[(](?:verso|verse|coro|chorus|estribillo|puente|bridge|"
    r"intro|outro|pre[- ]?coro|pre[- ]?chorus|interlude|instrumental|"
    r"refr[áa]n|solo)[^\]\)]*[\])]\s*$",
    re.IGNORECASE,
)


def _split_plain_lines(plain: str) -> list[str]:
    """Split lrclib plain text into singable lines.
    Drops empties + section markers ([Verso], [Chorus], etc.) so the
    output is exactly the lines a vocalist actually performs.
    """
    if not plain:
        return []
    out: list[str] = []
    for raw in plain.splitlines():
        stripped = raw.strip()
        if not stripped:
            continue
        if _PLAIN_SECTION_MARKER.match(stripped):
            continue
        out.append(stripped)
    return out


def _align_whisper_to_plain(segments: list[dict],
                             plain: str) -> list[tuple[int, float]]:
    """Find time anchors by fuzzy-matching Whisper's surviving segments
    against lrclib plain lyric lines.

    Even when Whisper hallucinates the back half of a song, the FIRST
    segments are usually correct — they anchor onto real audio cues.
    We can use those segments as time landmarks: "Whisper heard this
    text at 0.2s; that text matches plain line 0; therefore line 0
    starts at 0.2s." With multiple anchors, the synthesizer interpolates
    the rest of the lyric lines piecewise instead of distributing them
    uniformly across the full duration. Result: timestamps land much
    closer to the actual singing without any operator effort.

    Returns sorted list of (line_index, time_seconds) tuples. Each
    anchor satisfies:
      - the segment passes the per-segment hallucination signals
        (no mega-segment, no fuzzy intra-loop),
      - it fuzzy-matches a plain line with token-set Jaccard ≥ 0.3,
      - and its line index is strictly greater than every prior anchor's
        (a later-in-time anchor that matches an EARLIER lyric line is
        almost certainly a wrong match — we drop it rather than confuse
        the interpolation).

    Empty list when no segment qualifies — caller falls back to uniform
    distribution from 0.
    """
    plain_lines = _split_plain_lines(plain)
    if not segments or not plain_lines:
        return []

    # Use the same normalisation as _has_fuzzy_intra_loop: lowercase +
    # accent fold + punctuation strip. Otherwise "podía" / "podia" or
    # "haciendo," / "haciendo" register as distinct tokens and the
    # Jaccard match score collapses below the 0.3 threshold.
    plain_token_sets = [
        {n for n in (_normalize_token(w) for w in line.split()) if n}
        for line in plain_lines
    ]

    raw: list[tuple[int, float]] = []
    for s in segments:
        text = (s.get("text") or "").strip()
        if not text:
            continue
        # Per-segment plausibility — same signals the global detector
        # uses. With no audio_duration only the mega-segment + fuzzy-loop
        # checks fire.
        per_seg_bad, _ = _detect_hallucination([s], audio_duration=None)
        if per_seg_bad:
            continue
        seg_set = {n for n in (_normalize_token(w) for w in text.split()) if n}
        if not seg_set:
            continue
        best_idx = -1
        best_score = 0.0
        for i, p_set in enumerate(plain_token_sets):
            if not p_set:
                continue
            inter = len(seg_set & p_set)
            union = len(seg_set | p_set)
            if union == 0:
                continue
            score = inter / union
            if score > best_score:
                best_score = score
                best_idx = i
        if best_idx >= 0 and best_score >= 0.3:
            raw.append((best_idx, float(s.get("start", 0.0))))

    raw.sort(key=lambda a: a[1])
    # Monotonic filter: drop later-in-time anchors that point earlier in
    # the lyrics (almost always a bad match).
    filtered: list[tuple[int, float]] = []
    last_idx = -1
    for idx, t in raw:
        if idx > last_idx:
            filtered.append((idx, t))
            last_idx = idx
    return filtered


def _synthesize_segments_from_plain(plain: str,
                                     audio_duration: float,
                                     anchors: list[tuple[int, float]] | None = None,
                                     start_time: float = 0.0,
                                     ) -> list[dict]:
    """Distribute lrclib plain lyrics across the audio duration.

    Used when Whisper has hallucinated and we need to ship the operator
    a complete transcription instead of 3 broken rows. With no anchors
    we distribute lines uniformly; with anchors we interpolate piecewise
    between (line_index, time) points so each lyric line lands near the
    moment Whisper actually heard it.

    Args:
        plain: lrclib plain text, one line per lyric line. Section
            markers like "[Verso]" / "[Chorus]" are filtered out.
        audio_duration: total audio length in seconds.
        anchors: optional list of (line_index, time_seconds) pairs from
            `_align_whisper_to_plain`. Empty/None falls back to even
            distribution from `start_time`.
        start_time: where the song body actually starts in the user's
            audio. Default 0 (whole audio is song). When the audio has
            a spoken-intro / dialogue prefix that the lrclib studio
            version doesn't have (e.g. the YouTube "Video Oficial" cut
            of "El Plan de la Mariposa - El Riesgo" has 73 s of
            dialogue before the song proper begins), the caller passes
            `start_time=intro_offset` so the synthesized song lyrics
            distribute over [intro_offset, audio_duration] instead of
            getting compressed by the spoken intro region.

    Returns segments in the same shape as `transcribe()` — list of
    {start, end, text} dicts, monotonically increasing, last `end`
    capped at `audio_duration`.
    """
    if not plain or not plain.strip() or not audio_duration:
        return []

    lines = _split_plain_lines(plain)
    if not lines:
        return []
    n = len(lines)

    # Filter / dedupe anchors to keep them strictly inside the line+time
    # window and strictly monotonic. The aligner already does this, but
    # we re-check defensively in case the caller hand-built anchors.
    monotonic: list[tuple[float, float]] = []
    last_idx_f, last_t_f = -1.0, -1.0
    for raw_anchor in (anchors or []):
        try:
            idx, t = float(raw_anchor[0]), float(raw_anchor[1])
        except (TypeError, ValueError, IndexError):
            continue
        if not (0 <= idx < n):
            continue
        if not (0 <= t < audio_duration):
            continue
        if idx > last_idx_f and t > last_t_f:
            monotonic.append((idx, t))
            last_idx_f, last_t_f = idx, t

    # Build the piecewise interpolation table. Always end at
    # (n, audio_duration); start at (0, start_time) unless an anchor
    # lives at line 0. start_time defaults to 0 (whole audio is song);
    # for tracks with a non-song prefix (spoken intro, dialogue) the
    # caller passes intro_offset so the song lyrics distribute over
    # the song region only.
    safe_start = max(0.0, min(float(start_time), float(audio_duration) - 0.5))
    points: list[tuple[float, float]] = list(monotonic)
    if not points or points[0][0] > 0:
        points.insert(0, (0.0, safe_start))
    if points[-1][0] < float(n):
        points.append((float(n), float(audio_duration)))

    def _time_at(line_index: float) -> float:
        for (l1, t1), (l2, t2) in zip(points, points[1:]):
            if line_index <= l2:
                if l2 == l1:
                    return t1
                return t1 + (line_index - l1) / (l2 - l1) * (t2 - t1)
        return points[-1][1]

    GAP = 0.05
    segments: list[dict] = []
    for i, line in enumerate(lines):
        start = _time_at(float(i))
        end = _time_at(float(i + 1)) - GAP
        if end <= start:
            end = start + 0.5
        segments.append({"start": start, "end": end, "text": line})
    if segments and segments[-1]["end"] > audio_duration:
        segments[-1]["end"] = audio_duration
    return segments


def _detect_speech_regions(audio_path: str,
                            top_db: float = 30.0,
                            min_region_s: float = 0.4,
                            merge_gap_s: float = 0.5,
                            ) -> list[tuple[float, float]]:
    """Return non-silent intervals from the audio, in seconds.

    Uses librosa's energy-based VAD (`effects.split` on the loaded
    waveform with a `top_db` threshold below the peak). Generic — works
    for any song. Output is what we use to decide WHERE in the audio
    a vocal subtitle could legitimately be placed; gap-fill avoids
    placing reference lines inside long instrumental silences.

    Defaults tuned for music (top_db=30 keeps quiet vocals in but drops
    drum-kit-only regions). Returns [] on any failure so the caller
    can fall back to time-uniform distribution.
    """
    try:
        import librosa
        import numpy as np
        # Mono, native rate sufficient for VAD; 22 kHz is librosa default.
        y, sr = librosa.load(audio_path, sr=22050, mono=True)
        intervals = librosa.effects.split(y, top_db=top_db)
        regions: list[tuple[float, float]] = []
        for start_sample, end_sample in intervals:
            start = float(start_sample) / sr
            end = float(end_sample) / sr
            if end - start < min_region_s:
                continue  # too short — likely click/spike, not speech
            regions.append((start, end))
        # Merge regions separated by very small gaps (single breath
        # between two phrases) so consecutive vocal phrases don't get
        # split into N micro-regions.
        if not regions:
            return []
        merged: list[tuple[float, float]] = [regions[0]]
        for start, end in regions[1:]:
            prev_start, prev_end = merged[-1]
            if start - prev_end <= merge_gap_s:
                merged[-1] = (prev_start, end)
            else:
                merged.append((start, end))
        return merged
    except Exception as e:
        logger.warning("[VAD] _detect_speech_regions failed (%s); skipping VAD", e)
        return []


def _fill_gaps_with_reference(whisper_segments: list[dict],
                               reference: str,
                               audio_duration: float,
                               coverage_threshold: float = 0.7,
                               audio_path: str | None = None,
                               ) -> list[dict] | None:
    """Generic recovery for outlier songs: keep Whisper's plausible
    segments, then fill the uncovered time intervals with lines from
    the reference text distributed proportionally.

    This is the right model whenever Whisper returns SOME real
    transcription (e.g. a spoken-dialogue intro that captures real
    words at real timestamps) interleaved with hallucinated segments
    (instrumental-passage mega-segments, synonym intra-loops). We
    must not throw the real segments away.

    Returns the merged segment list, or None when there's nothing
    sensible to return (no plausible Whisper AND no reference). The
    caller can decide whether to surface a coverage_warning.

    `coverage_threshold`: when the kept Whisper segments cover more
    than this fraction of the audio, return them as-is (no synthesis
    needed — Whisper worked). Default 0.7.

    Used by the Gemini-fallback path in /transcribe where lrclib was
    unavailable and we therefore don't know intro_offset. The lrclib-
    plain branch uses a different (more accurate) flow because it
    knows the song-body offset.
    """
    if not audio_duration or audio_duration <= 0:
        return whisper_segments or None

    # 1. Keep only segments that pass per-segment plausibility.
    kept: list[dict] = []
    dropped = 0
    for s in (whisper_segments or []):
        bad, _ = _detect_hallucination([s], audio_duration=None)
        if bad:
            dropped += 1
            continue
        kept.append(s)
    kept.sort(key=lambda x: float(x.get("start", 0)))

    coverage = sum(
        float(s.get("end", 0)) - float(s.get("start", 0)) for s in kept
    ) / float(audio_duration)

    # 2. Whisper covers most of the audio → no synthesis needed.
    if coverage >= coverage_threshold:
        return kept

    # 3. Sparse coverage: distribute reference lines into the gaps.
    ref_lines = _split_plain_lines(reference) if reference else []
    if not ref_lines:
        # Nothing to synthesize from. Return the (possibly empty) kept
        # set; caller falls back to whatever default it had.
        return kept or None

    # Build the gap list. We start from one of two sources:
    #   - VAD-detected SPEECH regions (preferred when audio_path is
    #     supplied) — distributing reference lines only where someone
    #     is actually singing/speaking. This is the right model for
    #     songs with long instrumental sections where uniform fill
    #     would land subtitles in silence (verified failure mode for
    #     "El Plan de la Mariposa - El Riesgo": 73 s spoken intro,
    #     instrumental gaps, then sung body).
    #   - Whole-audio gaps (legacy path) when no audio_path is given.
    # Each "gap" then has the time spans of any kept Whisper segments
    # subtracted from it so we don't double up subtitles in the same
    # window.
    speech_regions: list[tuple[float, float]] = []
    if audio_path:
        speech_regions = _detect_speech_regions(audio_path)
        if speech_regions:
            logger.info("[VAD] %s speech regions detected; reference will be distributed inside them",
                        len(speech_regions))
    if not speech_regions:
        speech_regions = [(0.0, float(audio_duration))]

    # Subtract kept Whisper time-windows from each speech region so
    # we don't synthesize over a real Whisper segment.
    kept_intervals = sorted(
        (float(s["start"]), float(s["end"])) for s in kept
    )

    def _subtract_kept(start: float, end: float) -> list[tuple[float, float]]:
        out: list[tuple[float, float]] = []
        cur = start
        for ks, ke in kept_intervals:
            if ke <= cur or ks >= end:
                continue
            if ks > cur:
                out.append((cur, min(ks, end)))
            cur = max(cur, ke)
            if cur >= end:
                break
        if cur < end:
            out.append((cur, end))
        return out

    gaps: list[tuple[float, float]] = []
    for region_start, region_end in speech_regions:
        for sub_start, sub_end in _subtract_kept(region_start, region_end):
            if sub_end - sub_start >= 1.0:
                gaps.append((sub_start, sub_end))

    if not gaps:
        return kept

    total_gap = sum(end - start for start, end in gaps)
    if total_gap <= 0:
        return kept

    # 4. Allocate reference lines per gap, proportional to gap duration.
    #
    # We use the largest-remainder method (Hamilton method) to guarantee
    # `sum(allocations) == n_lines` exactly, regardless of how the floats
    # round. Old `round()`-per-gap allocation could:
    #   - sum to > n_lines and starve the final gap into negative remainder,
    #   - sum to << n_lines for songs with one big gap and many tiny ones,
    #     piling lines into the trailing gap.
    n_lines = len(ref_lines)
    GAP_BETWEEN = 0.05
    audio_dur_f = float(audio_duration)
    output = list(kept)

    floor_alloc = [int((end - start) / total_gap * n_lines) for (start, end) in gaps]
    fracs = [
        ((end - start) / total_gap * n_lines) - floor_alloc[i]
        for i, (start, end) in enumerate(gaps)
    ]
    leftover = n_lines - sum(floor_alloc)
    # Distribute one extra line at a time to the gap with the largest
    # fractional part. Tiebreak by gap index for determinism.
    if leftover > 0:
        ranked = sorted(range(len(gaps)), key=lambda i: (-fracs[i], i))
        for idx in ranked[:leftover]:
            floor_alloc[idx] += 1
    # `leftover` cannot be negative under largest-remainder, but guard
    # defensively against fp drift on degenerate inputs.
    elif leftover < 0:
        ranked = sorted(range(len(gaps)), key=lambda i: (fracs[i], i))
        for idx in ranked[:(-leftover)]:
            if floor_alloc[idx] > 0:
                floor_alloc[idx] -= 1

    line_cursor = 0
    for i, (start, end) in enumerate(gaps):
        line_count = floor_alloc[i]
        if line_count <= 0:
            continue
        gap_dur = end - start
        per_line = gap_dur / line_count
        for j in range(line_count):
            line_idx = line_cursor + j
            if line_idx >= n_lines:
                break
            # Clamp BOTH start and end to [0, audio_duration]. Old code
            # only clamped end, which let `start > audio_duration` slip
            # through and produce inverted segments.
            line_start = max(0.0, min(start + j * per_line, audio_dur_f))
            line_end = min(start + (j + 1) * per_line - GAP_BETWEEN, audio_dur_f)
            if line_end <= line_start:
                # Drop degenerate (zero-or-negative duration) segments
                # outright. Pre-fix code synthesized a 0.5 s pad here,
                # which the renderer then drew on top of the next segment.
                continue
            output.append({
                "start": line_start,
                "end": line_end,
                "text": ref_lines[line_idx],
            })
        line_cursor += line_count

    output.sort(key=lambda s: float(s["start"]))
    return output


def _audio_duration(audio_path: str) -> float | None:
    """Best-effort audio duration in seconds. Handles both MP3 and WAV.
    For MP3 we use mutagen.mp3 (header-only, ~1 ms). For WAV we use the
    stdlib `wave` module (also header-only). Falls back to moviepy
    (slower, opens the full file) on any failure. Returns None if
    everything fails."""
    name_lower = audio_path.lower()
    if name_lower.endswith(".mp3"):
        try:
            from mutagen.mp3 import MP3
            return float(MP3(audio_path).info.length)
        except Exception:
            pass
    elif name_lower.endswith(".wav"):
        try:
            import wave
            with wave.open(audio_path, "rb") as wf:
                frames = wf.getnframes()
                rate = wf.getframerate() or 0
                if rate > 0:
                    return float(frames) / rate
        except Exception:
            pass
    try:
        from moviepy.editor import AudioFileClip
        with AudioFileClip(audio_path) as a:
            return float(a.duration)
    except Exception:
        return None


def _slice_audio_window(input_path: str, output_path: str,
                         start_seconds: float, duration_seconds: float) -> bool:
    """Slice an arbitrary [start, start+duration] window from an MP3.

    Uses ``-ss`` AFTER ``-i`` for sample-accurate seek (slow seek), and
    re-encodes via libmp3lame so we don't depend on keyframe alignment.
    Slower than ``_slice_audio_prefix`` (re-encode vs stream copy) but
    more reliable for arbitrary offsets where MP3 frame boundaries may
    not line up with the requested cut.

    Returns True on success, False on any failure. Best-effort.
    """
    if start_seconds < 0 or duration_seconds <= 0:
        return False
    import subprocess as _sp
    try:
        _sp.run(
            ["ffmpeg", "-y", "-i", input_path,
             "-ss", str(start_seconds), "-t", str(duration_seconds),
             "-acodec", "libmp3lame", "-q:a", "5",
             "-loglevel", "error", output_path],
            check=True, timeout=30,
        )
        return os.path.exists(output_path) and os.path.getsize(output_path) > 0
    except (_sp.CalledProcessError, _sp.TimeoutExpired, FileNotFoundError, OSError) as e:
        logger.warning("[LYRICS] _slice_audio_window failed: %s", e)
        return False


def _whisper_quick_text(mp3_path: str, job_id: str | None = None) -> str:
    """Minimal whisper-1 transcription of a short clip — used by alignment
    verification. Returns plain text with no post-processing (no spam
    filter, no dedup). Best-effort: returns "" on any failure.

    `job_id` is optional; when provided, the call gets recorded in
    ai_provenance so the cost dashboard counts it. These clips are
    short (~5 s) so the cost is ~$0.0005 each, but at scale across
    every job the cents add up.
    """
    if not os.path.exists(mp3_path):
        return ""
    recorder = None
    if job_id:
        from provenance import record_ai_call
        recorder = record_ai_call(
            job_id=job_id,
            step="whisper_quick_align",
            tool_name="whisper-1",
            tool_provider="openai",
            prompt="(audio alignment short clip)",
            input_data_types=["audio_file_short"],
        )
    try:
        from openai import OpenAI
        with open(mp3_path, "rb") as f:
            r = OpenAI().audio.transcriptions.create(
                model="whisper-1", file=f, response_format="text",
            )
        if recorder is not None:
            try:
                recorder.finish(response_summary="whisper_quick_ok")
            except Exception:
                pass
        return (r or "").strip()
    except Exception as e:
        logger.warning("[LYRICS] _whisper_quick_text failed: %s", e)
        return ""


def _env_flag(name: str) -> bool:
    """True iff env var `name` is set to a truthy value. Treats unset,
    empty string, '0', 'false', 'no', 'off' as falsy. Used to gate
    opt-in Tier-1 quality helpers off by default (so prod behavior is
    unchanged unless explicitly enabled per-deploy or per-benchmark)."""
    import os as _os
    return _os.environ.get(name, "").strip().lower() in ("1", "true", "yes", "on")


# Tenants whose contract requires the UMG Guideline 15 content filter
# (no faces / hands / logos in backgrounds). All other tenants render
# freely by default and can opt in via force_content_validation. Mirror
# of frontend ContentValidationToggle.UMG_TENANTS — keep in sync.
UMG_TENANTS = {"umg", "omg"}


def _compute_allow_people(job_id: str | None) -> bool:
    """Should the AI be allowed to render people in the bg for this job?

    Returns True when the content_validator would be SKIPPED for the job
    (per tenant default + operator override). False when validator would
    run. Drives the `allow_people` flag threaded into
    `_analyze_lyrics_for_background`, `_generate_veo_video`, and
    `_generate_imagen_image` so the no-people clauses in those system
    prompts get dropped consistently with the post-gen check.

    Pre-fix the toggle "Asumir el riesgo / fondo libre" only relaxed the
    post-gen validator; the pre-gen prompt sanitization still stripped
    people. The operator's prompt "woman lies upside down on armchair"
    rendered an empty armchair (incident 2026-05-19). This helper closes
    that gap.

    Safe to call with `job_id=None` — returns False (conservative).
    """
    if not job_id:
        return False
    try:
        from database import SessionLocal as _SL, Job as _Job
        with _SL() as _db:
            row = _db.query(_Job).filter(_Job.job_id == job_id).first()
            if not row:
                return False
            tenant = row.tenant_id or ""
            rp = row.render_params if isinstance(row.render_params, dict) else {}
            bypass = bool(rp.get("bypass_content_validation"))
            force = bool(rp.get("force_content_validation"))
            is_umg = tenant in UMG_TENANTS
            # Allow people when validator would NOT run. Same boolean
            # algebra as Step 1b — but inverted (allow_people = NOT validate).
            should_validate = (is_umg and not bypass) or (not is_umg and force)
            return not should_validate
    except Exception as e:
        logger.warning("[BG] _compute_allow_people fallback to False for job %s: %s", job_id, e)
        return False


def _validate_segments_against_audio(audio_path: str, segments: list[dict],
                                      job_id: str | None = None,
                                      n_samples: int = 3) -> list[dict]:
    """Sample N short audio clips, transcribe each with Whisper, and
    flag any segment whose text disagrees with what Whisper hears in
    the same window. Returns segments with `seg["flagged"] = True`
    for the suspicious ones. Originals returned unmodified when the
    `VALIDATE_SEGMENTS` env flag is off.

    The expensive bit is N extra Whisper API calls (~1-2 s each for
    8-s slices). Capped at 3 samples by default to keep cost ~$0.005
    per job — marginal next to a $0.50 Veo render. Worth it if the
    operator catches a Whisper hallucination in the editor before the
    full render burns.

    Why we don't validate every segment: that would 10-50x the cost
    of a transcribe call. Sampling 3-5 strategically-chosen windows
    (intro / first chorus / late verse) catches most systematic
    failures without paying per-line.
    """
    if not _env_flag("VALIDATE_SEGMENTS") and not _env_flag("ENABLE_TIER1"):
        return segments
    if not segments or len(segments) < 2:
        return segments
    # Pick samples spaced across the song: first non-trivial line,
    # middle, late. Skip lines whose text is empty or shorter than
    # ~3 words (sample isn't meaningful for "oh!" or "yeah").
    import random as _r
    candidates = [
        s for s in segments
        if s.get("text") and len(str(s["text"]).split()) >= 3
        and s.get("end") and s.get("start") is not None
        and float(s["end"]) - float(s["start"]) >= 1.5
    ]
    if not candidates:
        return segments
    n = min(n_samples, len(candidates))
    # Spread the picks across the song so we don't sample 3 lines from
    # the same chorus repetition.
    step = max(1, len(candidates) // n)
    picks = candidates[::step][:n]

    import tempfile, subprocess as _sp
    from difflib import SequenceMatcher
    flagged_ids: set[int] = set()
    for seg in picks:
        try:
            start = float(seg["start"])
            end = float(seg["end"])
            dur = min(8.0, end - start + 1.0)  # add 1s tail for breath
            with tempfile.NamedTemporaryFile(suffix=".mp3", delete=False) as f:
                clip_path = f.name
            try:
                _sp.run(
                    ["ffmpeg", "-y", "-ss", f"{max(0.0, start - 0.2):.2f}",
                     "-i", audio_path, "-t", f"{dur:.2f}",
                     "-acodec", "libmp3lame", "-loglevel", "error", clip_path],
                    check=True, timeout=30,
                )
                # Re-transcribe just this slice
                from pipeline import transcribe as _transcribe  # self-import for testing
                heard_segs = _transcribe(clip_path, language="es", lyrics_hint=None)
                heard_text = " ".join((s.get("text") or "").strip() for s in (heard_segs or [])).lower()
                expected = (seg.get("text") or "").lower().strip()
                if not heard_text or not expected:
                    continue
                ratio = SequenceMatcher(None, expected, heard_text).ratio()
                if ratio < 0.5:
                    flagged_ids.add(id(seg))
                    print(f"[VALIDATE] segment at {start:.1f}s flagged: "
                          f"expected '{expected[:40]}' vs heard '{heard_text[:40]}' (ratio {ratio:.2f})")
            finally:
                try:
                    os.unlink(clip_path)
                except OSError:
                    pass
        except Exception as e:  # pragma: no cover — best-effort
            print(f"[VALIDATE] sample failed: {e}")
            continue

    if not flagged_ids:
        return segments
    # Mutate copies so callers can rely on dict identity changing
    # iff something was flagged.
    out = []
    for s in segments:
        new = dict(s)
        if id(s) in flagged_ids:
            new["flagged"] = True
        out.append(new)
    return out


def _polish_segments_text(segments: list[dict], artist: str = "",
                           song_title: str = "") -> list[dict]:
    """Single Gemini pass that takes the full Whisper output text +
    artist/title and returns corrections for common Spanish errors
    ('de la amor' → 'del amor', missing accents, etc.). Timings are
    untouched — only `seg["text"]` may change.

    Returns segments unchanged when `POLISH_TEXT` env flag is off, or
    when Gemini doesn't return parseable JSON. Designed to never make
    things worse: if Gemini's output doesn't match the input segment
    count, we abort the polish (no partial application).
    """
    if not _env_flag("POLISH_TEXT") and not _env_flag("ENABLE_TIER1"):
        return segments
    if not segments:
        return segments
    try:
        from google import genai
        client = _get_genai_client()
    except Exception as e:  # pragma: no cover
        print(f"[POLISH] genai client unavailable, skip: {e}")
        return segments

    # Build a numbered list — Gemini returns same numbering, we map back.
    lines = []
    for i, s in enumerate(segments):
        text = (s.get("text") or "").strip()
        lines.append(f"{i}: {text}")
    numbered = "\n".join(lines)

    system_prompt = (
        "You are a Spanish lyrics proofreader. The input is a numbered list of "
        "lines from a Whisper auto-transcription of a song. Whisper makes "
        "predictable errors in Spanish: missing accents (se→sé, mas→más, "
        "te→té), wrong contractions ('de la amor'→'del amor', 'a el'→'al'), "
        "homophone confusions (haya/halla, hay/ay), capitalization of proper "
        "nouns.\n\n"
        "Return STRICT JSON: an array where each element is "
        '{"i": <line index>, "text": "<corrected text>"} ONLY for lines you '
        "actually changed. Do NOT return unchanged lines. Do NOT add new lines. "
        "Do NOT translate or paraphrase — only fix obvious transcription errors. "
        "Preserve all original words you do not need to fix.\n\n"
        "When in doubt, leave the line as-is."
    )
    user_content = f"Artist: {artist}\nSong: {song_title}\n\nLines:\n{numbered}"

    try:
        # Audit 2026-05-26: timeout wrapper. POLISH_TEXT is flag-gated
        # so impact is limited, but the same hang vector applies — Vertex
        # latency degrades, worker blocks, find_orphan_polling_jobs reaps.
        response = _call_with_timeout(
            lambda: client.models.generate_content(
                model="gemini-2.5-flash",
                contents=user_content,
                config=genai.types.GenerateContentConfig(
                    system_instruction=system_prompt,
                    temperature=0.1,
                    max_output_tokens=2000,
                ),
            ),
            timeout_s=45.0,
            label="POLISH",
        )
        text = (response.text or "").strip()
        # Strip code fences if any
        if text.startswith("```"):
            text = "\n".join(text.split("\n")[1:-1])
        parsed = json.loads(text)
        if not isinstance(parsed, list):
            print(f"[POLISH] expected array, got {type(parsed).__name__}; skip")
            return segments
    except Exception as e:
        print(f"[POLISH] Gemini call/parse failed: {e}; segments unchanged")
        return segments

    # Apply corrections
    corrections = {}
    for item in parsed:
        try:
            idx = int(item["i"])
            new_text = str(item["text"]).strip()
            if 0 <= idx < len(segments) and new_text:
                corrections[idx] = new_text
        except (KeyError, ValueError, TypeError):
            continue
    if not corrections:
        return segments
    print(f"[POLISH] applied {len(corrections)} text correction(s)")
    out = []
    for i, s in enumerate(segments):
        new = dict(s)
        if i in corrections and new.get("text") != corrections[i]:
            new["text"] = corrections[i]
        out.append(new)
    return out


def _verify_lrclib_alignment(audio_path: str, expected_text: str,
                              claimed_start: float, window: float = 5.5) -> float | None:
    """Slice a ~window-second clip of audio starting just before
    `claimed_start`, run Whisper on it, fuzzy-match against `expected_text`.

    Returns a similarity ratio in [0, 1] (1.0 = identical, 0 = nothing in
    common), or None if slicing or Whisper failed and we cannot verify.

    Used to confirm lrclib's offset-shifted timestamps actually line up
    with what's being sung in the user's audio. Cheap (~3 s, ~$0.0005).
    Conservative threshold for "trust": ~0.4. For UMG-style operator
    review, this is the difference between "subtitles look right" and
    "subtitles are 30 s off and we shipped it."
    """
    if claimed_start < 0 or not expected_text:
        return None
    import tempfile
    from difflib import SequenceMatcher
    import re as _re

    fd, clip_path = tempfile.mkstemp(suffix=".mp3")
    try:
        os.close(fd)
        slice_start = max(0.0, claimed_start - 0.3)
        if not _slice_audio_window(audio_path, clip_path, slice_start, window):
            return None
        actual = _whisper_quick_text(clip_path)
        if not actual:
            return None
        def _norm(s: str) -> str:
            return _re.sub(r"[^\w\s]", "", s.lower()).strip()
        return SequenceMatcher(None, _norm(actual), _norm(expected_text)).ratio()
    finally:
        try:
            os.unlink(clip_path)
        except OSError:
            pass


def _slice_audio_prefix(input_path: str, output_path: str, seconds: float) -> bool:
    """Slice the first ``seconds`` of an MP3 into ``output_path`` using ffmpeg.

    Used when the user uploads a song version with extra audio at the start
    (a dialogue intro on an "Official Video" cut, e.g.) — we slice that
    intro chunk and feed it to Whisper separately so the operator gets a
    transcription of the dialogue too. The song proper is timestamped from
    lrclib's synced lyrics with an offset.

    Uses ``-acodec copy`` so there is no re-encode — just a stream copy of
    the audio bytes through the cut point. Fast (< 1 s for typical sizes).

    Returns True on success, False on any failure. Best-effort: caller
    treats False as "no intro transcription available" and continues.
    """
    if seconds <= 0:
        return False
    import subprocess as _sp
    try:
        _sp.run(
            ["ffmpeg", "-y", "-i", input_path,
             "-t", str(seconds), "-acodec", "copy",
             "-loglevel", "error", output_path],
            check=True, timeout=30,
        )
        return os.path.exists(output_path) and os.path.getsize(output_path) > 0
    except (_sp.CalledProcessError, _sp.TimeoutExpired, FileNotFoundError, OSError) as e:
        logger.warning("[LYRICS] _slice_audio_prefix failed: %s", e)
        return False


# ─────────────────────────────────────────────────────────────────────
# Defense helpers for `_gemini_cleanup_lyrics`. Pure functions so unit
# tests can exercise every failure-mode without mocking the SDK.
#
# The base function already rejects: empty response, line-count ratio
# outside [0.5, 2.5], Gemini exceptions, missing creds. These 4 helpers
# add protection against more subtle Gemini misbehaviour:
#  1. Refusal / meta-commentary returned as if it were lyrics
#  2. Preamble like "Sure, here are the corrected lyrics:" before content
#  3. Silent translation to English (when input was Spanish)
#  4. Hallucination — text that's lyrics-shaped but doesn't match input
# ─────────────────────────────────────────────────────────────────────

# Match common refusal openings in EN / ES. Lowercased substring check
# so we don't have to enumerate every phrasing.
_GEMINI_REFUSAL_MARKERS = (
    "i cannot",
    "i can't",
    "i am unable",
    "i'm unable",
    "i'm sorry",
    "i am sorry",
    "as an ai",
    "as a language model",
    "no puedo proporcionar",
    "no puedo transcribir",
    "no puedo ayudar",
    "lo siento, no puedo",
    "no es posible",
    "i don't have access",
    "i'm not able to",
    "i must decline",
)


def _gemini_cleanup_is_refusal(text: str) -> bool:
    """True if the (already-stripped) text looks like a Gemini refusal
    or meta-explanation rather than corrected lyrics. Lowercases and
    looks for opener fragments in the first 240 chars — refusals always
    state themselves up front."""
    if not text:
        return True
    head = text[:240].lower()
    return any(m in head for m in _GEMINI_REFUSAL_MARKERS)


# Preamble lines Gemini sometimes prefixes when it doesn't follow the
# "no markdown / no preamble" instruction. Drop these so the cleaned
# text is just the lyrics. Conservative: only strip the FIRST line
# and only when it matches one of these openers.
_GEMINI_PREAMBLE_OPENERS = (
    "sure,", "sure ", "here are", "here is", "here you go",
    "aqui esta", "aqui estan", "aquí está", "aquí están",
    "las letras corregidas",
    "la letra corregida",
    "okay,", "ok,", "claro,",
    "corrected lyrics",
)


def _gemini_cleanup_strip_preamble(text: str) -> str:
    """If the first non-empty line is a recognised preamble (e.g.
    'Sure, here are the corrected lyrics:'), drop it. Idempotent —
    safe to call multiple times. Cleaned lyrics never start with these
    English openers, so the false-positive risk is minimal."""
    if not text:
        return text
    lines = text.splitlines()
    # Skip leading blank lines.
    i = 0
    while i < len(lines) and not lines[i].strip():
        i += 1
    if i >= len(lines):
        return text
    first = lines[i].strip().lower().rstrip(":.")
    for opener in _GEMINI_PREAMBLE_OPENERS:
        if first.startswith(opener):
            return "\n".join(lines[i + 1:]).lstrip()
    return text


# Spanish orthography markers — chars that strongly indicate the text is
# Spanish (or at least romance-language with proper accents). When the
# input has these and the output doesn't, Gemini likely translated.
_SPANISH_MARKERS = "ñÑáéíóúÁÉÍÓÚüÜ¿¡"


def _gemini_cleanup_language_intact(cleaned: str, plain: str) -> bool:
    """If `plain` had Spanish-specific chars at density D_in, `cleaned`
    must have at least D_in × 0.5 (allowing for some normalisation).
    Catches the case where Gemini translates to English silently.

    True (intact) when:
      - input has no Spanish markers (we can't measure → trust the
        line-count gate and word-overlap gate to catch issues), OR
      - cleaned has at least half the marker density of input.
    """
    def density(s: str) -> float:
        if not s:
            return 0.0
        markers = sum(1 for c in s if c in _SPANISH_MARKERS)
        return markers / max(1, len(s))
    d_in = density(plain)
    if d_in < 0.001:
        return True
    d_out = density(cleaned)
    return d_out >= d_in * 0.5


def _gemini_cleanup_word_overlap(cleaned: str, plain: str) -> float:
    """Jaccard overlap of normalised word sets (lowercase, accents
    stripped, punctuation dropped). Returns 0..1. Hallucinations score
    low because Gemini invented new vocabulary that doesn't appear in
    lrclib. Stop-words excluded so a song with "el / la / que" doesn't
    fake a high score."""
    import unicodedata
    def words(s: str) -> set[str]:
        s = unicodedata.normalize("NFKD", s or "")
        s = "".join(c for c in s if not unicodedata.combining(c))
        tokens = re.findall(r"[a-z0-9]+", s.lower())
        # Strip very common Spanish stop-words so a "el la que de" overlap
        # doesn't fake a positive score on otherwise-unrelated text.
        stop = {"el","la","los","las","de","del","y","o","a","en","que",
                "se","un","una","es","con","por","para","no","si",
                "the","a","an","and","or","of","to","in","is","it","you"}
        return {t for t in tokens if t not in stop and len(t) >= 2}
    a, b = words(plain), words(cleaned)
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def _gemini_cleanup_cache_key(audio_path: str, lrclib_plain: str):
    """Content-addressable cache key for Gemini lyrics cleanup. Same
    audio + same lrclib hint = same cleaned output (deterministic with
    temperature=0.1). Mirrors `whisperx_transcribe._compute_cache_key`."""
    import hashlib
    try:
        h = hashlib.sha256()
        with open(audio_path, "rb") as f:
            for chunk in iter(lambda: f.read(1024 * 1024), b""):
                h.update(chunk)
        audio_hash = h.hexdigest()[:16]
    except Exception:
        return (None, None, None)
    hint = (lrclib_plain or "").strip()
    hint_hash = hashlib.sha1(hint.encode("utf-8")).hexdigest()[:16] if hint else ""
    key = f"gem-clean:{audio_hash}:{hint_hash}"
    return (key, audio_hash, hint_hash)


def _gemini_cleanup_cache_lookup(cache_key: str) -> str | None:
    """Return cached cleaned text for `cache_key`, or None on miss."""
    try:
        from database import TranscriptionCache, SessionLocal
        import json as _json
        db = SessionLocal()
        try:
            row = db.query(TranscriptionCache).filter(
                TranscriptionCache.cache_key == cache_key
            ).first()
            if not row:
                return None
            payload = _json.loads(row.segments)
            return payload.get("cleaned") if isinstance(payload, dict) else None
        finally:
            db.close()
    except Exception as e:
        logger.warning("[GEMINI-CLEAN] cache lookup failed (%s); will run live", e)
        return None


def _gemini_cleanup_cache_write(cache_key: str, audio_hash: str,
                                 hint_hash: str, cleaned: str) -> None:
    """Persist `cleaned` text under `cache_key`. Best-effort."""
    try:
        from database import TranscriptionCache, SessionLocal
        import json as _json
        db = SessionLocal()
        try:
            row = TranscriptionCache(
                cache_key=cache_key,
                audio_hash=audio_hash,
                engine="gemini_cleanup",
                language=None,
                lyrics_hint_hash=hint_hash or None,
                segments=_json.dumps({"cleaned": cleaned}),
            )
            db.merge(row)
            db.commit()
        finally:
            db.close()
    except Exception as e:
        logger.warning("[GEMINI-CLEAN] cache write failed (%s); ignoring", e)


def _gemini_cleanup_lyrics(audio_path: str, lrclib_plain: str,
                            *, artist: str = "", song: str = "",
                            timeout_s: int = 90) -> str | None:
    """Send the audio + lrclib plain lyrics to Gemini 2.5 Flash and return
    the proofread text. Used when lrclib has the canonical text but it has
    the predictable defects of community transcriptions:
      - Missing accents (mi, se, mas → mí, sé, más).
      - Spelling errors (querete → quererte, tenes → tienes when
        Castilian, kept as tenés when voseo).
      - Wrong repetition counts in chorus (3 "para mí" when there are 4).
      - Misheard words (lrclib transcribers can fall for the same
        homophone trap as Whisper).

    INCIDENT 2026-05-26: black-box test of Rotor Videos' Transcribe & Sync
    revealed they use LyricFind's licensed catalog (post-acquisition
    Dec-2023) which has these defects fixed. We use community lrclib;
    this helper closes the gap without the licensing cost.

    Gated behind `GEMINI_LYRICS_CLEANUP_ENABLED=1`. Returns None on:
      - feature flag off
      - missing audio / lrclib text
      - genai client unavailable
      - Gemini error or rejection (safety filter)
    Caller falls back to the un-cleaned lrclib text.

    Cost: ~$0.01 per 6-minute song (Gemini 2.5 Flash with thinking=0,
    audio counts as ~1700 tokens/min input). Latency ~15 s.

    Content-addressable cache: same audio + same lrclib hint → cache hit
    (no Gemini call). Multi-retry pipelines pay the cost once.
    """
    if not _env_flag("GEMINI_LYRICS_CLEANUP_ENABLED"):
        return None
    if not audio_path or not os.path.exists(audio_path):
        return None
    plain = (lrclib_plain or "").strip()
    if not plain:
        return None

    cache_key, audio_hash, hint_hash = _gemini_cleanup_cache_key(audio_path, plain)
    if cache_key:
        cached = _gemini_cleanup_cache_lookup(cache_key)
        if cached:
            logger.info("[GEMINI-CLEAN] cache hit audio_hash=%s (skipped live call)", audio_hash)
            return cached

    try:
        from google import genai
        client = _get_genai_client()
    except Exception as e:
        logger.warning("[GEMINI-CLEAN] genai client unavailable: %s", e)
        return None

    system_prompt = (
        "You are a Spanish-language lyrics proofreader.\n"
        "Input: (1) full audio recording of a song, (2) a community "
        "transcription with errors.\n\n"
        "CRITICAL: Return the FULL corrected lyrics for the ENTIRE song. "
        "The input may have 50-100+ lines — your output must cover ALL "
        "of them. Do not stop early. Do not summarize. Do not skip the "
        "second half.\n\n"
        "Fix:\n"
        "- Missing accents (mi→mí, se→sé, mas→más when applicable).\n"
        "- Misspellings (querete→quererte, etc.).\n"
        "- Wrong word counts in repeated lines (if chorus is 'X, X, X, "
        "X' 4 times in the audio, write 4).\n"
        "- Misheard words you can verify against the audio.\n\n"
        "PRESERVE any non-verbal vocalizations or ad-libs you HEAR in "
        "the audio (extended vowels, sung interjections, repeated "
        "syllables) as separate lines in the position they occur. "
        "CRITICAL: ONLY include vocalizations that are actually sung "
        "in the recording — do NOT invent vocalizations that are not "
        "present in the audio. This matters most for outros/codas "
        "where rock and pop often end with vocalizations missing "
        "from community transcriptions.\n\n"
        "KEEP the same line-break style as the input. Do not merge or "
        "split lines unless the audio clearly disagrees.\n\n"
        "Return ONLY the corrected lyrics, one line per row. "
        "No preamble, no markdown, no commentary."
    )

    try:
        with open(audio_path, "rb") as f:
            audio_bytes = f.read()
    except OSError as e:
        logger.warning("[GEMINI-CLEAN] could not read audio (%s); skip", e)
        return None

    # mime-type from extension; Gemini accepts wav/mp3/flac/etc.
    ext = os.path.splitext(audio_path)[1].lower().lstrip(".")
    mime = {"wav": "audio/wav", "mp3": "audio/mpeg", "flac": "audio/flac",
            "ogg": "audio/ogg", "m4a": "audio/mp4"}.get(ext, "audio/wav")

    user_content = [
        genai.types.Part.from_bytes(data=audio_bytes, mime_type=mime),
        genai.types.Part.from_text(
            text=f"Artist: {artist}\nSong: {song}\n\n"
                 f"Transcription to verify and fix:\n\n{plain}"
        ),
    ]

    import time as _time
    t0 = _time.time()
    try:
        # Audit 2026-05-26: wrap with _call_with_timeout. Without this, a
        # Vertex hiccup hangs the worker indefinitely in the transcription
        # step and the job stays "transcribing" until find_stuck_transcriptions
        # reaps it 120 min later. timeout_s arg of this function was
        # previously declared but unused (line 3729).
        response = _call_with_timeout(
            lambda: client.models.generate_content(
                model="gemini-2.5-flash",
                contents=user_content,
                config=genai.types.GenerateContentConfig(
                    system_instruction=system_prompt,
                    temperature=0.1,
                    max_output_tokens=8000,
                    thinking_config=genai.types.ThinkingConfig(thinking_budget=0),
                ),
            ),
            timeout_s=float(timeout_s),
            label="GEMINI-CLEAN",
        )
    except Exception as e:
        logger.warning("[GEMINI-CLEAN] Gemini call failed: %s — using lrclib raw", e)
        return None

    elapsed = _time.time() - t0
    cleaned = (response.text or "").strip()
    if not cleaned:
        # Could be safety filter rejection (explicit content) or empty
        # response. Fall back to raw text. Try to surface the reason.
        try:
            finish = response.candidates[0].finish_reason
        except Exception:
            finish = "unknown"
        logger.info("[GEMINI-CLEAN] empty response (finish=%s, %.1fs) — using lrclib raw", finish, elapsed)
        return None

    # Strip any "Sure, here are the corrected lyrics:" preamble Gemini
    # sometimes prefixes despite the system prompt. Conservative: only
    # the first line and only when it matches a known opener.
    cleaned = _gemini_cleanup_strip_preamble(cleaned)

    # Defense 1: refusal / meta-commentary returned as if it were lyrics.
    if _gemini_cleanup_is_refusal(cleaned):
        logger.warning(
            "[GEMINI-CLEAN] refusal-like output detected (%.1fs) — using lrclib raw",
            elapsed,
        )
        return None

    # Defense 2 (line count): output must have at least 50% as many
    # lines as input and at most 250% (chorus expansions OK, but not
    # 10x explosion).
    out_lines = [l for l in cleaned.splitlines() if l.strip()]
    in_lines = [l for l in plain.splitlines() if l.strip()]
    ratio = len(out_lines) / max(1, len(in_lines))
    if ratio < 0.5 or ratio > 2.5:
        logger.warning(
            "[GEMINI-CLEAN] suspicious line-count ratio %.2f (in=%d → out=%d), "
            "%.1fs — using lrclib raw to be safe",
            ratio, len(in_lines), len(out_lines), elapsed,
        )
        return None

    # Defense 3: language drift. If lrclib was Spanish (had ñ/á/é/...),
    # the cleaned text must preserve that marker density. Catches the
    # case where Gemini translates to English without telling us.
    if not _gemini_cleanup_language_intact(cleaned, plain):
        logger.warning(
            "[GEMINI-CLEAN] Spanish marker density dropped vs input (%.1fs) — "
            "likely translation, using lrclib raw",
            elapsed,
        )
        return None

    # Defense 4: hallucination floor. ≥ 50% Jaccard overlap of
    # content words (stopwords excluded) between input and output.
    # Lower than that means Gemini invented vocabulary the song
    # doesn't have. Accent/typo fixes preserve the words themselves,
    # so the floor is generous.
    overlap = _gemini_cleanup_word_overlap(cleaned, plain)
    if overlap < 0.5:
        logger.warning(
            "[GEMINI-CLEAN] low word overlap %.2f vs input (%.1fs) — "
            "likely hallucination, using lrclib raw",
            overlap, elapsed,
        )
        return None

    logger.info(
        "[GEMINI-CLEAN] cleaned %d → %d lines, overlap=%.2f in %.1fs (audio_hash=%s)",
        len(in_lines), len(out_lines), overlap, elapsed, audio_hash,
    )

    if cache_key:
        _gemini_cleanup_cache_write(cache_key, audio_hash, hint_hash, cleaned)

    return cleaned


def _llm_segment_words(segs: list[dict], *, audio_path: str, artist: str = "",
                       song: str = "", timeout_s: int = 90) -> list[dict]:
    """Re-segment a whisperX word stream into clean phrase lines via Gemini,
    grounded in the audio. The LLM decides the LINE GROUPING + fixes orthography
    (a language task heuristics can't do); whisperX provides the exact TIMING —
    each line's start/end come from its words' real timestamps, never re-timed.

    Why this beats both alternatives for DIVERGENT LIVES: pause/length heuristics
    can't find grammatical phrase boundaries, and `reconcile` aborts when the
    live diverges from lrclib's studio structure. This re-segments the live's
    OWN words, so there is no reference template to drift against. Validated in
    the lab on "Nada Fue Un Error (En Vivo)": output matches Rotor line-for-line.

    Self-declining (returns the INPUT segs unchanged) on: flag off, too-short,
    a segment without word timing, Gemini failure, unparseable output, low
    coverage, or low word-overlap (hallucination guard). Never raises. Gated
    behind LLM_SEGMENT_ENABLED (default off)."""
    if not _env_flag("LLM_SEGMENT_ENABLED"):
        return segs
    W: list[dict] = []
    for s in (segs or []):
        ws = s.get("words")
        if not isinstance(ws, list):
            return segs  # no word timing → can't map lines back to time
        for w in ws:
            if isinstance(w, dict) and "word" in w:
                W.append(w)
    if len(W) < 8:
        return segs

    numbered = " ".join(f"[{i}]{(w.get('word') or '').strip()}"
                        for i, w in enumerate(W))
    system_prompt = (
        "Sos un editor profesional de lyric videos en español (estilo Rotor).\n"
        "Te doy: (1) el AUDIO de la canción (puede ser un vivo), (2) su "
        "transcripción palabra-por-palabra (cada palabra con su índice [n]). "
        "Agrupá las palabras en LÍNEAS de lyric video.\n\n"
        "CÓMO CORTAR (lo más importante):\n"
        "- UNA frase corta por línea, estilo karaoke (~4 a 9 palabras).\n"
        "- Cortá en cada límite de frase/cláusula natural. Ej: 'Yo quería que "
        "nos pasara' y 'Y tú, y tú lo dejaste pasar' son DOS líneas.\n"
        "- NO juntes dos frases en una sola línea.\n\n"
        "ORTOGRAFÍA: acentos, MAYÚSCULA al inicio de CADA línea (estilo karaoke, "
        "aunque sea continuación de la oración), signos (¿¡ incluidos).\n"
        "AUDIO: usalo para (a) corregir palabras mal escuchadas, (b) poner los "
        "coros/ad-libs de fondo entre paréntesis.\n\n"
        "REGLAS DURAS:\n"
        "- Cubrí TODAS las palabras, en orden, cada una en EXACTAMENTE una línea.\n"
        "- Podés corregir ortografía/mishears contra el audio, pero NO inventes "
        "contenido ni reordenes.\n"
        "- Formato EXACTO por línea, sin nada más: [primer_indice-ultimo_indice] <texto>\n"
        "  Ej: [0-3] Tengo una mala noticia\n"
        "- Sin preámbulo, markdown ni comentarios."
    )
    try:
        from google import genai
        client = _get_genai_client()
        if client is None:
            return segs
        with open(audio_path, "rb") as f:
            audio_bytes = f.read()
        ext = os.path.splitext(audio_path)[1].lower().lstrip(".")
        mime = {"wav": "audio/wav", "mp3": "audio/mpeg", "flac": "audio/flac",
                "ogg": "audio/ogg", "m4a": "audio/mp4"}.get(ext, "audio/wav")
        resp = _call_with_timeout(
            lambda: client.models.generate_content(
                model="gemini-2.5-flash",
                contents=[
                    genai.types.Part.from_bytes(data=audio_bytes, mime_type=mime),
                    genai.types.Part.from_text(text=numbered),
                ],
                config=genai.types.GenerateContentConfig(
                    system_instruction=system_prompt, temperature=0.1,
                    max_output_tokens=8000,
                    thinking_config=genai.types.ThinkingConfig(thinking_budget=0),
                ),
            ),
            timeout_s=float(timeout_s), label="LLM-SEGMENT",
        )
        out = (resp.text or "").strip()
    except Exception as e:
        logger.warning("[LLM-SEGMENT] failed (%s); keeping whisperX segments", e)
        return segs

    parsed = []
    for ln in out.splitlines():
        m = re.match(r"\s*\[(\d+)\s*-\s*(\d+)\]\s*(.+)", ln)
        if not m:
            continue
        i, j, txt = int(m.group(1)), int(m.group(2)), m.group(3).strip()
        if i > j or i < 0 or j >= len(W) or not txt:
            continue
        parsed.append((i, j, txt))
    if not parsed:
        logger.warning("[LLM-SEGMENT] no parseable lines; keeping whisperX")
        return segs

    # Gate (a): the line ranges must cover ~all the words (no big drops).
    cov = set()
    for i, j, _ in parsed:
        cov.update(range(i, j + 1))
    coverage = len(cov) / max(1, len(W))
    if coverage < 0.9:
        logger.warning("[LLM-SEGMENT] coverage %.2f < 0.9; keeping whisperX", coverage)
        return segs
    # Gate (b): anti-hallucination — the LLM text must overlap the whisperX text.
    wx_text = " ".join((w.get("word") or "") for w in W)
    llm_text = " ".join(t for _, _, t in parsed)
    if _gemini_cleanup_word_overlap(llm_text, wx_text) < 0.5:
        logger.warning("[LLM-SEGMENT] word-overlap < 0.5 (hallucination guard); keeping whisperX")
        return segs

    out_segs = []
    for i, j, txt in parsed:
        grp = W[i:j + 1]
        st = next((w.get("start") for w in grp
                   if isinstance(w.get("start"), (int, float))), None)
        en = next((w.get("end") for w in reversed(grp)
                   if isinstance(w.get("end"), (int, float))), None)
        if st is None or en is None:
            continue
        out_segs.append({"start": float(st), "end": float(en),
                         "text": txt, "words": grp})
    if len(out_segs) < 2:
        return segs
    logger.info("[LLM-SEGMENT] re-segmented %d whisperX segs → %d clean lines "
                "(coverage %.2f)", len(segs), len(out_segs), coverage)
    return out_segs


def _recording_diverges(segs: list[dict], canonical: str,
                        ratio: float = 1.25) -> bool:
    """True when the recording sings MEANINGFULLY MORE than the studio lyric
    (live/extended: repeated verses, ad-libs, long outros).

    Gate for letting LLM line-segmentation PREEMPT the canonical-recovery
    cascade (forced_align / whisper-align) after `reconcile` aborts. Reconcile
    also aborts on plain whisperX MISHEARS of a studio song (incident "Viejas
    Locas — 638"), and for those FA must win because it recovers the canonical
    TEXT — LLM-segment only re-groups whisperX's own (misheard) words and never
    compares against `canonical`, so it would ship the mishears. We therefore
    only declare divergence when the transcribed word count clearly exceeds the
    canonical's by `ratio` (a true live has the extra content; a misheard studio
    song has ~the same count). Returns False when there is no canonical text."""
    canon_n = len((canonical or "").split())
    if canon_n <= 0:
        return False
    # Count words from text; fall back to the word stream when a seg has no
    # `text` yet (whisperX segs can carry `words` with an empty `text`).
    wx_n = sum(len((s.get("text") or "").split()) or len(s.get("words") or [])
               for s in (segs or []))
    return wx_n >= ratio * canon_n


def _env_float(name: str, default: float) -> float:
    """Read env var `name` as a float, falling back to `default` on unset or
    parse error. Used for request-time tuning knobs (no redeploy)."""
    try:
        v = os.environ.get(name, "").strip()
        return float(v) if v else default
    except (TypeError, ValueError):
        return default


def _word_containment(snippet: str, reference: str) -> float:
    """Fraction of `snippet`'s content words (accents stripped, stop-words
    dropped) that also appear in `reference`. Unlike Jaccard overlap, this does
    NOT penalise a short snippet against a long reference — the right gate for
    "are these few recovered words real vocabulary from this song?". A total
    hallucination scores low; a real (even repeated) line scores high (the loop
    case is caught separately by the repeat guard). Returns 0..1."""
    import unicodedata

    def words(s: str) -> list[str]:
        s = unicodedata.normalize("NFKD", s or "")
        s = "".join(c for c in s if not unicodedata.combining(c))
        toks = re.findall(r"[a-z0-9]+", s.lower())
        stop = {"el", "la", "los", "las", "de", "del", "y", "o", "a", "en",
                "que", "se", "un", "una", "es", "con", "por", "para", "no", "si"}
        return [t for t in toks if t not in stop and len(t) >= 2]

    snip = words(snippet)
    ref = set(words(reference))
    if not snip or not ref:
        return 0.0
    return sum(1 for t in snip if t in ref) / len(snip)


# Test/local seam: a callable (clip, sr, c0, lines) -> [stamp,…] | None. When
# set (e.g. to a whisperX-align backend in the lab), it overrides the hosted
# aligner below. Production leaves it None and uses the Replicate force-aligner.
_GAP_CLIP_ALIGNER = None


def _gap_words_to_lines(stamps: list[dict], lines: list[str]):
    """Map flat forced-aligned word `stamps` onto the recovered `lines` by token
    count, in order, so each recovered line gets REAL start/end + per-word timing
    instead of a uniform guess. Returns one seg-dict per line, or None when the
    stamps don't line up with the text (→ caller keeps uniform timing).

    The aligner is fed the SAME lyric text, so it returns ~one stamp per token;
    we keep the line's own (Gemini) spelling and take only the timing from the
    stamps. Recovered words carry a real score (so beat_snap leaves their
    now-accurate timing alone) and provenance='gap-recovery'."""
    total = sum(len(l.split()) for l in lines)
    if not stamps or total <= 0 or len(stamps) < total - 2:  # small slack
        return None
    out, wi = [], 0
    for txt in lines:
        toks = txt.split()
        grp = stamps[wi:wi + len(toks)]
        wi += len(toks)
        if not grp:
            return None
        st = grp[0].get("start")
        en = grp[-1].get("end")
        if not isinstance(st, (int, float)) or not isinstance(en, (int, float)):
            return None
        words = []
        for k, tok in enumerate(toks):
            s = grp[k] if k < len(grp) else grp[-1]
            ws, we = s.get("start"), s.get("end")
            words.append({
                "word": tok,
                "start": float(ws if isinstance(ws, (int, float)) else st),
                "end": float(we if isinstance(we, (int, float)) else en),
                "score": float(s.get("score") if isinstance(s.get("score"),
                                                            (int, float)) else 0.6),
                "provenance": "gap-recovery",
            })
        out.append({"start": float(st), "end": float(en), "text": txt,
                    "words": words, "provenance": "gap-recovery"})
    return out


def _align_words_in_clip(clip, sr: int, c0: float, lines: list[str],
                         timeout_s: int = 60):
    """Forced-align the recovered `lines` to the audio `clip` → REAL absolute
    word stamps [{word,start,end,score}], or None (caller keeps uniform timing).

    Gated by GAP_RECOVERY_ALIGN_ENABLED. Never raises. Default backend is the
    hosted cureau force-aligner (the same one the canonical cascade uses), run
    on the SHORT clip; clip-relative stamps are shifted back by `c0` to absolute
    song time. A `_GAP_CLIP_ALIGNER` hook overrides it for the lab/tests."""
    if not _env_flag("GAP_RECOVERY_ALIGN_ENABLED"):
        return None
    try:
        if _GAP_CLIP_ALIGNER is not None:
            return _GAP_CLIP_ALIGNER(clip, sr, c0, lines)
        import forced_align as _fa
        if not _fa.is_enabled():
            return None
        import tempfile
        import soundfile as sf
        tmp = None
        handles: list = []
        try:
            with tempfile.NamedTemporaryFile(suffix=".wav", delete=False) as tf:
                sf.write(tf.name, clip, sr, format="WAV")
                tmp = tf.name

            def factory():
                f = open(tmp, "rb")
                handles.append(f)
                return {"audio_file": f, "transcript": "\n".join(lines),
                        "show_probabilities": True}
            out = _fa._call_with_budget(
                _fa._MODEL, factory,
                total_budget_s=float(timeout_s), backoff=[0, 5])
        finally:
            for h in handles:
                try:
                    h.close()
                except Exception:
                    pass
            if tmp:
                try:
                    os.unlink(tmp)
                except OSError:
                    pass
        ws = ((out.get("wordstamps") or out.get("words"))
              if isinstance(out, dict) else out)
        if not ws:
            return None
        stamps = []
        for w in ws:
            s, e = w.get("start"), w.get("end")
            if isinstance(s, (int, float)) and isinstance(e, (int, float)):
                sc = w.get("score")
                if not isinstance(sc, (int, float)):
                    sc = w.get("probability")
                stamps.append({
                    "word": w.get("word") or w.get("text") or "",
                    "start": float(s) + c0, "end": float(e) + c0,
                    "score": float(sc) if isinstance(sc, (int, float)) else 0.6,
                })
        return stamps or None
    except Exception as e:
        logger.warning("[GAP-ALIGN] failed (%s); uniform timing", e)
        return None


# Test seam for the acoustic-twin finder: (segs, gs, ge, Csync, beat_t) ->
# (src_segs, cost) | None. Production leaves it None and uses chroma DTW below.
_GAP_TWIN_FINDER = None


def _gap_warp_segments(src_segs: list[dict], gap_start: float,
                       gap_end: float) -> list[dict] | None:
    """Warp a run of transcribed source segments into [gap_start, gap_end] by
    linear time-scaling (a repeated chorus shares tempo). Keeps the source TEXT
    (clean lead lyrics) and only re-times. Pure / unit-testable. Returns
    seg dicts tagged provenance='gap-transplant', or None."""
    if not src_segs:
        return None
    ss0 = src_segs[0].get("start")
    ss1 = src_segs[-1].get("end")
    if not isinstance(ss0, (int, float)) or not isinstance(ss1, (int, float)):
        return None
    span = ss1 - ss0
    if span <= 0 or gap_end <= gap_start:
        return None

    def warp(t):
        # Clamp into the gap so a source word slightly outside [ss0,ss1] can't
        # land on a neighbouring real segment after merge/sort.
        return min(max(gap_start + (t - ss0) / span * (gap_end - gap_start),
                       gap_start), gap_end)

    out = []
    for sg in src_segs:
        words = []
        for w in sg.get("words", []):
            if not isinstance(w.get("start"), (int, float)) \
                    or not isinstance(w.get("end"), (int, float)):
                continue
            if w["end"] <= w["start"]:
                continue            # skip degenerate (zero-duration) source words
            ws = float(warp(w["start"]))
            we = max(float(warp(w["end"])), ws)   # enforce end >= start
            words.append({"word": w.get("word", ""), "start": ws, "end": we,
                          "score": 0.45, "provenance": "gap-transplant"})
        if not words:
            continue
        sgs = float(warp(sg["start"]))
        out.append({"start": sgs, "end": max(float(warp(sg["end"])), sgs),
                    "text": (sg.get("text") or "").strip(),
                    "words": words, "provenance": "gap-transplant"})
    return out or None


def _find_gap_twin(segs, gs, ge, Csync, beat_t, max_cost):
    """Single subsequence-DTW (gap query vs the whole song): the earlier,
    already-transcribed region most acoustically similar to the gap (typically
    the chorus the lead sang clean). Returns (src_segs, cost) | None.

    One O(L·beats) DTW call instead of a DTW per window — validated to find the
    same twins as the per-window scan, far cheaper on long recordings."""
    nb = Csync.shape[1]
    hb = np.where((beat_t >= gs) & (beat_t <= ge))[0]
    if len(hb) < 3:
        return None
    Q = Csync[:, hb[0]:hb[-1] + 1]
    L = Q.shape[1]
    if L < 3 or L >= nb:
        return None
    D, _ = librosa.sequence.dtw(X=Q, Y=Csync, metric="cosine", subseq=True)
    costs = np.array(D[-1, :], dtype=float) / L   # per-step cost of ending at j
    # Mask end-beats whose ~L-beat window overlaps the gap itself.
    for j in range(nb):
        if not (beat_t[j] < gs or beat_t[max(j - L + 1, 0)] > ge):
            costs[j] = np.inf
    # Take the best-cost window that actually CONTAINS transcribed segments —
    # iterate in cost order so an empty (untranscribed) best window can't
    # silently shadow a slightly-worse window that has lyrics to transplant.
    for j in np.argsort(costs):
        if not np.isfinite(costs[j]) or costs[j] > max_cost:
            break
        t0, t1 = beat_t[max(j - L + 1, 0)], beat_t[j]
        src = [sg for sg in segs
               if sg.get("words") and t0 <= sg.get("start", -1) <= t1]
        if src:
            return src, float(costs[j])
    return None


def _transplant_gap(segs, gs, ge, Csync, beat_t, y=None, sr=22050,
                    voice_th=None, *, max_cost=0.12, min_words=3, canonical=""):
    """Fill a gap by transplanting its acoustic TWIN's lyrics (earlier clean
    chorus) warped to the gap's timeframe — clean words + real placement, no ASR
    of the crowd. The validated heart of the combined pipeline. Self-declining
    (returns None on weak match / too few words / low vocabulary overlap / any
    error); the caller then falls through to the Gemini re-transcription path."""
    try:
        # Vocal-presence gate: never transplant lyrics over an INSTRUMENTAL gap
        # (a solo's chord loop can match the chorus's chroma). `y` is the vocal
        # stem, so a gap with little voiced energy is instrumental/silence →
        # decline. Cheap (RMS) and it also skips the DTW on instrumental gaps.
        if y is not None and voice_th is not None and ge > gs:
            seg = y[int(gs * sr):int(ge * sr)]
            if len(seg):
                fl = max(int(0.05 * sr), 1)
                rms = librosa.feature.rms(y=seg, frame_length=fl,
                                          hop_length=fl)[0]
                if float(np.mean(rms > voice_th)) < 0.35:
                    logger.info("[GAP-TRANSPLANT] gap %.0f–%.0f not voiced "
                                "(instrumental?) — declining", gs, ge)
                    return None
        found = (_GAP_TWIN_FINDER(segs, gs, ge, Csync, beat_t)
                 if _GAP_TWIN_FINDER is not None
                 else _find_gap_twin(segs, gs, ge, Csync, beat_t, max_cost))
        if not found:
            return None
        src, cost = found
        if sum(len(sg.get("words", [])) for sg in src) < min_words:
            return None
        out = _gap_warp_segments(src, gs, ge)
        if not out:
            return None
        # Smear guard: a faithful chorus repeat packs words densely. If we'd be
        # stretching few words across a long gap (e.g. an 84 s outro that is
        # really screams + silence), the linear warp would smear lyrics over
        # non-vocal audio — decline and let the bounded Gemini path handle it.
        n_w = sum(len(s.get("words", [])) for s in out)
        if n_w == 0 or (ge - gs) / n_w > _env_float("GAP_TRANSPLANT_MAX_WORD_S", 1.2):
            logger.info("[GAP-TRANSPLANT] gap %.0f–%.0f too sparse "
                        "(%.1fs/word) — declining (smear guard)", gs, ge,
                        (ge - gs) / max(n_w, 1))
            return None
        ref = canonical or " ".join((w.get("word") or "")
                                    for sg in segs for w in sg.get("words", []))
        if _word_containment(" ".join(s["text"] for s in out), ref) < 0.5:
            return None
        logger.info("[GAP-TRANSPLANT] gap %.0f–%.0f ← acoustic twin "
                    "(cost %.3f, %d line(s))", gs, ge, cost, len(out))
        return out
    except Exception as e:
        logger.warning("[GAP-TRANSPLANT] failed (%s); falling back", e)
        return None


def _recover_gap_lyrics(segs: list[dict], *, audio_path: str, artist: str = "",
                        song: str = "", canonical: str = "",
                        timeout_s: int = 60) -> list[dict]:
    """Recover lyrics whisperX DROPPED inside large gaps, by re-transcribing a
    SHORT, BOUNDED clip at the start of each gap.

    Why bounded+short (the core finding): a LONG clip makes Gemini pattern-
    complete the chorus into a mechanical loop — lab on "Nada Fue Un Error (En
    Vivo)": an 84 s outro clip returned "Nada fue un error / Nada de esto fue"
    x84, a pure hallucination over what is actually screams + a held "¡papá!"
    (which Rotor itself only LABELS as "(grito)"). The SAME audio cut to ~8 s
    returns the real 1–2 lines, no loop. So we recover ONLY the first voiced run
    after a gap (the part adjacent to real lyrics) and STOP at the first
    sustained silence — we never try to fill the whole gap. Screams/instrumental
    stay untranscribed (as today), to be labelled by the operator.

    Timing for recovered lines is APPROXIMATE (distributed across the detected
    voiced run) — gap regions have no whisperX word timing by definition. Every
    whisperX-timed line is left byte-identical; recovered words carry
    provenance="gap-recovery" + low score so downstream can tell them apart.

    Self-declining (returns INPUT unchanged) on: flag off, short/again-less word
    stream, no qualifying gap, stem unreadable, librosa/Gemini failure,
    unparseable / looping / low-overlap output. Never raises. Gated behind
    GAP_RECOVERY_ENABLED (default off)."""
    if not _env_flag("GAP_RECOVERY_ENABLED"):
        return segs
    try:
        W: list[dict] = []
        for s in (segs or []):
            ws = s.get("words")
            if not isinstance(ws, list):
                return segs  # no word timing → can't locate gaps
            for w in ws:
                if (isinstance(w, dict)
                        and isinstance(w.get("start"), (int, float))
                        and isinstance(w.get("end"), (int, float))):
                    W.append(w)
        if len(W) < 8 or not audio_path or not os.path.exists(audio_path):
            return segs
        W.sort(key=lambda w: w["start"])

        GAP_MIN = _env_float("GAP_RECOVERY_MIN_GAP", 8.0)
        CLIP_MAX = _env_float("GAP_RECOVERY_CLIP_MAX", 10.0)
        MAX_GAPS = int(_env_float("GAP_RECOVERY_MAX_GAPS", 4))
        MAX_LINES = int(_env_float("GAP_RECOVERY_MAX_LINES", 6))
        MIN_OVERLAP = _env_float("GAP_RECOVERY_MIN_OVERLAP", 0.45)

        import soundfile as sf
        import io
        from google import genai
        client = _get_genai_client()
        if client is None:
            return segs

        sr = 22050
        y, _sr = librosa.load(audio_path, sr=sr, mono=True)
        audio_end = len(y) / sr

        # Gaps = silences between consecutive words PLUS a trailing gap from the
        # last word to end-of-audio. The trailing one matters because an
        # upstream cleaner (LLM-segment / hallucination filter) may DROP the
        # outro shouts, so the dropped lyrics are no longer "between" two words
        # — they sit past the last surviving word.
        gaps = [(W[i]["end"], W[i + 1]["start"]) for i in range(len(W) - 1)
                if W[i + 1]["start"] - W[i]["end"] >= GAP_MIN]
        if audio_end - W[-1]["end"] >= GAP_MIN:
            gaps.append((W[-1]["end"], audio_end))
        if not gaps:
            return segs

        # Reference voiced loudness: median RMS of the stem's louder frames
        # (robust to the long quiet tails). Voiced threshold = 10% of it.
        fl = int(0.25 * sr)
        rms_all = librosa.feature.rms(y=y, frame_length=fl, hop_length=fl)[0]
        loud = rms_all[rms_all > np.percentile(rms_all, 60)]
        ref = float(np.median(loud)) if loud.size else 1e-6
        voice_th = 0.10 * max(ref, 1e-6)
        SIL_END = 0.8  # a silence this long ends the voiced run

        ref_text = canonical or " ".join((w.get("word") or "") for w in W)
        recovered: list[dict] = []
        n_aligned = 0
        n_transplant = 0

        # Combined-pipeline Stage 3 (GAP_RECOVERY_TRANSPLANT_ENABLED, default
        # off): precompute beat-synchronous chroma once so each gap can look for
        # its acoustic twin (an earlier chorus the lead sang clean). Heavy, so
        # only when the flag is on; any failure disables it (→ Gemini path).
        _tx_on = _env_flag("GAP_RECOVERY_TRANSPLANT_ENABLED")
        _Csync = _beat_t = None
        if _tx_on:
            try:
                _ch = librosa.feature.chroma_cqt(y=y, sr=sr, hop_length=512)
                _, _bts = librosa.beat.beat_track(y=y, sr=sr, hop_length=512)
                _beat_t = librosa.frames_to_time(_bts, sr=sr, hop_length=512)
                _Csync = librosa.util.sync(_ch, _bts, aggregate=np.median)
                _nb = min(_Csync.shape[1], len(_beat_t))
                _Csync, _beat_t = _Csync[:, :_nb], _beat_t[:_nb]
                if _nb < 8:        # too few beats (beatless/rubato) → can't match
                    _tx_on = False
            except Exception as _e:
                logger.warning("[GAP-TRANSPLANT] chroma setup failed (%s)", _e)
                _tx_on = False

        for (gs, ge) in gaps[:MAX_GAPS]:
            # Stage 3: try transplanting the gap's acoustic twin BEFORE a blind
            # Gemini re-transcription. Self-declines → falls through below.
            if _tx_on and _Csync is not None:
                _tx = _transplant_gap(
                    segs, gs, ge, _Csync, _beat_t, y, sr, voice_th,
                    max_cost=_env_float("GAP_TRANSPLANT_MAX_COST", 0.12),
                    canonical=canonical)
                if _tx:
                    recovered.extend(_tx)
                    n_transplant += len(_tx)
                    continue
            # Walk frames from the gap start; capture the FIRST voiced run,
            # ending it at the first sustained silence (or the clip cap).
            run_start = run_end = None
            sil = 0
            tt = gs
            cap = min(ge, gs + CLIP_MAX + 2.0)
            while tt < cap:
                seg = y[int(tt * sr):int((tt + 0.25) * sr)]
                rms = float(np.sqrt(np.mean(seg ** 2))) if len(seg) else 0.0
                if rms > voice_th:
                    if run_start is None:
                        run_start = tt
                    run_end = tt + 0.25
                    sil = 0
                elif run_start is not None:
                    sil += 1
                    if sil * 0.25 >= SIL_END:
                        break
                tt += 0.25
            if run_start is None or run_end is None or (run_end - run_start) < 1.5:
                continue
            c0 = max(gs, run_start - 0.3)
            c1 = min(c0 + CLIP_MAX, run_end + 0.3, ge)
            if c1 - c0 < 1.5:
                continue

            clip = y[int(c0 * sr):int(c1 * sr)]
            buf = io.BytesIO()
            sf.write(buf, clip, sr, format="WAV")
            sysp = (
                "Sos un transcriptor experto de lyric videos en español, nivel "
                "Rotor.\n"
                f"Te doy un FRAGMENTO CORTO de audio ({c1 - c0:.0f} s) de un vivo"
                + (f" ({artist} — {song})" if artist else "") + ".\n"
                "Transcribí EXACTAMENTE lo que se canta en ESTE fragmento corto.\n\n"
                "REGLAS:\n"
                "1. SOLO lo que escuchás en estos pocos segundos — son 1 o 2 "
                "frases. NO repitas en loop, NO completes el estribillo entero.\n"
                "2. Si es grito/instrumental/silencio, etiquetá: "
                "(grito)/(instrumental)/(silencio).\n"
                "3. Una frase por línea, mayúscula al inicio.\n"
                + (f"- Letra oficial (ortografía, NO forzar): {ref_text}\n"
                   if canonical else "")
                + "FORMATO por línea, sin nada más: texto"
            )
            try:
                resp = _call_with_timeout(
                    lambda: client.models.generate_content(
                        model="gemini-2.5-flash",
                        contents=[
                            genai.types.Part.from_bytes(
                                data=buf.getvalue(), mime_type="audio/wav"),
                            genai.types.Part.from_text(
                                text="Transcribí este fragmento corto."),
                        ],
                        config=genai.types.GenerateContentConfig(
                            system_instruction=sysp, temperature=0.1,
                            max_output_tokens=400,
                            thinking_config=genai.types.ThinkingConfig(
                                thinking_budget=0),
                        ),
                    ),
                    timeout_s=float(timeout_s), label="GAP-RECOVER",
                )
                out = (resp.text or "").strip()
            except Exception as e:
                logger.warning("[GAP-RECOVER] Gemini failed on gap %.0f–%.0f: %s",
                               gs, ge, e)
                continue

            lines = [re.sub(r"^\s*\[\d+(?:\.\d+)?\]\s*", "", ln).strip()
                     for ln in out.splitlines() if ln.strip()]
            lines = [l for l in lines if l]
            if not lines or len(lines) > MAX_LINES:
                continue
            # Loop guard: any phrase repeated ≥3× is the hallucination tell.
            if any(lines.count(x) >= 3 for x in lines):
                logger.info("[GAP-RECOVER] loop detected in gap %.0f–%.0f; skip",
                            gs, ge)
                continue
            # v1: keep only LYRIC lines (drop pure (label) / ¡shout! lines —
            # non-lyrical labelling is a separate feature).
            lyric = [l for l in lines if not re.match(r"^\s*[\(¡]", l)]
            if not lyric:
                continue
            if _word_containment(" ".join(lyric), ref_text) < MIN_OVERLAP:
                logger.info("[GAP-RECOVER] low containment in gap %.0f–%.0f "
                            "(hallucination guard); skip", gs, ge)
                continue

            # Path 1: REAL per-word timing via forced-align of the recovered
            # text against the clip (GAP_RECOVERY_ALIGN_ENABLED). Lab on "Nada":
            # cut the recovered-line timing error 1.06s → 0.27s vs the uniform
            # split below. Falls back to the uniform split when the aligner is
            # off / unavailable / the stamps don't line up with the text.
            stamps = _align_words_in_clip(clip, sr, c0, lyric)
            aligned = _gap_words_to_lines(stamps, lyric) if stamps else None
            if aligned:
                recovered.extend(aligned)
                n_aligned += len(aligned)
            else:
                span = (run_end - run_start) / len(lyric)
                for li, txt in enumerate(lyric):
                    st = run_start + li * span
                    en = run_start + (li + 1) * span
                    toks = txt.split()
                    wsp = (en - st) / max(1, len(toks))
                    words = [{"word": tok, "start": st + wi * wsp,
                              "end": st + (wi + 1) * wsp, "score": 0.3,
                              "provenance": "gap-recovery"}
                             for wi, tok in enumerate(toks)]
                    recovered.append({"start": float(st), "end": float(en),
                                      "text": txt, "words": words,
                                      "provenance": "gap-recovery"})

        if not recovered:
            return segs
        merged = list(segs) + recovered
        merged.sort(key=lambda s: s.get("start", 0.0))
        logger.info("[GAP-RECOVER] recovered %d line(s) across %d gap(s) "
                    "(%d transplanted, %d forced-aligned, %d uniform; tagged)",
                    len(recovered), min(len(gaps), MAX_GAPS), n_transplant,
                    n_aligned, len(recovered) - n_aligned - n_transplant)
        return merged
    except Exception as e:
        logger.warning("[GAP-RECOVER] failed (%s); keeping segments", e)
        return segs


def _sanitize_gemini_lyrics(text):
    """Strip section/pilcrow markers that some Spanish lyrics sites use
    as estrofa separators (Letras.com, AZLyrics, etc.). These are HTML
    structure artifacts from scraping — they never appear in the actual
    sung lyrics.

    Why this matters: the cleaned text is used in two downstream paths
    that both fail when these chars leak through:
      1. Cached into lyrics_cache.lyrics — the row gets returned to all
         future callers including the lyrics_hint primer for Whisper.
      2. Passed as Whisper's `prompt` parameter — when the prompt
         contains `§`, Whisper biases toward emitting `§` in its own
         transcription output, which then lands in jobs.segments_json
         and renders as visible text in the lyric video (root cause
         of the Mujer Amante / Rata Blanca incident, 2026-05-12).

    Strictly conservative: removes only U+00A7 SECTION SIGN and U+00B6
    PILCROW. Diacritics, em-dashes, Spanish quotes, and every other
    char that legitimately appears in lyrics are preserved.
    """
    if not text:
        return text
    cleaned = text.replace("§", "").replace("¶", "")
    if cleaned != text:
        stripped = len(text) - len(cleaned)
        # Logged at WARNING so the operator can see which Gemini-grounded
        # sources keep returning these chars — over time, this surfaces
        # which lyric sites are dirty and whether the sanitizer needs
        # to grow (e.g. another scraping artifact appears).
        logger.warning("[lyrics_sanitize] stripped %s char(s) (S/P) from Gemini response", stripped)
    return cleaned


def _fetch_lyrics_via_gemini_search(
    artist: str, song: str,
    job_id: str | None = None,
    db=None,
) -> str | None:
    """Fetch reference lyrics for (artist, song) via Gemini 2.5 Flash with
    the google_search grounding tool. Returns plain-text lyrics on success,
    None on cache miss + fetch failure + validation reject.

    Best-effort — never raises. The /transcribe endpoint falls through to
    lyrics.ovh when this returns None.

    Provenance: caller passes `job_id` to record an AIProvenance row keyed
    to that job (UMG audit trail). When called from /transcribe (pre-job),
    job_id=None and the LyricsCache row itself serves as the audit record
    (timestamp + source URLs + model name).
    """
    if not artist or not song:
        return None

    # Kill switch — flip to false in Railway if Gemini path misbehaves in prod.
    if not _truthy_env(os.environ.get("LYRICS_GEMINI_SEARCH_ENABLED", "true")):
        return None

    cache_key = _lyrics_cache_key(artist, song)

    # Cache lookup (Postgres — shared across the worker fleet).
    if db is not None:
        try:
            from database import LyricsCache
            row = db.query(LyricsCache).filter(
                LyricsCache.cache_key == cache_key
            ).first()
            if row and row.lyrics:
                logger.info("[LYRICS] cache hit %s (%s chars)", cache_key, len(row.lyrics))
                # Sanitize on read so existing poisoned rows (cached
                # before this fix shipped) still return clean text to
                # downstream callers without requiring a DB cleanup.
                return _sanitize_gemini_lyrics(row.lyrics)
        except Exception as e:
            logger.error("[LYRICS] cache read failed: %s", e)

    # Build Gemini call.
    from google import genai
    from google.genai import types
    from provenance import record_ai_call

    system_prompt = (
        "You are a lyrics retrieval assistant. Use the google_search tool to "
        "find the official lyrics of a song from public lyrics websites "
        "(genius.com, letras.com, azlyrics.com, lyrics.com, musixmatch.com, "
        "songmeanings.com). Return ONLY the lyrics as plain text, one line "
        "per song line. No commentary, no bracketed section headers like "
        "[Chorus] or [Verse], no translation, no annotations. "
        "If you cannot verify the lyrics from a lyrics website, respond "
        "exactly with: LYRICS_NOT_FOUND"
    )
    user_content = f'Find the lyrics for the song "{song}" by {artist}.'
    full_prompt = f"system:{system_prompt}\nuser:{user_content}"

    recorder = record_ai_call(
        job_id=job_id,
        step="lyrics_reference_fetch",
        tool_name="gemini-2.5-flash",
        tool_provider="google_vertex",
        tool_version=getattr(genai, "__version__", None),
        prompt=full_prompt,
        input_data_types=["artist_name", "song_title"],
    ) if job_id else None

    # INCIDENT 2026-05-25 audit (Crítico #3): `client.models.generate_content`
    # was called without a timeout. If Vertex is degraded, the call could
    # block indefinitely → asyncio.to_thread holds the worker thread →
    # with 10 concurrent workers, all 10 stuck on Gemini → service deadlock.
    # We wrap the call in `concurrent.futures.ThreadPoolExecutor` with a
    # hard 30 s timeout. Lyrics-fetch is best-effort; if Vertex is slow we
    # rather fall through to bare Whisper than freeze the worker.
    def _gemini_call():
        client = _get_genai_client()
        search_tool = types.Tool(google_search=types.GoogleSearch())
        return client.models.generate_content(
            model="gemini-2.5-flash",
            contents=user_content,
            config=types.GenerateContentConfig(
                system_instruction=system_prompt,
                tools=[search_tool],
                temperature=0.1,
                max_output_tokens=2000,
                thinking_config=types.ThinkingConfig(thinking_budget=0),
            ),
        )
    try:
        # Tier 4 (H3): use the fixed _call_with_timeout (shutdown(wait=False))
        # instead of an inline `with ThreadPoolExecutor`, whose __exit__ would
        # re-block on the hung orphan after a timeout — defeating the timeout.
        response = _call_with_timeout(_gemini_call, 30.0, label="gemini-lyrics-search")

        text = ""
        try:
            text = (response.text or "").strip()
        except Exception:
            text = ""

        # Sanitize ONCE here, before everything downstream sees the text.
        # Some Spanish lyrics sites (Letras.com, AZLyrics) use § as estrofa
        # separators; the Gemini scrape leaks them into our string. Without
        # this strip the text would land in lyrics_cache.lyrics AND be
        # passed as Whisper's prompt parameter, biasing transcription to
        # emit § in segments_json. See _sanitize_gemini_lyrics for context.
        text = _sanitize_gemini_lyrics(text)

        candidates = getattr(response, "candidates", None) or []
        if not candidates:
            if recorder:
                recorder.finish(response_summary="no_candidates")
            return None
        cand = candidates[0]
        finish_reason = getattr(cand, "finish_reason", None)
        finish_str = str(finish_reason) if finish_reason is not None else ""

        # Gemini blocks copyrighted recitation aggressively. Degrade silently.
        if "RECITATION" in finish_str or "SAFETY" in finish_str:
            logger.warning("[LYRICS] gemini blocked: finish_reason=%s", finish_str)
            if recorder:
                recorder.finish(response_summary=f"blocked={finish_str}")
            return None
        if not text or text.strip() == "LYRICS_NOT_FOUND":
            if recorder:
                recorder.finish(response_summary=f"empty_or_sentinel; finish={finish_str}")
            return None

        # Extract grounding sources (proves the answer was grounded, not
        # purely hallucinated from training data).
        gm = getattr(cand, "grounding_metadata", None)
        chunks = getattr(gm, "grounding_chunks", None) or []
        source_urls: list[str] = []
        source_titles: list[str] = []
        for c in chunks:
            web = getattr(c, "web", None)
            if not web:
                continue
            uri = getattr(web, "uri", None)
            title = getattr(web, "title", None)
            if uri:
                source_urls.append(uri)
            if title:
                source_titles.append(title)

        if not source_urls:
            logger.warning("[LYRICS] no grounding sources — refusing to trust ungrounded text")
            if recorder:
                recorder.finish(response_summary="no_grounding_sources")
            return None

        # Soft signal: did any grounding chunk hit a known lyric site?
        on_lyric_site = False
        haystack = " ".join(source_urls + source_titles).lower()
        for d in _LYRIC_DOMAINS:
            if d in haystack:
                on_lyric_site = True
                break
        if not on_lyric_site:
            logger.warning("[LYRICS] grounding off lyric-domain allow-list (soft warn): %s",
                           source_urls[:2])

        # Lyrics-shape validation.
        lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
        if len(lines) < 8:
            if recorder:
                recorder.finish(response_summary=f"too_few_lines={len(lines)}")
            return None
        if len(text) < 80:
            if recorder:
                recorder.finish(response_summary=f"too_short_chars={len(text)}")
            return None

        # Merged-line quality guard. Algunos sitios de letras devuelven
        # las estrofas como párrafos largos en vez de líneas separadas
        # (ej. Letras.com en flow mode). Si Gemini scrapea ese formato,
        # avg chars/line se dispara (paragraph-style ~80+ chars vs
        # lyric-style ~20-40 chars). Cuando este texto se usa como
        # `lyrics_hint` de Whisper o como reference para gap-fill, el
        # output queda mergeado de a 2-3 líneas con timestamps mal
        # distribuidos (caso real: Noches Sin Sueño Rata Blanca en
        # staging, Gemini devolvió 439 chars / 12 lines = 36.6 chars/line
        # cuando lrclib synced tenía ~30 líneas para esa canción).
        #
        # Threshold 50: lyric lines típicas de pop/rock/balada son 20-40
        # chars. 50+ es signature de merged-stanza scraping. Rechazar
        # es preferible a contaminar — el caller cae al path de Whisper
        # sin hint en vez de Whisper sesgado con merged-text.
        avg_chars_per_line = sum(len(l) for l in lines) / len(lines)
        if avg_chars_per_line > 50.0:
            logger.warning("[LYRICS] gemini output looks merged (avg %.1f chars/line over %s lines) — rejecting",
                           avg_chars_per_line, len(lines))
            if recorder:
                recorder.finish(
                    response_summary=f"rejected_merged_lines="
                                     f"{avg_chars_per_line:.1f}cpl/{len(lines)}",
                )
            return None

        # Repetition guard — Gemini hallucination loops on a single line.
        from collections import Counter
        most_common, mc_count = Counter(lines).most_common(1)[0]
        if mc_count / len(lines) > 0.4:
            if recorder:
                recorder.finish(response_summary=f"repetition={mc_count}/{len(lines)}")
            return None

        # Persist to cache.
        if db is not None:
            try:
                from database import LyricsCache
                row = db.query(LyricsCache).filter(
                    LyricsCache.cache_key == cache_key
                ).first()
                if row is None:
                    row = LyricsCache(
                        cache_key=cache_key,
                        artist=artist[:255],
                        title=song[:255],
                        lyrics=text,
                        source_urls=source_urls[:20],
                        fetched_by_model="gemini-2.5-flash",
                    )
                    db.add(row)
                    db.commit()
                # If row already exists (race), keep existing — first writer wins.
            except Exception as e:
                logger.error("[LYRICS] cache write failed: %s", e)
                try:
                    db.rollback()
                except Exception:
                    pass

        if recorder:
            try:
                summary = json.dumps({
                    "lyrics_chars": len(text),
                    "lyrics_lines": len(lines),
                    "distinct_lines": len(set(lines)),
                    "grounding_sources": source_urls[:10],
                    "grounding_titles": source_titles[:10],
                    "on_lyric_site_allowlist": on_lyric_site,
                    "finish_reason": finish_str,
                    "validation_passed": True,
                })[:2000]
            except Exception:
                summary = (f"chars={len(text)} lines={len(lines)} "
                           f"grounding={len(source_urls)}")
            recorder.finish(
                response_summary=summary,
                output_artifact=f"lyrics_cache:{cache_key}",
            )

        logger.info("[LYRICS] gemini fetched %s chars / %s lines / %s sources for %r - %r",
                    len(text), len(lines), len(source_urls), artist, song)
        return text

    except Exception as e:
        logger.error("[LYRICS] gemini search failed: %s", e)
        if recorder:
            try:
                recorder.finish(response_summary=f"error: {str(e)[:200]}")
            except Exception:
                pass
        return None


def _fetch_lyrics_from_sources(
    artist: str, song: str,
    job_id: str | None = None,
    db=None,
) -> list[str]:
    """Backward-compat wrapper used by callers that still expect list[str].

    /transcribe in main.py now calls _fetch_lyrics_via_gemini_search directly
    (it needs the parallel-with-Whisper kickoff), but this wrapper stays so
    any future caller that wants a single function call still works.
    """
    text = _fetch_lyrics_via_gemini_search(artist, song, job_id=job_id, db=db)
    return [text] if text else []


# ---------------------------------------------------------------------------
# Step 1.5 — AI Background Generation (Google Veo 3 → SD fallback)
# ---------------------------------------------------------------------------

_VERTEX_CREDENTIALS = os.environ.get(
    "GOOGLE_APPLICATION_CREDENTIALS",
    os.path.join(os.path.dirname(__file__), "vertex_credentials.json"),
)
_VERTEX_PROJECT = os.environ.get("VERTEX_PROJECT", "gen-lang-client-0900526123")
_VERTEX_LOCATION = os.environ.get("VERTEX_LOCATION", "us-central1")

# Set credentials env var for Google SDK
os.environ.setdefault("GOOGLE_APPLICATION_CREDENTIALS", _VERTEX_CREDENTIALS)

_genai_client = None


# OAuth token refresh to Google's endpoint sits INSIDE the Veo hot path:
# _veo_access_token() is called on every Veo submit, every poll iteration,
# and the download (pipeline ~6117/6224/6275), and _get_genai_client()
# refreshes once at init. google-auth's default transport applies a 120s
# per-call timeout — far too long when Railway's networking flaps: the
# worker thread hangs on the refresh, and the Veo poll's own 600s deadline
# only guards the poll HTTP call, NOT the token refresh that precedes it.
# Binding the refresh to a session with a short default timeout makes a blip
# fail fast so the pipeline can fall through to its fallback. Env-tunable.
_OAUTH_REFRESH_TIMEOUT = float(os.environ.get("VEO_OAUTH_REFRESH_TIMEOUT", "10"))


def _make_timeout_session(timeout: float):
    """A requests.Session that CAPS the per-call ``timeout`` at ``timeout``.

    We can't use setdefault: google-auth's transport ALWAYS passes an explicit
    timeout (google/auth/transport/requests.py: __call__ defaults it to
    _DEFAULT_TIMEOUT=120 and forwards it to session.request), so a setdefault
    would be a no-op and the refresh would still hang up to 120s. Instead we
    force-shorten to our bound while never EXTENDING a deliberately-shorter
    explicit timeout. requests is imported lazily (matching the rest of this
    module) to keep import cost down."""
    import requests as _req

    class _TimeoutSession(_req.Session):
        def request(self, *args, **kwargs):
            existing = kwargs.get("timeout")
            if existing is None or (isinstance(existing, (int, float)) and existing > timeout):
                kwargs["timeout"] = timeout
            return super().request(*args, **kwargs)

    return _TimeoutSession()


def _oauth_refresh_request():
    """google-auth transport Request whose HTTP calls carry a bounded timeout
    (VEO_OAUTH_REFRESH_TIMEOUT) instead of the 120s transport default."""
    from google.auth.transport.requests import Request as _AuthReq

    return _AuthReq(session=_make_timeout_session(_OAUTH_REFRESH_TIMEOUT))


def _get_genai_client():
    """Get a cached Vertex AI GenAI client.

    We pass credentials EXPLICITLY (not relying on the SDK's default
    application-default-credentials discovery) because Railway's container
    environment has been triggering "invalid_scope: Invalid OAuth scope or
    ID token audience provided" with default discovery — the SDK's auth
    chain ends up requesting an ID token instead of an OAuth2 access token,
    or hits a regional endpoint that rejects the default scope.

    Building Credentials.from_service_account_file with explicit
    cloud-platform scope gives us a normal OAuth2 access token that all
    Vertex endpoints accept. Same credentials work locally — the explicit
    binding just removes the SDK's environment guesswork.
    """
    global _genai_client
    if _genai_client is None:
        from google import genai
        from google.oauth2 import service_account

        creds_path = os.environ.get("GOOGLE_APPLICATION_CREDENTIALS", "").strip()
        logger.info("[VERTEX] google-genai version: %s", genai.__version__)
        logger.info("[VERTEX] project=%s location=%s", _VERTEX_PROJECT, _VERTEX_LOCATION)
        logger.info("[VERTEX] credentials path: %s", creds_path)
        logger.info("[VERTEX] credentials exists: %s", os.path.exists(creds_path))

        client_kwargs = dict(
            vertexai=True,
            project=_VERTEX_PROJECT,
            location=_VERTEX_LOCATION,
        )
        if creds_path and os.path.exists(creds_path):
            try:
                credentials = service_account.Credentials.from_service_account_file(
                    creds_path,
                    scopes=["https://www.googleapis.com/auth/cloud-platform"],
                )
                # Bind the quota project explicitly. Some Vertex AI endpoints
                # (Veo specifically) reject token requests when quota project
                # is ambiguous, surfacing as "invalid_scope: Invalid OAuth
                # scope or ID token audience provided."
                credentials = credentials.with_quota_project(_VERTEX_PROJECT)

                # Validate the token at startup so we surface auth issues
                # here in the worker logs instead of inside the model call.
                try:
                    credentials.refresh(_oauth_refresh_request())
                    logger.info("[VERTEX] token refresh OK; valid=%s expiry=%s",
                                credentials.valid, credentials.expiry)
                except Exception as refresh_err:
                    logger.error("[VERTEX] token refresh FAILED: %s", refresh_err)

                client_kwargs["credentials"] = credentials
                logger.info("[VERTEX] using explicit service account credentials (%s, quota_project=%s)",
                            credentials.service_account_email, _VERTEX_PROJECT)
            except Exception as e:
                logger.warning("[VERTEX] failed to load explicit credentials (%s); "
                               "falling back to ADC discovery", e)

        _genai_client = genai.Client(**client_kwargs)
    return _genai_client

# Combinatorial prompt system — elements combine to create unique prompts.
# 21 scenes x 12 palettes x 10 cameras x 8 conditions = 20,160 combinations
_BG_SCENES = [
    "calm ocean waves on a sandy beach",
    "northern lights aurora over a mountain lake",
    "abstract colorful smoke swirling slowly",
    "sunset clouds forming and dissolving",
    "underwater light rays through deep blue ocean",
    "tropical coral reef with colorful fish",
    "rolling fog over a green mountain valley",
    "lavender field stretching to the horizon",
    "gentle rain falling on a still lake",
    "desert sand dunes with wind patterns",
    "autumn leaves falling in a forest",
    "snow falling gently over pine trees",
    "bioluminescent waves crashing on dark shore",
    "cherry blossom petals floating in the wind",
    "crystal clear river flowing over smooth rocks",
    "volcanic lava flowing slowly into the ocean",
    "stars and milky way rotating over a landscape",
    "tropical waterfall cascading into a lagoon",
    "wildflowers swaying in a meadow breeze",
    "hot air balloon shadows over green countryside",
    "lightning illuminating storm clouds from within",
]

_BG_PALETTES = [
    "golden hour warm tones",
    "cool blue and teal tones",
    "vibrant pink and purple sunset",
    "soft pastel colors",
    "deep navy and silver moonlight",
    "warm amber and orange",
    "vivid turquoise and coral",
    "moody indigo and violet",
    "bright green and emerald",
    "rose gold and blush pink",
    "fiery red and orange",
    "icy blue and white",
]

# Camera registers for the combinatorial fallback (used only when Gemini
# fails to return a usable prompt). Split into MOTION and STATIC so the
# fallback can honour movement intent instead of always reintroducing a
# camera move (the old list was 10/10 motion → every fallback drifted).
# Legibility cap (UMG 2026-05-21): the lyrics are overlaid, so forward
# travel (dolly forward, push-in, flyover, first-person glide) makes them
# nauseating to read — the MOTION pool is now lateral / orbit / vertical /
# parallax only, never advancing toward the subject.
_BG_CAMERAS_MOTION = [
    "gentle sideways tracking shot",
    "slow upward crane shot",
    "slow orbit around the scene",
    "smooth descending aerial shot",
    "slow parallax movement",
    "slow lateral drift across the scene",
    "gentle vertical crane reveal",
    "slow arc around the subject",
]
_BG_CAMERAS_STATIC = [
    "locked static wide shot on a tripod, no camera movement",
    "fixed tripod composition, the camera does not move",
    "held static frame, motion only within the scene",
]
# Back-compat alias: anything still reading _BG_CAMERAS gets the full pool.
_BG_CAMERAS = _BG_CAMERAS_MOTION + _BG_CAMERAS_STATIC

_BG_CONDITIONS = [
    "cinematic depth of field",
    "soft natural lighting",
    "volumetric light rays",
    "misty atmospheric haze",
    "crystal clear vivid detail",
    "dreamy soft focus bokeh",
    "dramatic rim lighting",
    "ethereal glow",
]

_USED_PROMPTS_FILE = os.path.join(ASSETS_DIR, ".used_prompts.json")


_GENRE_SCENE_GUIDE = {
    "rock": (
        "High-energy dramatic scenes — concert stage lights cutting through "
        "smoke and laser beams (no people), stormy desert highway at dusk, "
        "mountain peaks during a lightning storm, vintage analog amplifiers "
        "and electric guitars in close-up (no hands), empty arena tunnels "
        "with shafts of light, raw weather over open plains, dramatic "
        "chiaroscuro on raw concrete or asphalt textures. Vary the setting "
        "per song — alleyways and narrow streets are ONE option among many, "
        "NOT the default."
    ),
    "pop": (
        "Vibrant colorful neon lights, disco reflections, glittering city "
        "nightlife, abstract liquid color, mirrored prisms, geometric light "
        "patterns, energetic confetti motion, glossy gradient skies."
    ),
    "ballad": (
        "Soft sunset over calm ocean, slow drifting clouds, warm golden light "
        "through trees, candlelight macro, gentle rain on a window, "
        "single rose-gold reflections, pastel mountain mist."
    ),
    "latin": (
        "Tropical beach at golden hour, palm trees swaying, vibrant flower "
        "fields, salsa-club neon reds and yellows, sunlit Caribbean water, "
        "colorful murals motion-blurred, festive lantern strings."
    ),
    "reggaeton": (
        "Night cityscape with red and pink neon, palm-lined boulevards, "
        "luxury car reflections, abstract gold dust, velvet-textured colors, "
        "club laser patterns, vibrant rooftop lights."
    ),
    "hiphop": (
        "City skyline at night with gold accents, abstract luxury textures, "
        "marble and gold reflections, smoke-filled spotlights, rain on dark "
        "limousine paint, urban rooftop with skyline below."
    ),
    "electronic": (
        "Abstract glowing geometry, particle storms, fractal liquid metal, "
        "deep space nebulas, laser grid landscapes, holographic surfaces, "
        "cymatic patterns in colored ink."
    ),
    "indie": (
        "Misty forest at dawn, quiet vintage interiors with warm lamps, "
        "open road through autumn leaves, lone lighthouse on a cliff, soft "
        "film grain, dreamy lake reflections, hand-held cinematic frames."
    ),
    "folk": (
        "Mountain vistas at golden hour, dusty roads with sun flares, fields "
        "of wheat moving in wind, riverside campfire glow, weathered wood "
        "textures, sun rays through forest canopies."
    ),
    "metal": (
        "Volcanic landscapes with lava streams, dark cathedral interiors, "
        "stormy thunderclouds with lightning, cracked obsidian textures, "
        "burning pyres at dusk, abandoned iron mills."
    ),
}


def _normalize_genre(g: str) -> str:
    """Map free-text or UI selection to a key in _GENRE_SCENE_GUIDE."""
    if not g:
        return ""
    g = g.strip().lower()
    aliases = {
        "rock/punk": "rock", "punk": "rock", "alt rock": "rock",
        "pop/dance": "pop", "dance": "pop", "edm": "electronic",
        "house": "electronic", "techno": "electronic",
        "ballad/romantic": "ballad", "romantic": "ballad", "balada": "ballad",
        "latin/reggaeton": "latin", "latino": "latin", "salsa": "latin",
        "cumbia": "latin", "bachata": "latin",
        "hip hop": "hiphop", "hip-hop": "hiphop", "rap": "hiphop", "trap": "hiphop",
        "indie rock": "indie", "alternative": "indie",
    }
    if g in aliases:
        return aliases[g]
    if g in _GENRE_SCENE_GUIDE:
        return g
    return ""


# Concept selector — operator-controlled visual category for the background.
# When set, this hard-overrides the genre's scene vocabulary and forces
# Gemini's prompt into the chosen category. UMG asked for it because the
# genre alone wasn't tight enough — different songs in the same genre
# need different visual registers (a Karol G ballad vs a Karol G party
# anthem are both "latin" but should not look the same).
#
# Each value is the English vocabulary Gemini will pick from. Order in
# the catalogue matches the UI dropdown order.
_CONCEPT_SCENE_GUIDE = {
    "naturaleza":   "natural outdoor landscapes — dense forests, mountain valleys, rolling hills, open fields, rivers, sunsets over horizons",
    "tropical":     "tropical scenes — palm trees, caribbean beaches, vibrant flowers, festive lanterns, sunlit turquoise water, lush jungle",
    "acuatico":     "water-centric scenes — underwater light rays, rain on glass and pavement, deep ocean, slow-motion water droplets, flowing rivers",
    "ciudad":       "city skylines — modern downtowns, skyscrapers at golden hour, aerial cityscapes, glass facades, bridges, observation decks",
    "urbano":       "gritty urban — narrow alleys, neon-lit rain-slicked streets, graffiti walls, rooftops, fire escapes, smoking vents, industrial corners",
    "industrial":   "industrial environments — factories, exposed pipes, machinery, decaying warehouses, steel beams, smokestacks, foundries",
    "abstracto":    "abstract visuals — flowing geometric shapes, fractal patterns, particle clouds, color gradients, liquid metal, kaleidoscopic motion",
    "cosmico":      "cosmic scenes — spiral galaxies, star fields, colorful nebulas, planetary surfaces, deep space, comets, supernovae",
    "atmosferico":  "atmospheric mood — drifting smoke, dense fog, volumetric light rays, dust motes, soft haze, ethereal glow",
    "romantico":    "romantic mood — warm sunsets, candlelight, scattered rose petals, soft fabric textures, calm beaches at dusk, fireplace embers",
    "vintage":      "vintage / retro — Super 8 film grain, sepia tones, faded photographs, retro patterns, analog noise, old-paper textures",
    "cinematic":    "cinematic dramatic — chiaroscuro lighting, film-noir contrast, dramatic shadows, anamorphic lens flares, moody atmosphere",
    "club":         "club / dance scene — laser beams, smoke machines, neon strips, disco balls, strobe lights, dancefloor energy (no people, no faces)",
    "lujo":         "luxury aesthetics — polished marble, gold accents, crystal facets, high-gloss surfaces, fashion textures, jewelry close-ups",
    "minimalista":  "minimalist design — clean geometric shapes, smooth gradients, solid color planes, single-subject compositions, negative space",
}


# Movement-style hints injected into the Gemini system prompt's Hard-Rules
# section. UMG referenced 3 distinct registers in their meeting; we
# surface 4 explicit options (plus Auto) so the operator can pick the
# right "feel" per song. The genre + concept selectors decide WHAT the
# scene is; this decides HOW it moves.
_MOVEMENT_STYLE_RULES = {
    "estatico":      "Camera: LOCKED STATIC TRIPOD. The camera does NOT move at all — no pan, no tilt, no zoom, no dolly, no push-in, no drift, no orbit, no crane, no handheld, no parallax. A single fixed frame held for the whole shot, like a photograph on a tripod. ALL motion lives WITHIN the scene only (water ripples, fire, drifting clouds, smoke, flickering light, swaying foliage, floating particles). The frame edges never move.",
    "sutil":         "Movement: minimal and ambient — gentle sway, slow drift, breathing motion. Subjects barely move. Easy to loop seamlessly.",
    "estandar":      "",  # no extra rule; the existing prompt template controls motion
    "foto-parallax": "Aesthetic: photographic still with subtle parallax — composition feels like a single photo, motion is restricted to slow camera moves, depth-of-field shifts, and lighting passes. No moving subjects.",
    "animado":       "Aesthetic: stylised 2D animated illustration — flat shapes, deliberate cartoon-like motion. NOT photorealistic.",
}


# Color-grading hints injected into the Gemini prompt so the operator's
# palette choice ACTUALLY steers the generated background's colors (until now
# `style` only tinted the gradient fallback, never the Veo output). "auto" /
# "" → no constraint (the scene's natural colors). Keys mirror the frontend
# STYLES codes.
_PALETTE_COLOR_HINTS = {
    "oscuro":  "deep purples, magenta, midnight blue and black — dramatic, moody color grading",
    "neon":    "an electric neon palette — magenta, cyan and violet, high-saturation glow",
    "minimal": "a neutral, understated palette — soft grays, off-whites and gentle pastels",
    "calido":  "warm earthy tones — amber, orange, terracotta and golden light",
}


def _color_directive(style: str, custom_colors: str) -> str:
    """Build the COLOR DIRECTION line for the Gemini prompt from the operator's
    palette choice. custom_colors (hex or names, comma-separated) wins; then a
    preset hint; "auto"/empty → no directive (scene-natural colors)."""
    cc = (custom_colors or "").strip()
    if cc:
        return (f"COLOR DIRECTION (important): grade the entire scene around these "
                f"dominant colors: {cc}. Lighting, atmosphere and key surfaces must "
                f"read in this palette.")
    hint = _PALETTE_COLOR_HINTS.get((style or "").strip().lower())
    if hint:
        return f"COLOR DIRECTION: lean the color palette toward {hint}."
    return ""


def _normalize_movement_style(s: str) -> str:
    """Map free-text or UI selection to a key in _MOVEMENT_STYLE_RULES.
    Returns "" for empty / unknown — caller treats that as Auto."""
    if not s:
        return ""
    s = s.strip().lower()
    aliases = {
        "static": "estatico", "estatica": "estatico", "estática": "estatico",
        "fija": "estatico", "fixed": "estatico", "tripod": "estatico",
        "locked": "estatico", "still": "estatico", "camara-fija": "estatico",
        "subtle": "sutil", "minimal": "sutil", "minimo": "sutil",
        "standard": "estandar", "default": "estandar",
        "photo": "foto-parallax", "parallax": "foto-parallax",
        "foto+parallax": "foto-parallax", "foto_parallax": "foto-parallax",
        "animated": "animado", "illustration": "animado", "cartoon": "animado",
    }
    if s in aliases:
        return aliases[s]
    if s in _MOVEMENT_STYLE_RULES:
        return s
    return ""


def _normalize_concept(c: str) -> str:
    """Map free-text or UI selection to a key in _CONCEPT_SCENE_GUIDE."""
    if not c:
        return ""
    c = c.strip().lower()
    # Common alternate spellings (operator might tab in raw or with accents).
    aliases = {
        "nature": "naturaleza", "natural": "naturaleza",
        "city": "ciudad", "downtown": "ciudad", "skyline": "ciudad",
        "urban": "urbano", "street": "urbano", "alley": "urbano",
        "tropical/beach": "tropical", "playa": "tropical", "beach": "tropical",
        "water": "acuatico", "agua": "acuatico", "underwater": "acuatico",
        "abstract": "abstracto", "geometric": "abstracto",
        "cosmic": "cosmico", "space": "cosmico", "galaxy": "cosmico",
        "atmospheric": "atmosferico", "smoke": "atmosferico", "fog": "atmosferico",
        "romantic": "romantico", "love": "romantico",
        "vintage/retro": "vintage", "retro": "vintage",
        "cinematic/film": "cinematic", "film noir": "cinematic", "noir": "cinematic",
        "club/dance": "club", "rave": "club", "neon": "club",
        "luxury": "lujo", "premium": "lujo", "fashion": "lujo",
        "minimalist": "minimalista", "minimal": "minimalista",
        "industrial/factory": "industrial", "factory": "industrial",
    }
    if c in aliases:
        return aliases[c]
    if c in _CONCEPT_SCENE_GUIDE:
        return c
    return ""


# Keywords that mark the overused "noir urban alley" cliché. Gemini has a
# strong prior: melancholic Spanish-language rock lyrics → rain-slicked
# graffiti alley at night. The system prompt instructs against it but the
# model ignores the instruction ~consistently, so we DETECT the output and
# corrective-re-roll instead of just steering with words. Shared by the
# detector and the re-roll guard in _analyze_lyrics_for_background.
_ALLEY_BIAS_KEYWORDS = (
    "alley", "callejón", "callejon", "alleyway",
    "narrow street", "back street", "back-street",
    "rain-slicked street", "rain slicked street", "wet pavement",
    "graffiti", "fire escape", "industrial corridor",
)


def _looks_like_alley(prompt: str) -> bool:
    """True if the prompt reads like the noir-urban-alley cliché.

    Uses word-boundary matching, NOT substring: a plain `"alley" in p`
    check false-positives on "valley" (mountain valley — a GOOD
    non-urban scene we must NOT re-roll). Caught by
    test_looks_like_alley_detects_cliche_keywords.
    """
    if not prompt:
        return False
    p = prompt.lower()
    return any(re.search(rf"\b{re.escape(k)}\b", p) for k in _ALLEY_BIAS_KEYWORDS)


# Hard-negative addendum appended to the Gemini system prompt on the
# corrective re-roll. Worded as a firm prohibition + a menu of concrete
# alternatives so the model has somewhere to go instead of the alley.
_ANTI_ALLEY_ADDENDUM = (
    "\n\n## HARD NEGATIVE — the previous attempt failed\n"
    "A previous attempt for this exact song wrongly defaulted to an "
    "urban alley / rain-slicked street / graffiti wall / fire escape / "
    "industrial corridor. That is the overused cliché we are explicitly "
    "rejecting. For THIS attempt you MUST choose a completely different "
    "category. Pick ONE and commit:\n"
    "  - natural landscape (forest, mountains, desert, fields, coastline)\n"
    "  - water / ocean (waves, underwater light, rain on a lake)\n"
    "  - cosmic / celestial (stars, nebula, aurora, planets)\n"
    "  - abstract / geometric (flowing color, light refraction, particles)\n"
    "  - interior space (warm room, cathedral light, empty theatre)\n"
    "  - weather / sky (storm clouds, golden hour, fog over hills)\n"
    "Absolutely NO alleys, streets, graffiti, urban night, wet pavement, "
    "fire escapes, or industrial corridors of any kind."
)


def _parse_gemini_bg_response(text: str) -> dict | None:
    """Parse Gemini's background-generation response into {style, prompt}.

    Handles three failure modes observed in prod:
      1. Bare JSON: `{"style":"video","prompt":"..."}` — happy path.
      2. Markdown-wrapped: ```json\\n{...}\\n``` — common with newer Gemini
         models that prefer fenced output.
      3. Truncated: `{"style":"video","prompt":"... mid-sentence` — when
         max_output_tokens is hit. The original parser failed here because
         `re.search(r'\\{.*?\\}')` requires a closing brace; without one,
         it returns None and the whole pipeline falls to the combinatorial
         random scene picker, ignoring concept/lyrics/hint.

    Returns the parsed dict on success, None if the response is so
    malformed that nothing usable can be extracted. The caller falls back
    to the combinatorial random only on None.
    """
    import re

    if not text:
        return None

    # Strip markdown code fences if present. Handles both ```json...``` and
    # bare ``` blocks. We only need to remove the fence markers; the JSON
    # body survives intact.
    cleaned = text.strip()
    if cleaned.startswith("```"):
        # Drop the opening fence (with optional language tag) and any
        # closing fence. Be defensive about partial fences (truncation).
        cleaned = re.sub(r"^```[a-zA-Z]*\s*\n?", "", cleaned)
        cleaned = re.sub(r"\n?```\s*$", "", cleaned)
        cleaned = cleaned.strip()

    # Stage 1: greedy match for the largest possible {...} block. Greedy
    # is intentional — the non-greedy version stops at the first inner `}`
    # which breaks on prompts containing JSON-encoded objects. The body of
    # `prompt` is a plain string, so a greedy match to the LAST `}` works.
    match = re.search(r"\{.*\}", cleaned, re.DOTALL)
    if match:
        try:
            data = json.loads(match.group())
            # Accept either complete shape or partial with at least prompt.
            if isinstance(data, dict) and ("prompt" in data or "style" in data):
                return data
        except json.JSONDecodeError:
            pass

    # Stage 2: the JSON is malformed (truncated, unescaped, etc.). Try to
    # recover the prompt field directly with a regex. We accept any quoted
    # string after `"prompt":` even if the closing quote and brace are
    # missing — better to render something coherent than fall to random.
    # Pattern: "prompt": "<content>" where <content> is everything up to
    # the next unescaped quote OR end of input. The `(?:\\.|[^"\\])*`
    # matches escaped chars (\", \\, \n) and plain non-quote chars.
    prompt_match = re.search(
        r'"prompt"\s*:\s*"((?:\\.|[^"\\])*)',
        cleaned, re.DOTALL,
    )
    if prompt_match:
        prompt_value = prompt_match.group(1)
        # Decode common JSON escapes manually since we may not have a
        # closing quote. Only handle the cheap ones; if Gemini emitted
        # exotic unicode escapes the prompt will still be usable.
        prompt_value = (prompt_value
                        .replace('\\"', '"')
                        .replace("\\n", " ")
                        .replace("\\\\", "\\")
                        .strip())
        # Only return if we recovered something substantive (≥15 chars
        # matches the existing length gate downstream).
        if len(prompt_value) >= 15:
            # Extract style too if it survived; default to "video".
            style_match = re.search(r'"style"\s*:\s*"(\w+)"', cleaned)
            style = style_match.group(1) if style_match else "video"
            logger.info("[BG] Recovered prompt from truncated JSON (raw_len=%s, prompt_len=%s)",
                        len(text), len(prompt_value))
            return {"style": style, "prompt": prompt_value}

    # Nothing recoverable.
    return None


def _analyze_lyrics_for_background(lyrics_text: str, artist: str, job_id: str = None,
                                    song_title: str = "", genre: str = "",
                                    concept: str = "",
                                    movement_style: str = "",
                                    match_lyrics: bool = True,
                                    background_hint: str | None = None,
                                    for_provider: str = "veo",
                                    style: str = "",
                                    custom_colors: str = "",
                                    allow_people: bool = False) -> dict:
    """Use Gemini to analyze lyrics and choose visual style + prompt.

    match_lyrics=True  ("Inspirado en la letra"): lyrics anchor or infuse the scene.
    match_lyrics=False: concept/genre vocabulary only, lyrics are ignored.
    background_hint: optional free-form text from the operator (set by /edit)
      describing what they want the new background to convey. Overrides
      Gemini's default interpretation when present.
    for_provider: "veo" (default) or "imagen". Adjusts the system prompt
      addendum so Gemini emits the right kind of prompt:
        - veo  → keep camera-movement / motion descriptors (text-to-video)
        - imagen → strip motion words, emphasize composition + lighting
          (text-to-image; motion words confuse Imagen-4 into frozen-action
          renders that look broken when the local Ken Burns animation
          overlays them).

    Returns dict with:
      - style: "video" | "photo" | "illustration"
      - prompt: the generation prompt for Veo 3 or Imagen 4
    """
    from google import genai
    from provenance import record_ai_call

    client = _get_genai_client()

    normalized_genre = _normalize_genre(genre)
    normalized_concept = _normalize_concept(concept)
    normalized_movement = _normalize_movement_style(movement_style)
    movement_rule = _MOVEMENT_STYLE_RULES.get(normalized_movement, "")
    movement_extra_line = f"\n- {movement_rule}" if movement_rule else ""

    # Camera-motion de-bias. Two flags drive how clause (2) and the few-shot
    # examples talk about the camera:
    #   _static       — operator picked "Estático / cámara fija": the prompt
    #                   must describe a LOCKED frame and NEVER a camera move.
    #   _auto_movement — operator left movement on Auto: instead of forcing a
    #                   cinematic drift on every song (the old monotony bug),
    #                   Gemini CHOOSES the register from the song's energy.
    # For an explicit non-static register (sutil/estandar/foto-parallax/
    # animado) the existing movement_rule steers it, so clause (2) keeps its
    # original "exact camera movement" wording.
    _static = normalized_movement == "estatico"
    _sutil = normalized_movement == "sutil"
    _auto_movement = normalized_movement == ""
    if _static:
        # C4 (2026-05-25) + UMG-style update: repetir LOCKED 3× para reforzar
        # el prior cuando estatico esté ruteado a Veo. CRÍTICO: el operador
        # NUNCA debe recibir foto 100% quieta. La escena MUST tener motion
        # rica in-scene (al menos 3 fuentes distintas) — references UMG
        # siempre tienen lluvia/nieve/humo/olas/nubes en movimiento.
        _clause2 = ("(2) framing only — wide/medium/close and angle — the camera "
                    "is LOCKED and STATIC, the camera is BOLTED in place, "
                    "explicitly NO camera movement of any kind; the frame is "
                    "FIXED. CRITICAL: motion lives WITHIN the scene and MUST "
                    "be RICH — describe AT LEAST 3 distinct motion sources, "
                    "e.g.: drifting smoke + flickering candle + dust motes; "
                    "or rolling waves + shifting clouds + falling petals; "
                    "or rain on glass + neon reflection + steam rising. "
                    "A scene with zero movement (e.g. \"empty room, no wind, "
                    "no light shift\") is INVALID — always include moving "
                    "elements")
    elif _sutil:
        # C4 (2026-05-25): branch nueva para sutil. Camera barely-breathing
        # con motion rica IN-SCENE. Distinguible de estatico (clavada) y
        # de estandar (camera se mueve).
        _clause2 = ("(2) framing — wide/medium/close and angle — and a "
                    "near-static camera that BARELY BREATHES (micro-drift "
                    "ONLY, the lens shifts no more than 5% of the frame width "
                    "over the whole shot); treat as a fixed photograph that "
                    "just breathes. Rich motion lives WITHIN the scene "
                    "(water, fire, foliage, particles, light); the camera "
                    "itself is essentially still. NEVER push-in, NEVER zoom, "
                    "NEVER dolly forward, NEVER orbit. Just a faint breath")
    elif _auto_movement:
        _clause2 = ("(2) the camera register that matches the song's energy — a "
                    "LOCKED STATIC frame for intimate/calm songs, SUBTLE minimal "
                    "motion for most, and at most a GENTLE LATERAL track, slow "
                    "orbit or parallax for genuinely high-energy tracks; NEVER a "
                    "camera that travels FORWARD toward the subject (no push-in, "
                    "dolly forward, fly-through or first-person glide) because the "
                    "lyrics are overlaid and forward motion makes them nauseating "
                    "to read; do NOT default to a constant cinematic drift — and "
                    "the framing")
    else:
        _clause2 = "(2) exact camera movement and framing"

    # The "no people" line is gated by `allow_people`. When the operator
    # opted into "fondo libre" (bypass_content_validation=True) OR the
    # tenant is non-UMG and didn't force validation, this restriction is
    # lifted at PROMPT level — Gemini will let people / faces / hands
    # through. The Veo safe_prompt suffix is gated by the same flag (see
    # `_generate_veo_video`). Pre-fix the toggle promised "fondo libre"
    # but the AI silently stripped people regardless — incident 2026-05-19
    # where art-rock prompt "woman lies upside down on armchair" rendered
    # an empty armchair.
    _people_rule = (
        "" if allow_people
        else "- Never include people, faces, hands, or readable text in the scene\n"
    )
    # A2 (2026-05-25) — Anti-cliché rule. Incidente "Legalícenla - Viejas
    # Locas": Gemini identificó correctamente "marihuana" como sujeto
    # literal y Imagen produjo cannabis-leaves-framing-sunset, textbook
    # stock-photo cringe. La regla substituye METÁFORA por LITERAL cuando
    # la letra trata temas sensibles — captura la energía sin el visual
    # cringe. Aplica transversalmente (Veo y Imagen).
    #
    # CARVE-OUT 2026-05-25: cuando el operador escribió un background_hint
    # explícito (modo "Mi prompt"), anti-cliché SE DESACTIVA. Razón: el
    # hint es la voz explícita del operador; si quiere literal, debemos
    # respetarlo (con o sin verbatim). Solo auto/inspirado-en-letra
    # (sin hint) siguen recibiendo la regla.
    _has_operator_hint = bool(background_hint and background_hint.strip())
    #
    # A3 (2026-05-25) — Editorial-photography rule, solo gated por
    # for_provider="imagen". Veo tiene sus propios equivalentes (lens,
    # grain, cinematografía) en otras reglas; Imagen no.
    _is_imagen = for_provider == "imagen"
    _imagen_quality_line = (
        "\n- EDITORIAL PHOTOGRAPHY AESTHETIC (required, Imagen path): Magnum / "
        "National Geographic / Vogue, NOT stock photo. Specific lens (e.g. 35mm "
        "f/1.4, 85mm f/1.8), natural imperfections (grain, slight focus falloff, "
        "real-world directional lighting with shadows), ASYMMETRIC composition, "
        "single clear subject — avoid centered 'product shot' framing and over-"
        "saturated color clichés (no symmetric leaves-framing-sunset, no perfectly-"
        "lined silhouettes against gradient skies, no \"AI sunset\" aesthetic)."
        "\n- TECHNICAL QUALITY (required): shot on a full-frame camera, RAZOR-SHARP "
        "focus on the subject, high dynamic range with rich tonal depth and true "
        "blacks, professional cinematic color grade, fine natural film grain, "
        "tack-sharp micro-detail and realistic textures, photographed (NOT "
        "illustrated/3D-rendered/CGI), no plastic or waxy surfaces, no over-"
        "smoothing, no HDR halos, no oversharpening artifacts, no watermark."
        if _is_imagen else ""
    )
    _PROMPT_RULES = (
        "- \"style\" must always be \"video\"\n"
        "- \"prompt\" is 80-120 words. Describe: (1) specific scene subject and setting "
        f"in detail, {_clause2}, (3) color palette and dominant "
        "tones, (4) lighting type and direction, (5) atmosphere, mood, and at least one "
        "specific texture or material detail. Be precise and cinematic — avoid vague "
        "adjectives like \"beautiful\" or \"amazing\".\n"
        "- Pick a DIFFERENT specific scene each time (don't repeat across songs)\n"
        f"{_people_rule}"
        "- When a concept and lyrics are both present, the LYRICS dictate the "
        "subject of the scene and the CONCEPT dictates its visual styling "
        "(palette, texture, atmosphere, register). Concept never replaces or "
        "contradicts the literal subject of the lyrics unless match_lyrics is "
        "explicitly disabled.\n"
        # Anti-cliché block — gated por _has_operator_hint. Cuando el operador
        # escribió un hint (modo "Mi prompt"), su voz explícita gana y la regla
        # se desactiva. Modos auto/inspirado-en-letra (sin hint) la reciben.
        + (
            ""
            if _has_operator_hint else
            "- ANTI-CLICHÉ / METAPHOR-OVER-LITERAL RULE: if the lyrics' literal subject "
            "is a SENSITIVE / POLITICAL / TABOO topic (drugs, sex, weapons, religion, "
            "politics, suicide, alcohol abuse, gang violence), DO NOT depict the subject "
            "literally — substitute a METAPHORICAL scene that captures the song's "
            "EMOTIONAL ENERGY (defiance, longing, ecstasy, melancholy, rebellion, "
            "freedom) without the on-the-nose imagery. Examples:\n"
            "  · Drug-positive anthem → smoke / haze drifting through warm window light, "
            "record-store dust motes, vinyl spinning under a single lamp — NOT plants, "
            "leaves, paraphernalia, pills, or rolled paper.\n"
            "  · Heartbreak after addiction → empty bar at dawn, single bottle on counter, "
            "cold blue light through smoke — NOT pills, needles, or hospital rooms.\n"
            "  · Political protest → marching shadows on wet pavement, banners viewed from "
            "behind, kinetic dust and torchlight — NOT readable flags, slogans, "
            "politician faces, or police uniforms.\n"
            "  · Gang / street violence → tense empty alley after rain, broken neon "
            "reflection in puddle, single dropped object — NOT guns, blood, masked "
            "figures, or threatening crowds.\n"
            "  · Sex / explicit lust → silk curtains in heat haze, candlelight on textured "
            "wall, single tangled bedsheet — NOT bodies, beds, or anatomy.\n"
            "  Goal: a viewer who DOESN'T know the song should feel its energy through "
            "the scene — without instantly clocking 'this is a [drugs/protest/violence] "
            "song'. The lyric video ACCOMPANIES the song; it does NOT narrate it."
        )
        + _imagen_quality_line
    )
    # Contrastive few-shot examples + a "do not copy verbatim" disclaimer.
    # The example SET is chosen by movement intent so the camera language the
    # model imitates matches what the operator asked for:
    #   _static       → all three examples hold a locked frame (motion in-scene)
    #   _auto_movement → one static + one subtle + one motion, so Gemini sees
    #                    the full range and varies per song instead of always
    #                    drifting (the monotony bug)
    #   explicit reg. → the original motion examples; movement_rule steers
    # Replaces the prior single example which biased Gemini toward "neon-lit
    # rain-slicked streets" whenever concept/genre came empty (prompt-bleed on
    # Rata Blanca "Mujer Amante", 2026-05-12). Genre-tone guard rail shared.
    if _static:
        _EXAMPLES_BLOCK = """Example for rock / energetic / dramatic track:
{"style":"video","prompt":"Locked static wide shot of a stormy desert highway at dusk, the camera fixed on a tripod and never moving, lightning fracturing the distant clouds, heat haze shimmering above the asphalt, a vintage road sign trembling in the wind, dust drifting across the still frame, dramatic and raw, cinematic 4k"}

Example for romantic ballad / love song:
{"style":"video","prompt":"Fixed static frame of a sunlit room at golden hour, the camera never moves, gauze curtains billowing gently, dust motes floating through the warm beam, a glass on the table catching slow glints of light, intimate and calm, cinematic 4k"}

Example for introspective acoustic / folk track:
{"style":"video","prompt":"Held static shot of a misty mountain valley at dawn on a locked tripod, layered blue and pink sky perfectly still, silhouetted pine trees motionless, low fog rolling slowly between them, a single bird crossing the far distance, contemplative and vast, cinematic 4k"}"""
    elif _sutil:
        # C4 (2026-05-25): ejemplos sutil — camera BARELY breathing, motion
        # rica in-scene. Para que Veo no confunda con estandar (movimiento
        # cinematográfico) ni con estatico (frame clavado).
        _EXAMPLES_BLOCK = """Example for rock / energetic track (sutil register — camera barely breathes, motion lives in the scene):
{"style":"video","prompt":"Near-static medium shot of an empty rock concert stage just before showtime, the camera barely breathes with imperceptible drift, smoke machines pumping thick haze across the empty platform, stage lights pulsing in red and amber sequences, a microphone stand swaying very slightly from a draft, dust drifting through the colored beams, anticipation and tension, cinematic 4k"}

Example for romantic ballad (sutil register — fixed frame with breath, rich in-scene motion):
{"style":"video","prompt":"Almost-fixed frame of a candlelit window seat at dusk, the camera barely shifts as if held by a steady hand, flames flickering in three candles, gauze curtains billowing in slow waves, golden hour light shifting subtly through translucent fabric, a wine glass catching tremulous light, intimate and warm, cinematic 4k"}

Example for introspective acoustic / folk (sutil register — near-locked camera, motion in nature):
{"style":"video","prompt":"Near-static wide shot of a misty mountain valley at dawn, the camera scarcely breathes, layers of fog rolling slowly between silhouetted pines, distant birds crossing the pink and blue sky, light shifting gradually as the sun rises behind the ridge, a single leaf drifting down through the cold air, contemplative and vast, cinematic 4k"}"""
    elif _auto_movement:
        _EXAMPLES_BLOCK = """Example (LOCKED STATIC camera — motion only within the scene):
{"style":"video","prompt":"Fixed static frame of a sunlit room at golden hour, the camera never moves, gauze curtains billowing gently, dust motes drifting through the warm beam, a glass catching slow glints, intimate and calm, cinematic 4k"}

Example (SUBTLE minimal motion):
{"style":"video","prompt":"Barely-moving shot of a misty mountain valley at dawn, an almost imperceptible drift, layered blue and pink sky, low fog rolling slowly between still pine trees, contemplative and vast, cinematic 4k"}

Example (ACTIVE camera movement — gentle LATERAL track only, never forward, used only when the song's energy genuinely calls for it):
{"style":"video","prompt":"Slow lateral tracking shot gliding sideways past a stormy desert ridge at dusk, lightning fracturing distant clouds, layered rock formations sliding through frame, dramatic and raw, cinematic 4k"}

CAMERA REGISTER (important): choose the register that matches the song — a LOCKED STATIC frame for intimate/calm/slow songs, SUBTLE minimal motion for most tracks, and at most a GENTLE LATERAL track / slow orbit / parallax for genuinely high-energy songs. NEVER move the camera FORWARD toward the subject (no push-in, dolly forward, fly-through, first-person glide) — the lyrics are overlaid and forward motion makes them nauseating to read. Do NOT default to constant cinematic drift; vary it per song. Scene motion (water, light, foliage, particles) is always allowed."""
    else:
        _EXAMPLES_BLOCK = """Example for rock / energetic / dramatic track:
{"style":"video","prompt":"Slow drone over a stormy desert highway at dusk, lightning fracturing distant clouds, asphalt reflecting the dying light, vintage road sign blurred in the foreground, dramatic and raw, cinematic 4k"}

Example for romantic ballad / love song:
{"style":"video","prompt":"Slow drift through a sunlit room at golden hour, warm light streaming through gauze curtains, soft focus on a glass catching the light, dust motes floating in the warm beam, intimate and calm, cinematic 4k"}

Example for introspective acoustic / folk track:
{"style":"video","prompt":"Slow aerial pull-back over a misty mountain valley at dawn, layers of soft blue and pink sky, distant silhouettes of pine trees, gentle wind moving low fog, contemplative and vast, cinematic 4k"}"""

    _BASE_INSTRUCTIONS = f"""Respond ONLY with a JSON object, no other text.

Output JSON shape — do NOT copy any of these example scenes verbatim;
they show only the format and the breadth of valid visual registers:

{_EXAMPLES_BLOCK}

GENRE-TONE COHERENCE (critical):
If the lyrics or declared genre suggest a love song, romantic ballad,
soft rock, acoustic, intimate or emotional theme, DO NOT default to
industrial, urban, dystopian, sewer, alleyway, or neon-rain backgrounds.
Bias toward warm interiors, golden-hour light, natural landscapes
(sunset, ocean, mountains at dusk), or symbolic intimate imagery (a
window, a candle, a glass catching light, hands intertwined). Industrial
alleys, neon streets, smoke, and rain are reserved for rock / metal /
punk / hip-hop tracks where the genre or lyrics anchor that vocabulary
explicitly. When in doubt, prefer warm/natural over urban/industrial."""
    # Keep _EXAMPLE pointing to the new block so existing f-strings below
    # absorb the change without further edits.
    _EXAMPLE = _BASE_INSTRUCTIONS

    if normalized_concept:
        concept_guide = _CONCEPT_SCENE_GUIDE[normalized_concept]
        genre_hint = (f"\n\nFor stylistic colour-grading flavour only "
                      f"(NOT for scene choice), the song genre is: "
                      f"{normalized_genre.upper()}.") if normalized_genre else ""

        if match_lyrics:
            # "Inspirado en la letra" + concept: LYRICS anchor the scene's
            # subject; the concept controls the visual styling (palette,
            # texture, atmosphere, register). Inverted from the prior
            # "concept binding" model because the lyrics are the product's
            # unique asset — no other generative video tool has them. When
            # the operator opts in to "Inspirado en la letra" they want
            # the song's literal imagery to drive the scene, with the
            # concept selector acting as the aesthetic filter. Strict
            # concept-only mode lives in the `else` branch below
            # (match_lyrics=False) for the cases where the operator wants
            # to suppress the literal subject (covers, instrumentals,
            # ironic juxtaposition).
            system_prompt = f"""{_EXAMPLE}

The operator has chosen the visual STYLING register: {normalized_concept.upper()}.
The concept's vocabulary controls palette, texture, atmosphere, and aesthetic register:
{concept_guide}{genre_hint}

STEP 0 — Read the lyrics and identify the PRIMARY VISUAL SUBJECT: the concrete setting, object, or action the song is literally about (e.g., a campfire in a forest, the ocean at night, a football match, a road trip, rain on glass, friends sharing warmth, a long goodbye at a station).

STEP 1 — Build a scene where:
- The SUBJECT comes from the lyrics' literal or strongly figurative imagery
- The STYLING (palette, texture, atmosphere, mood) comes from the {normalized_concept.upper()} visual register
- The two are fused, not stacked: the lyrics' subject is rendered through the concept's aesthetic

Examples:
  · Lyrics: campfire with friends in a forest + concept ABSTRACTO → organic flowing shapes of orange and green light, fire-like kinetic energy radiating outward, abstract bark and ember textures
  · Lyrics: football match + concept COSMICO → goalpost silhouettes against nebula colors, kinetic energy in deep blue and purple, stars suggesting a crowd
  · Lyrics: heartbreak + concept TROPICAL → empty beach at dusk, palms swaying but no people, melancholic warm light, abandoned beach chair
  · Lyrics: night drive + concept MINIMALISTA → single road line receding to vanishing point, two color planes (deep blue + warm tail-light glow), negative space dominant
  · Lyrics: longing for home + concept INDUSTRIAL → distant lit window viewed through factory pipework, warm interior glow contrasting with cold steel textures
  · Lyrics: celebration + concept VINTAGE → confetti and streamers rendered in Super 8 grain, sepia tones, faded warmth

If the lyrics are purely abstract or emotional with no concrete visual subject, fall back to the concept's own scene vocabulary as the scene itself.

Hard rules:
{_PROMPT_RULES}
- The lyrics control WHAT the scene shows; the concept controls HOW it looks
- Concept styling must be visible and recognizable in palette, texture, and mood — it is not optional, it is the aesthetic layer over the subject{movement_extra_line}"""
        else:
            # Strict concept mode: operator's visual choice, no lyrics influence.
            system_prompt = f"""{_EXAMPLE}

The operator has explicitly requested a {normalized_concept.upper()} background.

You MUST pick a scene that fits this concept's visual vocabulary:
{concept_guide}{genre_hint}

Hard rules:
{_PROMPT_RULES}
- The concept choice is binding — do NOT drift to a different visual category{movement_extra_line}"""

    elif normalized_genre:
        scene_guide = _GENRE_SCENE_GUIDE[normalized_genre]

        if match_lyrics:
            # "Inspirado en la letra": lyrics anchor the scene, genre styles it.
            system_prompt = f"""{_EXAMPLE}

The song genre is: {normalized_genre.upper()}

STEP 0 — Read the lyrics and identify the PRIMARY VISUAL SUBJECT: the concrete setting, object, or action the song is literally about (e.g., a football/soccer match, the ocean, a city at night, a road, a dance floor, rain, a forest). This is your FIRST input for scene choice.

STEP 1 — Choose the scene:
- If the lyrics have a CLEAR visual subject → build the scene around that subject. Apply the {normalized_genre.upper()} genre's color palette, lighting, and atmosphere to STYLE it — but the SCENE must reflect what the song is literally about.
- If the lyrics are abstract or purely emotional with no specific visual subject → fall back to this genre's visual vocabulary:
{scene_guide}

Hard rules:
{_PROMPT_RULES}
- If lyrics reference a sport (football, basketball, etc.) → use field/pitch/arena/equipment, NOT cars or generic cityscapes
- Do NOT default to "calm ocean at sunset" unless this song is BALLAD{movement_extra_line}"""
        else:
            # Strict genre mode: pick from genre vocabulary, ignore lyrics.
            system_prompt = f"""{_EXAMPLE}

The song genre is: {normalized_genre.upper()}

You MUST pick a scene from this genre's visual vocabulary:
{scene_guide}

Hard rules:
{_PROMPT_RULES}
- Do NOT default to "calm ocean at sunset" unless this song is BALLAD{movement_extra_line}"""

    else:
        if match_lyrics:
            # "Inspirado en la letra" + auto: lyrics anchor the scene,
            # genre classification controls color/mood only.
            system_prompt = f"""{_EXAMPLE}

STEP 0 — Read the lyrics and identify the PRIMARY VISUAL SUBJECT: the concrete setting, object, or action the song is literally about (e.g., a football/soccer match, the ocean, a city at night, a road trip, a dance floor, rain, a forest). This is your FIRST input for scene choice.

STEP 1 — Choose the scene:
- If the lyrics have a CLEAR visual subject → build the scene around that subject. Then classify genre (rock/pop/ballad/latin/reggaeton/hiphop/electronic/indie/folk/metal) to determine the COLOR PALETTE, LIGHTING, and ATMOSPHERE only — not the scene itself.
- If the lyrics are abstract or purely emotional with no specific visual subject → classify genre, then pick from the genre's vocabulary:
  - rock     → varied dramatic settings: concert stage smoke, stormy highways, mountain storms, vintage amps in close-up, empty arena tunnels, raw plains (alleys allowed but NOT the default)
  - pop      → vibrant neon, disco reflections, geometric light patterns, glossy gradient skies
  - ballad   → soft sunset, calm ocean, drifting clouds, warm golden light, candlelight
  - latin    → tropical beaches, palm trees, vibrant flowers, festive lanterns, sunlit caribbean water
  - reggaeton → night cityscape with red/pink neon, abstract color bursts, club laser patterns
  - hiphop   → city skyline at night with gold, marble luxury textures, smoke-filled spotlights
  - electronic → abstract geometry, particle storms, fractal liquid metal, laser grids
  - indie    → misty forests, vintage interiors, autumn roads, lone lighthouses, dreamy lakes
  - folk     → mountain vistas, dusty roads, wheat fields, riverside campfires
  - metal    → volcanic lava streams, dark cathedrals, stormy lightning, cracked obsidian

STEP 2 — Output JSON with an 80-120 word prompt. Describe: (1) specific scene subject and setting in detail, {_clause2}, (3) color palette and dominant tones, (4) lighting type and direction, (5) atmosphere, mood, and at least one specific texture or material detail. Be precise and cinematic — avoid vague adjectives like "beautiful" or "amazing".

Hard rules:
- "style" must always be "video"
- Pick a DIFFERENT specific scene each time (don't repeat across songs)
- If lyrics reference a sport (football, basketball, etc.) → use field/pitch/arena/equipment, NOT cars or generic cityscapes
- Do NOT default to "calm ocean at sunset" unless the song is genuinely BALLAD
""" + ("" if allow_people else "- Never include people, faces, hands, or readable text in the scene")
        else:
            # Strict auto mode: classify genre, pick vocabulary, no lyrics.
            system_prompt = f"""{_EXAMPLE}

Step 1: Classify the song's genre using the artist, title, and lyrics. Pick ONE of:
  rock, pop, ballad, latin, reggaeton, hiphop, electronic, indie, folk, metal

Step 2: Pick a scene from the matching genre's visual vocabulary:
- rock     → varied dramatic settings: concert stage smoke, stormy highways, mountain storms, vintage amps in close-up, empty arena tunnels, raw plains (alleys allowed but NOT the default)
- pop      → vibrant neon, disco reflections, geometric light patterns, glossy gradient skies
- ballad   → soft sunset, calm ocean, drifting clouds, warm golden light, candlelight
- latin    → tropical beaches, palm trees, vibrant flowers, festive lanterns, sunlit caribbean water
- reggaeton → night cityscape with red/pink neon, abstract color bursts, club laser patterns
- hiphop   → city skyline at night with gold, marble luxury textures, smoke-filled spotlights
- electronic → abstract geometry, particle storms, fractal liquid metal, laser grids
- indie    → misty forests, vintage interiors, autumn roads, lone lighthouses, dreamy lakes
- folk     → mountain vistas, dusty roads, wheat fields, riverside campfires
- metal    → volcanic lava streams, dark cathedrals, stormy lightning, cracked obsidian

Step 3: Output JSON with the chosen scene as an 80-120 word prompt.

Hard rules:
- "style" must always be "video"
- Pick a DIFFERENT specific scene each time (don't repeat across songs)
- Do NOT default to "calm ocean at sunset" unless the song is genuinely BALLAD
""" + ("" if allow_people else "- Never include people, faces, hands, or readable text in the scene")
        if movement_rule:
            system_prompt = system_prompt + "\n- " + movement_rule

    # Expanded from 600 → 1800 so canciones largas (3-4 min) llegan completas
    # a Gemini. Antes el truncado a 600 chars cortaba al medio del verso 2 y
    # Gemini no veía el chorus → fallback al genre vocab (callejón). UMG
    # 2026-05-14: rock arg con letras claras igual rendía callejones porque
    # el sample no llegaba al subject visual real.
    lyrics_sample = lyrics_text[:1800] if lyrics_text else ""
    # Data minimization (UMG Guideline 14): optionally anonymize artist name
    _send_artist = os.environ.get("SEND_ARTIST_TO_AI", "true").lower() == "true"
    artist_label = artist if _send_artist else "the artist"
    title_part = f"\nSong title: {song_title}" if song_title else ""
    genre_part = f"\nDeclared genre: {normalized_genre}" if normalized_genre else ""
    concept_part = f"\nDeclared concept: {normalized_concept}" if normalized_concept else ""
    # Operator hint (set by /edit when the user clicked "Regenerar fondo"
    # and typed a free-form description of what they want). Sits at the
    # TOP of user_content with a strong header so Gemini treats it as the
    # dominant signal — overriding genre/concept/lyrics defaults that
    # caused off-tone backgrounds the operator already rejected.
    hint_block = ""
    if background_hint:
        hint_block = (
            f"[OPERATOR OVERRIDE — HIGHEST PRIORITY]\n"
            f"The operator was unhappy with previous backgrounds for this song "
            f"and wants the new one to convey: {background_hint.strip()}\n"
            f"Build the visual scene around this hint. This overrides the "
            f"default interpretation of genre/concept/lyrics — the operator's "
            f"explicit guidance wins. Stay coherent with the song's emotional "
            f"tone, but the IMAGERY must follow the hint.\n\n"
        )
    user_content = (
        f"{hint_block}"
        f"Artist: {artist_label}{title_part}{genre_part}{concept_part}\n\n"
        f"Lyrics (may be incomplete or noisy):\n"
        f"{lyrics_sample or '[transcription failed; rely on artist + title + declared metadata]'}"
    )
    # Provider-specific addendum. When generating for Imagen-4 (still
    # image + local Ken Burns animation), strip the motion vocabulary
    # that Veo expects — text-to-image models render motion words as
    # frozen-mid-action poses (a "running figure" becomes a static
    # crouched silhouette), which then jitters weirdly under the local
    # zoom/pan animation. Composition + lighting + atmosphere only.
    if for_provider == "imagen":
        system_prompt = system_prompt + (
            "\n\n## PROVIDER OVERRIDE — Imagen-4 (text-to-image)\n"
            "The output is a STILL IMAGE that will be animated locally "
            "with a subtle zoom/pan. Optimize for COMPOSITION not motion:\n"
            "- REMOVE all camera-movement words (no \"drone\", \"tracking\", "
            "\"pull-back\", \"slow drift\", \"orbit\", \"pan\", \"dolly\").\n"
            "- REPLACE with composition descriptors (\"centered composition\", "
            "\"wide vista\", \"low-angle\", \"symmetrical framing\", "
            "\"rule-of-thirds\").\n"
            "- EMPHASIZE lighting direction, atmosphere, color palette, "
            "and material textures.\n"
            "- The output `style` field MUST be \"photo\" (not \"video\")."
        )

    # Operator color choice → real steer on the generated colors (not just the
    # gradient fallback). "auto"/empty adds nothing (scene-natural colors).
    _color_line = _color_directive(style, custom_colors)
    if _color_line:
        system_prompt = system_prompt + "\n\n" + _color_line

    full_prompt = f"system:{system_prompt}\nuser:{user_content}"

    recorder = record_ai_call(
        job_id=job_id or "unknown",
        step="lyrics_analysis",
        tool_name="gemini-2.5-flash",
        tool_provider="google_vertex",
        prompt=full_prompt,
        input_data_types=["artist_name", "lyrics_text_600chars"],
    ) if job_id else None

    try:
        # Corrective re-roll for the noir-urban-alley cliché. The system
        # prompt already instructs against alleys, but Gemini ignores that
        # word-level steer with high frequency on melancholic Spanish rock
        # (UMG 2026-05-14, Amanda Pujó "Ser Anti" 2026-05-20: 3 separate
        # renders all landed on rain-slicked graffiti alleys). So instead of
        # only logging the bias (the prior behavior), we DETECT an alley
        # result and re-roll ONCE with a hard-negative addendum + higher
        # temperature to escape the prior.
        #
        # Re-roll is gated to the case where it's actually unwanted: the
        # operator gave no background_hint AND didn't explicitly ask for
        # the "urbano" concept. An explicit alley request must be honored.
        _reroll_eligible = (not background_hint and normalized_concept != "urbano")
        _max_attempts = 2 if _reroll_eligible else 1
        text = ""
        response = None
        for _attempt in range(1, _max_attempts + 1):
            _sys_instr = system_prompt
            _temp = 0.8
            if _attempt > 1:
                # Hard-negative + a menu of alternatives, and widen the
                # sampling distribution so the model leaves the alley basin.
                _sys_instr = system_prompt + _ANTI_ALLEY_ADDENDUM
                _temp = 1.0
            # Audit 2026-05-26: wrap with _call_with_timeout. This step runs
            # on EVERY job that uses Veo/Imagen — a Vertex hang here means the
            # whole worker pool deadlocks at progress=22, current_step=background.
            # 60s is generous (p99 ~15s) but bounds the worst case so the
            # caller can fall through to the genre-based fallback below.
            response = _call_with_timeout(
                lambda: client.models.generate_content(
                    model="gemini-2.5-flash",
                    contents=user_content,
                    config=genai.types.GenerateContentConfig(
                        system_instruction=_sys_instr,
                        temperature=_temp,
                        # max_output_tokens=1500 (was 500): the concept+match_lyrics=True
                        # branch in #152 expanded the system_prompt with 6 worked examples
                        # and lyrics-anchor instructions. Gemini's output got more verbose
                        # and started truncating at 500 tokens (prod 2026-05-15), parse
                        # failed, combinatorial fallback fired 100% of jobs. 1500 covers a
                        # 120-word prompt + JSON envelope + preamble, ~3× headroom.
                        max_output_tokens=1500,
                        # thinking_budget=512: short chain-of-thought to extract the
                        # visual subject from lyrics before committing to a scene.
                        # Without it Gemini skipped STEP 0 and fell to the genre
                        # fallback (UMG 2026-05-14: 80% rock → callejón).
                        thinking_config=genai.types.ThinkingConfig(thinking_budget=512),
                    ),
                ),
                timeout_s=60.0,
                label="BG-ANALYZE",
            )
            text = response.text.strip()
            logger.info("[BG] Gemini raw attempt %s/%s (%s chars): %s",
                        _attempt, _max_attempts, len(text), text[:800])

            # Parse Gemini's response (three-stage parser handles bare JSON,
            # markdown-wrapped, and truncated-mid-property — see
            # _parse_gemini_bg_response).
            parsed = _parse_gemini_bg_response(text)
            if parsed is not None:
                style = parsed.get("style", "video")
                prompt = parsed.get("prompt", "")
                if style not in ("video", "photo", "illustration"):
                    style = "video"
                if prompt and len(prompt) > 15:
                    if _looks_like_alley(prompt) and _reroll_eligible:
                        if _attempt < _max_attempts:
                            logger.warning(
                                "[BG][ALLEY-BIAS] attempt %s chose alley; re-rolling "
                                "with hard-negative. genre=%s job=%s",
                                _attempt, normalized_genre or 'auto', job_id,
                            )
                            continue  # re-roll with the anti-alley addendum
                        logger.warning(
                            "[BG][ALLEY-BIAS PERSISTENT] re-roll still chose alley; "
                            "accepting to avoid an infinite loop. job=%s", job_id,
                        )
                    logger.info("[BG] Gemini chose: style=%s, prompt=%s...", style, prompt[:80])
                    if recorder:
                        recorder.finish(response_summary=f"attempt={_attempt} " + text[:480])
                    return {"style": style, "prompt": prompt}
            # Parse failed this attempt. If attempts remain, the loop retries
            # (a re-roll often parses cleanly); otherwise fall through.

        # All attempts exhausted without a usable parse.
        finish_reason = "unknown"
        try:
            if response is not None and getattr(response, "candidates", None):
                fr = getattr(response.candidates[0], "finish_reason", None)
                if fr is not None:
                    finish_reason = str(fr)
        except Exception:
            pass
        logger.warning("[BG] Failed to parse Gemini JSON, using combinatorial fallback. "
                       "raw_len=%s finish_reason=%s", len(text), finish_reason)
        if recorder:
            recorder.finish(response_summary=f"parse_failed: {text[:200]}")
        return {"style": "video", "prompt": None}

    except Exception as e:
        logger.error("[BG] Gemini analysis failed: %s, using video fallback", e)
        if recorder:
            recorder.finish(response_summary=f"error: {str(e)[:200]}")
        return {"style": "video", "prompt": None}


def _get_unique_prompt(lyrics_text: str = None, artist: str = "", job_id: str = None,
                       song_title: str = "", genre: str = "", concept: str = "",
                       movement_style: str = "", match_lyrics: bool = True,
                       background_hint: str | None = None,
                       for_provider: str = "veo",
                       bg_verbatim: bool = False,
                       palette_style: str = "", custom_colors: str = "",
                       allow_people: bool = False) -> dict:
    """Get a unique style+prompt combination. Returns {style, prompt}.

    `for_provider` ("veo" default | "imagen") nudges the prompt towards
    the strengths of the target generator:
      - "veo": prompts include camera movement, action verbs, motion
        descriptors (Veo 3.1 is a text-to-video model — these enrich
        the output).
      - "imagen": prompts focus on composition, lighting, atmosphere
        with NO motion descriptors (Imagen-4 is a text-to-image model
        — motion words confuse it and produce frozen-frame-of-action
        renders that read poorly with the local Ken Burns animation
        applied afterward).

    For the combinatorial fallback (no Gemini), we just swap the camera
    descriptor for a static composition word when generating for Imagen.
    For the Gemini path, we pass `for_provider` through so the analysis
    function can adjust its system prompt accordingly.

    Note: the local _USED_PROMPTS_FILE only sees this worker's previous
    prompts — Railway containers have ephemeral disk, so dedup across
    workers / restarts is best-effort. The Veo cache key downstream
    includes artist+title so even a duplicated Gemini prompt produces a
    fresh background per song (see `_generate_veo_video`).
    """
    # Verbatim mode: the operator chose "usar mi prompt tal cual". Skip the
    # Gemini rewrite entirely and send their exact text to the generator. The
    # safety + (when static) camera-motion negatives are still appended in
    # _generate_veo_video, so verbatim is "respect my words" — not "no rails".
    # This is the fix for the power-user case where a hand-written "Static
    # tripod, no camera motion…" prompt was paraphrased away by Gemini.
    if bg_verbatim and background_hint and background_hint.strip():
        logger.info("[BG] verbatim mode — bypassing Gemini, using operator prompt as-is")
        return {
            "style": "image" if for_provider == "imagen" else "video",
            "prompt": background_hint.strip(),
        }

    used: list[str] = []
    if os.path.exists(_USED_PROMPTS_FILE):
        try:
            with open(_USED_PROMPTS_FILE) as f:
                used = json.load(f)
        except (json.JSONDecodeError, OSError):
            used = []

    # Movement-aware camera pool for the combinatorial fallback (rare — only
    # when Gemini fails to parse). Static intent must NOT get a motion verb.
    _norm_move = _normalize_movement_style(movement_style)
    _camera_pool = _BG_CAMERAS_STATIC if _norm_move == "estatico" else _BG_CAMERAS

    # Gemini analysis
    if lyrics_text or song_title:
        result = _analyze_lyrics_for_background(
            lyrics_text or "", artist, job_id=job_id, song_title=song_title,
            genre=genre, concept=concept, movement_style=movement_style,
            match_lyrics=match_lyrics, background_hint=background_hint,
            for_provider=for_provider,
            style=palette_style, custom_colors=custom_colors,
            allow_people=allow_people,
        )
        if result["prompt"] and result["prompt"] not in used:
            used.append(result["prompt"])
            try:
                with open(_USED_PROMPTS_FILE, "w") as f:
                    json.dump(used, f)
            except OSError:
                pass
            return result

    # Fallback: combinatorial prompt. For Imagen, drop the camera move
    # descriptor (it adds motion words like "tracking shot" that confuse
    # a still-image generator) and substitute a composition descriptor.
    composition_terms = (
        "centered composition", "wide vista", "low-angle composition",
        "rule-of-thirds composition", "symmetrical framing", "dramatic perspective",
    )
    for _ in range(50):
        scene = random.choice(_BG_SCENES)
        palette = random.choice(_BG_PALETTES)
        condition = random.choice(_BG_CONDITIONS)
        if for_provider == "imagen":
            composition = random.choice(composition_terms)
            prompt = f"{composition} of {scene}, {palette}, {condition}, 4k, photorealistic"
            style = "image"
        else:
            camera = random.choice(_camera_pool)
            prompt = f"{camera} of {scene}, {palette}, {condition}, 4k, photorealistic"
            style = "video"
        if prompt not in used:
            used.append(prompt)
            try:
                with open(_USED_PROMPTS_FILE, "w") as f:
                    json.dump(used, f)
            except OSError:
                pass
            return {"style": style, "prompt": prompt}

    # Final fallback after 50 attempts — accept duplicate.
    if for_provider == "imagen":
        return {
            "style": "image",
            "prompt": f"{random.choice(composition_terms)} of {random.choice(_BG_SCENES)}, {random.choice(_BG_PALETTES)}, {random.choice(_BG_CONDITIONS)}, 4k, photorealistic",
        }
    return {
        "style": "video",
        "prompt": f"{random.choice(_camera_pool)} of {random.choice(_BG_SCENES)}, {random.choice(_BG_PALETTES)}, {random.choice(_BG_CONDITIONS)}, 4k, photorealistic",
    }


_last_veo_request = 0  # timestamp of last Veo API call
_VEO_COOLDOWN = 5      # seconds between Veo requests (Veo 3.1 has 50 req/min quota)


def _veo_access_token() -> str:
    """Build an explicit cloud-platform-scoped access token for the Vertex AI
    REST API. Bypasses google-genai SDK's internal auth chain which has been
    triggering invalid_scope errors on Railway despite the credentials being
    valid (Gemini works on the same token; only Veo rejects through the SDK)."""
    from google.oauth2 import service_account

    creds_path = os.environ.get("GOOGLE_APPLICATION_CREDENTIALS", "").strip()
    if not creds_path or not os.path.exists(creds_path):
        raise RuntimeError(f"GOOGLE_APPLICATION_CREDENTIALS not found: {creds_path!r}")
    creds = service_account.Credentials.from_service_account_file(
        creds_path,
        scopes=["https://www.googleapis.com/auth/cloud-platform"],
    )
    creds = creds.with_quota_project(_VERTEX_PROJECT)
    creds.refresh(_oauth_refresh_request())
    return creds.token


def _veo_cache_key(prompt: str, model: str, params: dict) -> str:
    """Stable hash of the Veo request. Two requests with the same prompt and
    parameters return the same key, so we can dedupe paid generations across
    runs (especially during testing — UMG production prompts are unique per
    song so cache hits are rare there)."""
    import hashlib as _hash
    import json as _json
    payload = _json.dumps(
        {"prompt": prompt, "model": model, "params": params},
        sort_keys=True,
        separators=(",", ":"),
    )
    return _hash.sha256(payload.encode()).hexdigest()[:16]


def _generate_veo_video(prompt: str, output_path: str, job_id: str = None,
                        cache_namespace: str = "",
                        image_path: str | None = None,
                        movement_style: str = "",
                        normalized_concept: str = "",
                        high_fidelity: bool = False,
                        allow_people: bool = False,
                        verbatim: bool = False,
                        cache_only: bool = False,
                        out_meta: dict | None = None) -> str:
    """Generate a video clip with Google Veo 3 via direct Vertex AI REST API.

    We bypass google-genai SDK for Veo specifically because its internal auth
    chain hits "invalid_scope: Invalid OAuth scope or ID token audience" on
    Railway even when our explicit credentials work for Gemini through the
    same SDK. Direct REST gives us full control over headers, scopes, and
    endpoints.

    Endpoint: predictLongRunning -> poll operation -> download mp4.
    Rate-limit aware (5 attempts with exponential backoff).
    R2-cached by prompt hash so identical retries do not bill twice.

    `image_path`: optional path to a JPG/PNG. When provided, the request is
    sent in image-to-video mode (Veo 3.1 supports a base64-encoded `image`
    field on `instances[0]`). The user's image is animated according to the
    prompt while preserving its identity. Defaults to None (text-to-video).

    `movement_style`: when set to "animado", the safe-prompt suffix drops
    the "no CGI / no animation" clauses so they don't contradict the
    cartoon-illustration aesthetic. All other safety clauses (no people,
    no text, etc.) stay in place.
    """
    from provenance import record_ai_call
    import storage as _storage
    import time as _time
    import requests as _req
    global _last_veo_request

    # Bias-buster: cuando el operador NO eligió concept=urbano explícito,
    # prohibimos callejón/alley como subject. UMG 2026-05-14: con genre=rock
    # y concept vacío, Gemini elegía alley ~80% del tiempo aún con guard-
    # rails en el system prompt. El negative en safe_prompt es la última
    # red de seguridad antes de Veo. Si el operador SÍ pidió urbano, no
    # bloqueamos (es su decisión consciente).
    # `verbatim`: el operador escribió su propio prompt ("Mi prompt manda",
    # decisión UMG 2026-05-21). Sus palabras de ESCENA y CÁMARA se respetan
    # tal cual — no le pegamos los de-bias (callejón/avance). Solo quedan los
    # rieles legales (sin personas/caras/texto/logos) más abajo.
    no_alley = "" if (normalized_concept == "urbano" or verbatim) else (
        "Avoid generic narrow alleyway, dark alley, callejón, and neon-lit "
        "back-street as the primary subject unless the lyrics demand it. "
    )

    # When `allow_people` is True the operator opted into "fondo libre"
    # for an UMG tenant, OR the tenant is non-UMG (their default). Drop
    # the "no people / no faces / no hands" clauses so Veo can render
    # subjects the operator's prompt requests (woman in armchair, hands
    # playing guitar, etc.). The logo / brand / readable-text negatives
    # stay regardless because those are legal/IP concerns separate from
    # the people question.
    _people_clause = "" if allow_people else " no people, no faces, no hands,"
    # Shared IP / content negatives (text, logos, optionally people) — present
    # in every register.
    _base_negatives = (
        "No text, no words, no letters, no signs, no billboards, no posters, "
        "no banners, no graffiti, no shop windows, no street signs, no neon "
        f"signs, no logos, no trademarks, no brand symbols,{_people_clause}"
        # Anti-UI-de-cámara (incidente 2026-06-19, multi-escena "No Hay Santos"):
        # la biblia "found footage / film viejo" hacía que Veo dibujara una
        # interfaz de camcorder falsa — visor, indicador REC, timecode, texto de
        # grabadora, un recuadro/botón en una esquina. Prohibido explícitamente.
        " no camera viewfinder, no recording overlay, no REC indicator, no "
        "timecode, no on-screen camera UI, no HUD, no camcorder interface, no "
        "VHS overlay, no film-frame border graphic, no lens UI, no corner "
        "buttons or icons,"
        # Anti-fotograma-físico (incidente 2026-06-19, "Intoxicados"): una biblia
        # con "16mm film grain" hacía que Veo dibujara el rollo de película entero
        # —perforaciones, marcas de borde, marco negro— como si fuera un escaneo.
        " no film sprocket holes, no film perforations, no film strip, no film "
        "edge markings, no 16mm or 35mm frame, no scanned film border, no black "
        "frame border, full-bleed edge-to-edge image,"
    )
    # Camera-motion negatives — the LAST line of defense for static intent.
    # Veo's payload exposes no structured camera-lock field, so these words
    # are the only lever; they fight Veo's strong drift prior. Appended only
    # when the operator asked for a locked frame (estatico).
    _camera_negatives = (
        " no camera movement, no pan, no tilt, no zoom, no dolly, no push-in, "
        "no drift, no orbit, no crane, no handheld, no parallax."
    )
    # Forward-travel negatives — legibility cap. UMG 2026-05-21: backgrounds
    # where the camera "advances / flies" forward make the OVERLAID LYRICS
    # nauseating to read ("marean cuando lees la letra"). Because there is
    # ALWAYS text on top of these clips, forward translation toward the
    # subject is never acceptable on the default path. Lateral / orbit /
    # ambient motion stays fine. Applied to every register EXCEPT an explicit
    # "Cinematográfico" (estandar) pick, which is the operator's conscious
    # choice — same opt-in principle as concept=urbano above.
    _forward_travel_negative = (
        " no forward camera travel, no push-in, no dolly forward, no "
        "fly-through, no first-person glide forward, no zoom toward the "
        "subject, no drone advancing toward the camera."
    )
    _norm_move = _normalize_movement_style(movement_style)
    if _norm_move == "animado":
        # Cartoon / 2D illustration aesthetic — keep all safety clauses
        # except the "no CGI / no animation" pair, which would directly
        # contradict the requested look.
        safe_prompt = (
            f"{prompt}. Stylised 2D animated illustration, flat shapes, "
            "deliberate cartoon-like motion. "
            f"{no_alley}"
            f"{_base_negatives}"
            " no extra animation noise."
        )
    elif _norm_move == "estatico":
        # C2 (2026-05-25) — Hardening del prompt estatico. Veo ignoraba
        # ~50% de las locked-frame requests pre-2026-05-22, motivando el
        # routing a Imagen. Ahora endurecemos vía:
        #   1) Repetición: las constraints aparecen 3× (al inicio, en el
        #      medio, y al final). Veo responde a repetición.
        #   2) Afirmativos a la par de negativos: no solo "no pan/zoom"
        #      sino "BOLTED tripod, FIXED frame, single static shot".
        #   3) Anti-Ken-Burns: explícitamente "no slow zoom despite
        #      locked appearance" para evitar el "frozen frame + zoom"
        #      que Veo aplica como compromiso cuando no puede decidir.
        #   4) "Filmed on a security camera" — phrasing que Veo ASOCIA
        #      con motion-locked (a diferencia de "cinema camera" que
        #      sugiere drift).
        safe_prompt = (
            f"{prompt}. "
            # Affirmative — repeated phrasing for Veo's prior.
            "LOCKED STATIC TRIPOD shot. The camera is BOLTED in place. "
            "Single FIXED frame held for the entire duration. "
            "Filmed on a security camera — NO operator, NO movement of the lens. "
            "All motion lives WITHIN the scene only (water ripples, fire, "
            "drifting clouds, smoke, foliage swaying, particles floating). "
            "The frame edges NEVER shift. "
            f"{no_alley}"
            f"{_base_negatives}"
            " no CGI, no animation,"
            f"{_camera_negatives}"
            # Anti-Ken-Burns + repetition at end.
            " No slow zoom despite locked appearance. No subtle push-in. "
            "No imperceptible drift. The camera is COMPLETELY STILL. "
            "Lens position is fixed for the entire shot."
        )
    elif _norm_move == "sutil":
        # C3 (2026-05-25) — Branch nueva para sutil. Antes caía en el `else`
        # final con solo _forward_travel_negative, que Veo trataba como
        # "cinematográfico atenuado" → forward push asumido.
        #
        # Sutil = cámara casi estática pero respira sutilmente. Distinguible
        # de estatico (clavada) y de estandar (se mueve). El affirmative
        # "near-static tripod, micro-drift only" + negativos de zoom/dolly/
        # push-in son el lever para que Veo entregue la escena viva con
        # micro-movimiento.
        safe_prompt = (
            f"{prompt}. "
            "Near-static tripod shot. The camera barely breathes — micro-drift "
            "ONLY — the lens shifts no more than 5% of the frame width over "
            "the whole shot. Treat as a fixed photograph that just breathes. "
            "Motion is rich WITHIN the scene (water, fire, foliage, "
            "particles, light shifts), but the FRAME itself is near-still. "
            f"{no_alley}"
            f"{_base_negatives}"
            " no CGI, no animation."
            " No zoom, no dolly, no push-in, no orbit, no crane, no whip pan."
            f"{_forward_travel_negative}"
            # Affirmative repetition at end.
            " The camera is essentially static — micro-drift is the ONLY "
            "movement permitted. No cinematic camera moves."
        )
    else:
        # Auto / foto-parallax (and any unknown register): cap forward
        # travel so the overlaid lyrics stay readable. Two opt-outs: the
        # explicit "Cinematográfico" pick (estandar), and verbatim mode (the
        # operator wrote their own camera language — "mi prompt manda").
        _legibility_cap = "" if (_norm_move == "estandar" or verbatim) else _forward_travel_negative
        safe_prompt = (
            f"{prompt}. Photorealistic, filmed with cinema camera, real footage. "
            f"{no_alley}"
            f"{_base_negatives}"
            " no CGI, no animation."
            f"{_legibility_cap}"
        )

    # veo-3.1-fast at $0.10/s (no audio) is 75% cheaper than the standard
    # veo-3.1-generate at $0.40/s. Visual quality is slightly softer; we
    # apply a small gaussian blur after generation to smooth edges and
    # improve lyric legibility on top of the background.
    #
    # Blur sigma was 2.0 originally — UMG flagged the rendered backgrounds
    # as low-definition during the live demo, and the heavy blur was the
    # main culprit (compounding the softness Veo Fast already has). Now
    # 1.0 by default — preserves more detail while still smoothing micro
    # artefacts. Tune via env var without redeploy if needed.
    model = os.environ.get("VEO_MODEL", "veo-3.1-fast-generate-001").strip()
    # Static / verbatim renders are exactly the cases where prompt adherence
    # matters most (the user asked for a precise, often locked-camera result).
    # The fast model has a stronger drift prior; the standard model follows
    # "static shot" better but costs ~4x. Route ONLY these renders to a
    # higher-fidelity model when VEO_MODEL_STATIC is set — leaves the default
    # untouched for everything else, and lets us A/B fast-vs-standard + measure
    # real cost without a redeploy (see plan Phase 5).
    _static_model = os.environ.get("VEO_MODEL_STATIC", "").strip()
    if _static_model and (high_fidelity or _norm_move == "estatico"):
        model = _static_model
        logger.info("[BG] high-fidelity render → model=%s (movement=%s, verbatim=%s)",
                    model, _norm_move or "auto", high_fidelity)
    veo_params = {
        "aspectRatio": "16:9",
        "sampleCount": 1,
        "generateAudio": False,
    }
    try:
        blur_sigma = float(os.environ.get("BG_BLUR_SIGMA", "1.0"))
    except ValueError:
        blur_sigma = 1.0

    # Cache key includes a per-song namespace (artist|title) so two different
    # songs that happen to receive the same Gemini prompt — common when
    # transcription degrades and Gemini falls back to a generic "ocean
    # sunset" template — still generate independent Veo backgrounds.
    # Without this, all problem-songs ended up sharing one cached video
    # because the cache key was prompt-only.
    cache_params = {**veo_params, "blur_sigma": blur_sigma, "ns": cache_namespace or ""}
    # Image-to-video: la imagen semilla entra al hash vía digest del
    # contenido — ver _seed_image_digest para el porqué (audit 2026-06-09).
    if image_path:
        cache_params["img"] = _seed_image_digest(image_path, job_id)
    cache_key_hash = _veo_cache_key(safe_prompt, model, cache_params)
    cache_object_key = f"cache/veo/{cache_key_hash}.mp4"
    # Audit M8: exponer la cache key usada para que el caller pueda GC el clip
    # viejo cuando una escena se regenera (evita huérfanos en cache/veo/).
    if out_meta is not None:
        out_meta["cache_object_key"] = cache_object_key

    recorder = record_ai_call(
        job_id=job_id or "unknown",
        step="video_bg",
        tool_name=model,
        tool_provider="google_vertex",
        prompt=safe_prompt,
        input_data_types=["generated_prompt"],
    ) if job_id else None

    # cache_only (multi-escena regen): SÓLO servir de caché, NUNCA generar
    # fresco. Garantiza que regenerar UNA escena no re-cobre las otras N: si su
    # clip no está cacheado, levantamos y el caller degrada (reusa otro clip) en
    # vez de pagar Veo. Sin esto, un cache miss en una regen re-facturaba todo.
    if cache_only:
        if (_storage.is_enabled() and _storage.object_exists(cache_object_key)
                and _storage.download_object(cache_object_key, output_path)):
            size_mb = os.path.getsize(output_path) / 1024 / 1024
            logger.info("[BG] Veo cache HIT (cache_only, %s): %.1f MB", cache_key_hash, size_mb)
            if recorder:
                recorder.finish(response_summary=f"cache_hit(cache_only): {size_mb:.1f}MB key={cache_key_hash}",
                                output_artifact=output_path)
            return output_path
        if recorder:
            recorder.finish(response_summary=f"cache_only_miss: key={cache_key_hash}")
        raise RuntimeError(
            f"[BG] cache_only: sin clip cacheado ({cache_key_hash}) — no se genera "
            "para no re-cobrar Veo en una regeneración de escena")

    if _storage.is_enabled() and _storage.object_exists(cache_object_key):
        if _storage.download_object(cache_object_key, output_path):
            size_mb = os.path.getsize(output_path) / 1024 / 1024
            logger.info("[BG] Veo cache HIT (%s): %.1f MB - skipped paid generation", cache_key_hash, size_mb)
            if recorder:
                recorder.finish(
                    response_summary=f"cache_hit: {size_mb:.1f}MB key={cache_key_hash}",
                    output_artifact=output_path,
                )
            return output_path
        # U6 (audit 2026-05-25) — cache existe pero download FALLÓ.
        # Sin este fix, el recorder quedaba "in-flight" mientras el Veo
        # call subsiguiente arrancaba; si el worker moría antes del
        # finish() de la Veo call, el reaper marcaba el row "orphan poll"
        # y el job entero como "error". Fix: cerramos el recorder actual
        # con summary descriptivo, recreamos uno nuevo para el Veo call.
        logger.warning(
            "[BG] Veo cache HIT pero download FALLÓ para %s — recorder "
            "cerrado como cache_hit_download_failed, arrancando Veo fresh.",
            cache_object_key,
        )
        if recorder:
            recorder.finish(
                response_summary=f"cache_hit_download_failed: key={cache_key_hash}",
            )
        # Re-crear recorder limpio para la Veo call que sigue.
        recorder = record_ai_call(
            job_id=job_id or "unknown",
            step="video_bg",
            tool_name=model,
            tool_provider="google_vertex",
            prompt=safe_prompt,
            input_data_types=["generated_prompt"],
        ) if job_id else None

    elapsed = _time.time() - _last_veo_request
    if elapsed < _VEO_COOLDOWN and _last_veo_request > 0:
        wait = _VEO_COOLDOWN - elapsed
        logger.info("[BG] Cooldown: waiting %.0fs before next Veo request...", wait)
        _time.sleep(wait)

    base_url = (
        f"https://{_VERTEX_LOCATION}-aiplatform.googleapis.com/v1"
        f"/projects/{_VERTEX_PROJECT}/locations/{_VERTEX_LOCATION}"
        f"/publishers/google/models/{model}"
    )
    submit_url = f"{base_url}:predictLongRunning"

    # Build the instance dict. When the operator supplied an image AND
    # marked "animar con AI", attach it as base64 — Veo 3.1 then animates
    # the image while honoring the prompt instead of generating from
    # scratch. Worker logs this so we can monitor success rate.
    instance: dict = {"prompt": safe_prompt}
    if image_path and os.path.isfile(image_path):
        try:
            import base64 as _b64
            with open(image_path, "rb") as _img:
                img_bytes = _img.read()
            ext = os.path.splitext(image_path)[1].lower()
            mime = "image/png" if ext == ".png" else "image/jpeg"
            instance["image"] = {
                "bytesBase64Encoded": _b64.b64encode(img_bytes).decode("ascii"),
                "mimeType": mime,
            }
            logger.info("[BG] image-to-video Veo call with user image (%s bytes, %s)",
                        len(img_bytes), mime)
        except OSError as e:
            logger.warning("[BG] failed to read image_path %s: %s; falling back to text-to-video",
                           image_path, e)

    request_body = {
        "instances": [instance],
        "parameters": veo_params,
    }

    # Retry policy
    # ------------
    # The previous loop conflated two failure modes (HTTP 429 and arbitrary
    # exceptions) under the same `for/else: raise "rate limit exceeded"` and
    # could fall through to the polling stage with operation_name=None on a
    # transient request error. We now:
    #   1. Track success/last-error explicitly so the exit reason is honest.
    #   2. Cap backoff at 120 s (was 60 × 5 = 300 s, exceeding the worker
    #      timeout under stress).
    #   3. Distinguish 429/RESOURCE_EXHAUSTED ("rate-limited") from network
    #      errors ("transient") so the surfaced error message is accurate.
    MAX_BACKOFF_S = 120
    MAX_ATTEMPTS = 5
    operation_name: str | None = None
    last_error: str | None = None
    rate_limit_hits = 0

    import veo_breaker
    # Wall-clock budget on the SUBMIT phase (separate from the 600s poll
    # deadline). Without it a 429 storm burns the full ~5.5 min of in-slot
    # backoff before falling to gradient. Default 240s only ever bites during a
    # repeated-429 cascade — a healthy submit succeeds on attempt 1, untouched.
    _submit_budget_s = float(os.environ.get("VEO_SUBMIT_BUDGET_S", "240"))
    _submit_deadline = _time.time() + _submit_budget_s

    for attempt in range(MAX_ATTEMPTS):
        try:
            logger.info("[BG] Veo 3: generating video (attempt %s/%s)...", attempt + 1, MAX_ATTEMPTS)
            token = _veo_access_token()
            r = _req.post(
                submit_url,
                headers={
                    "Authorization": f"Bearer {token}",
                    "Content-Type": "application/json",
                    "x-goog-user-project": _VERTEX_PROJECT,
                },
                json=request_body,
                timeout=60,
            )
            if r.status_code == 429 or "RESOURCE_EXHAUSTED" in r.text:
                rate_limit_hits += 1
                last_error = f"HTTP {r.status_code} rate-limited"
                veo_breaker.record_rate_limit()  # feeds the cross-worker breaker
                # Capped exponential backoff + ±20 % jitter. Without
                # jitter, N concurrent jobs that all hit a 429 at the
                # same instant retry in lock-step → second wave of
                # 429s → cascade. Jitter spreads the retry window so
                # quota recovers naturally.
                base = min(MAX_BACKOFF_S, 30 * (2 ** attempt))
                wait = base * random.uniform(0.8, 1.2)
                # Submit budget: don't keep sleeping in-slot past the budget —
                # bail so the job falls to gradient instead of burning the full
                # backoff during a quota storm.
                if _time.time() + wait > _submit_deadline:
                    logger.warning("[BG] Veo submit budget (%.0fs) exhausted — bailing to fallback", _submit_budget_s)
                    break
                logger.warning("[BG] Rate limited (HTTP %s), waiting %.1fs before retry...",
                               r.status_code, wait)
                _time.sleep(wait)
                continue
            if not r.ok:
                detail = r.text[:500]
                # Non-retryable: bubble immediately so the caller can mark
                # the job error with a useful reason.
                raise RuntimeError(
                    f"Veo predictLongRunning HTTP {r.status_code}: {detail}"
                )
            payload = r.json()
            operation_name = payload.get("name")
            if not operation_name:
                raise RuntimeError(f"Veo response missing 'name': {payload}")
            veo_breaker.record_success()  # Veo accepted → close the breaker if open
            break
        except RuntimeError:
            raise
        except Exception as e:
            last_error = f"network/transient: {e}"
            logger.error("[BG] Veo 3 attempt %s request error: %s", attempt + 1, e)
            base = min(MAX_BACKOFF_S, 15 * (2 ** attempt))
            wait = base * random.uniform(0.8, 1.2)
            if _time.time() + wait > _submit_deadline:
                logger.warning("[BG] Veo submit budget exhausted (transient) — bailing to fallback")
                break
            _time.sleep(wait)
            continue

    if operation_name is None:
        reason = last_error or "unknown"
        summary = (
            f"error: rate_limited_after_{MAX_ATTEMPTS}_retries"
            if rate_limit_hits == MAX_ATTEMPTS
            else f"error: {reason} after {MAX_ATTEMPTS} retries"
        )
        if recorder:
            recorder.finish(response_summary=summary)
        if rate_limit_hits == MAX_ATTEMPTS:
            raise RuntimeError(f"Veo 3 rate limit exceeded after {MAX_ATTEMPTS} retries")
        raise RuntimeError(f"Veo 3 submission failed after {MAX_ATTEMPTS} retries: {reason}")

    _last_veo_request = _time.time()
    logger.info("[BG] Veo 3 operation: %s", operation_name)

    # Poll the operation. The REST endpoint mirrors the model URL prefix.
    poll_url = (
        f"https://{_VERTEX_LOCATION}-aiplatform.googleapis.com/v1/{operation_name}"
    )
    fetch_url = f"{base_url}:fetchPredictOperation"
    poll_deadline = _time.time() + 600
    op_payload: dict | None = None
    # U10 (audit 2026-05-25): heartbeat cada 60s para que el reaper no
    # mate este job durante el Veo poll (hasta 10min sin update_job natural).
    _hb_counter = 0
    # Sub-progress crawl during Veo (audit 2026-05-27 "638" operator):
    # Without this, the bar froze at progress=22 for the entire 60-180s
    # Veo poll and the user thought the system was stuck. We tick
    # progress=22→38 in lockstep with elapsed seconds — caps at 38 so
    # the next caller (`update_job(progress=40)` after `_ensure_background()`
    # returns) doesn't have to overwrite a decrement. See `step_eta.py`
    # for the progress range (22..40 for "background" step).
    _veo_started_at = _time.time()
    _last_progress_written = 22
    while True:
        if _time.time() > poll_deadline:
            raise TimeoutError("Veo 3 operation timed out after 10 min")
        _time.sleep(10)
        _hb_counter += 1
        if job_id:
            # Crawl progress: +1% per 10s elapsed, capped at 38 so we leave
            # the 39→40 transition for `_ensure_background()`'s caller.
            _elapsed = _time.time() - _veo_started_at
            _sub_progress = min(38, 22 + int(_elapsed / 10))
            if _sub_progress > _last_progress_written:
                try:
                    from jobs import update_job as _uj
                    _uj(job_id, progress=_sub_progress)
                    _last_progress_written = _sub_progress
                except Exception:  # pragma: no cover
                    pass  # liveness is best-effort; never block Veo on it.
            elif _hb_counter % 6 == 0:
                # No new progress tick (already at cap 38) — keep the
                # heartbeat for the reaper.
                try:
                    from jobs import heartbeat as _heartbeat
                    _heartbeat(job_id)
                except Exception:  # pragma: no cover
                    pass
        token = _veo_access_token()
        # Vertex's long-running publisher operations need the
        # fetchPredictOperation helper (a plain GET on the operation name
        # returns 404 for publisher models). Body carries the operation name.
        r = _req.post(
            fetch_url,
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
                "x-goog-user-project": _VERTEX_PROJECT,
            },
            json={"operationName": operation_name},
            timeout=30,
        )
        if not r.ok:
            logger.warning("[BG] poll HTTP %s: %s; retrying...", r.status_code, r.text[:200])
            continue
        op_payload = r.json()
        if op_payload.get("done"):
            break

    if "error" in op_payload:
        err = op_payload["error"]
        if recorder:
            recorder.finish(response_summary=f"error: {str(err)[:200]}")
        raise RuntimeError(f"Veo operation failed: {err}")

    response_data = op_payload.get("response", {})
    videos = response_data.get("videos") or response_data.get("generatedVideos") or []
    if not videos:
        if recorder:
            recorder.finish(response_summary=f"error: no videos in response: {response_data}")
        raise RuntimeError(f"Veo response had no videos: {response_data}")

    video_entry = videos[0]
    # Field name varies between API versions: gcsUri / videoUri / video.uri
    video_uri = (
        video_entry.get("gcsUri")
        or video_entry.get("videoUri")
        or (video_entry.get("video") or {}).get("uri")
    )
    bytes_b64 = video_entry.get("bytesBase64Encoded") or (
        video_entry.get("video") or {}
    ).get("bytesBase64Encoded")

    if bytes_b64:
        # Inline bytes — decode and write directly.
        import base64 as _b64
        with open(output_path, "wb") as f:
            f.write(_b64.b64decode(bytes_b64))
    elif video_uri:
        token = _veo_access_token()
        dl = _req.get(
            video_uri,
            headers={"Authorization": f"Bearer {token}"},
            timeout=120,
        )
        dl.raise_for_status()
        with open(output_path, "wb") as f:
            f.write(dl.content)
    else:
        if recorder:
            recorder.finish(response_summary=f"error: video has no uri/bytes: {video_entry}")
        raise RuntimeError(f"Veo video has no uri or bytes: {video_entry}")

    size_mb = os.path.getsize(output_path) / 1024 / 1024
    logger.info("[BG] Veo 3 video saved: %.1f MB (raw)", size_mb)

    # Apply subtle gaussian blur. Veo Fast outputs are slightly softer than
    # standard; a small blur normalises that softness, hides minor artefacts,
    # and improves contrast for the lyric overlay rendered on top.
    import subprocess as _sp
    blurred = output_path + ".blurred.mp4"
    try:
        _sp.run(
            [
                "ffmpeg", "-y", "-loglevel", "error",
                "-i", output_path,
                "-vf", f"gblur=sigma={blur_sigma}",
                "-c:a", "copy",
                blurred,
            ],
            check=True,
            timeout=60,
        )
        os.replace(blurred, output_path)
        size_mb = os.path.getsize(output_path) / 1024 / 1024
        logger.info("[BG] Blur applied (sigma=%s): %.1f MB", blur_sigma, size_mb)
    except Exception as e:
        logger.warning("[BG] Blur skipped (non-fatal): %s", e)
        if os.path.exists(blurred):
            try:
                os.unlink(blurred)
            except OSError:
                pass

    if _storage.is_enabled():
        try:
            _storage.upload_file(output_path, cache_object_key)
            logger.info("[BG] Veo cache STORED: %s", cache_object_key)
        except Exception as e:
            logger.warning("[BG] Veo cache upload failed (non-fatal): %s", e)

    if recorder:
        recorder.finish(
            response_summary=f"video_generated: {size_mb:.1f}MB key={cache_key_hash}",
            output_artifact=output_path,
        )
    return output_path


def _generate_imagen_image(prompt: str, output_path: str, max_retries: int = 5,
                            job_id: str = None, model: str | None = None,
                            allow_people: bool = False) -> str:
    """Generate an image with Google Imagen 4. Auto-retries on rate limit.

    `model` lets the caller override the default. Library generation can
    pass `imagen-4.0-ultra-generate-001` for marquee-quality stills;
    runtime job rendering keeps the standard tier for cost reasons.

    `allow_people`: when True, the safe-prompt suffix drops the
    "no people / no faces / no hands" clauses so Imagen can render
    subjects the operator's prompt requested. Logo/text negatives stay.
    """
    from google import genai
    from google.genai.errors import ClientError
    from provenance import record_ai_call
    import time as _time

    client = _get_genai_client()

    chosen_model = (model
                    or os.environ.get("IMAGEN_MODEL")
                    or "imagen-4.0-generate-001").strip()

    _people_suffix = "" if allow_people else " no people, no faces, no hands,"
    safe_prompt = f"{prompt}. No text, no words, no letters,{_people_suffix} no logos, no readable signage."

    recorder = record_ai_call(
        job_id=job_id or "unknown",
        step="image_bg",
        tool_name=chosen_model,
        tool_provider="google_vertex",
        prompt=safe_prompt,
        input_data_types=["generated_prompt"],
    ) if job_id else None

    for attempt in range(max_retries):
        try:
            logger.info("[BG] %s: generating image (attempt %s)...", chosen_model, attempt + 1)
            # Audit 2026-05-26: timeout wrapper. Imagen-4 p99 is ~12s; 90s
            # is 7× headroom but bounds the worst case. Without this, a
            # Vertex hang during background generation would block the
            # worker for ages — find_orphan_polling_jobs catches it at 10
            # min but only via AIProvenance, which Imagen records only
            # after the call returns. Better to fail fast and let the
            # outer retry loop reschedule.
            response = _call_with_timeout(
                lambda: client.models.generate_images(
                    model=chosen_model,
                    prompt=safe_prompt,
                    config=genai.types.GenerateImagesConfig(
                        number_of_images=1,
                        aspect_ratio="16:9",
                    ),
                ),
                timeout_s=90.0,
                label="IMAGEN",
            )
            break
        except ClientError as e:
            if "429" in str(e) or "RESOURCE_EXHAUSTED" in str(e):
                wait = 60 * (attempt + 1)
                logger.warning("[BG] Rate limited, waiting %ss before retry...", wait)
                _time.sleep(wait)
            else:
                if recorder:
                    recorder.finish(response_summary=f"error: {str(e)[:200]}")
                raise
    else:
        if recorder:
            recorder.finish(response_summary="error: rate_limit_exceeded")
        raise RuntimeError("Imagen 4 rate limit exceeded after all retries")

    image = response.generated_images[0]
    # Save image bytes
    img_bytes = image.image.image_bytes
    with open(output_path, "wb") as f:
        f.write(img_bytes)

    size_kb = os.path.getsize(output_path) / 1024
    logger.info("[BG] Imagen 4 saved: %.0f KB", size_kb)
    if recorder:
        recorder.finish(
            response_summary=f"image_generated: {size_kb:.0f}KB",
            output_artifact=output_path,
        )
    return output_path


def _extract_frame_from_video(video_path: str, output_image_path: str) -> str:
    """Extract a representative still frame from a video and save it as PNG.

    Used by the "library variation" flow: we pick a frame from the
    user-selected library video and pass it to Veo as image-to-video
    seed so Veo derives a new clip visually similar to the original.

    The chosen timestamp is the middle of the clip — the first second
    is often a fade-in / black frame and the last second a fade-out, so
    the middle is the most representative single frame.

    Raises RuntimeError if ffprobe/ffmpeg is unavailable or the file is
    not a readable video.
    """
    try:
        probe = subprocess.run(
            [
                "ffprobe", "-v", "error",
                "-show_entries", "format=duration",
                "-of", "default=noprint_wrappers=1:nokey=1",
                video_path,
            ],
            capture_output=True, text=True, timeout=30,
        )
        duration = float((probe.stdout or "0").strip() or 0.0)
    except (subprocess.SubprocessError, ValueError, FileNotFoundError) as e:
        raise RuntimeError(f"ffprobe failed on {video_path}: {e}") from e
    timestamp = max(0.0, duration / 2.0) if duration > 0 else 0.0

    cmd = [
        "ffmpeg", "-y",
        "-ss", f"{timestamp:.3f}",
        "-i", video_path,
        "-frames:v", "1",
        "-vf", "scale='min(1920,iw)':-2",
        output_image_path,
    ]
    # output_path= catches the rc=0-but-no-file case ffmpeg occasionally
    # produces when the input is healthy but the seek lands past the
    # final frame — pre-fix the caller crashed on the next os.stat().
    run_checked(
        cmd,
        label="ffmpeg-bg-frame",
        timeout=60,
        output_path=output_image_path,
    )
    return output_image_path


# Umbral del detector de corte de escena. Calibrado 2026-06-10 con los
# clips REALES del incidente del mural (universal_argentina): los morphs
# contaminados (mural→café, mural→bosque) midieron 0.261-0.270; los clips
# sanos (café text-to-video, café edit, mural estático) midieron
# 0.004-0.059. 0.18 deja 3x de margen contra falsos positivos (un retry
# innecesario cuesta ~$0.80-3.20 de Veo) y 1.4x contra falsos negativos.
_BG_SCENE_CUT_THRESHOLD = float(os.environ.get("BG_SCENE_CUT_THRESHOLD", "0.18"))


def _frame_pair_discontinuity(frame_a, frame_b) -> float:
    """Núcleo puro del detector: distancia entre dos frames RGB (0.0-1.0).

    Métrica: 0.5 * MAE de píxeles (downsampleado 8x) + 0.5 * distancia
    de variación total de histogramas RGB (16 bins). El histograma
    aguanta movimiento de cámara/gente (escena igual, píxeles corridos);
    el MAE aguanta paletas parecidas con contenido distinto.
    """
    import numpy as _np
    a = _np.asarray(frame_a, dtype="float32")[::8, ::8]
    b = _np.asarray(frame_b, dtype="float32")[::8, ::8]
    mae = float(_np.mean(_np.abs(a - b))) / 255.0
    hist_d = 0.0
    for c in range(3):
        ha, _ = _np.histogram(a[..., c], bins=16, range=(0, 255))
        hb, _ = _np.histogram(b[..., c], bins=16, range=(0, 255))
        ha = ha / max(1, ha.sum())
        hb = hb / max(1, hb.sum())
        hist_d += float(_np.abs(ha - hb).sum()) / 2.0
    return 0.5 * mae + 0.5 * (hist_d / 3.0)


def _bg_scene_discontinuity(video_path: str) -> float:
    """Mide si el clip de fondo contiene un cambio de escena (0.0-1.0).

    Incidente 2026-06-09 (mural de Bersuit): Veo image-to-video con una
    semilla que no matchea el prompt devuelve un clip que ARRANCA en la
    semilla y morphea hacia el prompt — dos escenas en 8s que el loop
    palindrómico repite todo el video. El único QA previo era el
    relevance score sobre UN frame (t=4s), ciego al corte. Acá
    comparamos el primer y el último frame del clip: si son escenas
    distintas, el clip no sirve como fondo loopeable.

    Extracción vía ffmpeg subprocess (no moviepy): más liviano que abrir
    un VideoFileClip y testeable con el stub de moviepy del conftest.

    Fail-open: ante cualquier error devuelve 0.0 — un bug acá jamás
    debe bloquear un fondo bueno (mismo contrato que el relevance score).
    """
    try:
        from PIL import Image as _Img

        def _grab(out_png: str, seek: list[str]):
            run_checked(
                ["ffmpeg", "-y", "-loglevel", "error", *seek,
                 "-i", video_path, "-frames:v", "1", out_png],
                label="ffmpeg-scene-cut-frame",
                timeout=60,
                output_path=out_png,
            )
            with _Img.open(out_png) as im:
                return im.convert("RGB").copy()

        base = video_path + ".scenecut"
        first = _grab(base + "_a.png", ["-ss", "0.05"])
        # sseof busca desde el final — no necesitamos conocer la duración.
        last = _grab(base + "_b.png", ["-sseof", "-0.3"])
        for _p in (base + "_a.png", base + "_b.png"):
            try:
                os.unlink(_p)
            except OSError:
                pass
        return _frame_pair_discontinuity(first, last)
    except Exception as e:
        logger.warning("[BG][SCENE-CUT] check failed (fail-open): %s", e)
        return 0.0


def _score_video_relevance(video_path: str, prompt: str) -> int:
    """Ask Gemini Vision whether the video matches the intended scene prompt.

    Extracts one frame and returns a relevance score 1-10.
    Fails open (returns 8) so a Gemini error never blocks a good video.
    """
    from google import genai
    import tempfile

    tmp_frame = None
    try:
        with tempfile.NamedTemporaryFile(suffix=".jpg", delete=False) as f:
            tmp_frame = f.name
        _extract_frame_from_video(video_path, tmp_frame)

        client = _get_genai_client()
        with open(tmp_frame, "rb") as f:
            image_bytes = f.read()

        # Audit 2026-05-26: timeout wrapper. This call runs once per
        # generated Veo video; a hang here adds to total render latency
        # but is gated by the same outer except → fall-through to score=8.
        # 30 s suffices (single-frame Vision call is fast).
        response = _call_with_timeout(
            lambda: client.models.generate_content(
                model="gemini-2.5-flash",
                contents=[
                    genai.types.Part.from_bytes(data=image_bytes, mime_type="image/jpeg"),
                    (
                        f"This is a frame from an AI-generated background video.\n"
                        f"Intended scene: \"{prompt}\"\n\n"
                        f"Score how well the frame matches the intended scene, 1-10.\n"
                        f"Focus on whether the MAIN SUBJECT is correct "
                        f"(e.g. if the scene should show a football pitch but shows cars, score 1-2).\n"
                        f"Respond with ONLY a single integer, nothing else."
                    ),
                ],
                config=genai.types.GenerateContentConfig(
                    temperature=0.0,
                    max_output_tokens=5,
                    thinking_config=genai.types.ThinkingConfig(thinking_budget=0),
                ),
            ),
            timeout_s=30.0,
            label="VIDEO-SCORE",
        )
        import re as _re
        m = _re.search(r'\b(10|[1-9])\b', response.text)
        score = int(m.group()) if m else 5
        return max(1, min(10, score))
    except Exception as e:
        logger.warning("[BG] Relevance score error (fail-open): %s", e)
        return 8
    finally:
        if tmp_frame:
            try:
                os.unlink(tmp_frame)
            except OSError:
                pass


def _darken_prompt_for_effect(prompt: str, effect: str) -> str:
    """Bias an Imagen scene prompt toward a LOW-KEY / dark canvas when a
    luminous particle effect (stars / bokeh / snow / light / ...) will be
    screen-blended on top, so the particles actually read.

    Matrix test 2026-06-02: the per-effect pre-blend gain (fx_compositor)
    helps, but a bright AI background still drowns sparse effects — stars over
    a sunlit sky vanish. This gives them a dark canvas. Phrased as a
    grading / mood instruction (NOT "night" / "urban", which the scene guard
    forbids as a cliché): keep the subject and setting, only grade it darker.
    No-op for effect="" / "none" / unknown.
    """
    import fx_compositor as _fxc
    if not effect or effect.strip().lower() not in _fxc.EFFECTS:
        return prompt
    return (
        prompt.rstrip()
        + " The scene will receive a luminous atmospheric particle overlay, so"
        " render it LOW-KEY and moody: rich deep shadows, dark saturated tones,"
        " restrained lighting, and minimal large bright or blown-out areas —"
        " leave dark negative space for the particles to glow against. Keep the"
        " same subject and setting; only grade it darker and moodier."
    )


# ───────────────────────── Multi-escena ("Escenas") ──────────────────────
# Add-on premium: el video deja de ser un loop y pasa a ser un CONJUNTO DE
# ESCENAS con arco. La detección de secciones + el stitch viven en scenes.py
# (determinista, testeable). Acá quedan las piezas que SÍ dependen del
# pipeline: la biblia visual (Gemini), el wrapper de prompt por escena, la
# generación de los N clips Veo y la orquestación. Todo ADITIVO — el camino
# de fondo único de _ensure_background queda intacto.

_BIBLE_FALLBACK_PALETTE = {
    "oscuro": "deep shadows, low-key, desaturated cool tones",
    "neon": "saturated neon magenta and cyan, high contrast",
    "minimal": "muted minimal palette, lots of negative space",
    "calido": "warm golden amber tones, soft light",
    "custom": "cohesive cinematic palette",
}


def _build_visual_bible(lyrics_text: str, artist: str, song_title: str = "",
                        genre: str = "", concept: str = "", style: str = "",
                        custom_colors: str = "", job_id: str = None,
                        background_hint: str | None = None,
                        bg_verbatim: bool = False) -> dict:
    """Una sola llamada Gemini que fija el "look book" del video.

    Devuelve {world, palette, texture, camera, motif} — el ADN visual que TODA
    escena hereda (esto es lo que evita el "random": todas parecen el mismo
    film). Best-effort: si Gemini falla, cae a una biblia determinista derivada
    de style/genre/concept para que el feature nunca tumbe el job.
    """
    fallback = {
        "world": (concept or genre or "cinematic scene grounded in the song's mood"),
        "palette": (custom_colors or _BIBLE_FALLBACK_PALETTE.get(style, "cohesive cinematic palette")),
        "texture": "clean modern digital grade, fine subtle grain, soft cinematic depth of field",
        "camera": "slow, deliberate camera language",
        "motif": "a single recurring light source tying the scenes together",
    }
    try:
        from google import genai
        from provenance import record_ai_call
        client = _get_genai_client()
        sys_instr = (
            "You are an art director defining the SHARED visual world for a "
            "premium lyric video so its scenes feel like ONE film instead of "
            "random clips. Define ONLY what must be consistent across scenes — "
            "the per-scene look, texture and cinematography are decided later by "
            "the scene engine from the song itself, so DON'T impose a fixed "
            "aesthetic. Respond ONLY with a JSON object with exactly these string "
            "keys: world (the setting/environment family), palette (colors + "
            "lighting), texture (a light grade/mood note, kept neutral), camera "
            "(a light note on the camera language), motif (one recurring visual "
            "element). Keep each value under 25 words. No people's faces, no "
            "text/letters/logos in the described world. "
            # Prohibición factual (no es un patrón — evita un bug): nombrar un
            # formato/calibre de film hace que Veo dibuje el fotograma físico
            # (incidente 2026-06-19, "16mm film grain" → sprockets + marco negro).
            # Belt-and-suspenders: texture/camera además ya NO se inyectan al
            # prompt por-escena (ver scenes._bible_to_prompt_fragment).
            "NEVER name a film FORMAT or gauge (no '16mm', '35mm', '8mm', "
            "'Super 8', 'VHS', 'celluloid', 'film stock', 'analog tape'), and "
            "never describe found-footage, camcorder, viewfinder, film-strip/"
            "sprocket, or on-screen camera-UI aesthetics: naming a physical film "
            "format makes the AI render a literal film frame — sprocket holes, "
            "edge markings, a black border and fake recording chrome — over the "
            "scene."
        )
        # Dirección del operador ("Mi prompt"): moldea TODA la biblia → multi-
        # escena respeta auto/letra/prompt igual que el fondo único. Verbatim =
        # "usá mi visión tal cual" (manda sobre género/letra).
        _hint = (background_hint or "").strip()
        _direction = ""
        if _hint:
            _direction = (
                f"\nOPERATOR DIRECTION (this is the world the operator wants — "
                f"{'use it as the definitive vision, it OVERRIDES genre/lyrics inference' if bg_verbatim else 'honor it strongly while staying coherent with the song'}): {_hint[:600]}"
            )
        user = (f"Artist: {artist}\nTitle: {song_title}\nGenre: {genre}\n"
                f"Concept: {concept}\nPalette hint: {style} {custom_colors}{_direction}\n"
                f"Lyrics (excerpt):\n{(lyrics_text or '')[:600]}")
        recorder = record_ai_call(
            job_id=job_id or "unknown", step="visual_bible",
            tool_name="gemini-2.5-flash", tool_provider="google_vertex",
            prompt=f"system:{sys_instr}\nuser:{user}",
            input_data_types=["artist_name", "lyrics_text_600chars"],
        ) if job_id else None
        resp = _call_with_timeout(
            lambda: client.models.generate_content(
                model="gemini-2.5-flash",
                contents=user,
                config=genai.types.GenerateContentConfig(
                    system_instruction=sys_instr,
                    temperature=0.7,
                    # gemini-2.5-flash es un modelo de *thinking*: por default
                    # gasta tokens de razonamiento que cuentan contra
                    # max_output_tokens. Con 500 y thinking ON, el JSON salía
                    # TRUNCADO (finish_reason=MAX_TOKENS, ~478 tokens de
                    # thinking y la respuesta cortada) → parse fallaba → toda
                    # biblia caía al fallback genérico por género, perdiendo la
                    # coherencia "mismo film" que es el corazón del feature.
                    # Apagamos el thinking (no lo necesita para un look-book) y
                    # forzamos JSON puro vía response_mime_type (sin ```json).
                    max_output_tokens=800,
                    response_mime_type="application/json",
                    thinking_config=genai.types.ThinkingConfig(thinking_budget=0),
                ),
            ),
            timeout_s=45.0, label="SCENES-BIBLE",
        )
        text = (resp.text or "").strip()
        bible = _parse_json_object(text)
        if recorder:
            recorder.finish(response_summary=text[:300])
        if bible and isinstance(bible, dict):
            # Completar claves faltantes con el fallback (Gemini a veces omite una).
            merged = {k: (str(bible.get(k) or fallback[k]).strip()) for k in fallback}
            # Sanitizar formatos de film aunque el LLM los emita igual: nombrar un
            # calibre (16mm/35mm/Super8/VHS…) hace que Veo dibuje el fotograma
            # físico —sprockets, marcas de borde, marco negro, UI falsa— (incidente
            # 2026-06-19, "Intoxicados": texture="16mm film grain" → marco de film).
            return _sanitize_bible_film_formats(merged)
        logger.warning("[SCENES] biblia: parse falló, uso fallback. raw=%s", text[:200])
    except Exception as e:  # noqa: BLE001
        logger.warning("[SCENES] biblia visual falló (%s) — uso fallback determinista", e)
    return fallback


# Tokens que hacen que Veo dibuje un fotograma de film FÍSICO (sprockets, marcas
# de borde, marco negro) o cromo de grabación falso. Nombrar un calibre/formato
# es el disparador; "grain" como mood es inofensivo, así que sólo le sacamos el
# "film". Orden importa: los multi-palabra antes que los sueltos.
_FILM_FORMAT_SUBS = [
    (re.compile(r"\bsuper\s*-?\s*8\b", re.I), "fine"),
    (re.compile(r"\b(?:8|16|35|65|70)\s*mm\b", re.I), "fine"),
    (re.compile(r"\bfound[\s-]*footage\b", re.I), ""),
    (re.compile(r"\b(?:film\s+stock|celluloid|analog(?:ue)?\s+tape|"
                r"magnetic\s+tape|betacam|hi-?8|mini-?dv)\b", re.I), ""),
    (re.compile(r"\b(?:vhs|camcorder|viewfinder|sprocket(?:\s+holes?)?|"
                r"film\s+strip|film\s+border|film[\s-]*frame)\b", re.I), ""),
    (re.compile(r"\bfilm\s+grain\b", re.I), "grain"),
]


def _sanitize_bible_film_formats(bible: dict) -> dict:
    """Saca nombres de formato/calibre de film de los valores de la biblia.

    Nombrar "16mm/35mm/Super 8/VHS/film stock" hace que Veo renderice el
    fotograma físico (sprockets, marco negro, UI de grabación falsa) por mucho
    que el system prompt lo prohíba. Esto lo limpia post-parse —no depende de que
    el LLM obedezca— y deja el grano/grade como mood ('grain', 'soft grade').
    """
    out = {}
    for k, v in bible.items():
        s = str(v or "")
        for pat, repl in _FILM_FORMAT_SUBS:
            s = pat.sub(repl, s)
        # Limpieza cosmética: espacios dobles y comas/espacios colgando.
        s = re.sub(r"\s{2,}", " ", s)
        s = re.sub(r"\s+([,.;])", r"\1", s)
        s = re.sub(r"([,;])\s*(?=[,;])", "", s)
        out[k] = s.strip(" ,;").strip()
    return out


def _parse_json_object(text: str) -> dict | None:
    """Parser tolerante: JSON pelado o envuelto en ```json ... ```."""
    if not text:
        return None
    import json as _json
    t = text.strip()
    if t.startswith("```"):
        t = t.strip("`")
        if t.lower().startswith("json"):
            t = t[4:]
    a, b = t.find("{"), t.rfind("}")
    if a >= 0 and b > a:
        t = t[a:b + 1]
    try:
        obj = _json.loads(t)
        return obj if isinstance(obj, dict) else None
    except Exception:
        return None


def _make_scene_prompt_fn(lyrics_text, artist, song_title, genre, concept,
                          style, custom_colors, job_id, allow_people):
    """Fabrica la callable que scenes.build_scene_plan usa por escena.

    Reusa _get_unique_prompt (toda la maquinaria de seguridad/de-bias de Gemini)
    pasando la biblia + el beat de la sección como background_hint NO-verbatim,
    así Gemini ancla por sección pero hereda el ADN visual de la biblia.
    """
    def prompt_fn(background_hint="", movement_style="", section_type="", energy=0.0):
        return _get_unique_prompt(
            lyrics_text=lyrics_text, artist=artist, job_id=job_id,
            song_title=song_title, genre=genre, concept=concept,
            movement_style=movement_style, match_lyrics=True,
            background_hint=background_hint, bg_verbatim=False,
            palette_style=style, custom_colors=custom_colors,
            allow_people=allow_people,
        )
    return prompt_fn


def _scene_cache_ns(artist: str, song_title: str, key: str, token: str = "") -> str:
    """Namespace de caché R2 por escena. El `cache_token` (vacío en la
    generación inicial) cambia cuando el operador regenera UNA escena: así su
    clip se cachea bajo una key NUEVA (cache miss → Veo fresco) mientras las
    demás escenas siguen pegando su caché original (re-stitch sin costo). Al
    persistirse el token en scene_plan, un edit posterior re-baja la versión
    regenerada, no la vieja."""
    base = f"{artist}|{song_title}|{key}"
    return f"{base}|{token}" if token else base


def _persist_scene_thumb(clip_path: str, key: str, job_id: str) -> str | None:
    """Extrae un póster del clip y lo sube a R2; devuelve la key (o None).
    Best-effort: un thumb faltante no debe tumbar la generación del video."""
    if not job_id:
        return None
    try:
        import storage as _storage
        import scenes as _scenes
        if not _storage.is_enabled():
            return None
        thumb_local = os.path.join(os.path.dirname(clip_path), f"thumb_{key}.jpg")
        if not _scenes.extract_thumbnail(clip_path, thumb_local):
            return None
        return _storage.upload_file(thumb_local, f"scenes/{job_id}/thumb_{key}.jpg") or None
    except Exception as e:  # noqa: BLE001
        logger.warning("[SCENES] thumb de %s falló (%s) — sin póster", key, e)
        return None


def _generate_scene_clips(scene_plan: dict, job_dir: str, *, artist: str,
                          song_title: str, concept: str = "", job_id: str = None,
                          allow_people: bool = False,
                          regen_keys: set | None = None) -> dict:
    """Genera un clip Veo por escena ÚNICA. Devuelve {recurrence_key: clip_path}.

    - cache_namespace incluye la recurrence_key + el cache_token de la escena →
      escenas distintas no colisionan, los coros recurrentes (1 escena) pegan
      caché, y una escena regenerada (token nuevo) genera fresco mientras las
      otras re-bajan su caché sin costo.
    - `regen_keys`: si se pasa, SÓLO esas escenas pueden generar fresco en Veo;
      el resto va `cache_only=True` → se sirven de la caché R2 o se degradan,
      pero NUNCA re-cobran. Garantía de costo del regen: regenerar 1 escena no
      puede facturar las otras N aunque la caché falle (antes sí podía).
    - Degradación elegante: si una escena falla (incl. cache_only miss), reusa el
      primer clip exitoso en vez de tumbar el video. Si NINGUNA generó, levanta
      para que el caller caiga al fondo único.
    """
    import veo_breaker
    if veo_breaker.is_open():
        raise RuntimeError("veo breaker OPEN — multi-escena no puede generar clips")

    clip_for_key: dict[str, str] = {}
    first_ok = None
    for scene in scene_plan.get("scenes", []):
        key = scene["recurrence_key"]
        if key in clip_for_key:
            continue
        clip_path = os.path.join(job_dir, f"bg_scene_{key}.mp4")
        # En un regen (regen_keys set), las escenas NO-target son cache_only:
        # se sirven de caché o se degradan, nunca pagan Veo de nuevo.
        _cache_only = regen_keys is not None and key not in regen_keys
        _meta = {}
        try:
            _generate_veo_video(
                scene["prompt"], clip_path, job_id=job_id,
                cache_namespace=_scene_cache_ns(artist, song_title, key, scene.get("cache_token", "")),
                movement_style=scene.get("movement_style", ""),
                normalized_concept=_normalize_concept(concept),
                allow_people=allow_people,
                cache_only=_cache_only,
                out_meta=_meta,
            )
            if not os.path.exists(clip_path):
                raise RuntimeError("clip no escrito")
            # NIT: no persistimos clip_path (es un path local efímero del job_dir
            # que filtraba el filesystem del contenedor al JSON/DB). El stitch usa
            # clip_for_key (abajo), no scene["clip_path"].
            scene["status"] = "generated"
            if _meta.get("cache_object_key"):
                scene["clip_cache_key"] = _meta["cache_object_key"]
            # Póster para el filmstrip (best-effort). Sólo si cambió el clip.
            if regen_keys is None or key in regen_keys or not scene.get("thumb_key"):
                _tk = _persist_scene_thumb(clip_path, key, job_id)
                if _tk:
                    scene["thumb_key"] = _tk
            clip_for_key[key] = clip_path
            first_ok = first_ok or clip_path
        except Exception as e:  # noqa: BLE001
            logger.error("[SCENES] escena %s falló (%s) — se sustituye por una válida", key, e)
            scene["status"] = "failed"
    if not first_ok:
        raise RuntimeError("ninguna escena Veo se generó — fallback a fondo único")
    # Rellenar las que fallaron con un clip válido (mantiene el timeline entero).
    for scene in scene_plan.get("scenes", []):
        clip_for_key.setdefault(scene["recurrence_key"], first_ok)
    return clip_for_key


def _generate_scene_background(segments: list[dict], audio_duration: float,
                              job_dir: str, *, style_hint: str, lyrics_text: str,
                              artist: str, song_title: str = "", genre: str = "",
                              concept: str = "", movement_style: str = "",
                              custom_colors: str = "", background_hint: str | None = None,
                              bg_verbatim: bool = False, allow_people: bool = False,
                              job_id: str = None, target_w: int = 1920,
                              target_h: int = 1080) -> tuple[str, dict]:
    """Orquesta el fondo multi-escena. Devuelve (timeline_path, scene_plan).

    detect → biblia → scene plan → N clips Veo (con recurrencia de coro) →
    stitch con xfade. El timeline cubre toda la canción y entra al render con
    bg_prelooped=True. Cualquier fallo levanta para que run_pipeline caiga al
    camino de fondo único (cero regresión).
    """
    import scenes as _scenes
    secs = _scenes.detect_sections(segments, audio_duration)
    n_unique = len({s.recurrence_key for s in secs})
    logger.info("[SCENES] %d secciones, %d escenas únicas (canción %.0fs)",
                len(secs), n_unique, audio_duration or 0.0)
    bible = _build_visual_bible(lyrics_text, artist, song_title, genre, concept,
                                style_hint, custom_colors, job_id,
                                background_hint=background_hint, bg_verbatim=bg_verbatim)
    prompt_fn = _make_scene_prompt_fn(lyrics_text, artist, song_title, genre,
                                      concept, style_hint, custom_colors, job_id,
                                      allow_people)
    plan = _scenes.build_scene_plan(secs, bible, prompt_fn, artist=artist,
                                    song_title=song_title, style=style_hint,
                                    operator_movement=movement_style)
    clip_for_key = _generate_scene_clips(plan, job_dir, artist=artist,
                                         song_title=song_title, concept=concept,
                                         job_id=job_id, allow_people=allow_people)
    # Audit M3: exponer fallo parcial a nivel job. Las escenas fallidas se
    # sustituyen por un clip válido (degradación), pero el operador debe saber
    # cuántas — el filmstrip ya marca ⚠ por escena; esto da el agregado para un
    # badge a nivel job sin abrir el filmstrip.
    _failed = sum(1 for s in plan.get("scenes", []) if s.get("status") == "failed")
    plan["degraded"] = {"failed": _failed, "total": len(plan.get("scenes", []))}
    # Audit LOW: persistir la duración usada como fuente única, así el re-stitch
    # de un edit/regen no difiere por un frame entre _audio_duration y ffprobe.
    plan["audio_duration"] = float(audio_duration or 0.0)
    if _failed:
        logger.warning("[SCENES] %d/%d escenas fallaron (degradado a clip reusado) para job=%s",
                       _failed, len(plan.get("scenes", [])), job_id)
    timeline = _scenes.stitch_timeline(secs, clip_for_key, audio_duration, job_dir,
                                       target_w=target_w, target_h=target_h)
    return timeline, plan


def _regenerate_scene_background(scene_plan: dict, recurrence_key: str, job_dir: str, *,
                                 artist: str, song_title: str, audio_duration: float,
                                 concept: str = "", allow_people: bool = False,
                                 job_id: str = None, prompt_override: str = "",
                                 hint: str = "", movement_style: str = "",
                                 lyrics_text: str = "", genre: str = "",
                                 style_hint: str = "", custom_colors: str = "",
                                 target_w: int = 1920, target_h: int = 1080) -> tuple[str, dict]:
    """Regenera UNA escena del plan y re-arma el timeline. Devuelve
    (timeline_path, scene_plan_actualizado).

    Quirúrgico y barato: sólo la escena `recurrence_key` se regenera en Veo
    (cache_token nuevo → cache miss); las demás re-bajan su clip de la caché R2
    (mismo prompt+token → cache HIT, sin costo). Luego re-stitch del timeline.

    Tres modos según lo que pida el operador:
      - prompt_override: reemplaza el prompt de la escena tal cual.
      - hint: re-deriva el prompt heredando la biblia + el hint del operador.
      - ninguno ("otra toma"): mismo prompt, sólo el token nuevo → otra versión.
    """
    import scenes as _scenes
    import uuid

    target = next((s for s in scene_plan.get("scenes", [])
                   if s.get("recurrence_key") == recurrence_key), None)
    if target is None:
        raise ValueError(f"escena {recurrence_key!r} no existe en el plan")

    if movement_style:
        target["movement_style"] = movement_style
    if (prompt_override or "").strip():
        target["prompt"] = prompt_override.strip()
    elif (hint or "").strip():
        # Re-derivar el prompt con el hint, heredando la biblia (coherencia).
        prompt_fn = _make_scene_prompt_fn(lyrics_text, artist, song_title, genre,
                                          concept, style_hint, custom_colors, job_id,
                                          allow_people)
        bible_text = _scenes._bible_to_prompt_fragment(scene_plan.get("bible") or {})
        base_hint = ". ".join(x for x in (bible_text, hint.strip()) if x)
        try:
            res = prompt_fn(background_hint=base_hint,
                            movement_style=target.get("movement_style", ""),
                            section_type=target.get("section_type", ""),
                            energy=target.get("energy", 0.5))
            target["prompt"] = (res or {}).get("prompt") or target["prompt"]
        except Exception as e:  # noqa: BLE001
            logger.warning("[SCENES] regen prompt_fn falló (%s); mantengo prompt", e)

    # Bust de caché → Veo fresco SÓLO para esta escena. Guardamos la key vieja
    # para GC tras generar la nueva (audit M8: sin esto cada "otra toma" deja un
    # clip pago huérfano en cache/veo/ para siempre).
    _old_clip_key = target.get("clip_cache_key")
    target["cache_token"] = uuid.uuid4().hex[:8]
    target["status"] = "planned"

    clip_for_key = _generate_scene_clips(scene_plan, job_dir, artist=artist,
                                         song_title=song_title, concept=concept,
                                         job_id=job_id, allow_people=allow_people,
                                         regen_keys={recurrence_key})
    # GC del clip viejo (audit M8): sólo si la regen produjo uno NUEVO distinto.
    _new_clip_key = target.get("clip_cache_key")
    if _old_clip_key and _new_clip_key and _old_clip_key != _new_clip_key:
        try:
            import storage as _storage
            if _storage.is_enabled():
                _storage.delete_object(_old_clip_key)
                logger.info("[SCENES] GC clip Veo viejo %s (regen %s)", _old_clip_key, recurrence_key)
        except Exception as _e:  # noqa: BLE001
            logger.warning("[SCENES] GC del clip viejo falló (%s) — huérfano queda en R2", _e)
    sections = _scenes.sections_from_plan(scene_plan)
    timeline = _scenes.stitch_timeline(sections, clip_for_key, audio_duration, job_dir,
                                       target_w=target_w, target_h=target_h)
    return timeline, scene_plan


def _restitch_scenes_for_edit(scene_plan: dict, segments: list[dict],
                              audio_duration: float, job_dir: str, *, artist: str,
                              song_title: str, concept: str = "",
                              allow_people: bool = False, job_id: str = None,
                              target_w: int = 1920, target_h: int = 1080):
    """Re-arma el timeline multi-escena con la LETRA editada, SIN Veo.

    Audit M1: un edit de lyrics cambia los timings; las secciones persistidas
    quedan stale y los cortes desincronizan. Acá re-detectamos secciones con los
    segments nuevos y re-stitcheamos desde los clips CACHEADOS (cache_only → cero
    costo). Sólo si la estructura de recurrencia no cambió (las keys nuevas son
    subconjunto de las que ya tienen clip); si cambió, devolvemos (None, plan)
    y el caller reusa el timeline cacheado estático (degradación segura).
    """
    import scenes as _scenes
    new_secs = _scenes.detect_sections(segments, audio_duration)
    new_keys = {s.recurrence_key for s in new_secs}
    have_keys = {sc.get("recurrence_key") for sc in scene_plan.get("scenes", [])}
    if not new_keys or not new_keys.issubset(have_keys):
        return None, scene_plan  # estructura cambió → no es seguro re-stitch
    scene_plan = {**scene_plan, "sections": [s.to_dict() for s in new_secs]}
    # regen_keys=set() → TODAS las escenas son cache_only (ninguna paga Veo).
    clip_for_key = _generate_scene_clips(scene_plan, job_dir, artist=artist,
                                         song_title=song_title, concept=concept,
                                         job_id=job_id, allow_people=allow_people,
                                         regen_keys=set())
    timeline = _scenes.stitch_timeline(new_secs, clip_for_key, audio_duration,
                                       job_dir, target_w=target_w, target_h=target_h)
    return timeline, scene_plan


def _ensure_background(style_hint: str, job_dir: str, lyrics_text: str = None,
                       artist: str = "", job_id: str = None,
                       song_title: str = "", genre: str = "",
                       concept: str = "",
                       movement_style: str = "",
                       image_to_video_path: str | None = None,
                       match_lyrics: bool = True,
                       background_hint: str | None = None,
                       bg_mode: str = "veo",
                       bg_verbatim: bool = False,
                       custom_colors: str = "",
                       effect: str = "",
                       allow_people: bool = False,
                       audio_duration: float | None = None) -> str:
    """Generate background using AI. Gemini picks the best style for the song.

    background_hint: optional free-form operator description, set via /edit
    when the user clicks "Regenerar fondo" and types what they want. Flows
    into Gemini's user_content as a [OPERATOR OVERRIDE] block.

    bg_mode: "veo" (default — cinematic text-to-video, Google Veo 3.1) or
    "imagen" (Imagen-4 text-to-image + Ken Burns animation locally). Imagen
    mode is ~30x faster, ~17x cheaper, and avoids the face-validation
    failures that Veo hits on certain prompts (incident 2026-05-15: Veo
    inserted an identifiable face into "Lunes Por La Madrugada" bg). It
    trades cinematic camera movement for a simpler zoom/pan over a still.
    Operator picks via the /edit modal segmented toggle when they regen
    the background; default stays "veo" so the existing flow is unchanged.

    Returns path to .mp4 (video style) or .jpg/.png (photo/illustration style).
    """
    # If there are video files in backgrounds dir, use those instead
    all_videos = []
    if os.path.isdir(BACKGROUNDS_DIR):
        for root, _, files in os.walk(BACKGROUNDS_DIR):
            all_videos.extend(f for f in files if f.lower().endswith(".mp4"))
    if all_videos:
        return None

    _norm_move_bg = _normalize_movement_style(movement_style)

    # UMG-style fix (2026-05-25): si el operador eligió "Estático" o "Sutil"
    # SIN effect overlay, default a "light" (el más sutil de los 5). Las
    # references UMG NUNCA tienen 100% quieto — siempre hay particles
    # overlay o motion in-scene. Combined con subtle Ken Burns drift, esto
    # garantiza que el video parezca vivo.
    # Operator override: si seteó effect="" pero tildó algún otro motion
    # (no estatico/sutil), respetamos su elección (no forzamos light).
    # Foto fija / estático / sutil SIN effect → default a "light" (el más sutil)
    # para que el fondo nunca quede 100% muerto. La "Foto fija" (ex foto-parallax)
    # ya no panea — la vida/movimiento la da el efecto componible (lluvia/nieve/
    # luces/estrellas/bokeh vía fx_compositor). El operador elige el efecto en el
    # wizard; este default sólo cubre el caso sin elección.
    # The operator's ACTUAL effect choice, captured BEFORE the anti-dead-frame
    # "light" default below — used to gate background darkening so we darken for
    # the effect the operator really picked (and no-op when they picked none),
    # not for the forced default. (Review fix 2026-06-03: `effect` was never
    # forwarded here, so darkening fired off the forced "light" for every still.)
    _operator_effect = (effect or "").strip().lower()
    if _norm_move_bg in ("estatico", "sutil", "foto-parallax") and not (effect or "").strip():
        logger.info(
            "[BG] movement=%s + no effect selected — defaulting effect=light "
            "para evitar foto 100%% quieta (UMG-style guideline 2026-05-25)",
            _norm_move_bg,
        )
        effect = "light"

    # Animado is a Veo-only aesthetic (the 2D-illustration safe_prompt lives in
    # _generate_veo_video). Imagen renders stills, so an animado+imagen combo
    # is incoherent — downgrade to Veo. (Matrix rule: Imagen × Animado → Veo.)
    if _norm_move_bg == "animado" and bg_mode != "veo":
        logger.info("[BG] movement=animado overrides bg_mode → veo")
        bg_mode = "veo"
    # Foto + parallax is a still photo that gains depth via a slow LATERAL pan.
    # Veo can't do clean 2.5D parallax from a text prompt (it comes out muddy),
    # so this register always routes through Imagen-4 + the lateral Ken Burns
    # pan — controllable, premium, and consistent demo↔output. (Matrix rule:
    # foto-parallax → Imagen × lateral, regardless of the operator's bg_mode.)
    elif _norm_move_bg == "foto-parallax" and bg_mode != "imagen":
        logger.info("[BG] movement=foto-parallax overrides bg_mode → imagen (lateral pan)")
        bg_mode = "imagen"
    # Estático / Sutil: el design intent (clarificado por operador UMG
    # 2026-05-25) es que estos sean ESCENAS REALES generadas por Veo,
    # NO fotos estáticas:
    #   - "Estático" → Veo, cámara fija, motion in-scene (gente caminando,
    #     olas, nubes, neblina, fuego, ambiente vivo).
    #   - "Sutil"    → Veo, drift sutil de cámara + scene motion.
    #
    # "Foto + parallax" es el ÚNICO path Imagen → Ken Burns por diseño.
    #
    # HISTÓRICAMENTE (2026-05-22) estatico/sutil iban a Imagen como
    # workaround porque Veo ignoraba "locked camera" ~50%. El prompt
    # hardening C2+C3+C4 reforzado en _generate_veo_video mitiga eso.
    # Default ahora es Veo. Para volver al workaround (si Veo regresa
    # al comportamiento histórico), setear STATIC_SUTIL_VIA_IMAGEN=1.
    elif _norm_move_bg in ("estatico", "sutil") and bg_mode != "imagen":
        _force_imagen_legacy = (
            os.environ.get("STATIC_SUTIL_VIA_IMAGEN", "").strip().lower()
            in ("1", "true", "yes", "on")
        )
        if _force_imagen_legacy:
            logger.info("[BG] movement=%s overrides bg_mode → imagen (STATIC_SUTIL_VIA_IMAGEN=on, legacy workaround)", _norm_move_bg)
            bg_mode = "imagen"
        else:
            logger.info("[BG] movement=%s stays on Veo (design intent 2026-05-25 — scene REAL)", _norm_move_bg)
            # bg_mode stays "veo" — _generate_veo_video receives the
            # hardened safe_prompt for estatico/sutil (C2 + C3).

    # Imagen-4 + Ken Burns branch. Cabled 2026-05-16 — _generate_imagen_image
    # has existed in the codebase as dead code since the original architecture
    # but was never wired into the dispatch. This is the wire.
    if bg_mode == "imagen":
        result = _get_unique_prompt(
            lyrics_text, artist, job_id=job_id, song_title=song_title, genre=genre,
            concept=concept, movement_style=movement_style, match_lyrics=match_lyrics,
            background_hint=background_hint,
            for_provider="imagen",
            bg_verbatim=bg_verbatim,
            palette_style=style_hint, custom_colors=custom_colors,
            allow_people=allow_people,
        )
        # Foto fija + efectos (2026-06-03; review-fixed same day): if the
        # operator picked a luminous particle effect, bias the still toward a
        # dark/low-key canvas so the screen-blended particles read. Gated on the
        # operator's ACTUAL pick (_operator_effect), NOT the forced "light"
        # default, so it no-ops when no effect was chosen. AND never applied to
        # a verbatim operator prompt — "usá mi prompt tal cual" must not get
        # dark grading bolted on (the imagen branch returns before the Veo
        # path's _is_verbatim is computed, so we check it inline here).
        _verbatim_bg = bool(bg_verbatim and background_hint and background_hint.strip())
        prompt = (result["prompt"] if _verbatim_bg
                  else _darken_prompt_for_effect(result["prompt"], _operator_effect))
        image_path = os.path.join(job_dir, "bg_imagen.jpg")
        bg_path = os.path.join(job_dir, "bg_generated.mp4")
        # A1 (2026-05-25) — foto-parallax es el único register que el
        # operador eligió específicamente para path Imagen premium.
        # Merece el modelo ultra (~$0.04 vs $0.02 estándar — despreciable
        # comparado con los $0.80-3.20 de Veo de los otros registers).
        # Estatico/sutil legacy (cuando STATIC_SUTIL_VIA_IMAGEN=1) siguen con
        # el modelo estándar (default IMAGEN_MODEL). Default 2026-05-25 ya no
        # llega acá — estatico/sutil ahora van por Veo.
        _parallax_model = (
            os.environ.get("IMAGEN_MODEL_PARALLAX",
                           "imagen-4.0-ultra-generate-001").strip()
            if _norm_move_bg == "foto-parallax" else None
        )
        # Imagen-4 has its own internal rate-limit retry (5 attempts with
        # 60s backoff). Any other exception bubbles up to the caller's
        # try/except which falls back to the gradient.
        _generate_imagen_image(prompt, image_path, job_id=job_id,
                                model=_parallax_model,
                                allow_people=allow_people)
        # Ken Burns produces a 60s sample that downstream palindrome-loops
        # to match the audio duration. Same contract as the Veo path.
        #   - "Estático"        → hold the frame (no zoom/pan).
        #   - "Sutil"           → barely-there gentle drift (no zoom, no forward).
        #   - "Foto + parallax" → slow lateral pan (no zoom, no forward).
        #   - otherwise         → the usual zoom/pan Ken Burns.
        # Foto fija (2026-06-02): el fondo-imagen se rinde ESTÁTICO por ffmpeg
        # (C-level, RAM acotada) en vez del Ken Burns full-duration por moviepy.
        #
        # Causa raíz del cambio (incidente 2026-06-02, "Rata Blanca"): el render
        # Ken Burns por la duración COMPLETA (UMG fix 2026-05-25) hacía que moviepy
        # animara ~7.800 frames del paneo (easing + zoom-breath + back-and-forth)
        # uno por uno en Python; en una canción de 5 min + un render ProRes pesado
        # en paralelo, el worker se quedaba sin RAM y el kernel lo mataba (OOM /
        # SIGKILL) en medio del render → job huérfano en "processing" para siempre.
        #
        # Decisión de producto: sacamos el paneo y dejamos FOTO FIJA. La vida/
        # movimiento del video la da ahora el EFECTO componible (snow/rain/stars/
        # bokeh/light), que se compone por ffmpeg overlay (fx_compositor) — barato
        # y OOM-safe. Un loop estático de ffmpeg es imposible que OOMee, para
        # cualquier largo de canción y cualquier opción del wizard.
        # Render a SHORT static sample (60s, the original ken-burns contract);
        # the downstream loop — _prerender_looped_bg on the libass path, or
        # _get_background_clip_from_path on the moviepy fallback — seamlessly
        # extends a static frame to the full song length. Rendering the full
        # duration here was a SECOND redundant full-length encode (review
        # 2026-06-03): wasted CPU on long songs and exposed the static encode's
        # ffmpeg timeout. A static loop is identical at any sample length.
        _static_image_to_mp4(image_path, bg_path, duration=60.0)
        return bg_path

    # True verbatim = operator's own prompt is actually in use (bg_verbatim set
    # AND a non-empty hint). bg_verbatim alone with no hint falls through to
    # Gemini, so the de-bias rails must still apply there. Mirrors the
    # short-circuit condition in _get_unique_prompt.
    _is_verbatim = bool(bg_verbatim and background_hint and background_hint.strip())

    # Generate video background with Veo 3 (always video, no images)
    result = _get_unique_prompt(
        lyrics_text, artist, job_id=job_id, song_title=song_title, genre=genre,
        concept=concept, movement_style=movement_style, match_lyrics=match_lyrics,
        background_hint=background_hint,
        bg_verbatim=bg_verbatim,
        palette_style=style_hint, custom_colors=custom_colors,
        allow_people=allow_people,
    )
    prompt = result["prompt"]

    bg_path = os.path.join(job_dir, "bg_generated.mp4")
    import time as _time_bg
    # RQ's job_timeout (BG_PREVIEW_JOB_TIMEOUT, 900s) fires as a SIGALRM that
    # raises JobTimeoutException — which subclasses Exception, so the generic
    # `except Exception` below would swallow it and mask a real timeout as a
    # "successful" gradient job, deleting the exact telemetry we want in Sentry.
    # Catch it specifically and re-raise so the job is marked failed + stays
    # visible. Empty-tuple fallback keeps the clause a harmless no-op if rq
    # isn't importable (local/CI), where the death penalty never fires anyway.
    try:
        from rq.timeouts import JobTimeoutException as _JobTimeoutException
    except Exception:
        _JobTimeoutException = ()
    quality_retry_used = False
    # Tier 3b: if the cross-worker Veo breaker is OPEN (project-wide quota
    # event), skip Veo entirely and fall straight to the gradient fallback
    # below — no point burning a slot on attempts that will 429. is_open() is
    # fail-CLOSED (Redis down/disabled → False → try Veo as usual) and half-open
    # (lets one probe through to test recovery). Checked once before the loop.
    import veo_breaker
    _veo_breaker_open = veo_breaker.is_open()
    if _veo_breaker_open:
        logger.warning("[BG] Veo breaker OPEN — short-circuiting to gradient (skipping Veo)")
    # 1 retry interno (2 intentos). Antes eran 3, lo que sumado al
    # poll_deadline de 600s de Veo podía exceder cualquier job_timeout
    # razonable. Un solo "do-over" cubre tanto un fallo transitorio de Veo
    # como un re-roll por calidad (score<7), y entra cómodo en los 900s de
    # BG_PREVIEW_JOB_TIMEOUT. OJO: una falla de Veo cae al gradient fallback
    # de abajo (no re-lanza), así que el RQ Retry(max=2) NO reintenta Veo —
    # este loop es la única resiliencia ante hiccups transitorios de Veo.
    for attempt in range(2):
        if _veo_breaker_open:
            break  # breaker open → skip Veo, fall through to gradient fallback
        try:
            _generate_veo_video(
                prompt, bg_path, job_id=job_id,
                cache_namespace=f"{artist}|{song_title}",
                image_path=image_to_video_path,
                movement_style=movement_style,
                normalized_concept=_normalize_concept(concept),
                high_fidelity=bg_verbatim,
                allow_people=allow_people,
                verbatim=_is_verbatim,
            )
            # Semantic relevance check — always score, but cap retries at one
            # to bound cost (+$0.80 worst case). quality_retry_used gates the
            # re-generation decision, not the scoring itself, so the retry's
            # result is also evaluated before we accept and return it.
            score = _score_video_relevance(bg_path, prompt)
            logger.info("[BG] Relevance score: %s/10 for prompt: %s...", score, prompt[:60])
            # Detector de corte de escena (incidente mural 2026-06-09):
            # un clip cuyo primer y último frame son escenas distintas
            # produce un fondo que alterna escenas todo el video al
            # loopearse. El relevance score no lo ve (mira UN frame).
            discont = _bg_scene_discontinuity(bg_path)
            if discont >= _BG_SCENE_CUT_THRESHOLD:
                logger.warning(
                    "[BG][SCENE-CUT] discontinuidad %.3f >= %.2f en %s (job=%s) — "
                    "el clip contiene un cambio de escena",
                    discont, _BG_SCENE_CUT_THRESHOLD,
                    os.path.basename(bg_path), job_id,
                )
            # Verbatim: never re-roll. A re-roll re-runs _get_unique_prompt
            # which short-circuits to the SAME verbatim text → identical
            # safe_prompt → Veo cache HIT → same clip, wasting a scoring pass
            # and never improving. The operator asked for their exact prompt;
            # we accept the first result and just log the score.
            _needs_retry = (score < 7) or (discont >= _BG_SCENE_CUT_THRESHOLD)
            if _needs_retry and not quality_retry_used and not bg_verbatim:
                quality_retry_used = True
                logger.info("[BG] Score %s / discontinuidad %.3f — generating new prompt and retrying VEO",
                            score, discont)
                # Propagate background_hint into the quality retry. Without it,
                # Gemini regenerates from lyrics/genre alone and the operator's
                # explicit guidance is silently dropped — the same input that
                # produced a low-score first attempt is more likely to drift to
                # something unrelated (Enanitos Verdes "Amigos" 2026-05-15:
                # hint "fogón con bosque" → retry picked an iceberg scene).
                result = _get_unique_prompt(
                    lyrics_text, artist, job_id=job_id, song_title=song_title,
                    genre=genre, concept=concept, movement_style=movement_style,
                    match_lyrics=match_lyrics,
                    background_hint=background_hint,
                    bg_verbatim=bg_verbatim,
                    palette_style=style_hint, custom_colors=custom_colors,
                    allow_people=allow_people,
                )
                prompt = result["prompt"]
                continue
            if score < 7:
                logger.warning("[BG] Score %s < 7 after retry — accepting best available result", score)
            if discont >= _BG_SCENE_CUT_THRESHOLD:
                # Aceptamos igual (fail-open: bloquear el render es peor que
                # un fondo feo que la review humana atrapa), pero el operador
                # se entera por Sentry ANTES de que lo vea el cliente.
                # Fingerprint estable (estilo #630): un issue agrupado, no
                # uno por job.
                logger.warning(
                    "[BG][SCENE-CUT] aceptando clip con discontinuidad %.3f tras retry (job=%s) — revisar antes de aprobar",
                    discont, job_id,
                )
                try:
                    import sentry_sdk
                    with sentry_sdk.push_scope() as _scope:
                        _scope.fingerprint = ["bg-scene-cut"]
                        _scope.set_tag("job_id", job_id or "unknown")
                        _scope.set_extra("discontinuity", discont)
                        _scope.set_extra("prompt", (prompt or "")[:200])
                        sentry_sdk.capture_message(
                            "[BG][SCENE-CUT] fondo con cambio de escena aceptado tras retry",
                            level="warning",
                        )
                except Exception:
                    pass
            return bg_path
        except _JobTimeoutException:
            # Real RQ death-penalty timeout — propagate (don't degrade to
            # gradient + swallow). Keeps the JobTimeoutException visible.
            raise
        except Exception as e:
            logger.error("[BG] Veo 3 attempt %s/2 failed: %s", attempt + 1, e)
            if attempt < 1:
                wait = 30
                logger.info("[BG] Waiting %ss before retry...", wait)
                _time_bg.sleep(wait)

    # All Veo attempts failed — render a gradient as fallback.
    # We do NOT fall back to a library asset: UMG and other rights-sensitive
    # tenants need clear provenance of every visual element, and a stock asset
    # silently substituted into an AI-mode job would break that contract.
    logger.warning("[BG] Veo 3 unavailable, falling back to gradient background")
    fallback_path = os.path.join(job_dir, "bg_gradient_fallback.mp4")
    gradient = _make_gradient_clip(30.0, style_hint)
    gradient.write_videofile(fallback_path, fps=24, logger=None)
    gradient.close()
    return fallback_path


def _ken_burns_image_to_mp4(
    image_path: str,
    output_path: str,
    sample_duration: float = 60.0,
    spec: RenderSpec | None = None,
    static: bool = False,
    lateral: bool = False,
    subtle: bool = False,
) -> str:
    """Render a Ken Burns animation over a still image as a standalone MP4.

    ⚠️ NO LONGER WIRED (2026-06-02). The Imagen background path used to call
    this, but rendering the full-duration pan through moviepy animated ~7,800
    frames one-by-one in Python and OOM-SIGKILLed the worker on long songs
    (incident "Rata Blanca"), leaving the job stuck in "processing". The photo
    background is now a STATIC ffmpeg render (`_static_image_to_mp4`) + a
    composable effect overlay. DO NOT re-wire this for full-duration renders
    without first making it ffmpeg/memory-safe. Kept for reference/tests.

    Wraps `_ken_burns_clip` (which returns a moviepy VideoClip object) into
    a file-on-disk that matches the contract `_ensure_background` returns
    to its callers. Used by the Imagen-mode background path: Imagen-4
    generates a still photo, this function turns it into a short looped-
    able MP4 sample, and the rest of the pipeline (background palindrome
    loop, R2 cache via bg_r2_key_cached for subsequent typography/lyrics
    edits) treats it identically to a Veo output.

    `sample_duration` defaults to 60s — same ballpark as a Veo clip after
    palindrome looping. Downstream `_loop_palindrome_to_match_audio`
    extends it to match the full audio. Going much shorter would expose
    the Ken Burns cycle reset; longer doubles file size with no visible
    gain since palindrome reverses anyway.

    Returns the output path (same as the input arg) so callers can
    use it in expressions.
    """
    if spec is None:
        spec = RenderSpec.youtube_default()
    clip = _ken_burns_clip(image_path, sample_duration, spec=spec, static=static, lateral=lateral, subtle=subtle)
    try:
        clip.write_videofile(
            output_path,
            fps=spec.fps,
            codec="libx264",
            audio=False,
            logger=None,
            preset="medium",
            ffmpeg_params=["-pix_fmt", "yuv420p"],  # broad player compat
        )
    finally:
        clip.close()
    size_mb = os.path.getsize(output_path) / (1024 * 1024)
    print(f"[BG] Ken Burns MP4 rendered: {sample_duration:.0f}s, {size_mb:.1f} MB")
    return output_path


def _pan_frame_subpixel(
    img: np.ndarray, t: float, duration: float, *,
    base_x: int, base_y: int, cw: int, ch: int,
    travel_x: int, travel_y: int, dir_x: int, dir_y: int,
    out_w: int, out_h: int,
) -> np.ndarray:
    """Subpixel-sampled pan frame at time `t`. Pure, no moviepy.

    El pan lateral del fondo calmo (Imagen + Ken Burns ruteo de
    `_ensure_background`) se veía "super cortado" porque truncábamos el
    desplazamiento de cada frame a píxeles enteros. A 24 fps con ~1.25 px de
    travel/frame, la mayoría de los frames consecutivos terminaban en el
    mismo entero (stepping 0-1-1-1-2-2-2-3…). El fix: mantenemos el
    desplazamiento como float, hacemos el crop entero con 1 px extra a
    derecha/abajo, y aplicamos el shift fraccional vía PIL AFFINE+BILINEAR
    antes del LANCZOS final. ~1-2 ms/frame de overhead a 1080p (despreciable
    frente al LANCZOS posterior).

    Extraído a module-level (en vez de closure dentro de `_ken_burns_clip`)
    para que sea testable directamente sin instanciar moviepy.
    """
    h, w = img.shape[:2]
    p = min(1.0, t / duration) if duration else 0.0
    p = 0.5 - 0.5 * math.cos(p * math.pi)  # ease in/out
    fx = (p if dir_x == 1 else (1.0 - p)) * travel_x
    fy = (p if dir_y == 1 else (1.0 - p)) * travel_y
    ix, iy = int(fx), int(fy)
    sub_x, sub_y = fx - ix, fy - iy
    # 1 px extra para el lookup BILINEAR (lee vecinos 2x2). Cuando la imagen
    # es justo del tamaño del crop (degenerado, travel=0), leemos exacto y
    # BILINEAR clampa al borde sin shift visible.
    extra_x = 1 if w > cw else 0
    extra_y = 1 if h > ch else 0
    cx = max(0, min(base_x + ix, w - cw - extra_x))
    cy = max(0, min(base_y + iy, h - ch - extra_y))
    crop = img[cy:cy + ch + extra_y, cx:cx + cw + extra_x]
    shifted = Image.fromarray(crop).transform(
        (cw, ch),
        Image.AFFINE,
        (1, 0, sub_x, 0, 1, sub_y),
        Image.BILINEAR,
    )
    return np.array(shifted.resize((out_w, out_h), Image.LANCZOS))


def _ken_burns_clip(image_path: str, duration: float, spec: RenderSpec | None = None,
                    static: bool = False, lateral: bool = False, subtle: bool = False):
    """Create an animated Ken Burns clip with periodic direction changes.

    `static=True` returns a LOCKED frame instead: the image is center-cropped
    to the target aspect ratio and held for the whole duration — no zoom, no
    pan. Used by the Imagen background path when the operator picked
    "Estático", where the usual Ken Burns zoom/pan would directly contradict
    a locked-camera request.

    `lateral=True` returns a clean PARALLAX-style slide: a slow, constant
    horizontal pan over a fixed inward crop — no zoom, no vertical drift, no
    forward travel ("Foto + parallax").

    `subtle=True` returns a barely-there ambient DRIFT: a small, slow, diagonal
    pan of low amplitude — no zoom, no forward travel ("Sutil"). Distinct from
    `lateral` (a clearly visible sideways travel) and `static` (frozen).

    These deterministic camera moves replace Veo for the calm registers because
    Veo ignores "locked / no advance" ~half the time (measured 2026-05-22) and
    pushes in regardless. `static` > `lateral` > `subtle` if several are passed.
    """
    if spec is None:
        spec = RenderSpec.youtube_default()
    img = np.array(Image.open(image_path))
    h, w = img.shape[:2]

    if not static and (lateral or subtle):
        # Pan over a fixed inward crop — NO zoom (the scale is constant), so the
        # camera never advances. `lateral` uses the full horizontal room; the
        # `subtle` drift uses a fraction of it plus a touch of vertical, so it
        # reads as a barely-there breath rather than a clear slide.
        #
        # B1+B3 (2026-05-25) — En `lateral` (foto-parallax) sumamos:
        #   - Zoom-breath sutil (scale anima entre 1.15 y 1.22 cada ~32s):
        #     da sensación de profundidad sin advance forward. Mantiene la
        #     UMG motion policy (pan domina, breath es respiratory).
        #   - Back-and-forth para canciones >180s: en vez de un pan lineal
        #     unidireccional invisible sobre 5min, hacemos ida 60% del room
        #     en 0.55*duration + vuelta 40% en 0.45*duration. Mismo travel
        #     total, redistribuido en ciclos visibles.
        # B2 (easing) ya vive en `_pan_frame_subpixel` (cosine ease in/out).
        # Para `subtle` mantenemos el comportamiento original (drift mínimo,
        # sin breath ni back-forth — el operador de sutil pidió quietud).
        if lateral:
            scale_base, scale_amp = 1.185, 0.035  # breathes 1.15..1.22
            amp_x, amp_y = 1.0, 0.0
        else:  # subtle
            # Fix urgente 2026-05-25 (UMG: 'los últimos videos salieron
            # con fotos fijas'). Las amplitudes anteriores (amp_x=0.35,
            # amp_y=0.18, scale_amp=0.0) producían ~1px/segundo de pan
            # sobre 60s — visualmente indistinguible de una foto fija.
            # El operador UMG esperaba 'minimal movement' = perceptible
            # pero calmo, NO invisible.
            #
            # Cambio: 2x el horizontal travel + 2x el vertical drift +
            # breath sutil de ±1.5% del zoom para que la cámara 'respire'.
            # Sobre 60s da ~2-3px/seg + breath cycle de ~24s — barely-
            # there pero PERCEPTIBLE. Operador sigue percibiendo escena
            # calma sin marcas dramáticas.
            scale_base, scale_amp = 1.115, 0.015  # breath ~1.10..1.13
            amp_x, amp_y = 0.65, 0.32
        # Use scale_base for crop dims (cw/ch held constant — breath happens
        # in the FINAL resize step). Computing cw/ch per-frame would break
        # the base_x/base_y precomputation.
        cw = max(1, min(int(w / scale_base), w))
        ch = max(1, min(int(h / scale_base), h))
        room_x = max(0, w - cw)
        room_y = max(0, h - ch)
        travel_x = int(room_x * amp_x)
        travel_y = int(room_y * amp_y)
        base_x = (w - cw) // 2 - travel_x // 2
        base_y = (h - ch) // 2 - travel_y // 2
        dir_x = random.choice([1, -1])
        dir_y = random.choice([1, -1])
        # B3: back-and-forth gate. Solo para `lateral` y duration >180s.
        _do_back_forth = lateral and duration > 180.0

        def make_pan_frame(t):
            # B3: dos ciclos de pan (ida 60% + vuelta 40%). Time-warped
            # `effective_t` se mapea al duration original para que
            # `_pan_frame_subpixel` calcule la posición con su easing.
            if _do_back_forth:
                ida_dur = duration * 0.55
                if t <= ida_dur:
                    # Ida: 0 → 60% del travel sobre 0.55*duration
                    local_p = t / ida_dur
                    eased = 0.5 - 0.5 * math.cos(local_p * math.pi)
                    effective_p = eased * 0.6
                else:
                    # Vuelta: 60% → 20% del travel sobre 0.45*duration
                    local_p = (t - ida_dur) / max(0.001, duration - ida_dur)
                    eased = 0.5 - 0.5 * math.cos(local_p * math.pi)
                    effective_p = 0.6 - eased * 0.4
                # Convertimos a `effective_t` para que _pan_frame_subpixel
                # haga su propia interpolación basada en t/duration.
                # Como _pan_frame_subpixel aplica ease cosine sobre p=t/duration,
                # pre-eased aquí: pasamos un t cuyo eased equivale a effective_p.
                # Resolvemos: 0.5-0.5*cos(p*pi) = effective_p
                #             → cos(p*pi) = 1 - 2*effective_p
                #             → p = acos(1 - 2*effective_p) / pi
                p_target = math.acos(max(-1.0, min(1.0, 1.0 - 2.0 * effective_p))) / math.pi
                effective_t = p_target * duration
            else:
                effective_t = t
            frame = _pan_frame_subpixel(
                img, effective_t, duration,
                base_x=base_x, base_y=base_y, cw=cw, ch=ch,
                travel_x=travel_x, travel_y=travel_y,
                dir_x=dir_x, dir_y=dir_y,
                out_w=spec.width, out_h=spec.height,
            )
            # B1: zoom-breath sutil — re-resize el frame final con un scale
            # que oscila entre (scale_base - scale_amp) y (scale_base + scale_amp).
            # Cycle de 32s mantiene la respiración orgánica. Para `subtle`
            # scale_amp=0 → no-op (el frame queda igual).
            if scale_amp > 0:
                breath_phase = (t % 32.0) / 32.0
                breath_scale = 1.0 + (scale_amp / scale_base) * math.sin(breath_phase * 2.0 * math.pi)
                if abs(breath_scale - 1.0) > 0.001:
                    # Re-resize con shift centrado para no introducir wobble.
                    fh, fw = frame.shape[:2]
                    new_w = max(1, int(round(fw / breath_scale)))
                    new_h = max(1, int(round(fh / breath_scale)))
                    off_x = max(0, (fw - new_w) // 2)
                    off_y = max(0, (fh - new_h) // 2)
                    sub = frame[off_y:off_y + new_h, off_x:off_x + new_w]
                    frame = np.array(
                        Image.fromarray(sub).resize((fw, fh), Image.LANCZOS)
                    )
            return frame

        return VideoClip(make_pan_frame, duration=duration).set_fps(spec.fps)

    if static:
        # Center-crop to target aspect ratio, then resize once. Held constant.
        target_ar = spec.width / spec.height
        src_ar = w / h if h else target_ar
        if src_ar > target_ar:
            ch = h
            cw = int(round(h * target_ar))
        else:
            cw = w
            ch = int(round(w / target_ar)) if target_ar else h
        cw = max(1, min(cw, w))
        ch = max(1, min(ch, h))
        cx = (w - cw) // 2
        cy = (h - ch) // 2
        crop = img[cy:cy + ch, cx:cx + cw]
        frame = np.array(
            Image.fromarray(crop).resize((spec.width, spec.height), Image.LANCZOS)
        )
        return VideoClip(lambda t: frame, duration=duration).set_fps(spec.fps)

    # Each cycle lasts ~12 seconds, with a different random direction
    cycle_dur = 12.0
    num_cycles = max(1, int(math.ceil(duration / cycle_dur)))

    # Pre-generate random directions for each cycle
    random.seed(None)
    cycles = []
    for _ in range(num_cycles):
        cycles.append({
            "zoom_in": random.choice([True, False]),
            "pan_x": random.uniform(-0.08, 0.08),
            "pan_y": random.uniform(-0.05, 0.05),
        })

    def make_frame(t):
        idx = min(int(t / cycle_dur), num_cycles - 1)
        c = cycles[idx]
        progress = (t - idx * cycle_dur) / cycle_dur

        # Smooth ease in/out within each cycle
        progress = 0.5 - 0.5 * math.cos(progress * math.pi)

        if c["zoom_in"]:
            scale = 1.0 + 0.25 * progress
        else:
            scale = 1.25 - 0.25 * progress

        cw = int(w / scale)
        ch = int(h / scale)
        cx = int((w - cw) / 2 + c["pan_x"] * progress * w)
        cy = int((h - ch) / 2 + c["pan_y"] * progress * h)
        cx = max(0, min(cx, w - cw))
        cy = max(0, min(cy, h - ch))

        crop = img[cy:cy + ch, cx:cx + cw]
        resized = np.array(
            Image.fromarray(crop).resize((spec.width, spec.height), Image.LANCZOS)
        )
        return resized

    return VideoClip(make_frame, duration=duration).set_fps(spec.fps)


# ---------------------------------------------------------------------------
# Step 2 — Full HD lyric video
# ---------------------------------------------------------------------------

_USED_BACKGROUNDS_FILE = os.path.join(ASSETS_DIR, ".used_backgrounds.json")


def _find_background_video(exclude: list[str] | None = None) -> str | None:
    """Pick a random background video without repeating until all are used.

    `exclude` is a per-call blacklist of paths to skip (used by the
    content-validation retry loop to avoid re-picking a background that
    just failed policy in this same job).
    """
    exclude = exclude or []
    all_videos: list[str] = []
    if os.path.isdir(BACKGROUNDS_DIR):
        for root, _, files in os.walk(BACKGROUNDS_DIR):
            all_videos.extend(
                os.path.join(root, f)
                for f in files if f.lower().endswith(".mp4")
            )

    if not all_videos:
        return None

    # Load history of used videos
    used: list[str] = []
    if os.path.exists(_USED_BACKGROUNDS_FILE):
        try:
            with open(_USED_BACKGROUNDS_FILE) as f:
                used = json.load(f)
        except (json.JSONDecodeError, OSError):
            used = []

    # Filter out already used + per-call exclusions; if nothing left, reset
    # the cycle but keep honouring the exclusion list (we still don't want
    # to re-pick a background that already failed validation this job).
    available = [v for v in all_videos if v not in used and v not in exclude]
    if not available:
        logger.info("[BG] All %s backgrounds used, resetting cycle", len(all_videos))
        used = []
        available = [v for v in all_videos if v not in exclude]
    if not available:
        return None

    pick = random.choice(available)
    used.append(pick)

    # Save updated history
    try:
        with open(_USED_BACKGROUNDS_FILE, "w") as f:
            json.dump(used, f)
    except OSError:
        pass

    logger.info("[BG] Selected: %s (%s of %s used)",
                os.path.basename(pick), len(all_videos) - len(available), len(all_videos))
    return pick


def _select_validated_background(job_id: str, max_attempts: int = 3) -> tuple[str | None, list[dict]]:
    """Pick a library background and validate it against UMG Guideline 15
    BEFORE the expensive render kicks in. If validation rejects, pick a
    different background and try again, up to `max_attempts`.

    Returns (chosen_path, all_rejection_issues). chosen_path is None if
    no clean background was found within the attempts budget.
    """
    from content_validator import validate_video, validate_image

    rejected: list[str] = []
    issues: list[dict] = []
    for attempt in range(1, max_attempts + 1):
        candidate = _find_background_video(exclude=rejected)
        if not candidate:
            break
        ext = os.path.splitext(candidate)[1].lower()
        validate_fn = (
            validate_video if ext in (".mp4", ".mov", ".webm") else validate_image
        )
        result = validate_fn(candidate, job_id=job_id)
        if result.get("passed"):
            logger.info("[VALIDATION] bg accepted on attempt %s: %s",
                        attempt, os.path.basename(candidate))
            return candidate, issues
        logger.warning("[VALIDATION] bg rejected on attempt %s (%s): %s",
                       attempt, os.path.basename(candidate), result.get('issues'))
        for it in result.get("issues") or []:
            issues.append({"attempt": attempt, "bg": os.path.basename(candidate), **it})
        rejected.append(candidate)
    return None, issues


_GRADIENT_PALETTES = {
    "oscuro": [(10, 10, 30), (30, 15, 60), (80, 20, 80), (40, 10, 50)],
    "neon": [(10, 5, 40), (80, 0, 120), (0, 100, 130), (120, 0, 80)],
    "minimal": [(180, 180, 195), (200, 190, 210), (170, 180, 200), (210, 200, 195)],
    "calido": [(60, 20, 10), (140, 60, 15), (180, 90, 20), (100, 30, 10)],
}


def _make_gradient_clip(duration: float, style: str = "oscuro",
                        spec: RenderSpec | None = None):
    """Generate a cinematic animated gradient as fallback background."""
    if spec is None:
        spec = RenderSpec.youtube_default()
    palette = _GRADIENT_PALETTES.get(style, _GRADIENT_PALETTES["oscuro"])
    top = np.array(palette[0], dtype=np.float64)
    mid1 = np.array(palette[1], dtype=np.float64)
    mid2 = np.array(palette[2], dtype=np.float64)
    bot = np.array(palette[3], dtype=np.float64)

    _rows = np.zeros((spec.height, spec.width, 3), dtype=np.float64)
    for y in range(spec.height):
        ratio = y / spec.height
        if ratio < 0.33:
            color = top + (mid1 - top) * (ratio / 0.33)
        elif ratio < 0.66:
            color = mid1 + (mid2 - mid1) * ((ratio - 0.33) / 0.33)
        else:
            color = mid2 + (bot - mid2) * ((ratio - 0.66) / 0.34)
        _rows[y, :] = color

    def _gradient_frame(t):
        shift = 20 * np.sin(t * 0.12)
        shift2 = 12 * np.cos(t * 0.08)
        frame = _rows.copy()
        frame[:, :, 0] = np.clip(frame[:, :, 0] + shift, 0, 255)
        frame[:, :, 1] = np.clip(frame[:, :, 1] + shift2 * 0.5, 0, 255)
        frame[:, :, 2] = np.clip(frame[:, :, 2] - shift * 0.6, 0, 255)
        return frame.astype(np.uint8)

    return VideoClip(_gradient_frame, duration=duration).set_fps(spec.fps)


def _cover_resize(clip, target_w=1920, target_h=1080):
    """Resize and crop a video clip to cover target_w x target_h (CSS cover)."""
    src_w, src_h = clip.size
    # Scale so the smallest dimension fills the target
    scale = max(target_w / src_w, target_h / src_h)
    new_w = int(math.ceil(src_w * scale))
    new_h = int(math.ceil(src_h * scale))
    resized = clip.resize((new_w, new_h))
    # Center crop to exact target size
    x_offset = (new_w - target_w) // 2
    y_offset = (new_h - target_h) // 2
    return resized.crop(x1=x_offset, y1=y_offset, width=target_w, height=target_h)


def _video_dims(path: str) -> tuple[int, int] | None:
    """Return (width, height) of a video's first stream, or None on failure."""
    try:
        r = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "v:0",
             "-show_entries", "stream=width,height",
             "-of", "csv=s=x:p=0", path],
            capture_output=True, text=True, timeout=30,
        )
        w, h = (r.stdout or "").strip().split("x")
        return int(w), int(h)
    except Exception:
        return None


def _normalize_bg_to_spec(bg_path: str, job_dir: str,
                          target_w=1920, target_h=1080,
                          out_name: str = "bg_normalized.mp4") -> str:
    """Scale/crop a full-length background to the spec dims WITHOUT looping.

    Used by the multi-escena path: scenes.stitch_timeline already produced a
    timeline covering the whole song, so we must not palindrome-loop it (that
    would play the scenes in reverse at the tail). If the timeline already
    matches the target dims this is a no-op (returns the input path) to avoid
    a wasteful full-length re-encode — common case, since the stitch runs at
    the YouTube 1920x1080 dims and only off-size UMG specs need a rescale.
    """
    dims = _video_dims(bg_path)
    if dims == (target_w, target_h):
        return bg_path
    out_path = os.path.join(job_dir, out_name)
    cmd = [
        "ffmpeg", "-y", "-loglevel", "error",
        "-i", bg_path,
        "-vf", (
            f"scale={target_w}:{target_h}:force_original_aspect_ratio=increase,"
            f"crop={target_w}:{target_h},setsar=1"
        ),
        "-c:v", "libx264", "-preset", "fast", "-crf", "20",
        "-pix_fmt", "yuv420p", "-an",
        out_path,
    ]
    run_checked(cmd, label="ffmpeg-scenes-normalize", timeout=1800, output_path=out_path)
    return out_path


def _prerender_looped_bg(bg_path: str, duration: float, job_dir: str,
                         target_w=1920, target_h=1080,
                         out_name: str = "bg_looped.mp4") -> str:
    """Pre-render a seamlessly looped background using palindrome (A + reverse(A)).

    A straight -stream_loop jumps from the last frame back to the first, which
    is visible as a "pop" when the scene has camera movement. Concatenating A
    with its reverse makes the last frame of one pass match the first frame of
    the next — the loop is mathematically seamless.

    We scale and crop first, then palindrome, then loop the palindrome to fill
    the requested duration.
    """
    out_path = os.path.join(job_dir, out_name)
    cmd = [
        "ffmpeg", "-y",
        "-stream_loop", "-1",
        "-i", bg_path,
        "-t", str(duration),
        "-filter_complex", (
            f"[0:v]scale={target_w}:{target_h}:force_original_aspect_ratio=increase,"
            f"crop={target_w}:{target_h},setpts=PTS-STARTPTS,split[a][b];"
            "[b]reverse[br];"
            "[a][br]concat=n=2:v=1:a=0"
        ),
        "-c:v", "libx264",
        "-preset", "fast",
        "-crf", "20",
        "-pix_fmt", "yuv420p",
        "-an",
        out_path,
    ]
    # Audit 2026-05-26: timeout=900s. Without it, a corrupted Veo output
    # or a filter_complex that locks the encoder (palindrome on a
    # zero-length input has been observed) hung the worker indefinitely.
    # The fallback branch below already uses run_checked(timeout=900);
    # mirroring that bound here is the consistent fix. capture_output=True
    # buffers stderr in memory so we can still report the tail on failure.
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=900)
    except subprocess.TimeoutExpired:
        logger.error("[BG] Palindrome loop timed out after 900s — falling back")
        # Synthesize a "failed" result so we drop into the fallback branch
        # without duplicating the call.
        class _Timeout:
            returncode = 124
            stderr = "ffmpeg palindrome loop timed out (>900s)"
        result = _Timeout()
    if result.returncode != 0:
        # Fall back to the simple loop if the palindrome filter graph fails
        # (e.g. clip too short or memory-constrained machines).
        logger.warning("[BG] Palindrome loop failed, falling back to stream_loop: %s",
                       result.stderr[-200:])
        cmd_fallback = [
            "ffmpeg", "-y",
            "-stream_loop", "-1",
            "-i", bg_path,
            "-t", str(duration),
            "-vf", (
                f"scale={target_w}:{target_h}:force_original_aspect_ratio=increase,"
                f"crop={target_w}:{target_h},"
                "setpts=PTS-STARTPTS"
            ),
            "-c:v", "libx264", "-preset", "fast", "-crf", "20",
            "-pix_fmt", "yuv420p", "-an",
            out_path,
        ]
        # 900s mirrors the kenburns sibling — a stream_loop encode of a
        # multi-minute lyric video sits comfortably under 5 min in
        # healthy runs; double that as the cliff.
        run_checked(
            cmd_fallback,
            label="ffmpeg-palindrome-fallback",
            timeout=900,
            output_path=out_path,
        )

    size_mb = os.path.getsize(out_path) / 1024 / 1024
    logger.info("[BG] Pre-rendered palindrome loop: %.0fs, %.1f MB", duration, size_mb)
    return out_path


def _prerender_kenburns_bg(image_path: str, duration: float, job_dir: str,
                           *, spec: "RenderSpec",
                           out_name: str = "bg_kenburns_ass.mp4") -> str:
    """Turn a still image into a Ken Burns motion video with ffmpeg zoompan,
    so the libass fast path (which burns onto a video) can cover image
    backgrounds. ffmpeg-side analogue of the moviepy _ken_burns_clip: a
    smooth continuous zoom-in instead of the moviepy per-12s random
    direction changes — visually equivalent for a backdrop, and entirely
    C-level so it stays fast.

    We pre-upscale 2x before zoompan to damp the integer-step jitter
    zoompan shows on stills, then scale back to the target dims.
    """
    out_path = os.path.join(job_dir, out_name)
    total_frames = max(1, int(math.ceil(duration * float(spec.fps))))
    zoom_end = 1.15
    zoom_step = (zoom_end - 1.0) / total_frames
    up_w, up_h = spec.width * 2, spec.height * 2
    vf = (
        f"scale={up_w}:{up_h}:force_original_aspect_ratio=increase,"
        f"crop={up_w}:{up_h},"
        f"zoompan=z='min(zoom+{zoom_step:.6f},{zoom_end})':"
        f"d={total_frames}:x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)':"
        f"s={spec.width}x{spec.height}:fps={spec.fps_str}"
    )
    base = [
        "ffmpeg", "-y", "-loglevel", "error",
        "-i", os.path.abspath(image_path),
        "-t", str(duration),
    ]
    result = subprocess.run(
        base + ["-vf", vf, "-c:v", "libx264", "-preset", "fast", "-crf", "20",
                "-pix_fmt", "yuv420p", "-an", out_path],
        capture_output=True, text=True, timeout=900,
    )
    if result.returncode != 0:
        # Fall back to a static (motionless) scaled background so the job
        # still renders rather than failing on a zoompan quirk.
        logger.warning("[BG] zoompan Ken Burns failed (%s); using static bg",
                       result.stderr[-200:])
        static_vf = (
            f"scale={spec.width}:{spec.height}:force_original_aspect_ratio=increase,"
            f"crop={spec.width}:{spec.height},fps={spec.fps_str}"
        )
        run_checked(
            base + ["-vf", static_vf, "-c:v", "libx264", "-preset", "fast",
                    "-crf", "20", "-pix_fmt", "yuv420p", "-an", out_path],
            label="ffmpeg-kenburns-fallback",
            timeout=900,
            output_path=out_path,
        )

    size_mb = os.path.getsize(out_path) / 1024 / 1024
    logger.info("[BG] Ken Burns (zoompan): %.0fs, %.1f MB", duration, size_mb)
    return out_path


def _static_image_to_mp4(image_path: str, output_path: str, duration: float,
                         *, spec: "RenderSpec | None" = None) -> str:
    """Render a still image as a STATIC background video via ffmpeg — no motion.

    Replaces the moviepy Ken Burns render for the photo background path. The
    moviepy render animated thousands of pan frames one-by-one in Python and
    OOM-SIGKILLed the worker on long songs (incident 2026-06-02, "Rata Blanca"),
    leaving the job stuck in "processing". ffmpeg loops the single image at
    C-level with bounded memory — impossible to OOM at any song length. The
    video's life/motion now comes from the composable effect overlay
    (fx_compositor: snow/rain/stars/bokeh/light), not a camera pan.

    Same on-disk contract as the old Ken Burns output: a full-duration MP4 at
    `output_path` that the rest of the pipeline composes lyrics + effect onto.
    """
    if spec is None:
        spec = RenderSpec.youtube_default()
    # Fill the frame (cover) at the target dims, then hold it for `duration`.
    vf = (
        f"scale={spec.width}:{spec.height}:force_original_aspect_ratio=increase,"
        f"crop={spec.width}:{spec.height},fps={spec.fps_str}"
    )
    run_checked(
        ["ffmpeg", "-y", "-loglevel", "error",
         "-loop", "1", "-i", os.path.abspath(image_path),
         "-t", str(duration),
         "-vf", vf, "-c:v", "libx264", "-preset", "fast", "-crf", "20",
         "-pix_fmt", "yuv420p", "-an", output_path],
        label="ffmpeg-static-bg",
        # 900s to match every other full-duration ffmpeg encode in this module;
        # defensive even though the 60s static sample encodes in seconds.
        timeout=900,
        output_path=output_path,
    )
    size_mb = os.path.getsize(output_path) / 1024 / 1024
    logger.info("[BG] static image render: %.0fs, %.1f MB", duration, size_mb)
    return output_path


def _attach_close_chain(target_clip, owned_clips):
    """Make `target_clip.close()` also close `owned_clips`.

    moviepy's resize/crop/concatenate return new clips that retain refs
    to their source clips, but their .close() does NOT cascade to the
    sources. Long-running workers leaked an ffmpeg subprocess + FD per
    background load until this helper was introduced. Use only on the
    very clip the caller will eventually close, with the sources whose
    lifetime should match it.
    """
    original_close = target_clip.close
    def chained_close():
        try:
            original_close()
        finally:
            for c in owned_clips:
                try:
                    c.close()
                except Exception:
                    pass
    target_clip.close = chained_close
    return target_clip


def _get_background_clip_from_path(bg_path: str, style: str, duration: float,
                                   job_dir: str = None, spec: RenderSpec | None = None,
                                   bg_prelooped: bool = False):
    """Load a background video, loop it seamlessly via ffmpeg, return clip.

    Always returns a single VideoFileClip whose lifetime is owned by the
    caller (the caller is expected to .close() it when the composition
    is finished). When the source already covers the requested duration
    we return a derived clip with its source attached via
    _attach_close_chain so the caller's close cascades. When the source
    is shorter, we pre-render a single seamless loop file via ffmpeg —
    the previous "concatenate N opened clips" fallback leaked one
    VideoFileClip per loop iteration.
    """
    if spec is None:
        spec = RenderSpec.youtube_default()
    try:
        clip = VideoFileClip(bg_path)
        clip.get_frame(0)
        clip_dur = clip.duration
        clip.close()
    except Exception as e:
        raise RuntimeError(f"Cannot load background video: {e}")

    # Audit M2: bg_prelooped = timeline full-length de multi-escena → NUNCA
    # palindromear (las escenas se reproducirían en reversa al final). Forzamos
    # el branch de subclip aunque por redondeo el clip quede unos ms más corto.
    if bg_prelooped or clip_dur >= duration:
        # Open ONCE, derive subclip + cover-resize, attach the source so
        # the caller's eventual close() releases the underlying ffmpeg
        # reader.
        src = VideoFileClip(bg_path)
        _end = min(duration, clip_dur) if bg_prelooped else duration
        derived = _cover_resize(src.subclip(0, _end), spec.width, spec.height)
        return _attach_close_chain(derived, [src])

    # Always pre-render a single seamless loop file. The job_dir-supplied
    # path was already clean; the no-job_dir fallback used to concatenate
    # N opened VideoFileClips and leak each one because moviepy's
    # concatenate_videoclips does NOT cascade-close its inputs.
    if job_dir:
        looped_dir = job_dir
        cleanup_dir = None
    else:
        looped_dir = tempfile.mkdtemp(prefix="genly_bg_loop_")
        cleanup_dir = looped_dir
    looped_name = f"bg_looped_{spec.width}x{spec.height}.mp4"
    looped_path = _prerender_looped_bg(
        bg_path, duration, looped_dir,
        target_w=spec.width, target_h=spec.height,
        out_name=looped_name,
    )
    looped_clip = VideoFileClip(looped_path)

    if cleanup_dir is not None:
        # Sweep the temp dir when the caller closes the clip — no other
        # process should be reading the looped file by then.
        def _rmtree_safely():
            try:
                if os.path.exists(looped_path):
                    os.unlink(looped_path)
            except OSError:
                pass
            try:
                os.rmdir(cleanup_dir)
            except OSError:
                pass
        _orig_close = looped_clip.close
        def _close_with_cleanup():
            try:
                _orig_close()
            finally:
                _rmtree_safely()
        looped_clip.close = _close_with_cleanup
    return looped_clip


# Font pool — Google Fonts only (SIL OFL = full commercial use, no royalties)
# Fonts: prefer the bundled copy inside the backend (so the Docker image
# self-contains them and Railway's build context — which is just lyricgen/backend
# — has them available). Fall back to the repo-level /assets/fonts for local
# dev runs that haven't moved the files yet.
_FONTS_DIR_CANDIDATES = [
    os.path.join(os.path.dirname(__file__), "fonts"),
    os.path.join(os.path.dirname(__file__), "..", "assets", "fonts"),
]
_FONTS_DIR = next(
    (p for p in _FONTS_DIR_CANDIDATES if os.path.isdir(p)),
    _FONTS_DIR_CANDIDATES[0],
)
_LYRIC_FONTS = [
    # AUTO-selection pool — soft, friendly, rounded faces first (operators asked
    # for warmer, less "robotic" type). The condensed/industrial display faces
    # (Bebas / Oswald / Anton) stay user-selectable in _FONT_CATALOGUE but are
    # intentionally kept OUT of this random Auto pool so the default look is
    # friendly, not mechanical.
    "Fredoka-SemiBold.ttf",   # rounded, warm
    "Quicksand-Bold.ttf",     # rounded geometric, soft
    "Nunito-ExtraBold.ttf",   # rounded terminals, friendly
    "Poppins-Bold.ttf",       # geometric but rounded
    "Montserrat-Bold.ttf",    # neutral modern
    "Outfit-Bold.ttf",        # clean (Gilroy-ish)
]
_FONT_POOL = [
    os.path.join(_FONTS_DIR, f)
    for f in _LYRIC_FONTS
    if os.path.isfile(os.path.join(_FONTS_DIR, f))
] if os.path.isdir(_FONTS_DIR) else []


# Public-facing font catalogue. The frontend picker mirrors this list and
# renders previews in the browser via the Google Fonts CDN — every entry's
# `google_family` + `google_weight` matches the local TTF in `filename`,
# so what the operator sees in the dropdown is what the worker renders.
# UMG asked for these eight typefaces specifically; Futura and Gilroy are
# proprietary (Adobe/HypeForType) so we surface their closest libre
# substitutes (Jost / Outfit) and label the option honestly so the
# operator knows it's a stylistic match, not the licensed face.
_FONT_CATALOGUE = [
    # Soft / friendly / rounded (the warm default family)
    {"id": "fredoka",          "filename": "Fredoka-SemiBold.ttf",     "label": "Fredoka (redondeada)",      "google_family": "Fredoka",     "google_weight": 600},
    {"id": "quicksand",        "filename": "Quicksand-Bold.ttf",       "label": "Quicksand (suave)",         "google_family": "Quicksand",   "google_weight": 700},
    {"id": "nunito",           "filename": "Nunito-ExtraBold.ttf",     "label": "Nunito (amigable)",         "google_family": "Nunito",      "google_weight": 800},
    {"id": "jost-bold",        "filename": "Jost-Bold.ttf",            "label": "Jost (estilo Futura)",     "google_family": "Jost",        "google_weight": 700},
    {"id": "montserrat-bold",  "filename": "Montserrat-Bold.ttf",      "label": "Montserrat",                "google_family": "Montserrat",  "google_weight": 700},
    {"id": "poppins-bold",     "filename": "Poppins-Bold.ttf",         "label": "Poppins",                   "google_family": "Poppins",     "google_weight": 700},
    {"id": "outfit-bold",      "filename": "Outfit-Bold.ttf",          "label": "Outfit (estilo Gilroy)",   "google_family": "Outfit",      "google_weight": 700},
    {"id": "roboto-bold",      "filename": "Roboto-Bold.ttf",          "label": "Roboto",                    "google_family": "Roboto",      "google_weight": 700},
    {"id": "bebas-neue",       "filename": "BebasNeue-Regular.ttf",    "label": "Bebas Neue",                "google_family": "Bebas Neue",  "google_weight": 400},
    {"id": "oswald-bold",      "filename": "Oswald-Bold.ttf",          "label": "Oswald",                    "google_family": "Oswald",      "google_weight": 700},
    {"id": "anton",            "filename": "Anton-Regular.ttf",        "label": "Anton",                     "google_family": "Anton",       "google_weight": 400},
]


def _pick_concrete_font(font_id: str, job_id: str, job_dir: str, deterministic: bool) -> str | None:
    """Resuelve el font id del operador a un path; si vino vacío ("Auto"),
    ELIGE del pool acá — una sola vez por render — y persiste el id elegido
    en render_params para que retries/edits/re-renders mantengan la misma
    tipografía.

    Incidente UMG Chile 2026-06-11 ("les está cambiando la letra en los
    shorts"): con Auto, generate_lyric_video elegía la fuente ADENTRO y el
    short recibía font=None → moviepy caía a la fuente default de
    ImageMagick (una stencil hueca que no está en el catálogo). Video y
    short nunca compartían la elección, y cada re-render rotaba la fuente.
    deterministic=True (perfil UMG) usa el seed por job_dir, igual que el
    pick histórico de generate_lyric_video.
    """
    path = _resolve_font(font_id)
    if path:
        return path
    if not _FONT_POOL:
        return None
    if deterministic:
        seed = int(hashlib.sha1(job_dir.encode()).hexdigest()[:8], 16)
        path = _FONT_POOL[seed % len(_FONT_POOL)]
    else:
        path = random.choice(_FONT_POOL)
    # Persistir el id (no el path) para que el próximo render del job no
    # vuelva a sortear. Best-effort: si falla, el render sigue igual.
    picked = os.path.basename(path)
    picked_id = next((e["id"] for e in _FONT_CATALOGUE if e["filename"] == picked), None)
    if picked_id:
        try:
            from jobs import merge_render_params
            merge_render_params(job_id, {"font": picked_id})
            logger.info("[FONT] Auto pick persistido: %s (%s)", picked_id, picked)
        except Exception as e:
            logger.warning("[FONT] no pude persistir el pick %s: %s", picked_id, e)
    return path


def _resolve_font(font_id: str) -> str | None:
    """Map a public font id to a real path under _FONTS_DIR. Empty string
    or unknown id → None, signaling the caller to use the random pool
    (existing "Auto" behavior). Never raises; never returns a missing
    path."""
    if not font_id:
        return None
    for entry in _FONT_CATALOGUE:
        if entry["id"] == font_id:
            path = os.path.join(_FONTS_DIR, entry["filename"])
            return path if os.path.isfile(path) else None
    return None


# Proper nouns that stay capitalized even when a whole line is title-cased.
# Countries + common gentilicios (Spanish, single-token, accent-aware) plus a
# few high-frequency lyric proper nouns. Used only on the "uniformly
# title-cased" branch of _smart_lower (see below) — natural sentence-case lines
# preserve the operator's capitals directly, so this set doesn't need every
# name, just the common ones a title-cased lyric source would flatten.
_PROPER_NOUNS = frozenset({
    # países (es)
    "afganistán", "albania", "alemania", "andorra", "angola", "argentina",
    "argelia", "armenia", "australia", "austria", "azerbaiyán", "bangladés",
    "barbados", "baréin", "bélgica", "belice", "benín", "bielorrusia",
    "birmania", "bolivia", "botsuana", "brasil", "brunéi", "bulgaria",
    "burundi", "bután", "camboya", "camerún", "canadá", "catar", "chad",
    "chile", "china", "chipre", "colombia", "comoras", "congo", "croacia",
    "cuba", "dinamarca", "dominica", "ecuador", "egipto", "eritrea",
    "eslovaquia", "eslovenia", "españa", "estonia", "etiopía", "filipinas",
    "finlandia", "francia", "gabón", "gambia", "georgia", "ghana", "granada",
    "grecia", "guatemala", "guinea", "guyana", "haití", "honduras", "hungría",
    "india", "indonesia", "irak", "irán", "irlanda", "islandia", "israel",
    "italia", "jamaica", "japón", "jordania", "kazajistán", "kenia",
    "kirguistán", "kiribati", "kuwait", "laos", "lesoto", "letonia", "líbano",
    "liberia", "libia", "liechtenstein", "lituania", "luxemburgo",
    "madagascar", "malasia", "malaui", "maldivas", "malí", "malta",
    "marruecos", "mauricio", "mauritania", "méxico", "micronesia", "moldavia",
    "mónaco", "mongolia", "montenegro", "mozambique", "namibia", "nauru",
    "nepal", "nicaragua", "níger", "nigeria", "noruega", "omán", "pakistán",
    "palaos", "palestina", "panamá", "paraguay", "perú", "polonia", "portugal",
    "ruanda", "rumania", "rusia", "samoa", "senegal", "serbia", "seychelles",
    "singapur", "siria", "somalia", "sudán", "suecia", "suiza", "surinam",
    "tailandia", "taiwán", "tanzania", "tayikistán", "togo", "tonga", "túnez",
    "turquía", "turkmenistán", "tuvalu", "ucrania", "uganda", "uruguay",
    "uzbekistán", "vanuatu", "venezuela", "vietnam", "yemen", "yibuti",
    "zambia", "zimbabue", "salvador",
    # gentilicios frecuentes
    "argentino", "argentina", "argentinos", "argentinas", "mexicano",
    "mexicana", "mexicanos", "mexicanas", "español", "española", "españoles",
    "españolas", "colombiano", "colombiana", "peruano", "peruana", "chileno",
    "chilena", "venezolano", "venezolana", "boliviano", "cubano", "cubana",
    "brasileño", "brasileña", "uruguayo", "paraguayo", "americano", "americana",
    "latino", "latina", "latinos", "latinas",
    # nombres propios de alta frecuencia en letras
    "dios", "jesús", "cristo", "maría", "satán",
})


def _smart_lower(text: str) -> str:
    """Lowercase for the 'lower' aesthetic, preserving genuine proper nouns.

    Two sources of capitalization need different handling:

    1. NATURAL sentence-case (e.g. a Whisper transcription, "quizás llegue a
       Guinea"): the first word's capital is grammar, interior capitals are
       deliberate proper nouns. → lowercase the first word, keep interior
       casing exactly as typed. This also persists any casing the operator
       edited by hand. (Origin: agus.cafisi / Babasónicos, 2026-05-20 —
       "Guinea" must not become "guinea".)

    2. UNIFORMLY title-cased / ALL-CAPS lines (e.g. some lyric providers, or
       "Caminando Bajo La Lluvia"): every word capitalized means the SOURCE
       title-cased the line, not that every word is a proper noun. A blind
       interior-preserve would leave it fully title-cased, defeating the
       lowercase look. → lowercase everything EXCEPT words in _PROPER_NOUNS
       (countries / gentilicios / common names) so "Te Amo Argentina" →
       "te amo Argentina". (matrix test 2026-06-02.)

    Original whitespace is preserved so layout-sensitive renders don't collapse
    double spaces.
    """
    import re as _re

    def _first_alpha_upper(tok: str) -> bool:
        for ch in tok:
            if ch.isalpha():
                return ch.isupper()
        return False

    words = [t for t in _re.split(r"\s+", text) if any(c.isalpha() for c in t)]
    uniformly_titled = len(words) >= 2 and all(_first_alpha_upper(w) for w in words)

    out = []
    seen_word = False
    for tok in _re.split(r"(\s+)", text):
        if not tok or tok.isspace():
            out.append(tok)
            continue
        if uniformly_titled:
            # title-cased source: lowercase all but known proper nouns
            core = tok.lower().strip(".,;:!?¡¿\"'()[]—–-…")
            # Preserve proper nouns, but NORMALIZE to Title case so an ALL-CAPS
            # source ("DIOS ES AMOR", "TE AMO ARGENTINA") doesn't echo the word
            # back in uppercase inside an otherwise-lowercase line.
            out.append(tok.capitalize() if core in _PROPER_NOUNS else tok.lower())
        elif not seen_word:
            out.append(tok.lower())  # first word: lowercase for the aesthetic
            seen_word = True
        else:
            out.append(tok)  # interior: keep operator's casing (proper nouns)
    return "".join(out)


def _apply_case(text: str, case: str) -> str:
    """Apply text-case transformation matching the user's choice."""
    if case == "upper":
        return text.upper()
    if case == "title":
        return text.title()
    if case == "lower":
        return _smart_lower(text)
    return text  # "original" — keep as transcribed


def _text_position_func(spec, motion: str, seg_duration: float,
                        clip_x: int = 0, clip_y: int = 0,
                        shadow_offset: int = 0):
    """Return a position callable (or string/tuple) for a text clip.

    clip_x / clip_y are the top-left pixel coordinates that would place
    the clip centered on screen (computed by the caller from actual clip
    dimensions). shadow_offset shifts both axes so the shadow sits just
    behind the main text. Motion values: "none" | "subtle" | "float".

    NOTE: "float" is temporarily aliased to "subtle". The per-frame
    position callable forces moviepy's CompositeVideoClip to evaluate
    `pos(t)` for every frame of every text layer, blocking the
    optimizations that let static positions cache. For songs with 30+
    lyric lines × 4320 frames (3 min @ 24fps) that's an order of
    magnitude slower and was hitting the 20-min RQ timeout on prod.
    Re-enable the distinct "float" amplitude once we move the text
    layer rendering to ffmpeg overlay filters (where per-frame motion
    is essentially free).
    """
    import math
    if motion == "float":
        motion = "subtle"
    if motion == "none" or not motion:
        if shadow_offset:
            return (clip_x + shadow_offset, clip_y + shadow_offset)
        return "center"

    period = max(seg_duration, 0.5)
    amp_scale = spec.text_scale

    if motion == "subtle":
        amplitude = max(2, int(round(4 * amp_scale)))

        def pos(t):
            dy = amplitude * math.sin(2 * math.pi * t / period)
            return (clip_x + shadow_offset, clip_y + int(dy) + shadow_offset)
    else:  # "float"
        amp_y = max(4, int(round(8 * amp_scale)))
        amp_x = max(1, int(round(3 * amp_scale)))

        def pos(t):
            dy = amp_y * math.sin(2 * math.pi * t / period)
            dx = amp_x * math.sin(math.pi * t / period + 0.5)
            return (clip_x + int(dx) + shadow_offset, clip_y + int(dy) + shadow_offset)

    return pos


_CONTRAST_SETTINGS = {
    "subtle": {"stroke_mult": 1.5, "shadow_opacity": 0.40, "extra_shadow": False},
    "medium": {"stroke_mult": 2.5, "shadow_opacity": 0.55, "extra_shadow": False},
    "strong": {"stroke_mult": 3.5, "shadow_opacity": 0.65, "extra_shadow": True},
}


def _make_text_clip(
    text: str,
    seg_start: float,
    seg_end: float,
    font: str = "Arial",
    spec: RenderSpec | None = None,
    text_case: str = "upper",
    font_scale: float = 1.0,
    lyric_transition: str = "cut",
    text_motion: str = "none",
    text_contrast: str = "medium",
    line_pos: tuple | None = None,
    line_scale: float = 1.0,
    line_rot: float = 0.0,
):
    """Create a clean text clip matching pro lyric video style (bold white, outline + shadow).

    Per-line layout overrides (line_pos/line_scale/line_rot) come from the
    editor preview and mirror the ASS path (ass_render.segments_to_lines):
    line_scale multiplies the tier fontsize; line_pos centers the line at a
    0..1 fraction of the frame; line_rot tilts it (CSS-clockwise degrees). When
    none are set the centered/text_motion behavior is byte-for-byte unchanged."""
    import unicodedata
    if spec is None:
        spec = RenderSpec.youtube_default()

    # Apply case transform then sanitize for ImageMagick
    display_text = unicodedata.normalize("NFC", _apply_case(text, text_case))
    display_text = display_text.replace("@", "").replace("`", "'").replace("\x00", "")

    # Empty-string guard — ImageMagick errors with "label expected" on blank input
    if not display_text.strip():
        return []

    scale = spec.text_scale
    # font_scale is the user-chosen size multiplier (default 1.0 = unchanged)
    font_scale = max(0.6, min(1.5, float(font_scale or 1.0)))

    import ass_render as _ass
    text_len = len(display_text)
    # text_width (caption wrap) stays tier-based here; fontsize comes from
    # the shared tier helper so the moviepy and ASS paths size lines
    # identically (single source of truth in ass_render).
    if text_len > 80:
        text_width = int(round(1700 * scale))
    elif text_len > 50:
        text_width = int(round(1650 * scale))
    else:
        text_width = int(round(1500 * scale))

    fontsize = _ass.lyric_fontsize(text_len, scale, font_scale)
    # Per-line scale override from the editor preview (parity with ASS
    # segments_to_lines): multiply the tier fontsize, floor at 8px.
    if line_scale and float(line_scale) > 0 and float(line_scale) != 1.0:
        fontsize = max(8, int(round(fontsize * float(line_scale))))

    shadow_offset = max(1, int(round(3 * scale)))
    fallback_font = os.path.join(_FONTS_DIR, "Montserrat-Bold.ttf")
    contrast = _CONTRAST_SETTINGS.get(text_contrast, _CONTRAST_SETTINGS["medium"])
    stroke_width = max(1.0, contrast["stroke_mult"] * scale)

    seg_duration = max(0.1, seg_end - seg_start)

    # Fade + perceptual onset offset come from the shared tier helpers
    # (ass_render) so both render paths agree. The offset shifts the visual
    # onset earlier by half the fade: text ramps 0→100% opacity, humans
    # perceive "it appeared" at ~50%, so without this the operator's
    # anchored timestamp (Sync Mode Space tap) reads fade_dur/2 LATE. Cuts
    # → fade 0 → no shift. Fade is capped at seg/3 inside fade_seconds.
    fade_dur = _ass.fade_seconds(lyric_transition, seg_duration)
    adjusted_start = _ass.perceptual_start(seg_start, fade_dur)
    adjusted_end = seg_end  # End is unaffected; only the visual onset shifts.

    # Per-line layout override (move/rotate). When present we position the
    # line EXPLICITLY (centered on line_pos, rotated by line_rot) and ignore
    # text_motion — the operator placed it deliberately in the editor. Absent
    # → the centered/motion path below runs unchanged.
    _has_layout = line_pos is not None or bool(line_rot and float(line_rot) != 0.0)
    _mv_rot = -float(line_rot or 0.0)  # CSS clockwise → moviepy CCW-positive

    def _place(clip, dx=0, dy=0):
        # Rotate (expands the bbox) then center the rotated clip on line_pos,
        # plus an optional screen-space offset (drop-shadow displacement).
        if _mv_rot:
            clip = clip.rotate(_mv_rot)
        return clip.set_position(_ass.moviepy_line_placement(
            line_pos, clip.w, clip.h, spec.width, spec.height, dx, dy))

    def _try_text_clip(txt, fsize, fnt, color, **kwargs):
        try:
            return TextClip(txt, fontsize=fsize, font=fnt, color=color,
                            method="caption", size=(text_width, None), align="center", **kwargs)
        except Exception:
            return TextClip(txt, fontsize=fsize, font=fallback_font, color=color,
                            method="caption", size=(text_width, None), align="center", **kwargs)

    shadow = _try_text_clip(display_text, fontsize, font, "black").set_opacity(contrast["shadow_opacity"])
    sh = shadow.size[1]
    # Centered top-left coordinates for a clip of size (text_width, sh)
    base_x = (spec.width - text_width) // 2
    base_y = (spec.height - sh) // 2
    if _has_layout:
        shadow = _place(shadow, shadow_offset, shadow_offset)
    else:
        shadow_pos = _text_position_func(spec, text_motion, seg_duration,
                                         clip_x=base_x, clip_y=base_y,
                                         shadow_offset=shadow_offset)
        if callable(shadow_pos):
            shadow = shadow.set_position(lambda t, _p=shadow_pos: _p(t))
        else:
            shadow = shadow.set_position((base_x + shadow_offset, base_y + shadow_offset))
    shadow = shadow.set_start(adjusted_start).set_end(adjusted_end)

    layers = []

    # "strong" mode: add a counter-shadow at the opposite offset to widen the halo
    if contrast["extra_shadow"]:
        shadow2 = _try_text_clip(display_text, fontsize, font, "black").set_opacity(contrast["shadow_opacity"] * 0.5)
        if _has_layout:
            shadow2 = _place(shadow2, -shadow_offset, -shadow_offset)
        else:
            shadow2_pos = _text_position_func(spec, text_motion, seg_duration,
                                              clip_x=base_x, clip_y=base_y,
                                              shadow_offset=-shadow_offset)
            if callable(shadow2_pos):
                shadow2 = shadow2.set_position(lambda t, _p=shadow2_pos: _p(t))
            else:
                shadow2 = shadow2.set_position((base_x - shadow_offset, base_y - shadow_offset))
        shadow2 = shadow2.set_start(adjusted_start).set_end(adjusted_end)
        if fade_dur > 0:
            shadow2 = shadow2.crossfadein(fade_dur).crossfadeout(fade_dur)
        layers.append(shadow2)

    layers.append(shadow)

    txt = _try_text_clip(display_text, fontsize, font, "white",
                         stroke_color="black", stroke_width=stroke_width)
    if _has_layout:
        txt = _place(txt, 0, 0)
    else:
        txt_pos = _text_position_func(spec, text_motion, seg_duration,
                                      clip_x=base_x, clip_y=base_y,
                                      shadow_offset=0)
        if callable(txt_pos):
            txt = txt.set_position(lambda t, _p=txt_pos: _p(t))
        else:
            txt = txt.set_position("center")
    txt = txt.set_start(adjusted_start).set_end(adjusted_end)

    if fade_dur > 0:
        shadow = shadow.crossfadein(fade_dur).crossfadeout(fade_dur)
        txt = txt.crossfadein(fade_dur).crossfadeout(fade_dur)

    layers.append(txt)
    return layers


_UMG_PROFILE_NAMES = {
    3: {"HQ"},
    4: {"4444"},
    5: {"4444 XQ", "XQ"},
}


def _eval_fraction(value: str) -> float:
    """Evaluate a rational string like '24000/1001' into a float."""
    if value is None:
        return 0.0
    if "/" in value:
        num, den = value.split("/", 1)
        try:
            d = float(den)
            return float(num) / d if d else 0.0
        except ValueError:
            return 0.0
    try:
        return float(value)
    except ValueError:
        return 0.0


def _short_prores_spec(umg_spec: dict) -> "RenderSpec":
    """Build a vertical (1080×1920, 9:16) ProRes spec out of a UMG
    delivery dict. We keep the operator's chosen fps + prores_profile
    so the ProRes short stays consistent with the master, but flip
    dimensions and DAR for the short's vertical canvas.
    """
    from render_spec import UMG_PRORES_PROFILES
    profile_id = int(umg_spec.get("prores_profile", 3))
    fps_val = float(umg_spec.get("fps", 24.0))
    prof = UMG_PRORES_PROFILES.get(profile_id, UMG_PRORES_PROFILES[3])
    return RenderSpec(
        profile="umg",
        width=1080, height=1920,
        fps=fps_val,
        dar=(9, 16),
        codec="prores_ks",
        prores_profile=profile_id,
        pix_fmt=prof["pix_fmt"],
        audio_codec="pcm_s24le",
        color_primaries="bt709",
        container="mov",
    )


def _probe_dims_fps(path: str) -> tuple[int, int, str] | None:
    """ffprobe v:0 to extract (width, height, r_frame_rate). Returns None
    on any failure — caller falls back to the legacy scale+fps path so a
    flaky probe never breaks an otherwise-valid transcode."""
    try:
        out = subprocess.check_output(
            ["ffprobe", "-v", "error", "-select_streams", "v:0",
             "-show_entries", "stream=width,height,r_frame_rate",
             "-of", "default=noprint_wrappers=1:nokey=1", path],
            text=True, timeout=30,
        ).strip().splitlines()
        if len(out) < 3:
            return None
        return int(out[0]), int(out[1]), out[2]
    except Exception:
        return None


def _transcode_to_prores(input_path: str, mov_path: str,
                          spec: "RenderSpec",
                          timeout_sec: int = 600) -> None:
    """Transcode an h264 mp4 → ProRes .mov per the given RenderSpec.

    Used by /download/{id}/umg_master and /download/{id}/umg_short to
    produce ProRes deliverables lazily on the first download click,
    instead of running a second moviepy render at pipeline time.
    ffmpeg only — no moviepy involvement — so the moviepy palindrome-
    loop hang that breaks the dual-render path doesn't apply here.

    PURE RECODE FAST PATH (the world-class case for UMG):
    when the source MP4 is already at the exact target dimensions and
    fps (which is what `pipeline.run_pipeline` produces for any
    delivery_profile in (umg, both) since the world-class refactor),
    we skip the `scale=` and `fps=` filters entirely. Frames pass
    through 1:1 — no chroma stretch, no fps interpolation. UMG's
    manual QC explicitly rejects frame-rate-conversion artifacts, so
    this path is what makes the master shippable for any of the 4 ×
    8 frame-size × fps combinations they accept.

    LEGACY SCALE+FPS PATH:
    when the source dims/fps don't match (older jobs rendered before
    the refactor, or a custom upload route that bypasses the spec),
    fall back to the previous behaviour with `scale=lanczos` +
    `fps=`. Logs a warning because the output may fail UMG manual QC.

    Args:
      input_path: path to the existing source mp4 (lyric_video.mp4
                  or short.mp4).
      mov_path:   destination for the ProRes .mov.
      spec:       a RenderSpec — RenderSpec.umg(**umg_spec) for the
                  master, or _short_prores_spec(umg_spec) for the
                  vertical short.
      timeout_sec: hard kill after N seconds. 10 min is plenty for a
                   3-min song; longer means ffmpeg hung on a bad file.

    The output passes _validate_umg_master under normal conditions:
      - codec=prores_ks, profile per spec
      - exact width × height (no scale on fast path; lanczos on legacy)
      - fps via -r (rational for fractional fps); skipped on fast path
        so the bitstream timebase comes straight from the source
      - audio re-encoded to pcm_s24le @ 48 kHz @ 2 ch
      - bt709 color tags, progressive, mov container, DAR per spec.

    Raises RuntimeError on ffmpeg failure or post-transcode validation
    failure. The caller is responsible for cleaning up partial output.
    """
    src = _probe_dims_fps(input_path)
    pure_recode = (
        src is not None
        and src[0] == spec.width
        and src[1] == spec.height
        and src[2] == spec.fps_str
    )

    vf_chain = (
        # Fast path: SAR normalize + BT.709 metadata stamp. No scale,
        # no fps — frames go through 1:1.
        "setsar=1,setparams=colorspace=bt709:"
        "color_primaries=bt709:color_trc=bt709:range=tv"
        if pure_recode
        else
        # Legacy path: scale + fps conversion (may produce QC-failing
        # artifacts; logged below).
        f"scale={spec.width}:{spec.height}:flags=lanczos,"
        f"fps={spec.fps_str},setsar=1,"
        f"setparams=colorspace=bt709:color_primaries=bt709:"
        f"color_trc=bt709:range=tv"
    )

    cmd = [
        "ffmpeg", "-y", "-loglevel", "error",
        "-i", input_path,
        "-vf", vf_chain,
    ]
    if not pure_recode:
        # Force the timebase only when we're converting fps; the fast
        # path inherits the source fps which is already exact.
        cmd += ["-r", spec.fps_str]
    cmd += [
        "-c:v", "prores_ks",
        "-profile:v", str(spec.prores_profile),
        "-pix_fmt", spec.pix_fmt,
        "-vendor", "apl0",
        "-color_primaries", "bt709",
        "-color_trc", "bt709",
        "-colorspace", "bt709",
        "-color_range", "tv",
        "-aspect", f"{spec.dar[0]}:{spec.dar[1]}",
        "-movflags", "+faststart+write_colr",
        # Audio: re-encode to UMG's required spec regardless of input.
        "-c:a", "pcm_s24le",
        "-ar", "48000",
        "-ac", "2",
        "-f", "mov",
        mov_path,
    ]

    if pure_recode:
        logger.info("[PRORES] pure-recode %s -> %s (%sx%s @ %s, profile %s) — "
                    "source dims+fps match target, no scale/fps filter.",
                    os.path.basename(input_path), os.path.basename(mov_path),
                    spec.width, spec.height, spec.fps_str, spec.prores_profile)
    else:
        src_desc = "%sx%s@%s" % (src[0], src[1], src[2]) if src else "unknown"
        logger.warning("[PRORES] LEGACY scale+fps %s (%s) -> %s (%sx%s @ %s, profile %s). "
                       "WARNING: source mismatch may produce frame-rate-"
                       "conversion artifacts that fail UMG manual QC.",
                       os.path.basename(input_path), src_desc,
                       os.path.basename(mov_path),
                       spec.width, spec.height, spec.fps_str, spec.prores_profile)
    # ProRes UMG master — this output lands on a Drive folder UMG QCs by
    # hand. A silent 0-byte file would be caught by _validate_umg_master
    # below (ffprobe would choke on it), but checking here turns a
    # confusing downstream crash into a clear "ffmpeg-prores produced
    # empty output" in the worker log + Job.error column.
    run_checked(
        cmd,
        label="ffmpeg-prores",
        timeout=timeout_sec,
        output_path=mov_path,
    )

    # Log what ffprobe sees on the freshly-encoded master for any future
    # debugging — colorspace problems are notoriously sticky on ProRes.
    try:
        _probe = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "v:0",
             "-show_entries",
             "stream=codec_name,profile,width,height,pix_fmt,"
             "color_space,color_primaries,color_transfer,color_range,"
             "field_order,display_aspect_ratio",
             "-of", "default=noprint_wrappers=1", mov_path],
            capture_output=True, text=True, timeout=30,
        )
        logger.info("[PRORES] ffprobe stream fields:")
        for line in (_probe.stdout or "").strip().splitlines():
            logger.info("  %s", line)
    except Exception as _e:  # pragma: no cover
        logger.warning("[PRORES] ffprobe diagnostic failed: %s", _e)

    errors = _validate_umg_master(mov_path, spec)
    if errors:
        # Log errors before deleting so the worker logs surface
        # what ffprobe reported vs. what we expected — the diagnostic
        # ffprobe dump above gives the actual values; this line gives
        # the validator's interpretation.
        logger.error("[PRORES] validation failed: %s", errors)
        try:
            os.unlink(mov_path)
        except OSError:
            pass
        raise RuntimeError(
            f"transcoded ProRes failed UMG validation: {'; '.join(errors)}"
        )

    size_mb = os.path.getsize(mov_path) / 1024 / 1024
    logger.info("[PRORES] master ready: %.1f MB", size_mb)


def _validate_umg_master(path: str, spec: RenderSpec) -> list[str]:
    """Run ffprobe on the master and return a list of spec violations.

    ffprobe doesn't surface `color_primaries` and `color_transfer` for ProRes
    output (the colr atom is written but not always parsed). We require
    `color_space == "bt709"` (reliable, comes from bitstream coefficients) and
    tolerate missing color_primaries / color_transfer.

    For fractional fps (23.976 / 29.97 / 59.94), we require exact rational
    match in `r_frame_rate` to catch decimal-vs-rational drift that UMG QC
    may flag. For integer fps a 0.01 tolerance is fine.
    """
    cmd = [
        "ffprobe", "-v", "error",
        "-print_format", "json",
        "-show_streams", "-show_format",
        path,
    ]
    # Tier 4 (H2): bound the probe. This is the only critical-path subprocess
    # that was missing a timeout — ffprobe on a corrupt/truncated multi-GB
    # ProRes (the exact post-OOM state) can block forever, hanging the
    # /download lazy-transcode request thread or the prewarm worker.
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
    except subprocess.TimeoutExpired:
        return ["ffprobe timed out after 30s (corrupt or truncated master?)"]
    if result.returncode != 0:
        return [f"ffprobe failed: {result.stderr[-200:]}"]

    try:
        probe = json.loads(result.stdout)
    except json.JSONDecodeError as e:
        return [f"ffprobe output not JSON: {e}"]

    errors: list[str] = []
    v_streams = [s for s in probe.get("streams", []) if s.get("codec_type") == "video"]
    if not v_streams:
        return ["no video stream found"]
    v = v_streams[0]

    if v.get("codec_name") != "prores":
        errors.append(f"codec_name={v.get('codec_name')}, expected prores")
    expected_profiles = _UMG_PROFILE_NAMES.get(spec.prores_profile, set())
    if expected_profiles and v.get("profile") not in expected_profiles:
        errors.append(
            f"profile={v.get('profile')}, expected one of {expected_profiles}"
        )
    if (v.get("width"), v.get("height")) != (spec.width, spec.height):
        errors.append(
            f"dimensions={v.get('width')}x{v.get('height')}, "
            f"expected {spec.width}x{spec.height}"
        )

    # Frame rate: exact rational for fractional fps (R1); 0.01 tolerance for integer.
    actual_r_frame_rate = v.get("r_frame_rate")
    if spec.fps in FPS_RATIONAL:
        expected_rational = FPS_RATIONAL[spec.fps]
        if actual_r_frame_rate != expected_rational:
            errors.append(
                f"r_frame_rate={actual_r_frame_rate}, expected {expected_rational} "
                f"(exact rational required for fractional fps)"
            )
    else:
        actual_fps = _eval_fraction(actual_r_frame_rate)
        if abs(actual_fps - spec.fps) > 0.01:
            errors.append(
                f"r_frame_rate={actual_r_frame_rate} ({actual_fps:.3f}), "
                f"expected {spec.fps}"
            )

    if v.get("pix_fmt") != spec.pix_fmt:
        errors.append(f"pix_fmt={v.get('pix_fmt')}, expected {spec.pix_fmt}")

    # Color: only color_space is reliably surfaced for ProRes by ffprobe. The
    # colr atom (color_primaries + color_transfer) is written by ffmpeg but
    # ffprobe doesn't parse it for ProRes output across all versions. We
    # require color_space, and tolerate missing primaries/transfer.
    if v.get("color_space") != "bt709":
        errors.append(f"color_space={v.get('color_space')}, expected bt709")
    for optional_key in ("color_primaries", "color_transfer"):
        actual = v.get(optional_key)
        if actual is not None and actual != "bt709":
            errors.append(f"{optional_key}={actual}, expected bt709 (or absent)")

    # display_aspect_ratio is reported like "16:9" or "256:135"
    expected_dar = f"{spec.dar[0]}:{spec.dar[1]}"
    actual_dar = v.get("display_aspect_ratio")
    if actual_dar not in (expected_dar, None):
        # Tolerate equivalent ratios (e.g. "256:135" vs reduced form)
        if _eval_fraction(actual_dar.replace(":", "/")) and abs(
            _eval_fraction(actual_dar.replace(":", "/"))
            - spec.dar[0] / spec.dar[1]
        ) > 0.01:
            errors.append(
                f"display_aspect_ratio={actual_dar}, expected {expected_dar}"
            )
    field_order = v.get("field_order")
    if field_order not in (None, "progressive"):
        errors.append(f"field_order={field_order}, expected progressive")
    fmt = probe.get("format", {}).get("format_name", "")
    if "mov" not in fmt:
        errors.append(f"format_name={fmt}, expected a mov container")

    # Audio. UMG requires PCM 24-bit, 48 kHz, stereo. Catching this here
    # prevents the silent regression where moviepy's audio_fps default
    # (44100) overrides our ffmpeg `-ar 48000` and ships a non-compliant
    # master. Source MP3s are usually 44.1 kHz so this is a real risk.
    a_streams = [s for s in probe.get("streams", []) if s.get("codec_type") == "audio"]
    if not a_streams:
        errors.append("no audio stream found")
    else:
        a = a_streams[0]
        if a.get("codec_name") != "pcm_s24le":
            errors.append(
                f"audio codec_name={a.get('codec_name')}, expected pcm_s24le"
            )
        try:
            sample_rate = int(a.get("sample_rate", 0))
        except (TypeError, ValueError):
            sample_rate = 0
        if sample_rate != 48000:
            errors.append(
                f"audio sample_rate={sample_rate}, expected 48000"
            )
        if int(a.get("channels", 0)) != 2:
            errors.append(
                f"audio channels={a.get('channels')}, expected 2 (stereo)"
            )

    return errors


def _apply_display_timing(
    segments: list[dict],
    duration: float,
    max_hold_s: float = 4.0,
    gap_s: float = 0.05,
) -> list[dict]:
    """Set each lyric line's on-screen window. Two goals, one pass:

    1. HOLD-UNTIL-NEXT — Whisper/sync set `end` at the last clearly-decoded
       word, which on long sung lines lands BEFORE the vocal finishes
       (sustains, melisma), so the text vanished mid-phrase. UMG flagged
       this on "Costumbres argentinas" 2026-05-19 ("las lyrics se van antes
       de que se terminen de pronunciar"). We extend each line's display end
       toward the NEXT line's start (karaoke behaviour), capped at
       `max_hold_s` so a long instrumental break doesn't leave a line
       lingering the whole time.

    2. NO-OVERLAP — two subtitles must never render at once. Operator sync
       edits / batch replays can leave end > next.start; the ceiling
       (next.start - gap_s) enforces the upper bound.

    Both reduce to: end = min(base_end + max_hold_s, next.start - gap_s),
    floored to a >=0.3s readable window. The min() makes overlap (ceiling
    wins) and gap (hold wins) one expression. The last line holds past its
    final word, capped at `duration`. Returns a new list; input untouched.

    LOCKED lines (`seg["locked"] is True`) — the operator set this line's end
    by hand in the visual Timings editor. We must NOT auto-extend it: their
    chosen `end` is the intent. We still apply the NO-OVERLAP clamp for
    safety (a manual end can't be allowed to overrun the next line), but skip
    the hold-until-next extension. Untouched lines (no `locked`) keep the
    default karaoke hold. `locked` is preserved in the output so it round-
    trips through update_job(segments_json=...).
    """
    if not segments:
        return segments
    sorted_segs = sorted(segments, key=lambda s: s["start"])
    n = len(sorted_segs)
    cleaned = []
    for i, seg in enumerate(sorted_segs):
        base_end = seg["end"]
        locked = bool(seg.get("locked"))
        ceiling = (sorted_segs[i + 1]["start"] - gap_s) if i + 1 < n else duration
        if locked:
            # Respect the operator's manual end; only enforce no-overlap.
            new_end = min(base_end, ceiling)
            new_end = max(new_end, seg["start"] + 0.3)
        elif i + 1 < n:
            new_end = min(base_end + max_hold_s, ceiling)
            new_end = max(new_end, seg["start"] + 0.3)
        else:
            new_end = min(base_end + max_hold_s, duration)
        if new_end > duration:
            new_end = duration
        cleaned.append({**seg, "end": new_end})
    return cleaned


def _ffmpeg_filter_escape(path: str) -> str:
    """Escape a path for use inside an ffmpeg filtergraph option value
    (the subtitles= fontsdir argument). Backslashes and colons are the
    ones that bite on POSIX paths."""
    return path.replace("\\", "\\\\").replace(":", "\\:")


def _validate_rendered_mp4(path: str, expected_dur: float) -> None:
    """Raise if the rendered MP4 isn't browser-playable. Catches the
    malformed-but-exit-0 outputs the plain returncode check misses
    (incident 2026-05-21: a 124 MB ASS render with the moov atom at the
    END never started in the browser → infinite spinner). Checks: file
    non-empty, has a video + audio stream, duration within ±3 s of the
    audio, and the moov atom near the FRONT (faststart). On any failure
    the caller falls back to the proven moviepy path."""
    import json as _json
    if not os.path.exists(path) or os.path.getsize(path) < 4096:
        raise RuntimeError(f"rendered file missing/empty: {path}")
    probe = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries",
         "stream=codec_type:format=duration", "-of", "json", path],
        capture_output=True, text=True, timeout=60,
    )
    if probe.returncode != 0:
        raise RuntimeError(f"ffprobe failed on render: {probe.stderr[-300:]}")
    data = _json.loads(probe.stdout or "{}")
    types = {s.get("codec_type") for s in data.get("streams", [])}
    if "video" not in types:
        raise RuntimeError("rendered mp4 has no video stream")
    if "audio" not in types:
        raise RuntimeError("rendered mp4 has no audio stream")
    try:
        dur = float((data.get("format") or {}).get("duration") or 0)
    except (TypeError, ValueError):
        dur = 0.0
    if expected_dur and abs(dur - expected_dur) > 3.0:
        raise RuntimeError(
            f"rendered duration {dur:.1f}s != expected {expected_dur:.1f}s"
        )
    # faststart: moov must precede mdat near the front (mp4 only).
    if path.lower().endswith(".mp4"):
        with open(path, "rb") as f:
            head = f.read(2_000_000)
        moov = head.find(b"moov")
        mdat = head.find(b"mdat")
        if not (moov != -1 and (mdat == -1 or moov < mdat)):
            raise RuntimeError("rendered mp4 lacks faststart (moov not at front)")


def _render_lyrics_ass(
    bg_video_path: str,
    mp3_path: str,
    segments: list[dict],
    job_dir: str,
    duration: float,
    *,
    spec: "RenderSpec",
    font_path: str,
    text_case: str = "upper",
    font_scale: float = 1.0,
    lyric_transition: str = "cut",
    lyrics_animation: str = "none",
    line_transition: str = "none",
    text_contrast: str = "medium",
    artist: str = "",
    song_title: str = "",
    effect: str = "",
    style: str = "",
    custom_colors: str = "",
    # Lyric text colors 2026-05-25. Hex #RRGGBB; cadena vacía → blanco.
    # Para karaoke: lyric_color = palabra no cantada, lyric_sung_color =
    # palabra cantada. Para otras animaciones: lyric_color = único color
    # del texto (PrimaryColour del style en ASS).
    lyric_color: str = "",
    lyric_sung_color: str = "",
    # Title-card customization (Full Rotor v1). Defaults reproduce the
    # historical look exactly: auto layout, no size change, artist in
    # Montserrat ExtraBold, song in the lyric font.
    title_template: str = "auto",
    title_size: float = 1.0,
    title_artist_font: str = "",
    title_song_font: str = "",
    # UI v1.1 (2026-05-30): manual song-title line break. "" = auto wrap.
    title_song_break: str = "",
    # Multi-escena ("Escenas"): cuando el caller ya armó un timeline del largo
    # completo (scenes.stitch_timeline concatena las escenas con xfade), pasa
    # bg_prelooped=True para que NO se vuelva a loopear/palindromear acá — el
    # timeline ya cubre toda la canción. Default False = camino histórico de
    # fondo único (se loopea el clip Veo corto).
    bg_prelooped: bool = False,
) -> str:
    """Fast lyric render: burn the lyrics with libass in a single ffmpeg
    pass over the (ffmpeg-looped) background — no moviepy frame loop.

    Covers video backgrounds (caller pre-renders image/Ken Burns bg to a
    video first) and any spec whose codec ffmpeg can write directly: the
    YouTube H.264 MP4 and the UMG intermediate master (libx264 at the UMG
    dims/fps — the lazy ProRes transcode downstream is unchanged).
    text_motion drift still goes through the moviepy path. ~10-30x faster
    than the moviepy composite.

    Parity with _make_text_clip is enforced by reusing ass_render's
    fontsize/fade/offset tiers and the same _apply_case + outline/shadow
    derivation (stroke = contrast.stroke_mult * scale, shadow = 3*scale).
    The artist/song title card mirrors generate_lyric_video's two layouts.
    """
    import ass_render as _ass

    scale = spec.text_scale
    contrast = _CONTRAST_SETTINGS.get(text_contrast, _CONTRAST_SETTINGS["medium"])
    outline = max(1.0, contrast["stroke_mult"] * scale)
    shadow = max(1, int(round(3 * scale)))

    # 1) Background → looped file at the target dims (ffmpeg, C-level).
    #    Multi-escena: si el caller ya entregó un timeline del largo completo
    #    (escenas concatenadas con xfade), lo usamos tal cual — re-loopearlo
    #    haría un palíndromo del timeline entero (reproduciría las escenas en
    #    reversa al final). Igual lo normalizamos a las dims del spec por si el
    #    stitch corrió a otra resolución.
    if bg_prelooped:
        bg_looped = _normalize_bg_to_spec(
            bg_video_path, job_dir,
            target_w=spec.width, target_h=spec.height,
            out_name="bg_looped_ass.mp4",
        )
    else:
        bg_looped = _prerender_looped_bg(
            bg_video_path, duration, job_dir,
            target_w=spec.width, target_h=spec.height,
            out_name="bg_looped_ass.mp4",
        )

    # 2) Fonts: the lyric font + the title-card fonts. The title card can use
    #    operator-chosen fonts per element (Full Rotor v1); defaults keep the
    #    historical look — artist in Montserrat ExtraBold, song in the lyric
    #    font. All live in one fontsdir so libass can \fn-switch between them
    #    without mis-matching across the pool.
    family, bold = _ass.font_family(font_path)
    extrabold_font = os.path.join(_FONTS_DIR, "Montserrat-ExtraBold.ttf")
    if not os.path.exists(extrabold_font):
        extrabold_font = font_path  # graceful fallback
    # Per-element title fonts: resolve the chosen ids, else fall back to the
    # historical defaults (artist = ExtraBold, song = lyric font).
    title_artist_path = _resolve_font(title_artist_font) or extrabold_font
    title_song_path = _resolve_font(title_song_font) or font_path
    title_artist_family, _ = _ass.font_family(title_artist_path)
    title_song_family, _ = _ass.font_family(title_song_path)
    artist_family, _ = _ass.font_family(extrabold_font)
    font_dir = _ass.multi_font_dir(
        [font_path, extrabold_font, title_artist_path, title_song_path]
    )

    # 3) Segments → ASS lines (same case/sanitise/sizing as moviepy), plus
    #    the artist/song title card overlay (same two layouts).
    # Per-font visual-size normalization: libass scales \fs by the font's
    # internal vertical metrics, so the SAME \fs renders a different cap-height
    # per family (Poppins ~14% smaller than Montserrat → "muy chiquita").
    # Equalize to the Montserrat reference so a size setting looks consistent
    # across fonts. Keyed by the resolved family (works for explicit picks and
    # the random "Auto" pool alike). See ass_render.font_size_factor.
    font_factor = _ass.font_size_factor(family)
    lines = _ass.segments_to_lines(
        segments,
        text_scale=scale,
        font_scale=font_scale,
        font_factor=font_factor,
        lyric_transition=lyric_transition,
        animation=lyrics_animation,
        transition=line_transition,
        case_fn=lambda t: _apply_case(t, text_case),
    )
    # Lyric color mapping para build_ass (style line + override karaoke):
    # karaoke → primary = sung color, secondary = un-sung. Otras animaciones
    # usan primary = lyric_color (texto único) y secondary irrelevante.
    if lyrics_animation == "karaoke":
        primary_for_lines = lyric_sung_color or ""
        secondary_for_lines = lyric_color or ""
    else:
        primary_for_lines = lyric_color or ""
        secondary_for_lines = ""
    first_lyric_start = segments[0]["start"] if segments else duration
    lines += _ass.title_card_lines(
        artist, song_title, first_lyric_start,
        width=spec.width, height=spec.height,
        text_scale=scale,
        # Per-element fonts (operator-chosen, else historical defaults).
        lyric_font_family=title_song_family,
        artist_font_family=title_artist_family,
        # Real font files so long titles/artists are shrunk (then wrapped)
        # to the safe card width instead of overflowing the frame.
        lyric_font_path=title_song_path,
        artist_font_path=title_artist_path,
        # Operator layout + size (Full Rotor v1).
        template=title_template,
        size_multiplier=title_size,
        # UI v1.1 (2026-05-30): explicit line break for the song. Empty
        # string => None (auto wrap, historical). When the operator picked
        # their own break in the wizard, we split on the literal "\n" and
        # title_card_lines uses those lines as-is, fitting each one
        # individually.
        song_lines=(title_song_break.split("\n") if title_song_break else None),
    )
    base_fs = _ass.lyric_fontsize(40, scale, font_scale, font_factor=font_factor)
    # Reusamos el mapping primary/secondary computado arriba para
    # segments_to_lines — mismo eje semántico (karaoke usa sung como
    # PrimaryColour). Sin esto la palabra cantada se rendea con
    # PrimaryColour blanco aunque el operador haya elegido otro color.
    ass_doc = _ass.build_ass(
        width=spec.width, height=spec.height,
        font_name=family, base_fontsize=base_fs,
        outline=outline, shadow=shadow, lines=lines, bold=bold,
        primary_color=primary_for_lines,
        secondary_color=secondary_for_lines,
    )
    ass_path = os.path.join(job_dir, "lyrics.ass")
    with open(ass_path, "w", encoding="utf-8") as f:
        f.write(ass_doc)

    # 4) Single ffmpeg pass: burn ASS + mux audio. We run with cwd=job_dir
    #    so the subtitles file is referenced by basename (avoids the
    #    notorious filtergraph path escaping); fontsdir is absolute+escaped.
    #    Encode args are spec-driven so the YouTube MP4 and the UMG
    #    intermediate (libx264 at UMG dims/fps) both come out right.
    out_path = os.path.join(job_dir, f"lyric_video.{spec.container}")
    if spec.codec == "libx264":
        vargs = ["-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
                 "-pix_fmt", spec.pix_fmt]
    elif spec.codec == "prores_ks":
        vargs = ["-c:v", "prores_ks", "-profile:v", str(spec.prores_profile),
                 "-pix_fmt", spec.pix_fmt, "-vendor", "apl0"]
    else:
        vargs = ["-c:v", spec.codec, "-pix_fmt", spec.pix_fmt]
    if spec.audio_codec == "aac":
        aargs = ["-c:a", "aac", "-b:a", "320k"]
    elif spec.audio_codec == "pcm_s24le":
        aargs = ["-c:a", "pcm_s24le", "-ar", "48000", "-ac", "2"]
    else:
        aargs = ["-c:a", spec.audio_codec]

    # Optional effect overlay (screen-blended, RGB) + color grade, composed in
    # the SAME single pass before the subtitles burn. fx_compositor returns the
    # right filter form: a simple -vf when there's no effect (unchanged fast
    # path), or a -filter_complex with the looped fx clip as input #2.
    import fx_compositor as _fx
    vfilter, _use_complex, _extra_in = _fx.build_video_filter(
        ass_basename="lyrics.ass", font_dir=font_dir,
        width=spec.width, height=spec.height,
        effect=effect, style=style, custom_colors=custom_colors,
    )
    _filter_args = (
        ["-filter_complex", vfilter, "-map", "[out]", "-map", "1:a"]
        if _use_complex else
        ["-vf", vfilter, "-map", "0:v", "-map", "1:a"]
    )
    # Tier 4 (H6): optional encode thread cap. Without -threads, ffmpeg grabs
    # ALL cores; if Railway co-schedules render replicas on a shared host,
    # concurrent renders oversubscribe CPU → each encode stretches → renders
    # cross the reaper's stalled threshold and look "stuck". Default empty = no
    # cap = current behaviour. Set FFMPEG_THREADS=N (e.g. cores/expected-
    # colocated-jobs) if oversubscription is observed.
    _ff_threads = os.environ.get("FFMPEG_THREADS", "").strip()
    _threads_arg = ["-threads", _ff_threads] if (_ff_threads.isdigit() and int(_ff_threads) > 0) else []
    cmd = [
        "ffmpeg", "-y", "-loglevel", "error",
        "-i", os.path.abspath(bg_looped),
        "-i", os.path.abspath(mp3_path),
        *_extra_in,
        *_filter_args,
        *_threads_arg,
        *vargs,
        *aargs,
        "-r", spec.fps_str,
        # Move the moov atom to the front so the browser can start playback
        # immediately (progressive streaming). Without this the moov lands
        # at the END of the file; on a large main video (~124 MB) the player
        # can't reach it without downloading everything → infinite spinner.
        # The short played only because it's small enough to fully buffer.
        "-movflags", "+faststart",
        "-shortest",
        os.path.basename(out_path),
    ]
    # Effect blend + grade at 4K/ProRes (UMG) is heavier than the bare subtitles
    # burn; give those renders a wider budget so a slow-but-fine pass doesn't hit
    # the timeout. Plain 1080p H.264 keeps the original 900s.
    _render_timeout = 1800 if (spec.codec == "prores_ks" or spec.width >= 3000
                               or (effect and spec.width >= 1920)) else 900
    try:
        # libass main render — the H.264 deliverable. The downstream
        # _validate_rendered_mp4() would already catch most failures,
        # but checking here (rc + non-empty file) names the failure
        # mode clearly in the worker log instead of getting a generic
        # ffprobe "Invalid data" from the validator.
        run_checked(
            cmd,
            label="ffmpeg-libass",
            timeout=_render_timeout,
            output_path=out_path,
            cwd=job_dir,
        )
    finally:
        import shutil
        shutil.rmtree(font_dir, ignore_errors=True)

    # Validate the output is actually browser-playable; on failure the
    # caller (generate_lyric_video) catches and falls back to moviepy.
    _validate_rendered_mp4(out_path, duration)
    size_mb = os.path.getsize(out_path) / (1024 * 1024)
    logger.info("[ASS] lyric video rendered: %.0fs audio, %.1f MB (libass fast path, validated)",
                duration, size_mb)
    return out_path


def _resolve_title_song(song_title: str, mp3_path: str, artist: str) -> str:
    """Resolve the song title shown on the title card. Prefers the title the
    user set on the job; falls back to parsing the mp3 filename ("Artist -
    Title" or "Title_Artist", stripping "(Official Video)" etc.). Then a
    defensive scrub so legacy rows never render a literal underscore-joined
    filename. Single source of truth shared by the moviepy and libass paths.
    """
    if song_title:
        title_song = song_title
    else:
        raw_name = os.path.splitext(os.path.basename(mp3_path))[0]
        title_song = raw_name
        if " - " in raw_name:
            title_song = raw_name.split(" - ", 1)[1]
        elif "_" in raw_name:
            title_song = raw_name.split("_", 1)[0]
        for sfx in ["(Official Video)", "(Official Audio)", "(Lyric Video)",
                     "(Official Music Video)", "(Audio)", "(Video)", "(En Vivo)",
                     "(Live)", "(Lyrics)"]:
            title_song = title_song.replace(sfx, "").strip()

    if title_song:
        if " - " in title_song and artist and title_song.startswith(artist):
            title_song = title_song.split(" - ", 1)[1].strip()
        if artist and title_song.endswith(f"_{artist}"):
            title_song = title_song[: -(len(artist) + 1)].strip()
        if "_" in title_song and not artist:
            title_song = title_song.split("_", 1)[0].strip()
    return title_song


def generate_lyric_video(
    mp3_path: str,
    segments: list[dict],
    style: str,
    job_dir: str,
    artist: str,
    bg_image_path: str | None = None,
    spec: RenderSpec | None = None,
    font: str | None = None,
    song_title: str = "",
    text_case: str = "upper",
    font_scale: float = 1.0,
    lyric_transition: str = "cut",
    text_motion: str = "none",
    lyrics_animation: str = "none",
    line_transition: str = "none",
    text_contrast: str = "medium",
    effect: str = "",
    custom_colors: str = "",
    # Lyric text colors 2026-05-25. Hex #RRGGBB; "" → blanco default.
    lyric_color: str = "",
    lyric_sung_color: str = "",
    # Title-card customization (Full Rotor v1). Defaults = historical look.
    title_template: str = "auto",
    title_size: float = 1.0,
    title_artist_font: str = "",
    title_song_font: str = "",
    # UI v1.1 (2026-05-30): manual song-title line break ("" = auto wrap).
    title_song_break: str = "",
    # Multi-escena: bg_image_path ya es un timeline del largo completo (escenas
    # con xfade) → no re-loopear en el render. Se propaga a _render_lyrics_ass.
    bg_prelooped: bool = False,
) -> tuple[str, str, str | None]:
    """Generate a lyric video. Returns (video_path, font, bg_source).

    When `spec` is None, produces the YouTube MP4 (H.264 / 1080p / 24 fps /
    yuv420p). When `spec.profile == "umg"`, produces a ProRes .mov master
    with BT.709 color tags and display aspect ratio per UMG specs.

    `bg_source` is the path to the raw background (mp4 or jpg) so short/
    thumbnail can reuse it without burned-in lyrics.
    """
    if spec is None:
        spec = RenderSpec.youtube_default()

    import time as _time

    audio = AudioFileClip(mp3_path)
    duration = audio.duration

    # Load background — can be video (.mp4) or image (.jpg/.png with Ken Burns)
    bg_source = bg_image_path
    if not bg_source:
        bg_source = _find_background_video()
    if not bg_source:
        raise RuntimeError("No background available. Check Veo 3 API or add videos to assets/backgrounds/")

    # Pick a font for this job (or reuse the caller-provided one).
    # For UMG profile, the choice is deterministic (derived from job_dir hash)
    # so retries of the same job produce the same font — UMG QC and editorial
    # review don't expect font drift across re-deliveries.
    if font is None:
        if _FONT_POOL:
            if spec.profile == "umg":
                seed = int(hashlib.sha1(job_dir.encode()).hexdigest()[:8], 16)
                font = _FONT_POOL[seed % len(_FONT_POOL)]
            else:
                font = random.choice(_FONT_POOL)
        else:
            font = "Arial"
    logger.info("[FONT] Selected: %s", os.path.basename(font))

    # Drop empty / whitespace-only segments BEFORE clamping so the
    # neighbor indices used for overlap clamp are correct. Operator
    # can leave blank rows from "Agregar línea" if they don't type
    # lyrics; passing empty text to ImageMagick triggers a "label
    # expected" error and aborts the whole render.
    if segments:
        before = len(segments)
        segments = [s for s in segments if (s.get("text") or "").strip()]
        dropped = before - len(segments)
        if dropped:
            logger.info("[RENDER] dropped %s blank segment(s) before render", dropped)

    # Display-timing normalization (hold-until-next + no-overlap). See
    # _apply_display_timing for the full rationale + the UMG incident.
    if segments:
        segments = _apply_display_timing(segments, duration)

    # Title shown on the card — resolved once and shared by both render
    # paths (libass below, moviepy further down).
    title_song = _resolve_title_song(song_title, mp3_path, artist)

    # Fast path: libass single-pass render (engine=ass). Covers video bg +
    # H.264 (YouTube) and the UMG intermediate master (profile
    # "umg_intermediate", still libx264 → the lazy ProRes transcode is
    # unchanged). Image/Ken Burns bg is resolved to a video first (see below).
    #
    # 2026-05-23: removida la cláusula `text_motion == "none"` del gate.
    # text_motion quedó deprecado upstream (siempre coerce a "none" en
    # main.py), pero la condición se sacó para que jobs en cola anteriores
    # al deploy con text_motion legacy también pasen por ASS — antes
    # forzaban moviepy y apagaban silenciosamente lyrics_animation +
    # line_transition.
    #
    # 2026-05-25: default cambiado de "moviepy" → "ass". Razón: moviepy
    # NO renderiza los templates de lyrics_animation (karaoke/word_reveal/
    # pop/glow) ni line_transition (slide_up/slide_side/wipe/dissolve_blur).
    # Solo libass los implementa. El operador reportó que sus selecciones
    # "no salen en el video" — era esto: el default mandaba todo por
    # moviepy, ignorando silenciosamente las animaciones. Si libass falla
    # en runtime, el try/except (líneas ~7664+) cae a moviepy igual.
    # Override vía env LYRIC_RENDER_ENGINE=moviepy para forzar path viejo.
    _engine = os.environ.get("LYRIC_RENDER_ENGINE", "ass").lower()
    _bg_is_video = not bg_source.lower().endswith((".jpg", ".jpeg", ".png"))
    _ass_ok_profile = spec.profile in ("youtube", "umg_intermediate")
    if _engine == "ass" and _ass_ok_profile:
        try:
            ass_bg = bg_source
            if not _bg_is_video:
                # Image background → pre-render the Ken Burns motion to a
                # video with ffmpeg zoompan so the single-pass burn applies.
                ass_bg = _prerender_kenburns_bg(
                    bg_source, duration, job_dir, spec=spec,
                )
            logger.info("[ASS] libass fast path (engine=ass, profile=%s, bg=%s)",
                        spec.profile, "image" if not _bg_is_video else "video")
            _t0 = _time.monotonic()
            out = _render_lyrics_ass(
                ass_bg, mp3_path, segments, job_dir, duration,
                spec=spec, font_path=font, text_case=text_case,
                font_scale=font_scale, lyric_transition=lyric_transition,
                lyrics_animation=lyrics_animation,
                line_transition=line_transition,
                text_contrast=text_contrast,
                artist=artist, song_title=title_song,
                effect=effect, style=style, custom_colors=custom_colors,
                lyric_color=lyric_color, lyric_sung_color=lyric_sung_color,
                title_template=title_template, title_size=title_size,
                title_artist_font=title_artist_font, title_song_font=title_song_font,
                title_song_break=title_song_break,
                bg_prelooped=bg_prelooped,
            )
            logger.info("[ASS] render: %.1fs (engine=ass)", _time.monotonic() - _t0)
            audio.close()
            return out, font, bg_source
        except Exception as e:
            # Never fail the job on a fast-path error — fall through to the
            # proven moviepy composite below.
            logger.warning(
                "[ASS] fast path failed (%s); falling back to moviepy", e
            )

    # --- moviepy composite path (default) ---
    if bg_source.lower().endswith((".jpg", ".jpeg", ".png")):
        bg = _ken_burns_clip(bg_source, duration, spec=spec)
    else:
        bg = _get_background_clip_from_path(bg_source, style, duration, job_dir, spec=spec,
                                            bg_prelooped=bg_prelooped)

    # Build text clips — each segment gets its own shadow + text
    text_layers = []

    # Title overlay — pick ONE strategy based on whether there's a
    # real instrumental intro:
    # - intro >= 3s of silence before first lyric: cinematic centered
    #   "drop" title that fills the frame and fades just before the
    #   first sung line.
    # - no real intro (first lyric near t=0): compact top-third title
    #   card for the first 5s, top placement so it doesn't fight the
    #   centered subtitles.
    # Never both — they were rendering simultaneously when first_lyric
    # was past 3s, leaving "ARTIST/Title" stamped at top while the big
    # drop title also showed centered.
    first_lyric_start = segments[0]["start"] if segments else duration
    # title_song already resolved above (shared with the libass path) via
    # _resolve_title_song.

    if artist or title_song:
        # Artist name renders in ExtraBold (heavier weight) to visually
        # distinguish it from the song title, which stays in Bold.
        extrabold_font = os.path.join(_FONTS_DIR, "Montserrat-ExtraBold.ttf")
        if not os.path.exists(extrabold_font):
            extrabold_font = font  # graceful fallback

        # NFC-normalise so decomposed accents from macOS filenames (e.g.
        # "Así" as 'i'+combining-acute) render attached, not as a floating
        # mark — parity with the libass path's title_card_lines. The moviepy
        # caption method already word-wraps within card_width, so no overflow
        # shrink is needed on this fallback path.
        import unicodedata as _unicodedata
        artist_upper = _unicodedata.normalize("NFC", artist).upper() if artist else ""
        title_display = _unicodedata.normalize("NFC", title_song) if title_song else ""

        # The title card MUST always appear — users (UMG, internal QA)
        # want the artist+song readable on every video. Two layouts:
        #
        #   LONG intro (>0.8 s before first lyric):
        #     Centered "card" with large artist+song. Fades in/out over
        #     the intro period before the first lyric. Same visual as
        #     before the 2026-05-11 rewrite.
        #
        #   SHORT intro (≤0.8 s; Whisper often hallucinates the first
        #   "lyric" near t=0 even when there's a real instrumental):
        #     Compact lower-left "lower-third" overlay. Smaller font,
        #     sits in the bottom-left corner so it doesn't overlap the
        #     centred lyric line. Visible for 6 s, with crossfade in/out.
        #
        # The old code skipped the title entirely on short intros,
        # producing the "title NEVER appears" bug the user reported.
        #
        # Implementation note: moviepy 1.0.3 accepts crossfadein/
        # crossfadeout as clip transforms, but its set_opacity broke
        # when passed a function (TypeError: 'function' * 'float').
        # We now use STATIC opacity + crossfade transforms, which work
        # uniformly across moviepy versions.
        try:
            from moviepy.video.fx.crossfadein import crossfadein
            from moviepy.video.fx.crossfadeout import crossfadeout
        except Exception:  # pragma: no cover — older moviepy paths
            crossfadein = crossfadeout = None

        try:
            scale = spec.text_scale
            START_T = 0.3            # delay before card appears

            # Layout geometry comes from the SHARED helper (ass_render.
            # title_card_layout) so this moviepy fallback never drifts from
            # the libass path: same sizes, alignment, anchor, card width and
            # opacities per template + size_multiplier. Timing (how long the
            # card shows + fades) still follows the intro window below.
            import ass_render as _ass
            has_long_intro = first_lyric_start > START_T + 0.5
            L = _ass.title_card_layout(
                title_template, has_long_intro, size_multiplier=title_size)
            artist_size = max(L["floor_artist"], int(round(L["artist_base"] * scale)))
            song_size = max(L["floor_title"], int(round(L["title_base"] * scale)))
            card_width = int(round(spec.width * L["card_w_frac"]))
            stroke_w = max(1, int(round((1.6 if has_long_intro else 1.2) * scale)))
            anchor = L["anchor"]                     # center | lower_third | bottom
            position_x_center = L["x_frac"] >= 0.5
            base_opacity_artist = L["op_artist"]
            base_opacity_song = L["op_song"]
            # Per-element fonts: operator-chosen ids, else historical defaults
            # (artist = ExtraBold, song = lyric font).
            mvp_artist_font = _resolve_font(title_artist_font) or extrabold_font
            mvp_song_font = _resolve_font(title_song_font) or font
            if has_long_intro:
                title_end = min(first_lyric_start - 0.2, START_T + 8.0)
                clip_dur = title_end - START_T
                fade_in = min(0.4, max(0.1, clip_dur * 0.25))
                fade_out = min(0.7, max(0.1, clip_dur * 0.35))
            else:
                title_end = START_T + 6.0
                clip_dur = title_end - START_T
                fade_in, fade_out = 0.4, 0.8

            title_card_clips = []

            if artist_upper:
                artist_clip = TextClip(
                    artist_upper, fontsize=artist_size, font=mvp_artist_font,
                    color="white", stroke_color="black", stroke_width=stroke_w,
                    method="caption", size=(card_width, None), align="center" if position_x_center else "West",
                )
                title_card_clips.append((artist_clip, base_opacity_artist))

            if title_display:
                song_clip = TextClip(
                    title_display, fontsize=song_size, font=mvp_song_font,
                    color="white", stroke_color="black", stroke_width=max(1, int(round(1.2 * scale))),
                    method="caption", size=(card_width, None), align="center" if position_x_center else "West",
                )
                title_card_clips.append((song_clip, base_opacity_song))

            if title_card_clips:
                total_h = sum(c.size[1] for c, _ in title_card_clips) + 8 * (len(title_card_clips) - 1)

                if anchor == "center":
                    y_cursor = (spec.height - total_h) // 2
                elif anchor == "lower_third":
                    # block centred on ~74% of frame height (broadcast lower-third)
                    y_cursor = int(spec.height * 0.74 - total_h / 2)
                else:  # bottom — 8% safe-area margin
                    bottom_margin = int(spec.height * 0.08)
                    y_cursor = spec.height - bottom_margin - total_h

                if position_x_center:
                    cx = spec.width // 2
                else:
                    # Left margin = 6% of frame width.
                    left_margin = int(spec.width * 0.06)

                for clip, base_op in title_card_clips:
                    cw, ch = clip.size
                    if position_x_center:
                        x = cx - cw // 2
                    else:
                        x = left_margin
                    clip = (clip
                            .set_opacity(base_op)
                            .set_position((x, y_cursor))
                            .set_start(START_T).set_end(title_end))
                    # Apply crossfade transforms if moviepy provides them.
                    # Skipping fades on older paths still beats no title.
                    if crossfadein is not None and crossfadeout is not None:
                        clip = clip.fx(crossfadein, fade_in).fx(crossfadeout, fade_out)
                    text_layers.append(clip)
                    y_cursor += ch + 8
        except Exception as e:
            logger.warning("[TITLE] title card failed (%s); continuing", e)

    # CV1 (audit 2026-05-25) — Visibility en degraded path.
    # El moviepy fallback NO implementa los templates de lyrics_animation
    # (karaoke/word_reveal/pop/glow) ni line_transition (slide_up/
    # slide_side/wipe/dissolve_blur). Esos viven en ass_render.py (libass).
    # En condiciones normales, este path NO se ejecuta (libass anda en
    # Railway por default). Pero si llega acá CON animations seleccionadas,
    # el video se renderiza pero las animations se ignoran silenciosamente.
    # Acción: log WARNING + Sentry breadcrumb para que ops vea cuando este
    # path corre con feature seleccionada. Si nunca se ve en producción
    # tras 30 días, deprecar moviepy entirely (sprint 3+).
    if lyrics_animation != "none" or line_transition != "none":
        logger.warning(
            "[MOVIEPY_DEGRADED] rendering with moviepy fallback but "
            "lyrics_animation=%r / line_transition=%r — these libass "
            "templates are NOT applied in moviepy path; video will render "
            "with plain text. Investigate why libass fast path was bypassed "
            "(check LYRIC_RENDER_ENGINE env var or earlier '[ASS] fast "
            "path failed' log).",
            lyrics_animation, line_transition,
        )
        try:
            import sentry_sdk
            sentry_sdk.add_breadcrumb(
                category="render",
                message="moviepy fallback with animation requested",
                level="warning",
                data={
                    "lyrics_animation": lyrics_animation,
                    "line_transition": line_transition,
                    "spec_profile": getattr(spec, "profile", None),
                    "engine_env": os.environ.get("LYRIC_RENDER_ENGINE", "ass"),
                },
            )
        except Exception:  # pragma: no cover
            pass

    for seg in segments:
        # Per-line layout overrides set in the editor preview (parity with the
        # ASS path's segments_to_lines). Absent → centered/motion default.
        _lp = seg.get("pos")
        _line_pos = (
            (float(_lp["x"]), float(_lp["y"]))
            if isinstance(_lp, dict) and "x" in _lp and "y" in _lp else None
        )
        _ls = seg.get("scale")
        _line_scale = float(_ls) if isinstance(_ls, (int, float)) and _ls > 0 else 1.0
        _lr = seg.get("rot")
        _line_rot = float(_lr) if isinstance(_lr, (int, float)) else 0.0
        layers = _make_text_clip(
            seg["text"], seg["start"], seg["end"], font, spec=spec,
            text_case=text_case, font_scale=font_scale,
            lyric_transition=lyric_transition, text_motion=text_motion,
            text_contrast=text_contrast,
            line_pos=_line_pos, line_scale=_line_scale, line_rot=_line_rot,
        )
        text_layers.extend(layers)

    # Effect overlay + color grade — moviepy path (fallback, and the path that
    # runs whenever ffmpeg lacks libass). Mirror the libass pipeline exactly:
    #   bg → [effect SCREEN-blend] → [grade] → (lyrics on top).
    # TRUE screen blend (additive: 1-(1-bg)(1-fx)) over the bright-on-black loop
    # — never darkens, no channel bias. (The earlier luminance-mask approach did
    # alpha-OVER, which darkened mid-tones and used only the red channel.)
    import fx_compositor as _fx
    import numpy as _np
    _fx_clip = None
    try:
        _fx_path = _fx.effect_path(effect)
        if _fx_path:
            from moviepy.editor import VideoFileClip as _VFC, vfx as _vfx
            _fx_clip = (_cover_resize(_VFC(_fx_path), spec.width, spec.height)
                        .fx(_vfx.loop, duration=duration)
                        .set_duration(duration))
    except Exception as _e:
        logger.warning("[FX] moviepy effect skipped (%s); continuing", _e)
        _fx_clip = None

    # custom_colors must be threaded into both grade_filter() and grade_frame()
    # or the moviepy fallback drops the operator's custom palette (2026-06-02).
    _grade_style = style if _fx.grade_filter(style, custom_colors) else ""
    if _fx_clip is not None or _grade_style:
        _base_src = bg
        _fx_src = _fx_clip

        def _fx_base_frame(t):
            b = _base_src.get_frame(t).astype(_np.float32)
            if _fx_src is not None:
                f = _fx_src.get_frame(t).astype(_np.float32)
                b = 255.0 - (255.0 - b) * (255.0 - f) / 255.0  # screen
            return _fx.grade_frame(b, _grade_style, custom_colors).clip(0, 255).astype("uint8")

        base = VideoClip(_fx_base_frame, duration=duration).set_fps(spec.fps)
    else:
        base = bg

    _moviepy_t0 = _time.monotonic()
    video = CompositeVideoClip([base] + text_layers, size=(spec.width, spec.height))
    video = video.set_audio(audio).set_duration(duration)

    if spec.profile == "umg":
        out_path = os.path.join(job_dir, "umg_master.mov")
        ffmpeg_params = [
            "-r", spec.fps_str,
            "-profile:v", str(spec.prores_profile),
            "-pix_fmt", spec.pix_fmt,
            "-vendor", "apl0",
            "-color_primaries", "bt709",
            "-color_trc", "bt709",
            "-colorspace", "bt709",
            "-color_range", "tv",
            "-aspect", f"{spec.dar[0]}:{spec.dar[1]}",
            "-vf", "setsar=1",
        ]
        # moviepy 1.0.3 writes audio at the source MP3 rate (typically
        # 44.1 kHz). `audio_fps=48000` triggers a moviepy bug where it
        # mixes -c:a copy with an aresample filter and ffmpeg refuses
        # the combo, so we resample in a separate ffmpeg pass after the
        # moviepy write — two steps but each one stays in its lane.
        video.write_videofile(
            out_path,
            fps=spec.fps,
            codec=spec.codec,
            audio_codec=spec.audio_codec,
            ffmpeg_params=ffmpeg_params,
            threads=8,  # ProRes (prores_ks): preset no aplica; threads sí
            logger=None,
        )
        audio.close()
        bg.close()
        video.close()
        if _fx_clip is not None:
            _fx_clip.close()

        # Post-process: stream-copy the ProRes video and re-encode audio
        # to pcm_s24le at 48 kHz. UMG requires this exact audio spec; no
        # CPU is wasted re-encoding the multi-GB ProRes stream.
        tmp_resampled = out_path + ".audio48k.mov"
        run_checked(
            [
                "ffmpeg", "-y", "-loglevel", "error",
                "-i", out_path,
                "-c:v", "copy",
                "-c:a", "pcm_s24le",
                "-ar", "48000",
                "-ac", "2",
                tmp_resampled,
            ],
            label="ffmpeg-audio-resample",
            timeout=600,
            output_path=tmp_resampled,
        )
        os.replace(tmp_resampled, out_path)

        errors = _validate_umg_master(out_path, spec)
        if errors:
            raise RuntimeError(f"UMG validation failed: {'; '.join(errors)}")
        logger.info("[MOVIEPY] render: %.1fs (engine=moviepy, profile=%s)",
                    _time.monotonic() - _moviepy_t0, spec.profile)
        return out_path, font, bg_source

    out_path = os.path.join(job_dir, "lyric_video.mp4")
    # preset="veryfast" + threads=8: ~2.5-3x más rápido que el "medium"
    # default de moviepy en el encode x264. Seguro para el deliverable
    # YouTube/H264 (YouTube re-encoda en upload, así que el preset de la
    # fuente no afecta la calidad final; el master ProRes de UMG es un
    # path separado, intacto). Acelera la mitad-encode de cada re-render.
    video.write_videofile(
        out_path,
        fps=spec.fps,
        codec=spec.codec,
        audio_codec=spec.audio_codec,
        threads=8,
        preset="veryfast",
        # +faststart: moov atom al frente → reproducción progresiva en el
        # navegador sin depender de range-requests (universal para archivos
        # grandes). Mismo motivo que el path libass.
        ffmpeg_params=["-movflags", "+faststart"],
        logger=None,
    )
    audio.close()
    bg.close()
    video.close()
    if _fx_clip is not None:
        _fx_clip.close()
    logger.info("[MOVIEPY] render: %.1fs (engine=moviepy, profile=%s)",
                _time.monotonic() - _moviepy_t0, spec.profile)
    return out_path, font, bg_source


# ---------------------------------------------------------------------------
# Step 3 — YouTube Short (30s, vertical)
# ---------------------------------------------------------------------------

def _find_chorus_start(segments: list[dict], window_sec: int = 30) -> float:
    """Find the start of the chorus — the 30s window with most repeated lyrics."""
    if not segments:
        return 0.0

    if not segments[-1].get("end"):
        return 0.0

    total_duration = segments[-1]["end"]

    # Count how many times each line appears (normalized)
    from collections import Counter
    line_counts = Counter()
    for seg in segments:
        normalized = seg["text"].strip().lower()
        if len(normalized) > 5:  # skip very short fragments
            line_counts[normalized] += 1

    # Score each segment: repeated lines get higher scores
    for seg in segments:
        normalized = seg["text"].strip().lower()
        seg["_chorus_score"] = line_counts.get(normalized, 0)

    # Slide a window and find the 30s with highest total chorus score
    best_start = 0.0
    best_score = -1
    step = 1.0
    t = 0.0
    while t + window_sec <= total_duration + step:
        score = sum(
            seg["_chorus_score"]
            for seg in segments
            if seg["start"] >= t and seg["end"] <= t + window_sec
        )
        if score > best_score:
            best_score = score
            best_start = t
        t += step

    # Clean up temp keys
    for seg in segments:
        seg.pop("_chorus_score", None)

    # Clamp to valid range
    best_start = max(0, min(best_start, total_duration - window_sec))
    logger.info("[SHORT] Chorus detected at %.1fs (score=%s)", best_start, best_score)
    return best_start


def _make_short_text_clip(text: str, seg_start: float, seg_end: float, font: str = "Arial",
                          *, text_case: str = "upper", font_scale: float = 1.0,
                          lyric_color: str = "", text_contrast: str = "medium"):
    """Create text clips sized for vertical 1080x1920 short.

    Parity with the main video (2026-06-02 fix): honors the operator's
    text_case, font_scale, lyric_color and text_contrast instead of
    hardcoding UPPERCASE / white / fixed sizes / fixed stroke. The previous
    hardcoding is why a short looked typographically different from the main
    video (client report). Sizes are tuned for TikTok / Reels / Shorts.
    """
    # Validate the font path so a silent moviepy fallback to a system font
    # (which WOULD render different glyphs than the main video) is at least
    # visible in the logs.
    if font and font not in ("Arial",) and not os.path.isfile(font):
        logger.warning("[SHORT] font path not found, moviepy may fall back: %s", font)

    display_text = _apply_case(text, text_case or "upper")

    text_len = len(display_text)
    if text_len > 60:
        base_size, text_width = 75, 1000
    elif text_len > 35:
        base_size, text_width = 95, 980
    else:
        base_size, text_width = 115, 950
    # Honor font_scale (clamped like the main path) instead of a fixed size.
    fontsize = max(20, int(round(base_size * max(0.5, min(2.0, font_scale or 1.0)))))

    # Map text_contrast to stroke width + shadow opacity (mirrors the main
    # video's _CONTRAST_SETTINGS; medium == the previous hardcoded look).
    ct = _CONTRAST_SETTINGS.get(text_contrast or "medium", _CONTRAST_SETTINGS["medium"])
    stroke_width = max(1, int(round(3 * (ct["stroke_mult"] / 2.5))))
    shadow_op = ct["shadow_opacity"]
    fill = lyric_color or "white"

    # Guard against mid-word breaks (matrix test 2026-06-02: "CAMINANDO" at
    # font_scale=1.2 UPPER overflowed the caption box and ImageMagick split it
    # as "CAMINAND" / "O"). method="caption" only wraps at spaces, so if the
    # single widest word is wider than the box it breaks the word itself.
    # Probe the widest word's rendered width and shrink the font until it fits
    # whole. Best-effort: if the probe fails (e.g. stubbed in tests), keep the
    # computed size — the worst case is the pre-fix behaviour.
    longest = max(display_text.split(), key=len, default="")
    if longest:
        try:
            _probe = TextClip(longest, fontsize=fontsize, font=font,
                              method="label", stroke_width=stroke_width)
            _word_w = _probe.size[0]
            try:
                _probe.close()
            except Exception:
                pass
            if _word_w and _word_w > text_width:
                fontsize = max(20, int(fontsize * (text_width - 8) / _word_w))
        except Exception as _e:
            logger.warning("[SHORT] word-fit probe failed (%s); keeping size", _e)

    shadow = TextClip(
        display_text,
        fontsize=fontsize,
        font=font,
        color="black",
        method="caption",
        size=(text_width, None),
        align="center",
    ).set_opacity(shadow_op)

    sh = shadow.size[1]
    shadow_y = (1920 - sh) // 2 + 4
    shadow_x = (1080 - text_width) // 2 + 4
    shadow = shadow.set_position((shadow_x, shadow_y)).set_start(seg_start).set_end(seg_end)

    txt = TextClip(
        display_text,
        fontsize=fontsize,
        font=font,
        color=fill,
        stroke_color="black",
        stroke_width=stroke_width,
        method="caption",
        size=(text_width, None),
        align="center",
    ).set_position(("center", "center")).set_start(seg_start).set_end(seg_end)

    return [shadow, txt]


def _apply_short_effect(short_path: str, fx_path: str, fps: float, job_dir: str) -> str:
    """Screen-blend a looped fx overlay onto a finished short via ffmpeg.

    The short is moviepy-rendered and moviepy can't screen-blend, so the
    effect is applied as a C-level ffmpeg post-pass using the SAME pre-baked
    fx assets the main video composites (fx_compositor). Falls back to the
    un-effected short if ffmpeg fails."""
    tmp = os.path.join(job_dir, "short_fx.mp4")
    # Same per-effect pre-blend gain as the main libass path (fx_compositor),
    # so a dim effect (stars/bokeh/snow) reads the same in the short as in the
    # video. Derive the effect name from the asset filename. eq goes before
    # format=gbrp (runs on the clip's native YUV).
    import fx_compositor as _fx
    _eff = os.path.splitext(os.path.basename(fx_path))[0]
    _gain = _fx.fx_gain(_eff)
    _gain_step = f"{_gain}," if _gain else ""
    cmd = [
        "ffmpeg", "-y", "-loglevel", "error",
        "-i", os.path.abspath(short_path),
        "-stream_loop", "-1", "-i", os.path.abspath(fx_path),
        "-filter_complex",
        "[0:v]format=gbrp[b];"
        f"[1:v]scale=1080:1920,setpts=PTS-STARTPTS,{_gain_step}format=gbrp[f];"
        "[b][f]blend=all_mode=screen:shortest=1,format=yuv420p[o]",
        "-map", "[o]", "-map", "0:a?",
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
        "-c:a", "copy", "-movflags", "+faststart", "-shortest",
        "-r", str(fps), tmp,
    ]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
        if r.returncode != 0:
            logger.warning("[SHORT] effect overlay failed (%s); keeping plain short",
                           (r.stderr or "")[-200:])
            return short_path
        os.replace(tmp, short_path)
        logger.info("[SHORT] effect overlay applied")
    except Exception as e:
        logger.warning("[SHORT] effect overlay errored (%s); keeping plain short", e)
    return short_path


def _build_short_ass_doc(
    window_segments: list[dict],
    *,
    font_path: str,
    text_case: str,
    font_scale: float,
    lyric_color: str,
    lyric_sung_color: str,
    text_contrast: str,
    lyrics_animation: str,
    line_transition: str,
) -> str:
    """Documento ASS del short (1080x1920) con EXACTAMENTE las mismas
    derivaciones de estilo que _render_lyrics_ass usa para el video
    (familia/bold por font_family, fontsize por lyric_fontsize a
    scale=1.0, outline por contraste, shadow=3) — función pura para que
    el test de paridad compare Style line contra el doc del video."""
    import ass_render as _ass

    scale = 1.0
    contrast = _CONTRAST_SETTINGS.get(text_contrast, _CONTRAST_SETTINGS["medium"])
    outline = max(1.0, contrast["stroke_mult"] * scale)
    shadow = max(1, int(round(3 * scale)))
    family, bold = _ass.font_family(font_path)
    font_factor = _ass.font_size_factor(family)

    # Segmentos re-basados, SIN overrides de layout del editor (pos/scale/
    # rot son coordenadas del frame 16:9 — el short siempre centra).
    clean_segments = [
        {"start": s["start"], "end": s["end"], "text": s["text"]}
        for s in window_segments
    ]
    lines = _ass.segments_to_lines(
        clean_segments,
        text_scale=scale,
        font_scale=font_scale,
        font_factor=font_factor,
        lyric_transition="cut",
        animation=lyrics_animation,
        transition=line_transition,
        case_fn=lambda t: _apply_case(t, text_case),
    )
    if lyrics_animation == "karaoke":
        primary_for_lines = lyric_sung_color or ""
        secondary_for_lines = lyric_color or ""
    else:
        primary_for_lines = lyric_color or ""
        secondary_for_lines = ""
    base_fs = _ass.lyric_fontsize(40, scale, font_scale, font_factor=font_factor)
    return _ass.build_ass(
        width=1080, height=1920,
        font_name=family, base_fontsize=base_fs,
        outline=outline, shadow=shadow, lines=lines, bold=bold,
        margin_v=0, alignment=5,
        primary_color=primary_for_lines,
        secondary_color=secondary_for_lines,
    )


def _burn_short_text_ass(
    bg_short_path: str,
    window_segments: list[dict],
    job_dir: str,
    short_dur: float,
    fps: float,
    *,
    font_path: str,
    text_case: str,
    font_scale: float,
    lyric_color: str,
    lyric_sung_color: str,
    text_contrast: str,
    lyrics_animation: str,
    line_transition: str,
) -> str | None:
    """Quema la letra del short con LIBASS — el MISMO motor del video.

    Incidente UMG Chile 2026-06-11/12 (tercera vuelta): aunque video y
    short usaran la MISMA TTF, se veían distintos — el video renderiza
    con libass (faux-bold + outline exterior) y el short con
    ImageMagick/moviepy (stroke centrado que se come el relleno → letras
    más flacas, look "hueco"). Dos motores nunca van a ser idénticos:
    la única paridad real es UN solo motor. Bonus: el short hereda las
    animaciones reales (karaoke per-word, transiciones) que el camino
    moviepy no replicaba.

    Misma derivación que _render_lyrics_ass: text_scale=1.0 (el frame es
    1080 de ANCHO, igual que el alto del video 1080p → mismo tamaño de
    glifo), outline/shadow por contraste, font_size_factor por familia.
    Las líneas largas las envuelve libass (WrapStyle 0). Los overrides de
    posición/escala del editor (pos/scale/rot, coordenadas del frame
    16:9) NO se trasladan al frame vertical — el short siempre centra
    (\an5), igual que el comportamiento histórico.

    Devuelve el path del short con texto, o None si la pasada falla (el
    caller cae al camino moviepy histórico — un short con texto "menos
    idéntico" es mejor que un short sin letra)."""
    import ass_render as _ass

    try:
        font_dir = _ass.single_font_dir(font_path)
        ass_doc = _build_short_ass_doc(
            window_segments,
            font_path=font_path,
            text_case=text_case, font_scale=font_scale,
            lyric_color=lyric_color, lyric_sung_color=lyric_sung_color,
            text_contrast=text_contrast,
            lyrics_animation=lyrics_animation, line_transition=line_transition,
        )
        ass_path = os.path.join(job_dir, "short_lyrics.ass")
        with open(ass_path, "w", encoding="utf-8") as f:
            f.write(ass_doc)

        out_tmp = os.path.join(job_dir, "short_ass_tmp.mp4")
        # Mismo escaping canónico que el burn del video (fx_compositor).
        cmd = [
            "ffmpeg", "-y", "-loglevel", "error",
            "-i", os.path.basename(bg_short_path),
            "-vf", f"subtitles=short_lyrics.ass:fontsdir={_ffmpeg_filter_escape(font_dir)}",
            "-c:v", "libx264", "-preset", "veryfast", "-crf", "20",
            "-pix_fmt", "yuv420p",
            "-c:a", "copy", "-movflags", "+faststart",
            "-r", str(fps), os.path.basename(out_tmp),
        ]
        r = subprocess.run(cmd, capture_output=True, text=True,
                           timeout=600, cwd=job_dir)
        if r.returncode != 0 or not os.path.exists(out_tmp):
            logger.warning("[SHORT] libass text burn failed (%s) — fallback moviepy",
                           (r.stderr or "")[-300:])
            return None
        logger.info("[SHORT] texto quemado con libass (font=%s anim=%s)",
                    os.path.basename(font_path), lyrics_animation)
        return out_tmp
    except Exception as e:
        logger.warning("[SHORT] libass text pass errored (%s) — fallback moviepy", e)
        return None


def generate_short(
    mp3_path: str,
    segments: list[dict],
    job_dir: str,
    bg_source: str | None = None,
    style: str = "oscuro",
    font: str = "Arial",
    fps: float = 24,
    *,
    text_case: str = "upper",
    font_scale: float = 1.0,
    lyric_color: str = "",
    lyric_sung_color: str = "",
    text_contrast: str = "medium",
    effect: str = "",
    custom_colors: str = "",
    lyrics_animation: str = "none",
    line_transition: str = "none",
) -> str:
    """Generate a 1080x1920 vertical short from the chorus section.

    Parity with the main video (2026-06-02 fix): the short now honors the
    operator's typography (text_case / font_scale / lyric_color /
    text_contrast), color grade (custom_colors), foto-fija static background,
    line transitions, and the composable effect overlay. It was previously a
    second, lower-fidelity renderer that hardcoded all of these, so a short
    looked typographically + visually different from the main video (client
    report). NOTE: per-word karaoke ANIMATION is not replicated here (the
    short shows each line in the base color); the libass main path owns that.

    `fps` is propagated to the final write so the lazy ProRes short can
    do a pure recode when UMG asks for a non-24 frame rate. Stays at 24
    by default for the YouTube-only path.
    """
    # Cinturón (incidente UMG Chile 2026-06-11): si por CUALQUIER camino
    # llega font vacío/None, elegir del pool con seed determinístico por
    # job_dir en vez de dejar que moviepy caiga a la stencil default de
    # ImageMagick — el short SIEMPRE sale con una fuente del catálogo.
    if not font and _FONT_POOL:
        _seed = int(hashlib.sha1(job_dir.encode()).hexdigest()[:8], 16)
        font = _FONT_POOL[_seed % len(_FONT_POOL)]
        logger.warning("[SHORT] font vacío — fallback determinístico del pool: %s",
                       os.path.basename(font))

    import fx_compositor as _fx

    audio = AudioFileClip(mp3_path)
    start_time = _find_chorus_start(segments)
    end_time = min(start_time + 30, audio.duration)
    short_dur = end_time - start_time
    short_audio = audio.subclip(start_time, end_time)

    # Build vertical background from RAW source (no burned-in lyrics).
    # Foto fija parity (2026-06-02): a still image is held STATIC (no Ken
    # Burns pan), matching the main video's foto-fija background.
    if bg_source and bg_source.lower().endswith((".jpg", ".jpeg", ".png")):
        bg_full = _ken_burns_clip(bg_source, short_dur, static=True)
        bg = _cover_resize(bg_full, 1080, 1920)
    elif bg_source and os.path.exists(bg_source):
        try:
            raw = VideoFileClip(bg_source)
            raw.get_frame(0)
            raw = _cover_resize(raw, 1080, 1920)
            if raw.duration >= short_dur:
                bg = raw.subclip(0, short_dur)
            else:
                loops = math.ceil(short_dur / raw.duration) + 1
                clips = []
                for i in range(loops):
                    c = _cover_resize(VideoFileClip(bg_source), 1080, 1920)
                    clips.append(c)
                bg = concatenate_videoclips(clips).subclip(0, short_dur)
        except Exception:
            bg = _cover_resize(_make_gradient_clip(short_dur, style), 1080, 1920)
    else:
        bg = _cover_resize(_make_gradient_clip(short_dur, style), 1080, 1920)

    # Color-grade the BACKGROUND (before lyrics, like the main video — so the
    # lyrics stay ungraded). No-op when style/custom_colors yield no grade.
    _grade_style = style if _fx.grade_filter(style, custom_colors) else ""
    if _grade_style:
        # grade_frame returns an UNCLIPPED float frame; moviepy fl_image needs
        # uint8 [0,255] — clip+cast like the main moviepy fallback does.
        bg = bg.fl_image(
            lambda f: _fx.grade_frame(f.astype("float32"), _grade_style, custom_colors)
            .clip(0, 255).astype("uint8")
        )

    # Ventana de segmentos del short, re-basada a t=0 (la consumen tanto la
    # pasada libass como el fallback moviepy).
    window_segments = []
    for seg in segments:
        if seg["end"] <= start_time or seg["start"] >= end_time:
            continue
        s = max(0, seg["start"] - start_time)
        e = min(short_dur, seg["end"] - start_time)
        if e - s < 0.1:
            continue
        window_segments.append({**seg, "start": s, "end": e})

    # PASADA 1 — fondo + audio, SIN texto (moviepy, como siempre).
    # PASADA 2 — el texto lo quema LIBASS, el mismo motor del video.
    # Incidente UMG Chile (3ª vuelta, 2026-06-12): misma TTF en dos motores
    # ≠ misma letra en pantalla — ImageMagick dibuja el stroke comiéndose
    # el relleno y sin el faux-bold de libass. Un solo motor = paridad real
    # (y el short hereda karaoke/transiciones de verdad).
    final = CompositeVideoClip([bg], size=(1080, 1920))
    final = final.set_audio(short_audio).set_duration(short_dur)

    # 2026-05-26 OOM mitigation: workers SIGKILL'd at progress=75% on
    # dense-chorus songs (Sin Gamulán). Force gc before x264 encode +
    # drop threads 8→2 below to cap peak RSS.
    gc.collect()

    out_path = os.path.join(job_dir, "short.mp4")
    bg_only_path = os.path.join(job_dir, "short_bg_only.mp4")
    final.write_videofile(
        bg_only_path,
        fps=fps,
        codec="libx264",
        audio_codec="aac",
        threads=2,
        preset="veryfast",
        ffmpeg_params=["-movflags", "+faststart"],
        logger=None,
    )
    audio.close()
    final.close()

    burned = _burn_short_text_ass(
        bg_only_path, window_segments, job_dir, short_dur, fps,
        font_path=font,
        text_case=text_case, font_scale=font_scale,
        lyric_color=lyric_color, lyric_sung_color=lyric_sung_color,
        text_contrast=text_contrast,
        lyrics_animation=lyrics_animation, line_transition=line_transition,
    )
    if burned:
        os.replace(burned, out_path)
    else:
        # Fallback histórico (moviepy/ImageMagick): texto menos idéntico al
        # video, pero un short SIN letra sería peor. Además de loggearse,
        # se ALERTA en Sentry: este es el único camino que puede volver a
        # producir la divergencia tipográfica del incidente UMG Chile —
        # si dispara, hay que enterarse antes que el cliente.
        try:
            import sentry_sdk
            with sentry_sdk.push_scope() as _scope:
                _job_tag = os.path.basename(job_dir.rstrip("/"))
                _scope.fingerprint = ["short-libass-fallback"]
                _scope.set_tag("job_id", _job_tag)
                sentry_sdk.capture_message(
                    f"[SHORT-FALLBACK] {_job_tag}: la pasada libass falló — el short "
                    "salió con el motor moviepy (tipografía puede diferir del video)",
                    level="error",
                )
        except Exception:
            pass  # sin Sentry (dev/tests) el log de arriba alcanza
        _do_fade = (line_transition or "none") not in ("none", "cut", "")
        text_layers = []
        for seg in window_segments:
            layers = _make_short_text_clip(
                seg["text"], seg["start"], seg["end"], font,
                text_case=text_case, font_scale=font_scale,
                lyric_color=lyric_color, text_contrast=text_contrast,
            )
            if _do_fade:
                fd = min(0.25, (seg["end"] - seg["start"]) / 3.0)
                layers = [c.crossfadein(fd).crossfadeout(fd) for c in layers]
            text_layers.extend(layers)
        bg_clip = VideoFileClip(bg_only_path)
        composite = CompositeVideoClip([bg_clip] + text_layers, size=(1080, 1920))
        composite = composite.set_duration(short_dur)
        gc.collect()
        composite.write_videofile(
            out_path,
            fps=fps,
            codec="libx264",
            audio_codec="aac",
            threads=2,
            preset="veryfast",
            ffmpeg_params=["-movflags", "+faststart"],
            logger=None,
        )
        composite.close()
        bg_clip.close()
    try:
        os.unlink(bg_only_path)
    except OSError:
        pass

    # Effect overlay (snow/rain/stars/bokeh/light/aurora): screen-blend the
    # pre-baked fx loop over the short with ffmpeg — the SAME fx assets the
    # main video composites. moviepy can't screen-blend, so it's a post-pass.
    fx = _fx.effect_path(effect)
    if fx:
        out_path = _apply_short_effect(out_path, fx, fps, job_dir)

    return out_path


# ---------------------------------------------------------------------------
# Step 4 — Thumbnail
# ---------------------------------------------------------------------------

def _draw_text_with_outline(draw, xy, text, font, fill="white", outline="black", width=3):
    """Draw text with a thick outline for readability."""
    x, y = xy
    for ox in range(-width, width + 1):
        for oy in range(-width, width + 1):
            if ox != 0 or oy != 0:
                draw.text((x + ox, y + oy), text, font=font, fill=outline)
    draw.text((x, y), text, font=font, fill=fill)


def generate_thumbnail(
    artist: str,
    mp3_path: str,
    job_dir: str,
    bg_source: str | None = None,
    song_title: str = "",
) -> str:
    """Generate a thumbnail from the RAW background with artist and song name."""
    from PIL import ImageFilter, ImageEnhance

    # Grab a frame from the raw background (no burned-in lyrics)
    if bg_source and bg_source.lower().endswith((".jpg", ".jpeg", ".png")):
        img = Image.open(bg_source)
    elif bg_source and os.path.exists(bg_source):
        try:
            clip = VideoFileClip(bg_source)
            t = min(clip.duration * 0.4, clip.duration - 0.1)
            frame = clip.get_frame(t)
            clip.close()
            img = Image.fromarray(frame)
        except Exception:
            img = Image.new("RGB", (1280, 720), (30, 15, 60))
    else:
        img = Image.new("RGB", (1280, 720), (30, 15, 60))

    img = img.resize((1280, 720), Image.LANCZOS)

    # Slight darken so text is readable, but background is clearly visible
    enhancer = ImageEnhance.Brightness(img)
    img = enhancer.enhance(0.6)

    draw = ImageDraw.Draw(img)
    # Prefer the structured title from the job; fall back to the filename
    # only when it's missing. The filename heuristic handles both
    # "Artist - Title" and Suno-style "Title_Artist" so the thumbnail
    # never shows a literal underscore-joined basename.
    if song_title:
        song_name = song_title
    else:
        raw_name = os.path.splitext(os.path.basename(mp3_path))[0]
        song_name = raw_name
        if " - " in raw_name:
            song_name = raw_name.split(" - ", 1)[1]
        elif "_" in raw_name:
            song_name = raw_name.split("_", 1)[0]
    for suffix in ["(Official Video)", "(Official Audio)", "(Lyric Video)",
                   "(Official Music Video)", "(Audio)", "(Video)", "(En Vivo)",
                   "(Live)", "(Lyrics)"]:
        song_name = song_name.replace(suffix, "").strip()
    # Defensive scrub for legacy / manually-typed values that still carry
    # the artist concatenated to the title.
    if song_name:
        if " - " in song_name and artist and song_name.startswith(artist):
            song_name = song_name.split(" - ", 1)[1].strip()
        if artist and song_name.endswith(f"_{artist}"):
            song_name = song_name[: -(len(artist) + 1)].strip()

    # Use Montserrat ExtraBold for thumbnails (Google Font, OFL licensed)
    thumb_font = os.path.join(_FONTS_DIR, "Montserrat-ExtraBold.ttf")
    if not os.path.exists(thumb_font) and _FONT_POOL:
        thumb_font = _FONT_POOL[0]

    # Auto-shrink font until the text fits within max_width with a 60px
    # margin on each side. Without this, long artist names ("El Plan de la
    # Mariposa") or songs with explanatory subtitles overflow 1280px and
    # get cropped by the thumbnail frame.
    def _fit_font(text: str, start_size: int, max_width: int, min_size: int = 28):
        size = start_size
        while size > min_size:
            try:
                f = ImageFont.truetype(thumb_font, size)
            except (OSError, IOError):
                return ImageFont.load_default()
            bbox = draw.textbbox((0, 0), text, font=f)
            tw = bbox[2] - bbox[0]
            if tw <= max_width:
                return f
            size -= 4
        try:
            return ImageFont.truetype(thumb_font, min_size)
        except (OSError, IOError):
            return ImageFont.load_default()

    max_w = 1280 - 120  # 60px margin each side
    font_artist = _fit_font(artist.upper(), 100, max_w)
    font_song = _fit_font(song_name, 55, max_w)

    # Artist name centered
    bbox = draw.textbbox((0, 0), artist.upper(), font=font_artist)
    tw = bbox[2] - bbox[0]
    x = (1280 - tw) // 2
    _draw_text_with_outline(draw, (x, 240), artist.upper(), font_artist, fill="white", width=5)

    # Song name centered below
    bbox = draw.textbbox((0, 0), song_name, font=font_song)
    tw = bbox[2] - bbox[0]
    x = (1280 - tw) // 2
    _draw_text_with_outline(draw, (x, 380), song_name, font_song, fill=(230, 230, 240), width=3)

    out_path = os.path.join(job_dir, "thumbnail.jpg")
    img.save(out_path, "JPEG", quality=92)
    return out_path


# ---------------------------------------------------------------------------
# Edit pipeline — partial re-render at the review stage
# ---------------------------------------------------------------------------

_MAX_EDITS = 3


def run_edit_pipeline(
    job_id: str,
    edit_type: str,
    edit_params: dict,
) -> None:
    """Partial re-render triggered from POST /edit/{job_id}.

    edit_type:
        "typography" — keep existing background + segments; only re-render
            with new font/size/case/transition settings.  Cost: ~$0.
        "background" — re-generate Veo background; keep segments and
            (optionally) render params.  Cost: ~$0.90.
        "lyrics"     — keep cached background; replace segments with the
            caller-supplied list (edit_params["segments"]). Re-renders
            video/short/thumbnail.  Cost: ~$0. After success, the new
            segments overwrite segments_json so subsequent edits see
            the corrected version.
        "metadata"   — PR C 2026-05-26. Keep cached background AND
            segments. The /edit handler already wrote the corrected
            artist/song_title to the DB row before enqueueing, so
            `artist` and `song_title` read on line 9107-9108 below pick
            up the new values automatically. The re-render produces a
            new title card via libass with the corrected text. Same
            cost/timing as typography (~$0, ~5 min). Does NOT consume
            an edit slot — see main.py:request_edit for the rationale.

    After completion the job returns to "pending_review" so the reviewer
    can approve, reject, or request another edit (up to _MAX_EDITS total).
    """
    # Observability 2026-06-10: toda línea de log de este job lleva job_id.
    from observability import set_job_log_context
    set_job_log_context(job_id)
    import time as _time
    from database import SessionLocal, Job as JobModel

    started_at = _time.monotonic()
    db = SessionLocal()
    try:
        job_row = db.query(JobModel).filter(JobModel.job_id == job_id).first()
        if not job_row:
            raise RuntimeError(f"Job {job_id} not found")
        # Source of segments depends on edit_type. Lyrics edit uses the
        # caller-supplied list (they're the new "ground truth"); the
        # other two reuse what's already persisted.
        if edit_type == "lyrics":
            segments = edit_params.get("segments")
            if not segments or not isinstance(segments, list):
                raise RuntimeError(
                    f"Job {job_id}: edit_type='lyrics' requires non-empty segments in edit_params"
                )
        else:
            segments = job_row.segments_json
            if not segments:
                raise RuntimeError(f"Job {job_id} has no persisted segments — cannot edit")
        base_params = dict(job_row.render_params or {})
        artist = job_row.artist
        song_title = job_row.song_title or ""
        style = base_params.get("style") or job_row.style or "oscuro"
        delivery_profile = job_row.delivery_profile or "youtube"
        wants_youtube = delivery_profile in ("youtube", "both")
        wants_umg = delivery_profile in ("umg", "both")
        umg_spec = job_row.umg_spec
        tenant_id = job_row.tenant_id
        bg_r2_key_cached = job_row.bg_r2_key_cached
        input_r2_key = job_row.input_r2_key
        # Snapshot inputs: edit_count was already incremented by the /edit
        # handler before enqueuing, so it's the "version we're about to
        # produce". Archived .vN keys use this number so v1 is the file
        # that existed at the moment of the 1st edit, v2 at the 2nd, etc.
        prior_s3_keys = dict(job_row.s3_keys) if job_row.s3_keys else None
        version_n = int(job_row.edit_count or 0)
        prior_versions = list(job_row.previous_versions or [])
        # Multi-escena: para edit_type=="scene" reconstruimos el timeline desde
        # el storyboard persistido (regenerando sólo la escena pedida).
        scene_plan = dict(job_row.scene_plan) if job_row.scene_plan else None
    finally:
        db.close()

    # Merge base render params with the requested overrides.
    merged = {**base_params, **edit_params}
    font_id = merged.get("font") or ""
    text_case = merged.get("text_case") or "upper"
    font_scale = float(merged.get("font_scale") or 1.0)
    # text_contrast was missing here → a typography/bg edit re-render dropped
    # the operator's contrast choice on the MAIN video (2026-06-02 fix).
    text_contrast = merged.get("text_contrast") or "medium"
    lyric_transition = merged.get("lyric_transition") or "cut"
    text_motion = merged.get("text_motion") or "none"
    lyrics_animation = merged.get("lyrics_animation") or "none"
    line_transition = merged.get("line_transition") or "none"
    genre = merged.get("genre") or ""
    concept = merged.get("concept") or ""
    movement_style = merged.get("movement_style") or ""
    # Effect overlay + custom palette persist across edits via render_params,
    # so a re-render keeps the snow/rain/grade the operator picked at upload.
    effect = merged.get("effect") or ""
    custom_colors = merged.get("custom_colors") or ""
    # Lyric text colors 2026-05-25. Si el operador no los seteó, fall back
    # a "" (= blanco default en build_ass). Persisten en render_params
    # como custom_colors → un re-render de la misma variante mantiene los
    # colores elegidos.
    lyric_color = merged.get("lyric_color") or ""
    lyric_sung_color = merged.get("lyric_sung_color") or ""
    # Title-card customization (Full Rotor v1). Persist across edits via
    # render_params; defaults reproduce the historical look.
    title_template = merged.get("title_template") or "auto"
    title_size = float(merged.get("title_size") or 1.0)
    title_artist_font = merged.get("title_artist_font") or ""
    title_song_font = merged.get("title_song_font") or ""
    # UI v1.1 (2026-05-30): manual song-title break. "" = auto (legacy).
    title_song_break = merged.get("title_song_break") or ""
    # Per-edit operator hint for background regen (set by /edit when the
    # user typed in the "Aclarar tipo de fondo" textarea). None if absent;
    # propagates only into the `background` branch below.
    background_hint = edit_params.get("background_hint") or None
    # Operator-chosen generation mode for background regen: "veo" (Veo 3.1
    # cinematic video) or "imagen" (Imagen-4 still + Ken Burns animation).
    # Defaults to "veo" when unset for backward compatibility — pre-2026-05-16
    # edits never carried this field. Validated upstream by Pydantic enum.
    background_mode = edit_params.get("background_mode") or "veo"
    # "Usar mi prompt tal cual": send background_hint straight to Veo without
    # Gemini's rewrite. Read from merged so a verbatim choice persisted at
    # generate time survives, but it only takes effect in the background
    # branch when a hint is actually present (see _get_unique_prompt).
    bg_verbatim = bool(merged.get("bg_verbatim"))
    # Multi-escena (edit_type=="scene"): qué escena regenerar y cómo. El
    # timeline ya es full-length → bg_prelooped=True para que el render NO lo
    # vuelva a loopear/palindromear.
    scene_key = edit_params.get("scene_key") or ""
    scene_prompt_override = edit_params.get("scene_prompt") or ""
    scene_hint = edit_params.get("scene_hint") or ""
    scene_movement = edit_params.get("scene_movement") or ""
    bg_prelooped = False

    job_dir = os.path.join(OUTPUTS_DIR, job_id)
    os.makedirs(job_dir, exist_ok=True)

    try:
        # ----------------------------------------------------------------
        # Fetch the source audio
        # ----------------------------------------------------------------
        mp3_path = os.path.join(job_dir, "source_audio.mp3")
        if not os.path.exists(mp3_path):
            audio_ready = False
            # 1) Original input MP3 (best quality).
            if input_r2_key and storage.is_enabled() and storage.object_exists(input_r2_key):
                if storage.download_object(input_r2_key, mp3_path):
                    audio_ready = True
            # 2) Fallback: extract the audio track from a rendered deliverable
            # (video/short) when the input is missing — recovers re-renders for
            # jobs whose input was purged before the orphan-only retention fix
            # (the recurring "audio no disponible" incident, agus.cafisi). The
            # deliverable's audio is the same recording, just re-encoded.
            if not audio_ready and storage.is_enabled():
                for _src in ("video", "short"):
                    _vk = (prior_s3_keys or {}).get(_src)
                    if not _vk or not storage.object_exists(_vk):
                        continue
                    _tmp_v = os.path.join(job_dir, "fallback_src.mp4")
                    if not storage.download_object(_vk, _tmp_v):
                        continue
                    try:
                        run_checked(
                            ["ffmpeg", "-y", "-loglevel", "error", "-i", _tmp_v,
                             "-vn", "-c:a", "libmp3lame", "-q:a", "4", mp3_path],
                            label="ffmpeg-extract-audio-from-deliverable",
                            timeout=300, output_path=mp3_path,
                        )
                        logger.info("[EDIT] source audio recovered from %s deliverable "
                                    "(input missing/purged) for job %s", _src, job_id)
                        audio_ready = True
                        break
                    except Exception as _e:
                        logger.warning("[EDIT] audio extract from %s failed: %s", _src, _e)
                    finally:
                        try:
                            os.remove(_tmp_v)
                        except OSError:
                            pass
            if not audio_ready:
                raise RuntimeError(
                    "Source audio not available: input missing and no recoverable "
                    "deliverable (video/short) for this job"
                )

        # ----------------------------------------------------------------
        # Resolve background
        # ----------------------------------------------------------------
        if edit_type in ("typography", "lyrics", "metadata"):
            # All three reuse the cached background — only the foreground
            # layer changes. Lyrics edit ALSO swaps the segments, and
            # metadata edit re-renders the title card with the corrected
            # artist/song_title (already persisted by the /edit handler
            # before this worker spawned — see main.py:request_edit).
            update_job(job_id, status="editing", current_step="video", progress=35)
            _is_scene_job = bool(scene_plan and scene_plan.get("scenes"))
            # Audit M1: en un edit de LYRICS sobre un job multi-escena, los
            # timings cambiaron → re-detectamos secciones y re-stitcheamos desde
            # los clips cacheados (sin Veo) para que los cortes sigan cayendo en
            # los cambios de la canción. Si la estructura cambió o falla, caemos
            # al timeline cacheado estático.
            if edit_type == "lyrics" and _is_scene_job:
                _sc_audio = scene_plan.get("audio_duration")
                if not _sc_audio:
                    try:
                        _sc_audio = _audio_duration(mp3_path)
                    except Exception:
                        _sc_audio = _ffprobe_duration(mp3_path)
                try:
                    _re_tl, scene_plan = _restitch_scenes_for_edit(
                        scene_plan, segments, _sc_audio, job_dir, artist=artist,
                        song_title=song_title, concept=concept,
                        allow_people=_compute_allow_people(job_id), job_id=job_id)
                    if _re_tl:
                        bg_image_path = _re_tl
                        bg_prelooped = True
                        update_job(job_id, scene_plan=scene_plan)
                        logger.info("[EDIT] escenas re-stitcheadas con la letra editada (job=%s)", job_id)
                except Exception as _e:  # noqa: BLE001
                    logger.warning("[EDIT] re-stitch de escenas falló (%s) — uso timeline cacheado", _e)
            # La extensión viene de la key cacheada: para fondos humanos
            # puede ser .jpg/.png (imagen fija del usuario) y de ella
            # depende el manejo de stills aguas abajo. Antes estaba
            # hardcodeada .mp4 — un jpg cacheado se bajaba con nombre de
            # video (audit 2026-06-11).
            if not bg_prelooped:
                _cached_ext = os.path.splitext(bg_r2_key_cached or "")[1].lower() or ".mp4"
                bg_image_path = os.path.join(job_dir, f"bg_cached_edit{_cached_ext}")
                if not os.path.exists(bg_image_path):
                    if bg_r2_key_cached and storage.is_enabled():
                        ok = storage.download_object(bg_r2_key_cached, bg_image_path)
                        if not ok:
                            raise RuntimeError("Could not download cached background from R2")
                    else:
                        raise RuntimeError(
                            f"No cached background available for {edit_type} edit. "
                            "Use edit_type='background' to regenerate it."
                        )
                # Audit M1/M2: el bg cacheado de un job multi-escena ES el timeline
                # full-length → bg_prelooped=True para que el render NO lo
                # palindromee (si no, las escenas se reproducen en reversa al final).
                if _is_scene_job:
                    bg_prelooped = True

        elif edit_type == "background":
            update_job(job_id, status="editing", current_step="background", progress=22)
            lyrics_text = " ".join(seg["text"] for seg in segments)
            bg_image_path = _ensure_background(
                style, job_dir,
                lyrics_text=lyrics_text, artist=artist, job_id=job_id,
                song_title=song_title, genre=genre, concept=concept,
                movement_style=movement_style,
                background_hint=background_hint,
                bg_mode=background_mode,
                bg_verbatim=bg_verbatim,
                effect=effect,
                allow_people=_compute_allow_people(job_id),
            )
            update_job(job_id, progress=35)
            # Re-cache the new background so future typography edits work.
            if bg_image_path and os.path.exists(bg_image_path) and storage.is_enabled():
                try:
                    _bg_ext = os.path.splitext(bg_image_path)[1] or ".mp4"
                    new_bg_key = storage.upload_file(
                        bg_image_path,
                        f"backgrounds/{job_id}/bg_cached{_bg_ext}",
                    )
                    if new_bg_key:
                        update_job(job_id, bg_r2_key_cached=new_bg_key)
                except Exception as _e:
                    logger.warning("[EDIT] Warning: re-cache of new background failed: %s", _e)

        elif edit_type == "scene":
            # Regenerar UNA escena del storyboard y re-armar el timeline. Sólo
            # esa escena toca Veo (cache_token nuevo); las demás re-bajan de la
            # caché R2. El timeline resultante es full-length → bg_prelooped.
            if not scene_plan or not scene_plan.get("scenes"):
                raise RuntimeError("Este job no es multi-escena (sin scene_plan).")
            if not scene_key:
                raise RuntimeError("edit_type='scene' requiere scene_key.")
            update_job(job_id, status="editing", current_step="scenes", progress=22)
            lyrics_text = " ".join(seg.get("text", "") for seg in segments)
            _scene_audio_dur = scene_plan.get("audio_duration")
            if not _scene_audio_dur:
                try:
                    _scene_audio_dur = _audio_duration(mp3_path)
                except Exception:
                    _scene_audio_dur = _ffprobe_duration(mp3_path)
            bg_image_path, scene_plan = _regenerate_scene_background(
                scene_plan, scene_key, job_dir,
                artist=artist, song_title=song_title,
                audio_duration=_scene_audio_dur,
                concept=concept, allow_people=_compute_allow_people(job_id),
                job_id=job_id,
                prompt_override=scene_prompt_override, hint=scene_hint,
                movement_style=scene_movement,
                lyrics_text=lyrics_text, genre=genre,
                style_hint=style, custom_colors=custom_colors,
            )
            bg_prelooped = True
            # Persistir el plan actualizado (clip nuevo, cache_token, thumb).
            update_job(job_id, scene_plan=scene_plan, progress=35)

        else:
            raise ValueError(f"Unknown edit_type {edit_type!r}")

        # ----------------------------------------------------------------
        # Resolve font — misma elección para video Y short, persistida
        # (fix incidente UMG Chile 2026-06-11, ver _pick_concrete_font).
        # ----------------------------------------------------------------
        chosen_font = _pick_concrete_font(font_id, job_id, job_dir, deterministic=wants_umg)
        if chosen_font:
            logger.info("[EDIT] Font: %s", os.path.basename(chosen_font))

        # ----------------------------------------------------------------
        # Re-render video
        # ----------------------------------------------------------------
        update_job(job_id, current_step="video", progress=40)
        # Word-level animation timing (forced-align, once, cached) — same
        # gated/isolated path as run_pipeline. A re-render of an existing
        # karaoke / word_reveal job (incl. a typography edit) thus repairs its
        # sync and caches the result.
        if lyrics_animation in ("karaoke", "word_reveal"):
            import karaoke_align
            _enriched = karaoke_align.enrich_segments_with_word_timings(segments, mp3_path)
            if _enriched is not segments:
                segments = _enriched
                update_job(job_id, segments_json=segments)
        intermediate_spec = (
            RenderSpec.umg_intermediate_master(umg_spec) if wants_umg
            else None
        )
        _, chosen_font, bg_source = generate_lyric_video(
            mp3_path, segments, style, job_dir, artist, bg_image_path,
            font=chosen_font, spec=intermediate_spec,
            song_title=song_title,
            text_case=text_case,
            font_scale=font_scale,
            lyric_transition=lyric_transition,
            text_motion=text_motion,
            lyrics_animation=lyrics_animation,
            line_transition=line_transition,
            text_contrast=text_contrast,
            effect=effect, custom_colors=custom_colors,
            lyric_color=lyric_color, lyric_sung_color=lyric_sung_color,
            title_template=title_template, title_size=title_size,
            title_artist_font=title_artist_font, title_song_font=title_song_font,
            title_song_break=title_song_break,
            # Multi-escena: el timeline ya cubre toda la canción → no re-loopear.
            bg_prelooped=bg_prelooped,
        )
        files = {"video_url": f"/download/{job_id}/video"}
        update_job(job_id, progress=55)

        if wants_umg:
            files["umg_master_url"] = f"/download/{job_id}/umg_master"
            files["umg_short_url"] = f"/download/{job_id}/umg_short"

        # ----------------------------------------------------------------
        # Re-render short + thumbnail
        # ----------------------------------------------------------------
        if wants_youtube or wants_umg:
            update_job(job_id, current_step="short", progress=75)
            short_fps = float(umg_spec["fps"]) if wants_umg and umg_spec else 24
            generate_short(
                mp3_path, segments, job_dir, bg_source=bg_source,
                style=style, font=chosen_font, fps=short_fps,
                text_case=text_case, font_scale=font_scale,
                lyric_color=lyric_color, lyric_sung_color=lyric_sung_color,
                text_contrast=text_contrast, effect=effect, custom_colors=custom_colors,
                lyrics_animation=lyrics_animation, line_transition=line_transition,
            )
            files["short_url"] = f"/download/{job_id}/short"
            update_job(job_id, progress=85)

            update_job(job_id, current_step="thumbnail", progress=90)
            generate_thumbnail(artist, mp3_path, job_dir, bg_source=bg_source, song_title=song_title)
            files["thumbnail_url"] = f"/download/{job_id}/thumbnail"

        # ----------------------------------------------------------------
        # Verify + upload to R2 (replacing previous deliverables)
        # ----------------------------------------------------------------
        try:
            audio_dur = _audio_duration(mp3_path)
        except Exception:
            audio_dur = _ffprobe_duration(mp3_path)
        _verify_deliverables(job_dir, files, audio_dur)

        # Archive previous deliverables to {key}.vN before the upload
        # overwrites them. Non-fatal — if storage.copy_object errors out
        # for some keys we still want the new render to land. The
        # archived metadata (or None) goes into job.previous_versions so
        # an operator can find the rollback target without scraping R2.
        snapshot_entry = _snapshot_previous_deliverables(prior_s3_keys, version_n)
        if snapshot_entry:
            snapshot_entry["edit_type"] = edit_type
            update_job(
                job_id,
                previous_versions=prior_versions + [snapshot_entry],
            )

        # See run_pipeline's comment above the same call: per-key atomic
        # persistence happens inside _upload_deliverables_to_r2; we no
        # longer write s3_keys wholesale (that REPLACED concurrent prewarm
        # keys). Critical-deliverable failures raise → outer except marks
        # the edit `error` instead of advertising broken downloads. We still
        # capture the return value (dict of successfully-uploaded keys)
        # because the audit log below reports `files_updated` from it.
        #
        # ProRes invalidation (2026-06-09 fix): the edit regenerated
        # lyric_video.mp4 but NOT umg_master.mov / umg_short.mov — those are
        # lazy-transcoded from the MP4 at /download time. Passing their URLs
        # into the upload set would re-upload the STALE pre-edit .mov still
        # sitting in job_dir (left by a prior lazy download or prewarm),
        # cementing the old cut on R2. Exclude them from the upload; we
        # invalidate the stale .mov + s3_keys below and re-warm fresh.
        upload_files = {
            k: v for k, v in files.items()
            if k not in ("umg_master_url", "umg_short_url")
        }
        s3_keys = _upload_deliverables_to_r2(job_id, job_dir, upload_files)

        # Invalidate the lazy ProRes cache so the next /download/umg_master
        # re-transcodes from the freshly re-rendered lyric_video.mp4 instead
        # of serving the pre-edit cut. _snapshot_previous_deliverables above
        # already archived the prior .mov keys as {key}.vN, so the rollback
        # path is preserved. Then re-enqueue prewarm (mirrors run_pipeline)
        # so UMG gets an instant 302 instead of a cold 60-120 s transcode.
        if wants_umg:
            for _ft in ("umg_master", "umg_short"):
                _stale = os.path.join(job_dir, _DELIVERABLE_FILENAMES[_ft])
                if os.path.exists(_stale):
                    try:
                        os.unlink(_stale)
                    except OSError as _e:
                        logger.warning("[EDIT] could not drop stale %s: %s", _stale, _e)
            try:
                from jobs import remove_s3_keys
                remove_s3_keys(job_id, ["umg_master", "umg_short"])
            except Exception as _e:
                logger.warning("[EDIT] prores s3_keys invalidation skipped: %s", _e)
            try:
                from queue_jobs import enqueue_prores_prewarm, cancel_rq_job
                # Cancel any prewarm still QUEUED from a PRIOR render so it
                # can't transcode the now-stale source and publish over our
                # invalidation. A prewarm already MID-ffmpeg in another
                # process won't stop here, but it's independently fenced by
                # the edit_count/editing_started_at freshness check in
                # ensure_prores_exists (it discards the stale .mov instead of
                # publishing). Then re-enqueue fresh.
                for _ft in ("umg_master", "umg_short"):
                    cancel_rq_job(f"prewarm:{job_id}:{_ft}")
                enqueue_prores_prewarm(job_id, "umg_master")
                enqueue_prores_prewarm(job_id, "umg_short")
            except Exception as _e:
                logger.warning("[EDIT] prores prewarm re-enqueue skipped: %s", _e)

        _cleanup_local_intermediates(job_dir)

        # Persist the merged render params so the next edit sees them.
        update_job(job_id, render_params=merged)

        # For lyrics edits, also persist the new segments so subsequent
        # actions (another edit, a retry) see the corrected version.
        # Without this, the corrections would only live in the rendered
        # video bytes; any later run_pipeline call would read the OLD
        # segments_json and re-render with the bad words.
        if edit_type == "lyrics":
            update_job(job_id, segments_json=segments)

        # Back to pending_review — the reviewer decides what to do next.
        update_job(job_id, status="pending_review", progress=100, files=files)
        logger.info("[EDIT] job=%s edit_type=%s -> pending_review", job_id, edit_type)

        # Audit log: completion. The corresponding job.edit_request entry
        # was written by main.py at request time; this closes the loop
        # with duration + archived version so UMG can trace every change.
        _write_edit_audit(
            action="job.edit_completed",
            detail={
                "job_id": job_id,
                "edit_type": edit_type,
                "edit_count": version_n,
                "duration_seconds": round(_time.monotonic() - started_at, 2),
                "archived_version": snapshot_entry["version"] if snapshot_entry else None,
                "segments_count": len(segments) if segments else 0,
                "files_updated": sorted(list(s3_keys.keys())) if s3_keys else [],
            },
        )

    except Exception as exc:
        logger.error("[EDIT] job=%s FAILED: %s", job_id, exc, exc_info=True)
        from error_taxonomy import classify_error
        update_job(
            job_id, status="error", error=f"Edit failed: {exc}",
            error_category=classify_error(str(exc)),
        )
        _write_edit_audit(
            action="job.edit_failed",
            detail={
                "job_id": job_id,
                "edit_type": edit_type,
                "edit_count": version_n,
                "duration_seconds": round(_time.monotonic() - started_at, 2),
                "error": str(exc),
            },
        )
        # Tier 4 (C5): free the disk on a failed EDIT too — same leak/cascade as
        # run_pipeline. Guarded on R2-recoverability (the edit re-derives the
        # source from input_r2_key on retry). `input_r2_key` is a local set
        # partway through, and `job_dir` may not exist yet on a very early
        # failure — locals().get + the helper's own isdir guard make this safe.
        if locals().get("input_r2_key"):
            _cleanup_job_dir_on_failure(locals().get("job_dir"))
        else:
            try:
                _cleanup_local_intermediates(locals().get("job_dir") or "")
            except Exception:
                pass
        raise
