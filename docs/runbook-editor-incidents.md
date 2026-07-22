# Runbook — Incidentes del editor de lyrics

Guía operativa para triagear reportes tipo *"no se guardan mis cambios"*,
*"se congela el editor"*, *"se borraron los tiempos"* (clase de incidentes
UMG, ver issue #934). Actualizado: 2026-07-21.

## 1. La verdad fundamental: "Aprobar y generar" manda LO DE PANTALLA

Verificado en código y en producción (incidente Seba / "Amiga Mía",
21-jul-2026): al aprobar, los segments **en pantalla** viajan en el body del
POST (`/generate` con `segments_json`, o `/edit` con `edit_type=lyrics`) y el
backend pisa `segments_json` **antes** de encolar el render.

**Consecuencia operativa:** un autosave fallido NO afecta el render. Si el
operador tiene los tiempos bien en pantalla, que apruebe — sale con eso.
El autosave solo alimenta el **respaldo** (reanudar tras refresh, ancla del
reaper). Receta para el operador:

> Dejá los tiempos bien en pantalla → "Aprobar y generar" → si aparece el
> cartel de "no se pudo respaldar", continuá igual.

## 2. Tags de Sentry (frontend) — qué significa cada uno

El frontend manda a Sentry todo `console.warn` con tag `[...]`
(`observability.js`; throttle 1/tag/min, fingerprint por tag). Los
**freeze-tags** escalan a `error` y graban **Session Replay**:

| Tag | Significado | Acción |
|---|---|---|
| `[reseed-storm]` | Filas del editor re-montando en loop (≥6/s) — freeze | Mirar el replay; canción larga suele ser el disparador. Root cause en curso (plan maestro, tanda 2). |
| `[ui-freeze]` / `[ui-longtask-burst]` | Main thread bloqueado en el editor | Ídem: replay + nº de líneas del job. |
| `[editor-reload-loop]` | El editor recarga en loop | Replay. |
| `[autosave]` | POST `/save-segments` falló (red, 4xx/5xx) | **Señal real, no mutear.** Cruzar con el log backend (abajo). |
| `[drag-persist]` | Diagnóstico del drag del timeline | Se retira junto con el fix del reseed-storm. |

**Alert rule recomendada (configurar en Sentry UI, no en código):**
issue alert sobre eventos `level:error` con tag de mensaje
`reseed-storm|ui-freeze|editor-reload-loop` y `user.tenant_id` que empiece
con `universal` → notificar (email/Telegram). Así el freeze de un operador
UMG avisa ANTES del WhatsApp.

## 3. Métrica backend del autosave

`POST /jobs/{id}/save-segments` loguea outcome estructurado:

- Éxito: `[save-segments] ok job=… tenant=… count=…` (INFO)
- Bloqueo por status: `[save-segments] rejected outcome=409-status job=… tenant=… status=…` (WARNING)

En Railway: filtrar logs del servicio api por `[save-segments]` para medir
tasa de éxito por tenant. Un `409-status` recurrente = el editor está
montado en un estado que el endpoint no acepta (p.ej. `editing` mientras
renderiza un edit) — no es un bug de red del operador.

## 4. Soporte: entrar a arreglar el job de un cliente (super admin)

Desde PR #933 (en prod), los admins de plataforma pueden **abrir, editar,
guardar y re-renderizar** jobs de cualquier tenant desde la UI
(`/videos/{id}/edit-lyrics`). Todo acceso cross-tenant queda en `AuditLog`
(`admin.cross_tenant_access`). Si la UI no alcanza, la vía DB directa:

```bash
railway run --environment production --service Postgres -- bash -lc \
  'psql "$DATABASE_PUBLIC_URL" -x -c "SELECT job_id,status,tenant_id,edit_count,
   jsonb_array_length(segments_json) FROM jobs WHERE job_id='"'"'<JOB>'"'"';"'
```

- `status=editing` + `last_progress_at` avanzando = HAY un render en curso:
  **no tocar nada** hasta que termine (pisa el trabajo en vuelo).
- La provenance del fondo (qué prompt generó qué): tabla `ai_provenance`
  (`prompt_sent`, `tool_name`, por `job_id`).

## 5. Diagnóstico exprés por síntoma

| Síntoma | Causa probable | Verificación |
|---|---|---|
| "No se guardan los tiempos" + banner rojo | Autosave fallando (red o 409-status) | Log backend `[save-segments]` + Sentry `[autosave]`. Recordar §1: aprobar igual funciona. |
| "Volví a otro paso y se borraron" | Editor desmontado antes del flush (bug conocido, tanda 2 del plan) | Reproducir: step 6 → step 4 → volver. |
| "Se congela con canciones largas" | reseed-storm (59+ líneas) | Sentry replay del tag. |
| "El preview no es igual al render" | Paridad JS↔Python | Correr `renderParity` tests; si verde, comparar goldens (`golden-render.yml`). |
| "El fondo IA salió distinto otra vez" | No-convergencia por diseño (Gemini re-interpreta + cache key por artista+título) | Ver prompts reales en `ai_provenance`. Salida: fondo de biblioteca vía `edit_type=background_library` (PR #940). |

## 6. Referencias

- Issue #934 — autosave poco confiable (tracking del plan del editor).
- PR #933 — super admin cross-tenant (prod). PRs #936–#940 — plan maestro tanda 1/2/3.
- `.github/workflows/edit-smoke.yml` — GO/NO-GO post-deploy del camino edit.
- `.github/workflows/golden-render.yml` — regresión visual (re-bless con `--bless`).
