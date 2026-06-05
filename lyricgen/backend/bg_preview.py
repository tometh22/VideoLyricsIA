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

logger = logging.getLogger("genly.bg_preview")


# Campos del request que entran al hash. Determinístico — JSON con keys
# ordenadas para que el mismo dict en distinto orden produzca el mismo key.
# Si agregamos params futuros que afecten el background, sumarlos acá +
# bumpear CACHE_VERSION para invalidar el cache existente.
CACHE_VERSION = "v1"


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
    canonical = {
        "_cache_version": CACHE_VERSION,
        "artist":          (params.get("artist") or "").strip(),
        "song_title":      (params.get("song_title") or "").strip(),
        "style":           (params.get("style") or "").strip().lower(),
        "movement_style":  (params.get("movement_style") or "").strip().lower(),
        "effect":          (params.get("effect") or "").strip().lower(),
        "custom_colors":   (params.get("custom_colors") or "").strip().lower(),
        "genre":           (params.get("genre") or "").strip().lower(),
        "concept":         (params.get("concept") or "").strip(),
        "background_hint": (params.get("background_hint") or "").strip(),
        "bg_verbatim":     bool(params.get("bg_verbatim", False)),
        "background_mode": (params.get("background_mode") or "veo").strip().lower(),
        "animate_image":   bool(params.get("animate_image", False)),
        "match_lyrics":    bool(params.get("match_lyrics", True)),
    }
    payload = json.dumps(canonical, sort_keys=True, separators=(",", ":"))
    digest = hashlib.sha256(payload.encode("utf-8")).hexdigest()
    return digest[:12]


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


def run_bg_preview_job(job_id: str, bg_cache_key: str, params: dict) -> dict:
    """RQ entry point — runs ONLY the background generation step + uploads to R2.

    Reusa `_ensure_background` del pipeline existente. No render lyrics overlay,
    no encoding final, no R2 upload del output principal — solo el video crudo
    del background sube a `bg_cache/{key}.mp4`.

    Idempotente: si entre el enqueue y el work alguien más subió la cache key
    (ej. dos previews paralelos con mismo hash), el segundo detect que ya
    existe y skip.
    """
    from jobs import update_job

    # Idempotency check — race window entre enqueue y worker pickup.
    if cache_check(bg_cache_key):
        logger.info("[BG_PREVIEW] job=%s key=%s already cached, no-op", job_id, bg_cache_key)
        try:
            update_job(job_id, status="bg_preview_done", current_step="cached",
                       error=None)
        except Exception:
            pass
        return {"job_id": job_id, "status": "bg_preview_done", "bg_cache_key": bg_cache_key, "cached": True}

    try:
        update_job(job_id, status="bg_preview_generating", current_step="background")
    except Exception:
        pass

    # Importamos pipeline solo cuando lo necesitamos (importar main/pipeline
    # arrastra mucho — preferimos lazy para que el módulo bg_preview sea
    # importable en tests sin spin-up del backend entero).
    try:
        from pipeline import _ensure_background

        with tempfile.TemporaryDirectory() as job_dir:
            # _ensure_background returns path to generated bg video/image.
            # FIX 2026-05-25: la signatura real NO tiene `duration`, `effect`,
            # `animate_image`, `segments`, ni `lang` — eran kwargs que evolucionaron
            # fuera o nunca existieron. El primer arg posicional es `style_hint`
            # (no `style`). El effect overlay se compone en el render final
            # (fx_compositor), no en bg-gen. Pasamos solo lo que la signatura acepta.
            bg_path = _ensure_background(
                params.get("style", "auto"),  # style_hint (positional)
                job_dir,                       # job_dir (positional)
                # job_id REAL del job ghost (existe en `jobs`, 12 chars). Antes
                # se pasaba f"bgpreview_{job_id}" (~22 chars), lo que rompía el
                # INSERT de provenance: ai_provenance.job_id es VARCHAR(12) con
                # FK a jobs.job_id → StringDataRightTruncation (Sentry "Failed to
                # insert provenance start row") y, aun si entrara por longitud,
                # violaría el FK. Con el id real la auditoría UMG queda bien atada
                # al job, y el progress-crawl + heartbeat de Veo ahora
                # actualizan/protegen el job ghost correctamente.
                job_id=job_id,
                artist=params.get("artist", ""),
                song_title=params.get("song_title", ""),
                genre=params.get("genre", ""),
                concept=params.get("concept", ""),
                movement_style=params.get("movement_style", ""),
                match_lyrics=bool(params.get("match_lyrics", True)),
                background_hint=params.get("background_hint"),
                bg_verbatim=bool(params.get("bg_verbatim", False)),
                bg_mode=params.get("background_mode", "veo"),
                custom_colors=params.get("custom_colors", ""),
            )

            if not bg_path or not os.path.exists(bg_path):
                raise RuntimeError("background generation returned no file")

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
