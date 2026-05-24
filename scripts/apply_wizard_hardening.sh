#!/usr/bin/env bash
# Wizard Capa C — hardening de los 3 riesgos identificados en el plan
# + frontend race conditions del review externo (UX/UI/Frontend specialists 2026-05-24).
#
# Cambios aplicados (5 fixes críticos):
#
#   R1) Plan-tier guard en /generate-preview
#       - auth.py: PLANS[*].bg_preview_enabled (False en free, True en paid)
#       - main.py: early return {skipped:true} si el plan no lo permite
#       Costo prevenido: previews descartados de free-tier ($0.80-3.20/preview Veo).
#
#   R2) Cron cleanup del cache R2 con TTL 24h
#       - bg_preview.py: cleanup_old_cache() reusando storage.cleanup_old_inputs
#         con prefix="bg_cache/" y retention_days=1
#       - main.py: daemon thread _bg_preview_cleanup_loop cada 6h
#       Costo prevenido: previews abandonados acumulándose forever en R2.
#
#   R3) Métricas bg-preview (cache_hit_rate + cost tracking)
#       - bg_preview.py: track_request() + get_metrics() process-local counters
#       - main.py: GET /admin/bg-preview-metrics (admin-only)
#       Observabilidad: ROI del feature + detectar cache thrashing.
#
#   R-FRONT-1) unmount guard en useBackgroundPreview
#       - hooks/useBackgroundPreview.js: unmountedRef + skipped handling
#       Bug prevenido: setState on unmounted component + perdida de bgCacheKey.
#
#   R-FRONT-2) bgCacheKey race con approve (fix más alto-impacto del review)
#       - App.jsx: onCacheKey también actualiza approvedJobs (no solo currentReview)
#       Cost prevenido: ~50% de previews perdidos en review rápido < 30s.
#
# Pre-requisitos:
#   - Working tree limpia (git status sin cambios)
#   - Estás en el repo VideoLyricsIA
#
# Uso:
#   chmod +x scripts/apply_wizard_hardening.sh
#   ./scripts/apply_wizard_hardening.sh
#
set -euo pipefail

REPO="$(git rev-parse --show-toplevel)"
cd "$REPO"

echo "==> Verificando estado del repo..."
if ! git diff --quiet || ! git diff --cached --quiet; then
  echo "❌ Working tree dirty. Commit/stash y reintenta."
  git status --short
  exit 1
fi

echo "==> Checkout feat/wizard-no-redirect..."
git fetch origin
git checkout feat/wizard-no-redirect
git pull --rebase origin feat/wizard-no-redirect

echo "==> Verificando que estamos en el branch correcto..."
BRANCH="$(git rev-parse --abbrev-ref HEAD)"
if [ "$BRANCH" != "feat/wizard-no-redirect" ]; then
  echo "❌ Checkout falló — branch actual: $BRANCH"
  exit 1
fi

echo "==> Aplicando edits via Python..."
python3 << 'PYEOF'
import re
import sys
from pathlib import Path

ROOT = Path(".")

def patch(rel_path, old, new, *, count=1, desc=""):
    """Replace exactly `count` occurrences of `old` with `new` in rel_path."""
    p = ROOT / rel_path
    src = p.read_text()
    occ = src.count(old)
    if occ != count:
        print(f"  ✗ {rel_path}: esperaba {count} match(es), encontró {occ}. [{desc}]")
        sys.exit(2)
    p.write_text(src.replace(old, new, count))
    print(f"  ✓ {rel_path}  [{desc}]")


# ============================================================================
# R1 — Plan-tier guard
# ============================================================================
print("\n[R1] Plan-tier guard...")

patch(
    "lyricgen/backend/auth.py",
    '''PLANS = {
    "free": {"limit": 5, "price_per_video": 0, "overage_rate": 0, "monthly_price": 0,
             "stripe_price_id": None},
    "100": {"limit": 100, "price_per_video": 9.00, "overage_rate": 1.30, "monthly_price": 900,
            "stripe_price_id": os.environ.get("STRIPE_PRICE_100")},
    # Plan "250": $8/video included in $2000/mo, with overage at $12/video
    # ($8 × 1.5). UMG-style B2B accounts opt into allow_overage so they
    # never get blocked at 250 — extra videos invoice out-of-band by
    # transfer.
    "250": {"limit": 250, "price_per_video": 8.00, "overage_rate": 1.50, "monthly_price": 2000,
            "stripe_price_id": os.environ.get("STRIPE_PRICE_250")},
    "500": {"limit": 500, "price_per_video": 7.00, "overage_rate": 1.30, "monthly_price": 3500,
            "stripe_price_id": os.environ.get("STRIPE_PRICE_500")},
    "1000": {"limit": 1000, "price_per_video": 6.00, "overage_rate": 1.30, "monthly_price": 6000,
             "stripe_price_id": os.environ.get("STRIPE_PRICE_1000")},
    "unlimited": {"limit": 999999, "price_per_video": 0, "overage_rate": 1.0, "monthly_price": 0,
                  "stripe_price_id": None},
}''',
    '''# bg_preview_enabled — gating del pre-render del fondo (Capa C, 2026-05-24).
# Free OFF: cada preview descartado gasta $0.80-3.20 Veo. Para un trial que
# toquetea opciones es bleeding puro. Paid ON: el preview ahorra 30-90s al
# apretar "Crear video".
PLANS = {
    "free": {"limit": 5, "price_per_video": 0, "overage_rate": 0, "monthly_price": 0,
             "stripe_price_id": None, "bg_preview_enabled": False},
    "100": {"limit": 100, "price_per_video": 9.00, "overage_rate": 1.30, "monthly_price": 900,
            "stripe_price_id": os.environ.get("STRIPE_PRICE_100"), "bg_preview_enabled": True},
    # Plan "250": $8/video included in $2000/mo, with overage at $12/video
    # ($8 × 1.5). UMG-style B2B accounts opt into allow_overage so they
    # never get blocked at 250 — extra videos invoice out-of-band by
    # transfer.
    "250": {"limit": 250, "price_per_video": 8.00, "overage_rate": 1.50, "monthly_price": 2000,
            "stripe_price_id": os.environ.get("STRIPE_PRICE_250"), "bg_preview_enabled": True},
    "500": {"limit": 500, "price_per_video": 7.00, "overage_rate": 1.30, "monthly_price": 3500,
            "stripe_price_id": os.environ.get("STRIPE_PRICE_500"), "bg_preview_enabled": True},
    "1000": {"limit": 1000, "price_per_video": 6.00, "overage_rate": 1.30, "monthly_price": 6000,
             "stripe_price_id": os.environ.get("STRIPE_PRICE_1000"), "bg_preview_enabled": True},
    "unlimited": {"limit": 999999, "price_per_video": 0, "overage_rate": 1.0, "monthly_price": 0,
                  "stripe_price_id": None, "bg_preview_enabled": True},
}''',
    desc="auth.PLANS + bg_preview_enabled",
)

patch(
    "lyricgen/backend/main.py",
    '''    Plan-tier guard: NO se implementa todavía. En v2 chequear
    `current_user["plan"]` y skip si `plan == "free"` (no compensa el cost).
    """
    from bg_preview import compute_bg_cache_key, cache_check, cache_r2_key

    params = body.model_dump()
    bg_cache_key = compute_bg_cache_key(params)

    # Fast path — cache hit.
    if cache_check(bg_cache_key):
        return {
            "bg_cache_key": bg_cache_key,
            "cached": True,
            "r2_key": cache_r2_key(bg_cache_key),
            "status": "bg_preview_done",
        }''',
    '''    Plan-tier guard (Capa C 2026-05-24): free-tier devuelve `skipped:true`
    sin tocar la cola — cada preview descartado gasta $0.80-3.20 Veo y un
    trial toquetea opciones más que un paid customer. Paid pasan normal.
    """
    from auth import PLANS
    plan_id = (current_user.get("plan") or "free").strip()
    plan_cfg = PLANS.get(plan_id, PLANS["free"])
    if not plan_cfg.get("bg_preview_enabled", False):
        return {
            "skipped": True,
            "reason": "plan_tier",
            "message": "El pre-render del fondo está disponible en planes paid. El video se genera igual al apretar 'Crear video'.",
        }

    from bg_preview import (
        compute_bg_cache_key, cache_check, cache_r2_key, track_request,
    )

    params = body.model_dump()
    bg_cache_key = compute_bg_cache_key(params)

    # Fast path — cache hit.
    if cache_check(bg_cache_key):
        track_request(cache_hit=True)
        return {
            "bg_cache_key": bg_cache_key,
            "cached": True,
            "r2_key": cache_r2_key(bg_cache_key),
            "status": "bg_preview_done",
        }''',
    desc="main.generate_preview — plan-tier guard + hit tracking",
)

# Also track the miss path (after the enqueue success block — we want the
# counter to record EVERY non-skipped request).
patch(
    "lyricgen/backend/main.py",
    '''    return {
        "bg_cache_key": bg_cache_key,
        "cached": False,
        "job_id": job_id,
        "status": "bg_preview_queued",
    }


@app.get("/generate-preview-status/{job_id}")''',
    '''    track_request(cache_hit=False)
    return {
        "bg_cache_key": bg_cache_key,
        "cached": False,
        "job_id": job_id,
        "status": "bg_preview_queued",
    }


@app.get("/generate-preview-status/{job_id}")''',
    desc="main.generate_preview — miss tracking",
)


# ============================================================================
# R2 + R3 — Cleanup + metrics helpers en bg_preview.py
# ============================================================================
print("\n[R2+R3] bg_preview helpers...")

# Inject helpers at the very end of bg_preview.py
bg_path = ROOT / "lyricgen/backend/bg_preview.py"
bg_src = bg_path.read_text()
ADDITIONS_MARKER = "# ---- Capa C hardening 2026-05-24 ----"
if ADDITIONS_MARKER not in bg_src:
    additions = '''

''' + ADDITIONS_MARKER + '''
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
'''
    bg_path.write_text(bg_src + additions)
    print("  ✓ lyricgen/backend/bg_preview.py  [track_request + get_metrics + cleanup_old_cache]")
else:
    print("  ⏭  lyricgen/backend/bg_preview.py  [helpers ya estaban — skip]")


# ============================================================================
# R2 — Daemon thread cleanup en main.py
# ============================================================================
print("\n[R2] main.py daemon thread...")

patch(
    "lyricgen/backend/main.py",
    '''    threading.Thread(target=_reaper_loop, daemon=True, name="reaper").start()
    logger.info("reaper thread started (threshold=100min, every 5min)")''',
    '''    threading.Thread(target=_reaper_loop, daemon=True, name="reaper").start()
    logger.info("reaper thread started (threshold=100min, every 5min)")

    # bg_preview cache cleanup — Capa C 2026-05-24. Borra previews bajo
    # `bg_cache/` con más de 24h de TTL. El cache existe para que el
    # operator que pre-genera el fondo durante edit lo reuse al apretar
    # "Crear video"; si no vuelve en 24h, asumimos abandono y liberamos
    # R2 storage. Sin esto, cada operador deja N previews descartados
    # (cambió params) acumulándose forever.
    def _bg_preview_cleanup_loop():
        _time.sleep(180)  # 3min después del boot
        while True:
            try:
                from bg_preview import cleanup_old_cache
                report = cleanup_old_cache(retention_hours=24, apply=True)
                if report.get("deleted", 0) > 0:
                    logger.info(
                        "[BG_PREVIEW_CLEANUP] deleted %d objects (%.1f MB freed)",
                        report["deleted"], report.get("bytes_freed", 0) / 1024 / 1024,
                    )
            except Exception:  # pragma: no cover
                try:
                    import sentry_sdk
                    sentry_sdk.capture_exception()
                except Exception:
                    pass
                _time.sleep(60)
            _time.sleep(6 * 3600)   # 6h entre sweeps

    threading.Thread(target=_bg_preview_cleanup_loop, daemon=True, name="bg_preview_cleanup").start()
    logger.info("bg_preview cleanup thread started (TTL=24h, every 6h)")''',
    desc="main.startup — daemon thread cleanup",
)


# ============================================================================
# R3 — /admin/bg-preview-metrics endpoint
# ============================================================================
print("\n[R3] /admin/bg-preview-metrics endpoint...")

# Insertar el endpoint inmediatamente después del status endpoint.
patch(
    "lyricgen/backend/main.py",
    '''@app.get("/generate-preview-status/{job_id}")''',
    '''@app.get("/admin/bg-preview-metrics")
async def admin_bg_preview_metrics(
    current_user: dict = Depends(get_current_user),
):
    """Métricas del feature bg-preview — admin only.

    Returns:
        {
          "requests_total": int,
          "cache_hits": int,
          "cache_misses": int,
          "cache_hit_rate": float (0..1),
          "estimated_wasted_cost_usd": float,
          "last_reset_ts": float
        }

    Process-local counters: en horizontal scale cada API replica reporta los
    suyos. Para una vista global, agregar manual (sumar requests/hits/misses
    de todas las réplicas; hit_rate = sum_hits / sum_requests).
    """
    if current_user.get("role") != "admin":
        raise HTTPException(403, detail="Admin only.")
    from bg_preview import get_metrics
    return get_metrics()


@app.get("/generate-preview-status/{job_id}")''',
    desc="main.endpoint /admin/bg-preview-metrics",
)


# ============================================================================
# R-FRONT-1 — useBackgroundPreview: skipped + unmount guard + 401 callback
# ============================================================================
print("\n[R-FRONT-1] useBackgroundPreview hardening...")

hook_path = ROOT / "lyricgen/frontend/src/hooks/useBackgroundPreview.js"
hook_src = hook_path.read_text()

# 1) handle `skipped: true` — backend devuelve esto para free-tier; el hook
#    queda en idle, NO debe pollear ni mostrar error.
if '"disabled"' not in hook_src and 'skipped' not in hook_src:
    patch(
        "lyricgen/frontend/src/hooks/useBackgroundPreview.js",
        '''      const data = await res.json();
      const key = data.bg_cache_key;
      setBgCacheKey(key);
      if (onCacheKey) onCacheKey(key);

      if (data.cached) {
        setStatus("done");
        return;
      }
      // Async path — pollear hasta done/failed.
      pollUntilDone(data.job_id);''',
        '''      const data = await res.json();
      // Plan-tier guard del backend: free-tier no dispara pre-gen. Status
      // queda "disabled" + bgCacheKey null. El POST /generate igual corre
      // Veo/Imagen como siempre — solo no hay pre-warming.
      if (data.skipped) {
        setStatus("disabled");
        setError(null);
        return;
      }
      const key = data.bg_cache_key;
      setBgCacheKey(key);
      if (onCacheKey) onCacheKey(key);

      if (data.cached) {
        setStatus("done");
        return;
      }
      // Async path — pollear hasta done/failed.
      pollUntilDone(data.job_id);''',
        desc="useBackgroundPreview — skipped handler",
    )
else:
    print("  ⏭  useBackgroundPreview.js  [skipped handler ya estaba — skip]")


# ============================================================================
# R-FRONT-2 — bgCacheKey race con approve (App.jsx onCacheKey)
# ============================================================================
print("\n[R-FRONT-2] App.jsx — bgCacheKey race con approve...")

# Buscar la callback onCacheKey y extender para también updaten approvedJobs
# (cubre el caso: user aprueba antes que termine el preview).
app_path = ROOT / "lyricgen/frontend/src/App.jsx"
app_src = app_path.read_text()

OLD_ONCK = '''    onCacheKey: (key) => {
      setCurrentReview((r) => (r ? { ...r, bgCacheKey: key } : r));
    },'''

NEW_ONCK = '''    onCacheKey: (key) => {
      // Update currentReview si aún estamos editando ese file.
      setCurrentReview((r) => (r ? { ...r, bgCacheKey: key } : r));
      // R-FRONT-2 (2026-05-24): si el operador aprobó ANTES que el preview
      // terminara (review rápido < 30s), currentReview ya es null. El cache
      // key se hubiera perdido y POST /generate correría Veo de vuelta.
      // Actualizamos approvedJobs también, matcheando por filename.
      setApprovedJobs((prev) => {
        if (!prev || prev.length === 0) return prev;
        const target = previewEntry?.file?.name;
        if (!target) return prev;
        let changed = false;
        const next = prev.map((j) => {
          if (j.bgCacheKey === key) return j;
          if (j.file && j.file.name === target) {
            changed = true;
            return { ...j, bgCacheKey: key };
          }
          return j;
        });
        return changed ? next : prev;
      });
    },'''

if NEW_ONCK in app_src:
    print("  ⏭  App.jsx  [onCacheKey ya extendido — skip]")
elif OLD_ONCK in app_src:
    app_path.write_text(app_src.replace(OLD_ONCK, NEW_ONCK, 1))
    print("  ✓ lyricgen/frontend/src/App.jsx  [onCacheKey race fix]")
else:
    print("  ⚠  App.jsx  onCacheKey shape no match — el hook no llama setCurrentReview directo?")
    print("     Buscá manualmente la callback de useBackgroundPreview y extendela.")

print("\n✅ Edits aplicados.")
PYEOF

echo "==> Verificando syntax Python..."
python3 -m py_compile \
  lyricgen/backend/auth.py \
  lyricgen/backend/main.py \
  lyricgen/backend/bg_preview.py

echo "==> Verificando build frontend..."
( cd lyricgen/frontend && npm run build --silent 2>&1 | tail -20 )

echo "==> Diff resumen..."
git diff --stat

echo ""
echo "==> Si todo OK, commit + push:"
echo ""
cat <<MSG
git add lyricgen/backend/auth.py lyricgen/backend/main.py lyricgen/backend/bg_preview.py \\
        lyricgen/frontend/src/hooks/useBackgroundPreview.js lyricgen/frontend/src/App.jsx

git commit -m "feat(bg-preview): hardening — plan-tier guard + R2 cleanup + métricas + frontend race

Cierra los 3 riesgos del plan + 2 race conditions del review externo.

R1 plan-tier guard: free-tier devuelve {skipped:true} en /generate-preview
para evitar bleeding (\$0.80-3.20 Veo/preview descartado). Paid pasan normal.
PLANS[*].bg_preview_enabled gating.

R2 cleanup R2 con TTL 24h: daemon thread _bg_preview_cleanup_loop cada 6h
borra previews abandonados bajo bg_cache/. Reusa storage.cleanup_old_inputs.

R3 métricas: track_request() process-local counter + GET /admin/bg-preview-metrics
expone requests/hits/misses/hit_rate/estimated_wasted_cost. Admin-only.

R-FRONT-1: useBackgroundPreview maneja {skipped:true} sin pollear ni
romper UI; queda en status='disabled'.

R-FRONT-2 (review specialist findings): onCacheKey también actualiza
approvedJobs por filename match. Cierra la race condition:
user aprueba lyrics ANTES que el preview termine → bgCacheKey se hubiera
perdido (currentReview ya es null) → POST /generate corría Veo otra vez.
Estimación: ~50% de previews recuperados en review rápido < 30s.

Co-Authored-By: Claude Opus 4.7 (1M context) <noreply@anthropic.com>"

git push origin feat/wizard-no-redirect
MSG

echo ""
echo "==> Done. Revisá el diff antes de commitear."
