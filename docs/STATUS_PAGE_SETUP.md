# Status page público — setup en 10 min

> Cuando hay outage, los clientes van a buscar dónde verificar si es
> ellos o nosotros. Sin status page, asumen que es su problema y nos
> escriben. Con status page público en `status.genly.pro`, ven la luz
> roja directo y saben que estamos al tanto.

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

Agregar en el footer del frontend (`lyricgen/frontend/src/App.jsx`) o
en el menú de usuario:

```jsx
<a href="https://status.genly.pro" target="_blank" rel="noopener"
   className="text-xs text-ink-tertiary hover:text-white">
  Estado del servicio
</a>
```

Razón: cuando un usuario ve un error, el primer click natural es
"¿el servicio anda?". Que la respuesta esté a un click visible.

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
