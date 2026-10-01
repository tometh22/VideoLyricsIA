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

Repetido el 2026-10-01 con un esquema recién copiado y el head definitivo
(`ba888d1665d8`, staging 1.1.90): 11 revisiones aplicadas, 0 errores en la segunda
corrida, `require_api_schema.py` y `require_worker_schema.py` satisfechos.

Límites: sin datos, no prueba migraciones de relleno sobre filas reales (las de
`deliveries` ya se aplicaron a mano en la base compartida y son idempotentes).
**Volver a repetir el ensayo justo antes de promover** si cambia el head o el esquema de producción.

### Auditoría de que las migraciones pendientes son aditivas (1-oct)

Revisadas las 11 revisiones pendientes (`a7b9c1d3e5f7` … `ba888d1665d8`):
ninguna borra tablas, columnas ni índices, ni cambia tipos. Las únicas operaciones
que no son un simple agregado:

- `b8c0d2e4f6a8` (`delivery_change_requests.updated_at`): relleno de una vez y `NOT NULL` con `DEFAULT CURRENT_TIMESTAMP`; en producción ya está aplicada a mano, así que no cambia nada. El código viejo puede seguir insertando filas gracias al valor por defecto.
- `b4c6d8e0f2a4` (`deliveries.published_revision … DEFAULT 1 NOT NULL`): con valor por defecto, el código viejo sigue insertando.
- `e6a8c0d2f4b6` y `ef6a7b8c9d01`: tablas nuevas con sus `NOT NULL`; el código viejo no las toca.

Conclusión: el código anterior (`397965aa`) ignora todo lo nuevo, por lo que **volver atrás
el código no requiere deshacer migraciones**. Las columnas y tablas nuevas quedan inertes.

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

**Verificación de defaults (1-oct, 589 variables que lee staging contra 517 de la línea de producción):**
74 variables son nuevas y ninguna está definida en Railway producción. Los valores por defecto que
**se encienden solos** al promover son `ANCHOR_LOCAL_ALIGN_ENABLED` (cuarto alineador),
`DELIVERY_QC_UMG_OCR_ENABLED`, `LYRIC_REVIEW_FETCH_OFFICIAL` y `REANCHOR_CRAMMED_GUARD`; staging corre esos
mismos defaults, así que están probados. `LYRIC_REVIEW_MODE` tiene default `enforce` en el código y
staging usa `observe` (efectivo solo en trabajos de campaña masiva o `LYRIC_REVIEW_ENFORCE_TENANTS`):
**fijarlo en `observe`** en producción para igualar a staging.

**Dos bloqueos de aprobación nuevos, que la línea de producción no tiene** (no existen `delivery_readiness_gate`
ni `language_review.py` allí): (1) cualquier trabajo "de entrega UMG" (perfil `umg`/`both`, `umg_spec`, ProRes
listo, archivos maestros UMG o clase `batch`) exige preflight COMPLETO y vigente más revisión manual para
aprobar o publicar; (2) `409 language_review_unresolved` al aprobar la letra con una discrepancia de idioma
sin resolver. Para conservar el comportamiento actual de producción hasta rediseñar el control de calidad,
**fijar en producción**: `DELIVERY_QC_GATES_OFF=1` y `LANGUAGE_REVIEW_ADVISORY=1` (1.1.91). Quedan visibles en
`/health` (`features.delivery_qc_gates_off`, `features.language_review_advisory`). Los reportes se siguen
generando; solo dejan de bloquear. Para activar los bloqueos más adelante, borrar esas variables.

**Con valor distinto (producción → staging). Decide el dueño; propuesta entre paréntesis:**

| Variable | Producción | Staging | Propuesta |
|---|---|---|---|
| `LYRIC_LEAD_IN_S` | 0.4 | 0.08 | **DECIDIDO (1-oct): 0.08**, aplicar al promover (adelanto de la letra en pantalla; es lo que UMG aprobó, programa de paridad con Rotor) |
| `LYRIC_HOLD_S` | 0.25 | 0.5 | **DECIDIDO (1-oct): 0.5**, junto con la anterior. Solo afecta videos nuevos; avisar al equipo del cambio visual |
| `BACKGROUND_SMOKE_POLICY_MODE` | enforce | shadow | mantener `enforce` |
| `TRANSCRIBE_JOB_TIMEOUT` | 2700 | 1800 | mantener 2700 |
| `TARGETED_CONSENSUS_DEADLINE_SECONDS` / `_MAX_WINDOWS` / `_MAX_BILLED_SECONDS` | 120 / 3 / 180 | 480 / 64 / 300 | mantener los de producción (acotan costo) |
| `REAPER_THRESHOLD_MIN` / `REAPER_TRANSCRIBED_PENDING_TTL_MIN` | 600 / 1440 | 180 / 60 | mantener los de producción |
| `DB_POOL_SIZE` / `DB_MAX_OVERFLOW` | 8 / 8 | 4 / 2 | mantener |
| `ENV`, `ENVIRONMENT`, URLs, claves | propias de cada entorno | | no tocar |

Un valor fijado en Railway puede diferir del default del código: antes de decidir una
variable, leer `railway variables --kv` y el default en el código.

## 6. Runbook de promoción

Datos reales de Railway (proyecto "Genly IA", entorno `production` = `1a3e2a24-f806-4c03-bfbd-648e393df0b5`):
cada servicio tiene un *deployment trigger* que apunta a `tometh22/umg-chile-portal` **sin** "Wait for CI"
(`checkSuites=false`): cambiar la rama despliega **de inmediato y a los cuatro servicios a la vez**.

| Servicio | trigger id | deployment actual (a restaurar) | commit |
|---|---|---|---|
| api | `c6aa5639-99cc-420b-8816-a7d164aba993` | `a4a6b227-3bef-422e-a9c2-9d9f4738b34c` | `397965aa` |
| Worker | `b302fc82-d65d-45a9-b934-977159db27d5` | `2f62c483-116f-46fb-82e8-116007951f61` | `397965aa` |
| ShortWorker | `bf38f9f0-82f6-4693-88dd-1b566d952869` | `82e1d8c7-f65e-480c-8e0d-e16c2dafad6d` | `397965aa` |
| quality-worker | `7c9f699b-cddb-4f53-84f3-d260233648d1` | `55e34617-de3c-46c0-89bd-400cc3ccd6fc` | `397965aa` |

Precondiciones (todas verdes antes de empezar):
1. Los PRs de alineación mergeados a staging, con CI exacta verde y staging sin trabajo a medias.
2. Ensayo de migraciones repetido si cambió el head (sección 3).
3. Tabla de variables (sección 5) firmada por el dueño. Las variables a agregar o cambiar quedan **preparadas** pero se aplican en el paso 3.
4. La rama `tometh22/umg-chile-portal` **no se borra ni se mueve** (es el punto de retorno); anotar su commit `397965aa`.
5. Ventana de baja carga elegida, con alguien mirando.

Pasos (el orden evita un doble deploy):
1. **Llevar `main` al estado de staging** con un PR `staging` → `main` (solo con el "esto va a producción"). Mientras ningún servicio apunte a `main`, esto no despliega nada.
2. Confirmar que el PR dejó `main` idéntico a staging (`git diff origin/staging origin/main` vacío).
3. Aplicar las variables aprobadas a producción (sección 5) **sin redeploy** (`railway variables --set ... --skip-deploys`). Incluye `LYRIC_LEAD_IN_S=0.08`, `LYRIC_HOLD_S=0.5`, `LYRIC_REVIEW_MODE=observe`, `DELIVERY_QC_GATES_OFF=1` y `LANGUAGE_REVIEW_ADVISORY=1` (en los servicios que las leen: api y workers).
4. Apuntar los cuatro triggers a `main` (mutación `deploymentTriggerUpdate(id, input: {branch: "main"})` o el panel: Settings → Source → Branch). Se dispara **un solo** deploy por servicio con el estado final.
5. Seguir el deploy: la api migra antes de arrancar (`require_api_schema`); los workers esperan el esquema (`require_worker_schema`) y arrancan solos.
6. Verificar el backend: `/health` de `api.genly.pro` en `ok`, flota coherente, todos los workers en el commit nuevo, `features.delivery_qc_gates_off` y `features.language_review_advisory` en `true`; smoke de edición; portal de UMG (listado, descarga, pedido de cambio) en ambos dominios; Sentry sin errores nuevos.
7. **Frontend (Vercel)**: genly.pro lo sirve Vercel y su rama de Producción también apunta hoy a `tometh22/umg-chile-portal` (último deploy `397965aa`). Recién con el backend verificado, cambiar la rama de Producción del proyecto Vercel a `main` y redeployar. Orden: el backend nuevo tolera al frontend viejo; lo contrario no está garantizado.
8. Vigilar 24–48 h: salud, colas, errores, costos y la tasa de errores del portal.

**Criterios de aborto** (volver atrás sin discutir): `/health` no ok pasados 15 minutos, flota no coherente,
portal de UMG con errores, o error de migración.

**Rollback (en este orden de preferencia):**
0. *Frontend:* en Vercel, volver la rama de Producción a `tometh22/umg-chile-portal` y redeployar, o promover el deployment anterior (`397965aa`).
1. *Rápido, por imagen:* `deploymentRollback(id)` con los ids de la tabla de arriba (api primero, luego workers). Puede no estar disponible cuando Railway ya retiró el deployment antiguo.
2. *Seguro, por rama:* `deploymentTriggerUpdate(id, input: {branch: "tometh22/umg-chile-portal"})` en los cuatro triggers y esperar el build (~10–15 min). Funciona siempre que la rama siga en `397965aa`.
3. Las variables nuevas (`LYRIC_*`) se revierten con `railway variables --set LYRIC_LEAD_IN_S=0.4 LYRIC_HOLD_S=0.25`.
4. No se deshacen migraciones: son aditivas y el código anterior las ignora (sección 3).

## 7. Para que no vuelva a divergir

- Una rama por entorno: `main` = producción, `staging` = pre-producción. Archivar `tometh22/umg-chile-portal` y las ramas `release/*` viejas.
- Un hotfix va primero a `main` y se trae a `staging` el mismo día.
- Problema de fondo, aparte de esta promoción: el trabajo gestionado de UMG corre en staging y escribe en la base de producción. Darle a staging su propia base de entregas, o asumir ese trabajo en producción.

## 8. Promoción real del 2026-10-01 (staging 1.1.91 → producción) y lecciones

Resultado: producción quedó en `main` = `5ac8ce05` (árbol idéntico a staging), `/health` en `ok`,
11 workers coherentes, migración `ba888d1665d8` aplicada, interruptores `delivery_qc_gates_off` y
`language_review_advisory` en `true`, portales de UMG respondiendo (Argentina 185 versiones y Chile 34, todas con
archivos) y logs sin errores. La rama `tometh22/umg-chile-portal` (`397965aa`) **no se tocó**: es el punto de retorno.

Lo que el runbook no decía y hay que saber la próxima vez:

1. **Vercel despliega el frontend de producción al mergear a `main`.** Su rama de Producción es `main`: el merge
   del PR a `main` publicó el frontend ~10 minutos **antes** que el backend. No hay paso separado de Vercel.
   Para que el backend vaya primero, repuntar los disparadores de Railway a `staging` (mismo árbol), verificar, y
   recién entonces mergear a `main`. En esta promoción la ventana (frontend nuevo con backend viejo) no produjo
   errores observados.
2. **Cambiar la rama de un disparador de Railway no despliega nada**: hay que lanzar el deploy
   (`railway redeploy --service S --environment production --from-source -y`). Los cuatro servicios terminaron en ~6 min.
3. **`/health` marca "down" (503) durante un deploy escalonado** hasta que toda la flota queda en el commit nuevo y
   con la misma configuración. En esta promoción fueron ~14 min, de los cuales ~10 por un error mío (punto 4).
4. **`timing_config_mismatch`:** el chequeo exige que los tiempos de letra (`LYRIC_LEAD_IN_S`, `LYRIC_HOLD_S`) sean
   idénticos en TODOS los servicios, también en `quality-worker`. Hay que fijarlos en los cuatro de una vez.
5. **`fleet_runtime_token_mismatch` (degradado, no bloquea):** en producción los servicios tienen configuraciones de
   pipeline distintas entre sí (el `quality-worker` no define 15 variables que `api`/`Worker`/`ShortWorker` sí;
   `CTC_ALIGN_MIN_MED_SCORE` está en `api` y `ShortWorker` pero no en `Worker`; `QUALITY_V6_*` solo en `api` y
   `quality-worker`). Ya existía; el código nuevo lo hace visible. Alinearlas cambia comportamiento de alineado:
   decidirlo aparte, no con prisa. En staging el token es único.
6. **Errores de infraestructura de Railway** (`failed to fetch snapshot` al construir) dejan un servicio atrás:
   reintentar con `railway redeploy --service S --environment E --from-source -y`.
7. Corrección a la sección 5: producción **ya** usaba `LYRIC_LEAD_IN_S=0.08` en `Worker` y `ShortWorker` (los que
   renderizan); solo el `api` tenía 0.4. El cambio real fue `LYRIC_HOLD_S` 0.25 → 0.5.
