"""Background pre-generation — Capa C del wizard refactor (2026-05-24).

WHY
---
Hoy el flow es: drop MP3 → transcribir (~15s) → editar lyrics (~30-90s
operador) → click "Crear video" → enqueue pipeline → **Veo/Imagen
generan el background AHORA** (~60-180s) → render final + upload.

El operador percibe ~60-90s de wait después de clickear "Crear" porque
el background NO empezó hasta el click. Mientras tanto, durante los
30-90s que estuvo editando lyrics, los workers estaban ociosos.

CAPA C: empezar la generación del background EN PARALELO mientras el
operador edita lyrics. Cuando llegue el `/generate` "real", el video
del background ya está cacheado y la pipeline lo reusa.

DESIGN
------
1. Después de que el operador elige movimiento/efecto/paleta y el set
   queda estable (debounce 2s frontend), el frontend hace POST
   /generate-preview con los params del background. Backend computa un
   hash determinístico (`bg_cache_key`).
2. Si `bg_cache/{key}.mp4` ya existe en R2 → 200 OK con `cached=true`.
3. Si no existe → enqueue a la queue `bg_preview` que corre solo
   `_ensure_background()` y sube el resultado a `bg_cache/{key}.mp4`.
   Retorna 202 con `job_id` para polling.
4. Cuando el operador clickea "Crear video", el POST `/generate` incluye
   `bg_cache_key`. La pipeline chequea R2 ANTES de llamar a Veo/Imagen;
   si el archivo está, lo usa tal cual.

CACHE KEY
---------
El hash incluye TODOS los parámetros que influyen en el background y
NADA del editor (lyrics, font, animation). Si el operador cambia alguno
después del preview, el nuevo hash difiere y dispara otro preview — el
viejo queda como wasted cost en R2 (TTL 24h cron lo limpia, separado).

COST MODEL
----------
- Hit perfecto (operador no cambia opciones): $0 extra. Reuso del video.
- Hit con cambio: ~$0.80-3.20 Veo wasted del preview viejo. UMG paga
  $8/video → margen aún positivo. Si las métricas muestran > 30% miss
  rate, agregar guard "no pre-gen hasta que el set esté completamente
  filled" (vs el simple debounce 2s actual).
- Free-tier: no se dispara pre-gen (no compensa el costo).
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import tempfile
from typing import Optional

from background_policy import (
    cache_policy_fingerprint,
    policy_enforces,
    resolve_atmospherics_policy,
    resolve_creative_mode,
    runtime_rollout_fingerprint,
)

logger = logging.getLogger("genly.bg_preview")


# Campos del request que entran al hash. Determinístico — JSON con keys
# ordenadas para que el mismo dict en distinto orden produzca el mismo key.
# Si agregamos params futuros que afecten el background, sumarlos acá +
# bumpear CACHE_VERSION para invalidar el cache existente.
# v5 also invalidates previews created before human-output recovery and the
# camera-equipment prompt fix. The atmospheric policy itself remains v4. In
# particular off/shadow/enforce never share a key, and an asset generated
# under the legacy namespace cannot become an enforce-mode cache hit. Every
# v5 entry also passed the authoritative content gate before cache_put.
# v6 (2026-07-17): metió `_lyrics_fp` en el hash — REVERTIDO en v7 (mismo
# día). La letra en el hash rompía el reuso: el preview se dispara MIENTRAS
# el operador edita (con la letra a medio hacer) y el render usa la letra
# final; cualquier diferencia → keys distintas → cache-miss → se regenera
# el fondo que ya estaba hecho. Como el operador casi siempre ajusta la
# letra antes de crear, el hit-rate se desplomaba y el ahorro no llegaba.
# v7: la letra ya NO entra al hash (fondos abstractos: un desfasaje de unas
# ediciones es invisible), pero el preview SIGUE recibiendo lyrics_text para
# generar (run_bg_preview_job → _ensure_background), así el fondo queda
# temáticamente cerca de la canción. La letra afecta la GENERACIÓN, no la
# ETIQUETA. v7 además deja de cachear el gradiente de policy-fallback (de v6).
CACHE_VERSION = "v7"


def compute_bg_cache_key(params: dict) -> str:
    """Computes a deterministic SHA-256 key from background-influencing params.

    Solo incluimos los campos que el background generator realmente usa.
    Cualquier otro field del request (audio, lyrics, etc.) NO va al hash —
    irrelevante para el background, evita rotar el cache innecesariamente.

    Args:
        params: dict con keys del request /generate-preview.

    Returns:
        hex string de 12 chars (suficiente para uniqueness, corto para R2 key).
    """
    # Authorization is resolved exclusively from the raw operator-authored
    # background_hint.  Lyrics, genre, concept, visual-bible output and other
    # derived text must never opt a job into atmospheric effects.  The
    # creative mode is derived from the existing legacy fields so no endpoint
    # or payload change is required.
    raw_operator_prompt = params.get("background_hint") or ""
    creative_mode = resolve_creative_mode(
        match_lyrics=bool(params.get("match_lyrics", True)),
        operator_prompt=raw_operator_prompt,
        verbatim=bool(params.get("bg_verbatim", False)),
    )
    atmospheric_policy = resolve_atmospherics_policy(raw_operator_prompt)

    from scenes import effective_movement_for_tenant

    effective_movement = effective_movement_for_tenant(
        params.get("movement_style"), params.get("_tenant_id")
    )
    canonical = {
        "_cache_version": CACHE_VERSION,
        "_creative_mode": creative_mode,
        "_policy_fingerprint": cache_policy_fingerprint(atmospheric_policy),
        "artist":          (params.get("artist") or "").strip(),
        "song_title":      (params.get("song_title") or "").strip(),
        "style":           (params.get("style") or "").strip().lower(),
        "movement_style":  effective_movement.strip().lower(),
        "effect":          (params.get("effect") or "").strip().lower(),
        "custom_colors":   (params.get("custom_colors") or "").strip().lower(),
        "genre":           (params.get("genre") or "").strip().lower(),
        "concept":         (params.get("concept") or "").strip(),
        "background_hint": (params.get("background_hint") or "").strip(),
        "bg_verbatim":     bool(params.get("bg_verbatim", False)),
        "background_mode": (params.get("background_mode") or "veo").strip().lower(),
        "animate_image":   bool(params.get("animate_image", False)),
        "match_lyrics":    bool(params.get("match_lyrics", True)),
        # NOTA v7: la LETRA no entra al hash a propósito (ver CACHE_VERSION).
        # lyrics_text influye la generación del preview, no la etiqueta.
    }
    payload = json.dumps(canonical, sort_keys=True, separators=(",", ":"))
    digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()
    return digest[:12]


def job_bg_cache_key(*, artist, song_title, style, movement_style, effect,
                     custom_colors, genre, concept, background_hint,
                     bg_verbatim, match_lyrics, tenant_id=None):
    """Key esperado para el fast-path de fondo único AI de un job /generate.

    Única fuente de verdad de los hardcodes `background_mode="veo"` /
    `animate_image=False`: el preview SIEMPRE hashea así (App.jsx
    previewEntry los fija) y el caller excluye custom/library/escenas/
    variation antes de llamar. La usan main.py (recompute server-side
    cuando el frontend no mandó el key — race del debounce) y
    pipeline._validate_bg_cache_key (validación defensiva del worker), así
    que main y worker no pueden divergir en la normalización.

    Best-effort: None si el cómputo falla (el caller genera fresh).
    """
    try:
        return compute_bg_cache_key({
            "artist": artist or "",
            "song_title": song_title or "",
            "style": style or "",
            "movement_style": movement_style or "",
            "_tenant_id": tenant_id or "",
            "effect": effect or "",
            "custom_colors": custom_colors or "",
            "genre": genre or "",
            "concept": concept or "",
            "background_hint": background_hint or "",
            "bg_verbatim": bool(bg_verbatim),
            "background_mode": "veo",
            "animate_image": False,
            "match_lyrics": bool(match_lyrics),
        })
    except Exception:
        return None


def cache_r2_key(bg_cache_key: str) -> str:
    """The R2 object key under which we store the cached background video."""
    return f"bg_cache/{bg_cache_key}.mp4"


def cache_check(bg_cache_key: str) -> bool:
    """Check if a pre-generated background exists in R2 cache."""
    import storage
    return storage.object_exists(cache_r2_key(bg_cache_key))


def cache_put(bg_cache_key: str, local_path: str) -> Optional[str]:
    """Upload a generated background to R2 cache. Returns the R2 key on
    success, None if R2 is disabled. Idempotent — if already exists, upload
    overwrites (same content though, hash-keyed)."""
    import storage
    return storage.upload_file(local_path, cache_r2_key(bg_cache_key))


def cache_download(bg_cache_key: str, dest_path: str) -> bool:
    """Download a cached background to a local path. Returns True on success."""
    import storage
    return storage.download_object(cache_r2_key(bg_cache_key), dest_path)


def run_bg_preview_job(
    job_id: str,
    bg_cache_key: str,
    params: dict,
    background_policy_fingerprint: str | None = None,
) -> dict:
    """RQ entry point — runs ONLY the background generation step + uploads to R2.

    Reusa `_ensure_background` del pipeline existente. No render lyrics overlay,
    no encoding final, no R2 upload del output principal — solo el video crudo
    del background sube a `bg_cache/{key}.mp4`.

    Idempotente: si entre el enqueue y el work alguien más subió la cache key
    (ej. dos previews paralelos con mismo hash), el segundo detect que ya
    existe y skip.
    """
    # Observability 2026-06-10: toda línea de log de este job lleva job_id.
    from observability import set_job_log_context
    set_job_log_context(job_id)
    from jobs import update_job

    # New API requests carry the authenticated tenant in this internal field.
    # Resolve it from the ghost job as a compatibility fallback for previews
    # that were queued immediately before a deploy.
    params = dict(params)
    if not params.get("_tenant_id"):
        try:
            from database import Job, SessionLocal
            with SessionLocal() as _db:
                _row = (_db.query(Job.tenant_id)
                          .filter(Job.job_id == job_id).one_or_none())
            params["_tenant_id"] = (_row[0] if _row else "") or ""
        except Exception:
            params["_tenant_id"] = ""

    runtime_policy = resolve_atmospherics_policy(params.get("background_hint"))
    runtime_fingerprint = runtime_rollout_fingerprint(
        mode=runtime_policy.get("policy_mode")
    )
    from background_policy import compatible_policy_fingerprint
    background_policy_fingerprint = compatible_policy_fingerprint(
        background_policy_fingerprint, runtime_fingerprint,
    )
    expected_cache_key = compute_bg_cache_key(params)
    lockstep_invalid = bool(
        (background_policy_fingerprint
         and background_policy_fingerprint != runtime_fingerprint)
        or (policy_enforces(runtime_policy) and not background_policy_fingerprint)
        or bg_cache_key != expected_cache_key
    )
    if lockstep_invalid:
        logger.error(
            "[BG_PREVIEW] refusing job=%s due policy/cache mismatch "
            "queued_policy=%s runtime_policy=%s queued_key=%s expected_key=%s",
            job_id,
            background_policy_fingerprint or "missing",
            runtime_fingerprint,
            bg_cache_key,
            expected_cache_key,
        )
        try:
            update_job(
                job_id,
                status="bg_preview_failed",
                current_step="background",
                error=(
                    "Background safety configuration changed while the preview "
                    "was queued. Retry after deployment finishes."
                ),
            )
        except Exception:
            pass
        return {
            "job_id": job_id,
            "status": "bg_preview_failed",
            "bg_cache_key": bg_cache_key,
            "error": "background_policy_mismatch",
        }

    # Idempotency check — race window entre enqueue y worker pickup.
    if cache_check(bg_cache_key):
        logger.info("[BG_PREVIEW] job=%s key=%s already cached, no-op", job_id, bg_cache_key)
        try:
            update_job(job_id, status="bg_preview_done", current_step="cached",
                       error=None)
        except Exception:
            pass
        return {"job_id": job_id, "status": "bg_preview_done", "bg_cache_key": bg_cache_key, "cached": True}

    # "bg_preview_running" (≤20 chars): el nombre viejo
    # "bg_preview_generating" (21) excedía el varchar(20) de Job.status y
    # este update fallaba EN SILENCIO desde siempre en Postgres (el status
    # jamás existió en la DB — 0 filas históricas; lo destapó el CI de
    # Postgres del PR #924). El try/except queda, pero ahora loguea: un
    # guard que traga errores esconde bugs de schema.
    try:
        update_job(job_id, status="bg_preview_running", current_step="background")
    except Exception as _status_err:
        logger.warning("[BG_PREVIEW] no pude marcar running job=%s: %s",
                       job_id, _status_err)

    # Importamos pipeline solo cuando lo necesitamos (importar main/pipeline
    # arrastra mucho — preferimos lazy para que el módulo bg_preview sea
    # importable en tests sin spin-up del backend entero).
    try:
        from pipeline import (
            _ensure_background,
            _compute_allow_people,
            _raise_if_job_timeout,
            _validate_background_asset_for_job,
            _write_safe_gradient_background,
        )
        from scenes import effective_movement_for_tenant

        effective_movement = effective_movement_for_tenant(
            params.get("movement_style"), params.get("_tenant_id")
        )

        with tempfile.TemporaryDirectory() as job_dir:
            # Generation prompts are guidance, not a compliance boundary. If
            # the model hallucinates a person/smoke/brand, discard only that
            # background and make one fresh paid attempt under a unique cache
            # namespace. Never turn the preview tracker into a rejected video.
            bg_path = None
            for safety_attempt in range(2):
                try:
                    candidate = _ensure_background(
                        params.get("style", "auto"),
                        job_dir,
                        job_id=job_id,
                        artist=params.get("artist", ""),
                        song_title=params.get("song_title", ""),
                        genre=params.get("genre", ""),
                        concept=params.get("concept", ""),
                        movement_style=effective_movement,
                        match_lyrics=bool(params.get("match_lyrics", True)),
                        # v6: el preview genera CON la letra (si el frontend
                        # la mandó) — antes generaba ciego a ella y el render
                        # heredaba un fondo que no la reflejaba.
                        lyrics_text=params.get("lyrics_text") or "",
                        background_hint=params.get("background_hint"),
                        bg_verbatim=bool(params.get("bg_verbatim", False)),
                        bg_mode=params.get("background_mode", "veo"),
                        custom_colors=params.get("custom_colors", ""),
                        allow_people=_compute_allow_people(
                            job_id, params.get("background_hint")
                        ),
                        generation_nonce=(
                            f"preview-{job_id}-{safety_attempt}"
                            if safety_attempt else ""
                        ),
                    )
                except Exception as generation_error:
                    _raise_if_job_timeout(generation_error)
                    logger.warning(
                        "[BG_PREVIEW] job=%s generation attempt=%s failed: %s",
                        job_id, safety_attempt + 1, generation_error,
                    )
                    continue
                if not candidate or not os.path.exists(candidate):
                    continue
                if _validate_background_asset_for_job(
                    job_id,
                    candidate,
                    params.get("background_hint"),
                    failure_status=None,
                ):
                    bg_path = candidate
                    break
                logger.warning(
                    "[BG_PREVIEW] job=%s discarded unsafe background attempt=%s",
                    job_id,
                    safety_attempt + 1,
                )

            if bg_path is None:
                bg_path = _write_safe_gradient_background(
                    job_dir,
                    params.get("style", "auto"),
                    filename="bg_preview_policy_fallback.mp4",
                )
                update_job(job_id, validation_result={
                    "passed": True,
                    "issues": [],
                    "validation_scope": "deterministic_local_fallback",
                    "recovered_from_unsafe_generation": True,
                    "policy_version": runtime_policy.get("policy_version"),
                    "policy_mode": runtime_policy.get("policy_mode"),
                })
                # v6: el gradiente de fallback NO se sube bajo la key real.
                # Cachearlo hacía que un render futuro con la misma key lo
                # heredara como si fuera un fondo Veo bueno, salteando su
                # propia generación y recovery (audit adversarial 2026-07-17).
                # Sin objeto en cache → el render hace miss y genera fresh.
                logger.warning(
                    "[BG_PREVIEW] job=%s policy fallback (gradiente) — no se "
                    "cachea bajo key=%s; el render regenerará fresh",
                    job_id, bg_cache_key,
                )
            else:
                uploaded_key = cache_put(bg_cache_key, bg_path)
                if not uploaded_key:
                    raise RuntimeError("R2 upload failed (R2 disabled?)")

                logger.info(
                    "[BG_PREVIEW] job=%s uploaded bg to R2 key=%s (size=%d KB)",
                    job_id, uploaded_key, os.path.getsize(bg_path) // 1024,
                )

        try:
            update_job(job_id, status="bg_preview_done", current_step="cached", error=None)
        except Exception:
            pass
        return {
            "job_id": job_id,
            "status": "bg_preview_done",
            "bg_cache_key": bg_cache_key,
            "cached": False,
        }

    except Exception as e:
        try:
            _raise_if_job_timeout(e)
        except NameError:
            # Policy mismatch/import failures happen before the lazy pipeline
            # import. They cannot be an RQ timeout raised by provider work.
            pass
        import traceback
        logger.error("[BG_PREVIEW] job=%s failed: %s\n%s", job_id, e, traceback.format_exc())
        # Surface to Sentry with the job tag — bg_preview runs in the RQ
        # worker, outside any FastAPI request, so without this explicit
        # capture a Veo 429-storm or quota exhaustion is invisible.
        # Mirrors pipeline.py's run_pipeline failure capture.
        try:
            import sentry_sdk
            with sentry_sdk.push_scope() as _scope:
                _scope.set_tag("event", "bg_preview.failed")
                _scope.set_tag("job_id", job_id)
                sentry_sdk.capture_exception(e)
        except Exception:
            pass
        try:
            update_job(
                job_id,
                status="bg_preview_failed",
                current_step="error",
                error=f"{type(e).__name__}: {str(e)[:200]}",
            )
        except Exception:
            pass
        return {
            "job_id": job_id,
            "status": "bg_preview_failed",
            "bg_cache_key": bg_cache_key,
            "error": str(e)[:300],
        }


# ---- Capa C hardening 2026-05-24 ----
# Cleanup + metrics agregados después del PR #279 inicial. Mantenido al
# final del archivo para que un diff vs main sea trivial.

import threading as _threading

# Process-local counters. Suficientes para single-instance; en horizontal
# scale cada API replica reporta los suyos (operator agrega manual via /admin).
# Sin librería de metrics formal (no hay Prometheus exporter en el stack
# hoy) — un dict + lock es lo mínimo que rinde.
_metrics_lock = _threading.Lock()
_metrics = {
    "requests_total": 0,
    "cache_hits": 0,
    "cache_misses": 0,
    "last_reset_ts": __import__("time").time(),
}


def track_request(cache_hit: bool) -> None:
    """Bump el counter apropiado. Thread-safe. Best-effort: cualquier excepción
    se traga porque no queremos romper /generate-preview por telemetría."""
    try:
        with _metrics_lock:
            _metrics["requests_total"] += 1
            if cache_hit:
                _metrics["cache_hits"] += 1
            else:
                _metrics["cache_misses"] += 1
    except Exception:
        pass


def get_metrics() -> dict:
    """Snapshot de las métricas + cache_hit_rate. Llamado desde
    /admin/bg-preview-metrics. Incluye estimación de cost wasted (cada miss
    gasta $0.80-3.20 según el modelo Veo usado — usamos $1.50 como promedio
    representative)."""
    with _metrics_lock:
        snap = dict(_metrics)
    total = snap["requests_total"]
    hit_rate = (snap["cache_hits"] / total) if total > 0 else 0.0
    # Conservador: cada miss = $1.50 (Veo 8s típico). Hit es free desde R2.
    wasted_cost_usd = round(snap["cache_misses"] * 1.50, 2)
    return {
        **snap,
        "cache_hit_rate": round(hit_rate, 4),
        "estimated_wasted_cost_usd": wasted_cost_usd,
    }


def cleanup_old_cache(retention_hours: int = 24, apply: bool = True) -> dict:
    """Borra objetos bajo `bg_cache/` más viejos que retention_hours.

    Llamado por el daemon thread `_bg_preview_cleanup_loop` cada 6h. La TTL
    típica de un preview es la duración de la edición del operador (~30-90s)
    + buffer; 24h cubre el caso de un batch UMG grande que queda en review
    overnight. Más allá, los previews descartados acumulan storage en R2.

    Args:
        retention_hours: TTL. Default 24h.
        apply: True = realmente borra; False = dry-run, devuelve qué borraría.

    Returns:
        Estructura del cleanup_old_inputs (scanned/expired/deleted/bytes_freed/sample).
    """
    import storage
    # cleanup_old_inputs filtra `modified < cutoff` con cutoff=now-retention_days.
    # Convertimos horas a días redondeando hacia abajo (min 1) — 24h = 1 día.
    retention_days = max(1, retention_hours // 24)
    return storage.cleanup_old_inputs(
        retention_days=retention_days,
        apply=apply,
        prefix="bg_cache/",
    )
