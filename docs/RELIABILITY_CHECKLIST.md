# Reliability hardening — checklist

> Auditoría 2026-05-20. Marca qué quedó arreglado por código, qué ya estaba hecho,
> y qué pasos manuales (dashboard/DNS) faltan. Prod estaba `status:ok` al auditar.

## Hecho por código (branch `fix/reliability-hardening`)
- **#7 Retry en Gemini (fetch de letras):** `pipeline.py` `_fetch_lyrics_via_gemini_search` ahora reintenta solo errores transitorios (timeout/5xx/429/conexión) con backoff exponencial (3 intentos, `GEMINI_LYRICS_MAX_RETRIES`). Los bloqueos deterministas (RECITATION/SAFETY, LYRICS_NOT_FOUND) NO se reintentan. Antes, un hipo degradaba la transcripción en silencio.
- **#3 Redis self-healing:** `queue_jobs.py` `_init_redis` revalida la conexión cacheada con `ping` en cada llamada; si está muerta, reconecta sin reiniciar el pod. Timeouts de socket acotados (5s) + `health_check_interval=30` para que una partición de red falle rápido en vez de colgar el request/healthcheck.

## Ya estaba hecho (no duplicar)
- **Backups de Postgres:** PR #235 — backup off-Railway a R2 cada 6h (`db_backup.py` + workflow). Cubre la durabilidad de PG.
- **Alerting/uptime:** PR #234 — monitor de 5 min que alerta antes que el cliente (`uptime_ping.py` + workflow).
- **Deploys zero-downtime:** 2 réplicas API con healthcheck, 7 worker, graceful shutdown, RQ retry, reaper (railway.toml + #230/#232).

## No era problema
- **R2 staging "degraded":** fue **transitorio** (un read lento de 3s en el probe). Re-check posterior: `ok`, 189ms. `degraded` devuelve 200, no drena réplicas. Sin acción.

---

## Pasos manuales pendientes (dashboard / DNS — no se pueden hacer por código)

### #1 — Desacoplar prod del dominio crudo de Railway (ALTO impacto)
**Problema:** el bundle vivo de `app.genly.pro` apunta a `genly-ai.up.railway.app` (hardcodeado en build), mientras el doc dice `api.genly.pro`, que es **NXDOMAIN**. Un rebuild con la config "documentada" rompe prod.
**Fix (opción A, recomendada):**
1. GoDaddy → zona `genly.pro`: agregar **CNAME `api` → `<dominio Railway del service api>`** (el que muestra Railway → Settings → Networking → Custom Domain).
2. Railway → service `api` → agregar el custom domain `api.genly.pro` (Railway te da el target y maneja el cert).
3. Vercel → proyecto frontend → prod env: `VITE_API_URL=https://api.genly.pro` → **redeploy** (la var se hornea en build).
4. Verificar: `curl -s https://api.genly.pro/health` → `status:ok`, y que `app.genly.pro` siga funcionando.
**Opción B (rápida, menos prolija):** dejar `genly-ai.up.railway.app` y actualizar `docs/STAGING_SETUP.md` para que el doc coincida con la realidad. Sigue acoplado a la URL cruda.

### #2 — Verificar `max_connections` de Postgres
Math actual ~115 conexiones (2 API×2 workers×10 + 7 Worker×10 + reserva). Default Railway = 100.
```sql
-- en la PG de prod (Railway → Postgres → Connect):
SHOW max_connections;
```
Si es <150, subirla (plan Pro soporta hasta 500) o desplegar **PgBouncer** (documentado en `docs/SCALING.md`, no desplegado). Hoy el pool está calmo (util 0%), pero un pico + réplica extra puede agotarlo.

### #4 — `RAILWAY_SHUTDOWN_TIMEOUT_SECONDS=1200` en el Worker
No se puede setear por `railway.toml` (es var de plataforma). Incidente 2026-05-15: staging sin esta var → default 30s mató renders UMG en cada deploy.
- Railway → service **Worker** → Variables → `RAILWAY_SHUTDOWN_TIMEOUT_SECONDS=1200`. **Verificar en staging Y prod.**

### #6 — DNS de subdominios faltantes (NXDOMAIN global)
En GoDaddy, zona `genly.pro`, agregar:
- `api` → Railway (ver #1)
- `api-staging` → Railway (service api de staging)
- `staging.app` → Vercel (preview del branch `staging`) — necesario para poder ver staging en una URL estable
- `status` → tu status page (ver `docs/STATUS_PAGE_SETUP.md`)

---

## Orden sugerido
1. #1 (desacople + arregla DNS de `api`) — alto impacto.
2. #2 y #4 (5 min de dashboard, evitan outages conocidos).
3. #6 resto de DNS (`status`, `api-staging`, `staging.app`).
