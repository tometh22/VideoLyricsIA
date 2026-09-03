# Status page

> Cuando hay outage, los clientes van a buscar dónde verificar si es
> ellos o nosotros. Sin status page, asumen que es su problema y nos
> escriben. Con una status page pública ven la luz roja directo y saben
> que estamos al tanto.

## Estado actual: la página propia YA EXISTE

Desde sep-2026 hay una página de status **propia**, en `/status` del
frontend. No hace falta contratar nada para tenerla.

| | Página propia (`/status`) | BetterStack (abajo) |
|---|---|---|
| Incidentes redactados a mano | ✅ desde Admin → Ahora → Estado público | ✅ |
| Barra de aviso en la home del producto | ✅ | ❌ |
| Detecta caídas sola | ✅ (sondas internas de `/health`) | ✅ |
| Sirve si se cae **todo** (Vercel + Railway) | ❌ | ✅ |
| Manda el mail cuando prod no contesta | ❌ | ✅ (y `uptime.yml` ya lo hace) |

Las dos se complementan y no compiten: **una página propia no puede
reportar su propia caída total.** El backstop para ese caso es la sonda
externa —hoy `.github/workflows/uptime.yml`, que corre en infra de GitHub
cada 5 min y manda mail— o BetterStack si algún día se quiere sub-minuto
multi-región.

### Cómo funciona la propia

- **Frontend:** `src/components/StatusPage.jsx` (ruta pública `/status`,
  sin JWT) + `src/components/ServiceStatusBanner.jsx` (la barra de la home
  y de la landing) + `src/hooks/useServiceStatusSummary.js` (un solo poll
  compartido, cada 60 s).
- **Backend:** `backend/status_page.py`. Endpoints públicos bajo
  `/service-status` (el prefijo NO es `/status` porque `/status/{job_id}`
  ya existe para el polling de un job) y de admin bajo `/admin/status`.
- **Dos fuentes independientes.** El relato humano (`status_incidents` +
  `status_incident_updates`) y la sonda (`derive_components` sobre
  `health_snapshot()`). El indicador general es el peor de los dos.
- **Lo que la sonda NO publica:** el skew de release entre API y workers
  (`mixed_worker_releases`, `worker_fleet_incoherent`) y `disk_low`. Pasan
  en cada deploy y no cambian nada para el usuario; publicarlos dejaría la
  página amarilla después de cada merge.
- **Barras de 90 días.** Se calculan sobre tramos OBSERVADOS
  (`status_component_events`). Un día sin observaciones sale **gris, no
  verde**, y el `uptime_pct` viene siempre con su `coverage_pct`. Las
  observaciones las genera cualquier visita a `/service-status/*` más el
  latido de 5 min de `uptime.yml` (`_heartbeat_status_page`): si ese
  workflow se apaga, las barras se vuelven grises — que es la respuesta
  correcta, no un bug.

### Publicar un incidente

Admin → **Ahora** → **Estado público**. Se redacta título + primera
actualización, se marcan los servicios afectados y el impacto, y hay
preview del texto exacto que va a ver el cliente antes de publicar.

Reglas que impone el backend, no la UI:
- El timeline es **append-only**. No hay endpoint para editar una entrada
  publicada; corregir = publicar otra.
- Cambiar el estado (investigando → identificado → resuelto) **exige**
  texto: no se puede dejar la página en "En observación" sin contar qué
  se arregló.
- `banner` es independiente de `status`: se puede tener un incidente
  publicado en `/status` sin barra en la home (mantenimiento anunciado),
  y `public=false` conserva el registro interno sin publicarlo.
- Resolver apaga la barra. Re-resolver no mueve el `resolved_at`
  original, que es la ventana que usa el historial de uptime.

### Config

| Env var | Default | Para qué |
|---|---|---|
| `STATUS_AUTO_BANNER_MIN` | `partial_outage` | Estado mínimo de la sonda para que la barra aparezca SOLA, sin incidente redactado. En `degraded` un backlog de cola pintaría la home de amarillo. |
| `STATUS_TRANSCRIPTION_BACKLOG_DEGRADED` | `80` | Profundidad de cola de transcripción que se reporta como demoras. Arriba del lote típico de UMG (30-60) a propósito. |
| `STATUS_RENDER_BACKLOG_DEGRADED` | `80` | Idem para las colas de render. |

### Lo que NO tiene

- **Suscripción por email a las novedades** ("Subscribe to updates" de las
  páginas de OpenAI/Atlassian). Requiere tabla de suscriptores, doble
  opt-in y baja — no está construido y la página no lo ofrece, en vez de
  ofrecer un botón que no hace nada.
- **Dominio propio** `status.genly.pro`. Hoy la URL es `<app>/status`. Un
  CNAME a Vercel con la misma app la serviría, pero es una decisión de DNS
  aparte.

---

## Opción externa: BetterStack

Sigue siendo el camino recomendado para la sonda multi-región y el
backstop de la caída total. Nada de lo de abajo choca con la página
propia: son monitores externos.

## Provider elegido: BetterStack (free tier alcanza)

**Por qué BetterStack:**
- Free tier permite 10 monitors + status page público con dominio propio
- Ping cada 30 seg desde 7 regiones (NA, EU, Asia, SA) — detecta outage
  regional incluso si una región responde
- Integra con Slack/email automáticamente
- Genera transparencia que UMG va a valorar contractualmente

Alternativas si BetterStack no te cierra:
- UptimeRobot — más simple, free 50 monitors, ping cada 5 min (menos
  granular)
- Atlassian Statuspage — pro pero $29/mes mínimo

## Paso 1 — Crear cuenta y monitors

1. Ir a https://betterstack.com/uptime, crear cuenta (Google auth alcanza)
2. **Crear monitor 1: api prod**
   - URL: `https://genly-ai.up.railway.app/health`
   - Tipo: HTTP(s)
   - Expected status: 200
   - Expected response body contains: `"status":"ok"`
   - Check frequency: 30 sec
   - Alert after: 2 failures (= 1 min sostenido)
3. **Crear monitor 2: api staging**
   - URL: `https://api-staging-9b82.up.railway.app/health`
   - Mismo tipo, mismo body check
   - Alert after: 2 failures
4. **Crear monitor 3: frontend prod**
   - URL: `https://app.genly.pro/`
   - Expected: 200, body contains `"GenLy"`
   - Check frequency: 60 sec
5. **Crear monitor 4: r2 storage**
   - URL: `https://genly-ai.up.railway.app/health`
   - Body contains: `"r2":"ready"`
   - Indica que el bucket R2 está reachable

## Paso 2 — Configurar la status page

1. En BetterStack → Status pages → New status page
2. Subdomain: `genly` (te queda `genly.betteruptime.com`)
3. Custom domain: `status.genly.pro`
4. **Agregar componentes:**
   - "Generación de videos" → monitor "api prod"
   - "Almacenamiento de archivos" → monitor "r2 storage"
   - "Dashboard web" → monitor "frontend prod"
   - "Entorno de testing" → monitor "api staging" (oculto al público
     por default, visible solo para vos)
5. Página settings:
   - Public: ✓ ON
   - Show incident timeline: ✓ ON (transparencia = trust)
   - Show uptime % de últimos 90 días: ✓ ON
6. Branding:
   - Logo: subir el de Genly
   - Color: brand purple `#6D4AFF` (matchea el frontend)

## Paso 3 — DNS en GoDaddy

GoDaddy es donde están los nameservers (`ns71.domaincontrol.com`):

1. Login → Domains → `genly.pro` → DNS Management
2. Agregar registro:
   - Type: `CNAME`
   - Host: `status`
   - Points to: el target que BetterStack te muestra (algo como
     `monitors.betteruptime.com`)
   - TTL: 1 Hour
3. En BetterStack, "Verify domain". Tarda 5-15 min en propagar.
4. Probar: abrir `https://status.genly.pro` debería mostrar la página
   pública con tu logo.

## Paso 4 — Integraciones para que TE avise

1. BetterStack → Integrations → Slack → conectar workspace
2. Configurar el channel donde querés las alertas (sugerencia: `#genly-alerts`)
3. Severidad sugerida:
   - api/storage caído > 1 min → mensaje en Slack inmediato + email
   - frontend caído > 2 min → solo Slack
   - staging caído → solo email (no urgente, te enteras al día siguiente)

## Paso 5 — Linkear desde el frontend

YA HECHO para la página propia: el link vive en el footer de la landing y
en el punto de estado del pie del sidebar (que además refleja el estado
real — hasta sep-2026 estaba hardcodeado en verde y decía "Sistema
operativo" incluso durante una caída total).

Si algún día se contrata BetterStack con dominio propio, apuntar esos dos
links a `https://status.genly.pro` en vez de a la ruta interna.

## Verificación final

- [ ] `status.genly.pro` resuelve y muestra la página
- [ ] Los 4 monitors están verdes
- [ ] Forzar un fallo intencional (apagar staging temporalmente) → el
      Slack te avisa en < 90 seg
- [ ] La página muestra el incidente en el timeline
- [ ] Restaurar staging → la página vuelve a verde y registra el
      incidente como "resolved"

## Mantenimiento

- Una vez por mes revisar el uptime % y comparar contra SLA que pongas
  por escrito a UMG (sugerencia: 99.5% mensual = 3.6 hs de downtime/mes
  permitido sin penalidad)
- Si el uptime cae sostenido < 99% → priorizar multi-región o exit
  Railway (ver DEPLOY_RESILIENCE.md)
