# RUNBOOK — Launch día-0 (operadores concurrentes)

Procedimiento para el día que **3 operadores de Universal usan GenLy por
primera vez, en simultáneo**. No es un launch cualquiera: es reputación de
marca y son masters de audio sensibles. Este runbook asume que el hardening
de observabilidad (`feat/umg-launch-observability-hardening`) y los checks
de preflight (`feat/umg-launch-preflight`) ya están en `main`/prod.

Relacionado: [`RUNBOOK_UMG.md`](RUNBOOK_UMG.md) (infra Railway + env vars),
[`RUNBOOK_EMERGENCY.md`](RUNBOOK_EMERGENCY.md) (incidentes),
[`DEPLOY_RESILIENCE.md`](DEPLOY_RESILIENCE.md) (resiliencia de deploys).

Los 3 operadores comparten **un solo tenant** (modelo multi-operador: ven
los jobs de los demás — es intencional).

---

## 1. Checklist pre-launch

### T-24h — en staging

```bash
# 1. Readiness completo contra staging
STAGING_API_URL=https://<staging-api> \
  PREFLIGHT_USERNAME=<admin> PREFLIGHT_PASSWORD=<pass> \
  UMG_USERNAMES=op1,op2,op3 DATABASE_URL=<staging-db-url> \
  R2_PROBE_URL='<cualquier-URL-firmada-de-staging>' \
  make preflight-staging
# Esperado: ✅ GO (todos los P0 en verde)

# 2. Pen-test de aislamiento (necesita 2 cuentas de test en tenants
#    distintos + opcional A2 en el tenant de A)
cd lyricgen/backend && \
  PENTEST_BASE_URL=https://<staging-api> \
  PENTEST_A_USER=ptA PENTEST_A_PASS=... \
  PENTEST_B_USER=ptB PENTEST_B_PASS=... \
  PENTEST_A2_USER=ptA2 PENTEST_A2_PASS=... \
  ./venv/bin/python scripts/pentest_tenant_isolation.py
# Esperado: ✅ No leaks
```

> **Seed de cuentas de pen-test**: A y B se autoregistran con
> `--create-accounts` (cada registro recibe un tenant fresco). A2 (mismo
> tenant que A, control positivo) requiere que un admin lo cree en el
> tenant de A — el autoregistro no permite unirse a un tenant existente.

### T-1h — contra prod (solo lectura)

```bash
PROD_API_URL=https://genly-ai.up.railway.app \
  PREFLIGHT_USERNAME=<admin> PREFLIGHT_PASSWORD=<pass> \
  UMG_USERNAMES=op1,op2,op3 DATABASE_URL=<prod-db-url> \
  R2_PROBE_URL='<URL-firmada-de-prod>' \
  make preflight-prod
# Esperado: ✅ GO. Todos los checks son read-only — seguros en prod.
```

Confirmar manualmente lo que el preflight no cubre:

- [ ] **Sentry vivo**: dispará un evento de prueba (Sentry dashboard →
      Settings → Projects → "Send test event", o forzá un error conocido)
      y confirmá que llega al proyecto de **prod** con el `release` tag
      poblado. Verificá que existe una **regla de alerta** (email/Slack)
      para errores nuevos.
- [ ] **`SENTRY_DSN` en el servicio Worker** (Railway → Worker → Variables).
      Sin esto, los errores de render/transcripción NO llegan a Sentry —
      es el fix central de `feat/umg-launch-observability-hardening` y no
      tiene efecto si la var no está en el Worker.
- [ ] **`RAILWAY_SHUTDOWN_TIMEOUT_SECONDS=1200`** en el Worker (no se puede
      setear vía `railway.toml`; va en el dashboard). Sin esto, cada
      redeploy mata el render en vuelo y el operador ve "el servidor se
      reinició".
- [ ] **Worker ×7 / API ×2** (`railway.toml:99,191`; confirmar que el
      dashboard coincide).
- [ ] **Cuotas de los 3 operadores**: plan correcto (no `free`) y
      `allow_overage=true` si la demo puede pasar el límite mensual. Ajuste:
      `POST /admin/users/{id}` con `{"plan_id": "...", "allow_overage": true}`.

---

## 2. Monitoreo día-of

Tres pantallas abiertas durante la sesión:

1. **Sentry dashboard** (proyecto prod). Firmas a vigilar:
   - `event:reaper.loop_crash` → el reaper murió, los jobs trabados se
     acumulan.
   - `layer:render_pipeline` / `layer:transcription` → un job permanente
     murió. El tag `tenant_id` dice si es de UMG.
   - Cascadas de Veo 429 (`bg_preview.failed`) bajo carga concurrente.
   - NameError / F821 (la clase de bug del incidente 2026-05-26).

2. **`/admin/queue`** (token admin) o el watcher:
   ```bash
   watch -n 10 'curl -s -H "Authorization: Bearer $ADMIN_TOKEN" \
     https://genly-ai.up.railway.app/admin/queue'
   ```
   Las colas deben drenar. Si `processing` se estanca → ver §5.

3. **`/health`**:
   - `db_pool.utilization` > 0.8 → pool tensionado (riesgo del incidente
     del 17-may).
   - `queue_depth` creciendo sin drenar → falta capacidad de worker.
   - `reaper` debe ser un dict con `seconds_since_last_ok` bajo (no
     `never_ticked`).
   - `workers_alive` debe ser 7.

Los jobs `uptime_ping.py` y `daily_smoke.py` ya alertan por separado.

---

## 3. Dry-run E2E manual (antes de avisarles que arranquen)

Con una canción real, como si fueras el operador — **mirando con los ojos**,
no confiando en que "el pipeline corrió":

1. **Upload** (`/upload-url` → PUT a R2 → `/transcribe-uploaded`).
2. **Transcripción**: el editor abre rápido (~15-20s).
3. **Editar letra**: corregir alguna línea, guardar.
4. **Aprobar y generar** (`/approve` → render).
5. **Descargar** el video.
6. **Verificación visual de sincronía karaoke**: reproducir el render y
   confirmar que la letra cae en tiempo. Una letra fuera de tiempo para un
   sello es vergonzoso, no un detalle. Esto la automatización no lo puede
   verificar — es el paso humano clave.

Contraparte automatizada (no reemplaza el ojo): `scripts/preflight/run_umg_e2e.py`
y `tests/test_pipeline_umg_smoke.py`.

---

## 4. Test de 3 jobs concurrentes

```bash
cd lyricgen/backend && \
  PREFLIGHT_USERNAME=<user> PREFLIGHT_PASSWORD=<pass> \
  ./venv/bin/python -m scripts.preflight.run \
  --only concurrency --api-url https://<staging-api> \
  --concurrency-n 3 --concurrency-mp3 /ruta/a/cancion.mp3
```

Mirar que los 3 drenen en paralelo sobre las 7 réplicas (no en serie). Con
7 workers, 3 transcripciones + 3 renders concurrentes entran holgados.

---

## 5. Hotfix / rollback

### Aplicar un hotfix
Ya existe el worktree `VideoLyricsIA-prodfix` (rama `staging`):

```bash
cd /Users/tomi/VideoLyricsIA-prodfix
# ...hacer el fix...
make check                      # gate rápido (ruff F821 + i18n + build)
git commit -am "fix: ..." && git push   # → Railway deploya staging
# verificar en staging.app.genly.pro, luego promover a main:
gh pr create --base main ... && # (merge tras CI verde)
```

El flujo de deploy: push a `staging` → Railway deploya staging; merge a
`main` → deploya prod. Vercel autodeploya el frontend por rama. El release
command de la API corre `bash scripts/prod_migrate.sh` (`alembic upgrade head`).

### Rollback rápido (sin tocar código)
- **Railway**: dashboard → servicio → Deployments → "Redeploy" sobre un
  build anterior exitoso. Instantáneo.
  - ⚠️ Si el deploy a revertir corrió una **migración Alembic**, el
    rollback de imagen NO la revierte. Si hay que bajar el schema:
    `alembic downgrade -1` (con cuidado — ver `RUNBOOK_EMERGENCY.md`).
- **Vercel**: dashboard → Deployments → "Promote" el deploy previo.
- **Emergencia**: revertir el último commit y pushear:
  `git revert HEAD --no-edit && git push`.

### Jobs trabados
Ver `RUNBOOK_EMERGENCY.md` → "Si los videos se quedan trabados". El reaper
los limpia a los 100-180 min, pero para forzarlo: Railway → Worker →
Restart, o marcar como error vía SQL (query en el emergency runbook).

---

## 6. Escalación

| Severidad | Definición | Acción |
|---|---|---|
| **SEV1** | Prod caído o **leak de datos entre cuentas** | Página al operador de guardia inmediatamente; rollback ya |
| **SEV2** | Degradado (renders lentos, una cola estancada) | Investigar en vivo; avisar al cliente si afecta la demo |
| **SEV3** | Cosmético (badge mal, typo) | Anotar, arreglar post-launch |

- **Contacto cliente UMG**: ver `uptime_ping.py` (referencia "Santi").
- **Comms de outage al cliente**: plantilla en `docs/CLIENT_COMMS_OUTAGE.md`.
- Un leak cross-tenant es **SEV1 siempre** — es incidente de confianza, no
  un bug. Si el pen-test de §1 alguna vez da rojo, **no se lanza**.

---

## Fuera de alcance (follow-ups conocidos)

- **Audit de descargas del portal UMG** (`/api/deliveries/items`): hoy las
  descargas van directo a R2 vía URL firmada minteada en el listado, que se
  pollea — auditarlo ingenuamente inundaría la tabla. La solución correcta
  es un endpoint de descarga per-click que audite ahí. Es feature, no
  hardening; queda como follow-up.
