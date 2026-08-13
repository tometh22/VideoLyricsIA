# Auditoría pre-lanzamiento — Universal Music Argentina

**Fecha:** 2026-05-21 · **Go-live:** martes 2026-05-26
**Modo:** revisión profunda, solo detectar y reportar (sin cambios de código)
**Alcance día 1:** generar videos lyric · entrega ProRes/UHD · aprobación/compliance
**Probado contra:** staging Railway (`api-staging-9b82.up.railway.app`) + verificación de salud de producción (`api.genly.pro`)

---

## Veredicto

**No encontré bloqueantes de código ni de infraestructura.** Backend, pipeline, base de datos, cola y storage están sanos **tanto en staging como en producción**. El código está en buen estado (708/716 tests pasan; las 8 fallas son de los propios tests, no del producto). El gating de ProRes/compliance está bien implementado.

**El riesgo del martes NO es el código — es config y provisión.** Todo lo que puede salir mal son variables de entorno y datos de la cuenta UMG que solo vos podés confirmar en los dashboards (Railway/Vercel) y en `/admin`. Yo no tengo acceso a esos paneles ni a una cuenta UMG, así que esos puntos quedan como checklist verificable.

### Dos falsas alarmas descartadas (de un sondeo inicial)
- **"Secretos en git":** FALSO. Solo `.env.example` está trackeado. `.env`/`.env.local` están gitignoreados y NO en el repo.
- **`BENCHMARK_REPORT.md` "Do not ship":** NO es del producto. Compara un experimento de alineación de lyrics ("tier-1") contra el baseline ya en prod; significa "no actives tier-1, quedate con el baseline actual". No afecta el lanzamiento.

---

## Estado de salud confirmado en vivo

| Componente | Staging | **Producción (`api.genly.pro`)** |
|---|---|---|
| API / `/health` | ok | **ok (`env: production`)** |
| PostgreSQL | up | **up** |
| Redis | up | **up** |
| Workers RQ | 7 vivos | **7 vivos** |
| R2 (storage entregables) | ready (145ms) | **ready (125ms)** |
| OpenAI / Vertex / Gemini keys | ✓ / ✓ / ✓ | **✓ / ✓ / ✓** |
| Disco libre | 2738 GB | **1053 GB** |
| Cola enterprise/default | 0 / 0 | **0 / 0** |

Verificado además en staging vía API:
- Registro + login + `/auth/me` funcionan.
- Upload presignado a R2 funciona (`POST /upload-url` devuelve URL firmada, key tenant-scoped `inputs/<tenant>/...`, bucket `genly-deliverables`).
- Migraciones Alembic corren limpias contra DB nueva (7 migraciones, exit 0).
- Gating ProRes correcto: cuenta no-UMG tiene `features.prores_export=false`; el código rechaza con 403 cualquier `delivery_profile=umg/both` sin acceso (`main.py:1983`).

---

## 🔴 Bloqueantes día 1 — VERIFICAR EN PRODUCCIÓN (solo vos)

Estos no son bugs; son condiciones de config/provisión que, si faltan en **prod**, rompen un crítico día 1. Probé staging, pero **prod es un servicio Railway y un proyecto Vercel distintos** — staging verde NO garantiza prod.

### B1. `PRORES_TENANTS` debe incluir `umg` en prod (API **y** Worker)
Sin esto, hasta la cuenta UMG recibe **403 "Broadcast (ProRes) delivery is not enabled"** al pedir el máster ProRes/UHD — un crítico día 1.
**Verificar:** Railway → servicios `api` y `Worker` → Variables → `PRORES_TENANTS` contiene `umg` (ej. `PRORES_TENANTS=umg`).
**Repro del gating:** `main.py:1983` (`_parse_umg_params` → `has_prores_access`), `auth.py:50-72`.

### B2. Cuenta UMG provisionada con `tenant_id=umg`, `ai_authorized=true` y plan correcto
- `ai_authorized=true` es obligatorio: sin él, `/generate` devuelve **403 "AI tool usage not authorized"** (`main.py:2713`). Se setea vía `POST /admin/users/{id}/authorize-ai`.
- `tenant_id=umg` → habilita ProRes (junto con B1) y la **cola enterprise** (prioridad sobre el resto).
- Plan: en `/plans` **no existe "unlimited"**. El trato ($8/video × ~200/mes) calza con el tier **`250`** ($8/video, límite 250). Confirmar qué plan tiene la cuenta y `allow_overage` según se necesite pasar el tope.
**Verificar:** `GET /admin/users` → revisar la fila de UMG (`tenant_id`, `ai_authorized`, `plan`, `max_videos_per_day`, `max_concurrent_jobs`).

### B3. `RAILWAY_SHUTDOWN_TIMEOUT_SECONDS=1200` en el servicio **Worker** de prod
El propio `railway.toml` (líneas 25-38) documenta un **incidente del 2026-05-15**: sin esta var, el grace de 30s de Railway mata renders en vuelo (15-20 min) en **cada deploy**. No se puede setear desde el archivo; es paso de dashboard.
**Verificar:** Railway → servicio `Worker` → Variables → `RAILWAY_SHUTDOWN_TIMEOUT_SECONDS=1200`. Y **no deployar** durante una tanda de renders de UMG.

---

## 🟠 Riesgos altos — confirmar antes del martes

### A1. Compliance guideline 1 en estado "pending" (acción legal, no técnica)
`/compliance/status` reporta: *"ACTION REQUIRED: Confirmar con UMG que tu contrato Vertex AI enterprise califica como el acuerdo enterprise-level requerido para Google Veo."* Las guidelines 5 (autorización) y 6 (uso limitado) están "ok". **Cerrar esto con el contacto de UMG** para evitar una objeción de compliance el día 1.

### A2. Re-verificar config de prod que no pude inspeccionar con auth
Staging tiene todo, pero confirmá en **prod** (Railway → `api`/`Worker`): `JWT_SECRET` fuerte (no el placeholder), `REQUIRE_REVIEW` según el flujo de aprobación que querés, `CORS_ORIGINS`/regex incluye `app.genly.pro` y `umg.genly.pro`, `BREVO_API_KEY` (reset de contraseña / mails — medio).

### A3. Tenant nuevo ve 0 fondos de biblioteca
Confirmado en vivo: `GET /backgrounds` devuelve `[]` para un tenant fresco (la biblioteca de ~257 assets no es global para él). Si UMG entra a "Modo → Biblioteca" verá el mensaje *"No pre-approved backgrounds available"*. **No es bloqueante** porque el modo por defecto es "Generar con IA". Decidí: o le provisionás fondos al tenant `umg` (vía `/admin/backgrounds` + `/admin/background-tenants`), o le avisás que el camino día 1 es "Generar con IA".

### A4. Smoke test real con la cuenta UMG (lo único que no pude hacer)
No corrí un render Veo completo (cuesta plata + 15 min, y mi cuenta free no puede tocar ProRes). **Antes del martes, hacé 1 render end-to-end con la cuenta UMG real**: subir audio → transcribir → generar perfil UMG (UHD-4K + ProRes) → aprobar → descargar el `.mov` → confirmar que aparece en `umg.genly.pro`. Es la última validación que falta y exige la cuenta real.

---

## 🟡 Menores / post-launch (no bloquean)

- **Suite de tests con drift:** 8 fallas locales, todas de los tests (alembic sin PATH en subproceso; fixtures que insertan `jobs.user_id=NULL`; aserciones de reaper que buscan texto viejo "abandonó"; render ASS dependiente de ffmpeg local). CI con Postgres probablemente las pasa. Conviene limpiarlas post-launch para que la suite siga siendo confiable. Archivos: `tests/test_prod_readiness.py`, `tests/test_edit_race.py`, `tests/test_reaper.py`, `tests/test_retry_reset.py`, `tests/test_ass_integration.py`.
- **i18n `en`/`pt` incompletos** (faltan ~muchas claves de wizard/variant/validation). El base **español está completo**, así que para UMG (Argentina) no impacta. `npm run check:i18n` pasa porque ninguna clave falta en TODOS los idiomas.
- **`check:fetch` reporta 4 "POST sin res.ok":** revisados, son **falsos positivos** — `EditRequestPanel.jsx` (339/356/955) y `JobDetail.jsx` (217) sí manejan el error (vía valor de retorno o `if(!res.ok)`). El linter no reconoce esos patrones. Sin acción.
- **502 en upload de WAV grande (30-50MB) en conexión lenta:** `r2Upload.js` reintenta, pero un 502 de edge cae en un panel con botón "Reintentar" manual. Aceptable; documentarlo en ayuda al cliente.
- **`db_pool.overflow_open: -3`** (negativo) en `/health` de prod: artefacto de contador, utilización 0. Vigilar, no bloquea.
- **Landing usa `mailto` en vez de `/api/leads`** (el endpoint existe en backend). Irrelevante para UMG.
- **Limpieza:** dejé una cuenta de prueba en staging (`qaaudit1779415180` / `qa-audit-1779415180@epical.digital`). Borrala cuando quieras vía `/admin/users`.

---

## Checklist go-live (martes)

**Solo vos (dashboards / admin) — bloqueantes:**
- [ ] `PRORES_TENANTS` incluye `umg` en prod (servicios `api` **y** `Worker`)
- [ ] Cuenta UMG: `tenant_id=umg`, `ai_authorized=true`, plan `250` (o el acordado), `allow_overage` y caps razonables
- [ ] `RAILWAY_SHUTDOWN_TIMEOUT_SECONDS=1200` en `Worker` de prod; no deployar durante renders
- [ ] 1 smoke test real con la cuenta UMG: audio → ProRes UHD → aprobar → descargar `.mov` → verlo en `umg.genly.pro`

**Confirmaciones (altas):**
- [ ] Cerrar guideline 1 (contrato Vertex enterprise) con UMG
- [ ] Prod: `JWT_SECRET` fuerte, `REQUIRE_REVIEW`, `CORS_ORIGINS`, `BREVO_API_KEY`
- [ ] Decidir fondos para tenant `umg` o avisar "Generar con IA" como camino día 1

**Verificable por código (ya OK):** salud de infra, migraciones, gating ProRes/IA, upload R2, auth, error handling de UI.
