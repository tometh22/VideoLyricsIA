# Promoción de staging a producción: estado, ensayo y runbook

Documento vivo. Fecha del relevamiento: 2026-10-01. Todo lo de abajo se verificó
contra Railway (API de deployments), el `/health` de producción, la base de
producción (solo lectura) y git. **Nada de esto despliega a producción**: la
promoción necesita el "esto va a producción" explícito del dueño.

## 1. Las tres líneas hoy

Railway **producción** (api, Worker, ShortWorker, quality-worker) despliega la rama
`tometh22/umg-chile-portal`, no `main`: commit `397965aa`, `VERSION` 1.0.5, 29-sep-2026.
`main` (`1f16df51`) no se mueve desde el 3-sep. El servicio Sentinel de producción
despliega `staging`. La base de producción está en la revisión Alembic `f3b5d7e9a1c3`.

| Línea | Qué es | Frente a staging |
|---|---|---|
| `staging` | integración | ~637 commits más que producción |
| `tometh22/umg-chile-portal` | lo que corre en producción | 23 commits que staging no tiene como parche |
| `main` | abandonada | 19 commits que ni producción ni staging tienen del todo |

## 2. Hotfixes que existían solo en producción o en `main`

Verificados commit por commit contra el comportamiento (no solo el parche):

| Origen | Qué hace | Resultado |
|---|---|---|
| prod `ace4063b` | listado del portal: sin bloquear el event loop, sin transacción abierta durante R2/Redis, tope duro de 12 s | **portado** (PR #1451) |
| prod `d8c45331` | `_reference_is_live` no puede quedar sin asignar | **portado** (#1451) |
| main #1218 | no reintentar (ni re-facturar) un fallo por deadline | **portado** (#1451) |
| main #1220 | el replay ASGI propaga la desconexión real (SSE colgado) | **portado** (#1451) |
| main #1219 | tiempo en cola vs inferencia en `ai_provenance` (+ migración `ba888d1665d8`) | **portado**, re-encadenada tras `e5a7c9b1d3f6` (#1451) |
| prod `366d75a0`, `db724f4a`, `eac89b19`, `b21b65aa`, `671f47d5`, `09a1151f`, `fbcfdd01`, `700c61b3` | preparación de ProRes desde el portal, archivos publicados fijos, revisión visible, atestación de letras de catálogo, plomería de CI | ya están en staging, iguales o mejores |
| main #1225/#1221, #1247, #1216, página de estado, avisos de seguridad | audio del editor, duplicar línea, presupuesto de Replicate, estado público | ya están en staging |

**No usar `git merge main` en staging**: auto-fusiona mal (duplica `StatusIncident*` en
`database.py`, repite `_decodes_ok` en `pipeline.py`, deja `_should_discard_retry` sin
llamar). Tampoco tomar el `a1c3e5f7b902` de `main` (distinto `down_revision`).

**Producción hoy no tiene** el arreglo de "duplicar una línea borra la letra" (#1247);
la promoción lo corrige.

## 3. Ensayo de migraciones (hecho)

Esquema de producción copiado (`pg_dump --schema-only`, sin datos) a una base
descartable y estampado en `f3b5d7e9a1c3`. Con el código de staging + #1451:

- `alembic upgrade head` corrió las 11 revisiones pendientes sin errores y quedó en `ba888d1665d8` (**un solo head**).
- Segunda corrida (idempotencia): sin cambios. `downgrade -1` y `upgrade`: OK.
- `require_api_schema.py` y `require_worker_schema.py`: satisfechos.
- Quedaron presentes `deliveries.client_visibility`, `deliveries.published_file_keys`, `ai_provenance.predict_time_ms/queue_time_ms`, `jobs.pilot_id` y la tabla `system_settings`.

Límites: sin datos, no prueba migraciones de relleno sobre filas reales (las de
`deliveries` ya se aplicaron a mano en la base compartida y son idempotentes).
Repetir el ensayo con el head definitivo justo antes de promover.

## 4. Topología de servicios

| Servicio | Producción | Staging |
|---|---|---|
| api | 2 réplicas | sí |
| Worker | 7 | sí |
| ShortWorker | 3 | sí |
| quality-worker | sí | sí |
| BatchWorker, BatchShortWorker (colas `campaign_control`, `transcription_batch`) | **no existen** | 2 + 2 |
| cost-collector | **no existe** | 1 |

Decisión por defecto: **no crear** los servicios de campañas en producción hasta
decidir activar campañas allí (el flag `BATCH_CAMPAIGN_ENABLED` no está en producción).
Verificar que `/health` de producción no marque como faltantes las colas de campañas.

## 5. Variables de entorno (servicio `api`)

Principio: **se promueve el código, no la configuración.** Producción mantiene sus
variables; cada funcionalidad nueva se enciende después, una a una, con verificación.
Producción tiene 159 variables y staging 221. Nunca copiar valores secretos entre entornos.

**Solo en producción (conservar; el código de staging las lee):**
`EDITOR_V2_GLOBALLY_ENABLED`, `ART_TRACK_GLOBALLY_ENABLED`, `TRANSCRIPTION_LANG_BY_TENANT`,
`YOUTUBE_PUBLISH_ENABLED`, `DEFAULT_DAILY_CAP`, `SUBMISSIONS_PAUSED`, `REAPER_ORPHAN_POLL_THRESHOLD_MIN`.

**Solo en staging, NO copiar a producción:**
`DELIVERY_QC_STAGING_GATES_OFF`, `DELIVERY_QC_UMG_STAGING_*`, `LANGUAGE_REVIEW_STAGING_ADVISORY`
(solo actúan con `ENVIRONMENT=staging`), `DELIVERIES_DATABASE_URL`, `PEER_DATABASE_URL`,
`RECONCILE_CAPTURE_*`, `REVIEWER_ASSIST_CAMPAIGN_ID`, `REVIEWER_TIMING_CAPTURE_EPOCH`,
`CORS_ORIGIN_REGEX`, `STRIPE_*` (claves de prueba; producción no tiene ninguna `STRIPE_*`:
**verificar cómo cobra hoy** antes de promover).

**Solo en staging, funcionalidades a encender en producción más tarde, con decisión explícita:**
`BATCH_*`, `CAMPAIGN_CHANGE_REQUESTS_ENABLED`, `CAMPAIGN_CHANGE_REQUEST_ACTIONS`,
`CHANGE_REQUEST_*`, `REVIEWER_*`, `QUALITY_*`, `LYRIC_REVIEW_MODE`, `DELIVERY_QC_MODE`,
`DELIVERY_QC_OCR_ENABLED`, `DELIVERY_QC_MEDIA_SCAN_ENABLED`, `DELIVERY_REPAIR_SHADOW_MODE`,
`ARTIST_LEXICON_RAG_ENABLED`, `BG_LYRIC_ANCHORS`, `REFERENCE_ATTESTATION_MODE`, `LORA_V1_*`.

**Solo en staging, candidatas a igualar (infraestructura y costo; revisar una por una):**
`VEO_MODEL` / `VEO_MODEL_STATIC` (`veo-3.1-lite-generate-001`, ~75 % menos costo), `DEMUCS_MAX_CONCURRENT`,
`DEMUCS_LEASE_TTL_S`, `DEMUCS_SLOT_WAIT_MAX_S`, `MAX_UPLOAD_MB`, `EXPECTED_WORKER_REPLICAS`,
`EXPECTED_SHORT_WORKER_REPLICAS` (ajustar a 7 y 3 si se activa el chequeo de flota).

**Con valor distinto (producción → staging). Decide el dueño; propuesta entre paréntesis:**

| Variable | Producción | Staging | Propuesta |
|---|---|---|---|
| `LYRIC_LEAD_IN_S` | 0.4 | 0.08 | decidir: es el adelanto de la letra en pantalla; staging valida 0.08 |
| `LYRIC_HOLD_S` | 0.25 | 0.5 | decidir junto con la anterior |
| `BACKGROUND_SMOKE_POLICY_MODE` | enforce | shadow | mantener `enforce` |
| `TRANSCRIBE_JOB_TIMEOUT` | 2700 | 1800 | mantener 2700 |
| `TARGETED_CONSENSUS_DEADLINE_SECONDS` / `_MAX_WINDOWS` / `_MAX_BILLED_SECONDS` | 120 / 3 / 180 | 480 / 64 / 300 | mantener los de producción (acotan costo) |
| `REAPER_THRESHOLD_MIN` / `REAPER_TRANSCRIBED_PENDING_TTL_MIN` | 600 / 1440 | 180 / 60 | mantener los de producción |
| `DB_POOL_SIZE` / `DB_MAX_OVERFLOW` | 8 / 8 | 4 / 2 | mantener |
| `ENV`, `ENVIRONMENT`, URLs, claves | propias de cada entorno | | no tocar |

Un valor fijado en Railway puede diferir del default del código: antes de decidir una
variable, leer `railway variables --kv` y el default en el código.

## 6. Runbook de promoción

Precondiciones (todas verdes antes de empezar):
1. #1451 y los demás PRs de alineación mergeados a staging, con CI exacta verde y staging sin trabajo a medias.
2. Ensayo de migraciones repetido con el head definitivo (sección 3).
3. Tabla de variables (sección 5) revisada y firmada por el dueño.
4. Id del deployment de producción actual anotado (`397965aa`) y comando de rollback probado.
5. Ventana de baja carga elegida, con alguien mirando.

Pasos:
1. Llevar `main` al estado de staging (PR `staging` → `main`, solo con el "esto va a producción").
2. Aplicar a producción solo las variables aprobadas (sección 5), sin redeploy.
3. Apuntar Railway producción (api, Worker, ShortWorker, quality-worker) a la rama `main`.
4. Dejar que el deploy corra Alembic (la api migra antes de arrancar; los workers esperan el esquema).
5. Verificar: `/health` = ok y flota coherente; todos los workers en el commit nuevo; smoke de edición; portal de UMG (listado, descarga, pedido de cambio) en ambos dominios; Sentry sin errores nuevos.
6. Vigilar 24–48 h: salud, colas, errores y costos.

**Criterios de aborto** (volver atrás sin discutir): `/health` no ok pasados 15 minutos, flota no coherente,
portal de UMG con errores, o error de migración.

**Rollback:** redeployar el deployment anterior de cada servicio (`397965aa`) en Railway y devolver la
rama a `tometh22/umg-chile-portal`. Las migraciones de esta promoción son solo aditivas (columnas
nulas y tablas nuevas), así que el código viejo ignora lo nuevo y la vuelta atrás es segura; verificarlo
revisando las revisiones pendientes antes de promover.

## 7. Para que no vuelva a divergir

- Una rama por entorno: `main` = producción, `staging` = pre-producción. Archivar `tometh22/umg-chile-portal` y las ramas `release/*` viejas.
- Un hotfix va primero a `main` y se trae a `staging` el mismo día.
- Problema de fondo, aparte de esta promoción: el trabajo gestionado de UMG corre en staging y escribe en la base de producción. Darle a staging su propia base de entregas, o asumir ese trabajo en producción.
