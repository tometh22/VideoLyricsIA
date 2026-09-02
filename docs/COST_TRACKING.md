# Costo real en el panel de admin

Cómo GenLy mide lo que cuesta producir un video, y cómo conectar cada
proveedor para que el número del panel sea el de la factura.

---

## Por qué existe esto

Hasta la auditoría de agosto 2026 el costo por video se calculaba
multiplicando filas de `ai_provenance` por una tabla de tarifas. Ese
modelo tenía tres defectos que se anulaban entre sí lo suficiente como
para parecer creíble:

| Defecto | Efecto |
|---|---|
| Los **cache hits se cobraban** — el recorder se abre antes del lookup en R2, así que un hit escribe fila igual | +19% en jul-2026 (40 de 248 filas Veo) |
| **Imagen 4 y Replicate no estaban en la tabla** de tarifas | Imagen caía al default de $0,01; Replicate era invisible (facturas reales: $3,67 may / $7,12 jun) |
| El gasto se dividía por **jobs creados**, no por videos entregados | Ocultó que el 59% del gasto en Veo no produjo nada |

El tercero es el importante. Dividir los $199,53 de Google Cloud de
jun-2026 por los 173 jobs creados da **$1,15/video**. Dividirlos por los
65 que efectivamente se entregaron da **$3,07**. El segundo es el real:
los previews descartados se pagan igual.

---

## Los dos números y para qué sirve cada uno

**Costo modelado** (`provenance.py`) — filas × tarifa. Es el único que
sabe atribuir gasto **por job y por tenant**, porque una factura no puede
decir a qué cliente pertenece un dólar. Sirve para saber cuánto cuesta
UMG Chile vs UMG Argentina.

**Costo facturado** (`billing_sources.py`) — lo que cobró el proveedor.
Es la verdad, pero no se puede desagregar por cliente.

`GET /admin/cost/reconcile` compara ambos y devuelve un
`calibration_factor`. En jun-2026 el modelo dio $163 contra $199,53
facturados (factor 1,22; la diferencia era staging + precios reales de
tokens). Ese factor es diagnóstico; `POST /admin/cost/calibrate-rates`
deriva y guarda tarifas mensuales por SKU. `COST_PER_CALL` queda sólo como
fallback para períodos todavía no calibrados.

---

## Endpoints

| Endpoint | Para qué |
|---|---|
| `POST /admin/cost/refresh?period=YYYY-MM` | Consulta cada proveedor y persiste el snapshot. **Correr al menos una vez cuando cierra el mes.** |
| `GET /admin/cost/real?period=YYYY-MM` | Costo facturado por proveedor. `live=true` consulta las APIs sin guardar. |
| `GET /admin/cost/unit-economics?period=…&price_per_video_usd=13.5` | **El número para cotizar**: costo real ÷ videos entregados, más margen. |
| `GET /admin/cost/reconcile?period=…` | Modelado vs facturado + factor de calibración. |
| `POST /admin/cost/calibrate-rates?period=…` | Deriva y persiste tarifas mensuales desde la factura GCP por SKU. Requiere ambos entornos. |
| `GET /admin/cost/rates?period=…` | Muestra la tarifa aplicada y si viene de factura o del fallback estimado. |
| `GET /admin/margin` | Dashboard modelado, ahora con `waste` y `row_quality`. |
| `GET /admin/cost/by-video-type?period=YYYY-MM` | Costo y margen por **tipo de producto**: `lyric_veo` / `lyric_static` / `art_track`. Ver detalle abajo. |

> ⚠️ **`POST /admin/cost/refresh` hay que correrlo todos los meses.** Las
> APIs de los proveedores sólo exponen una ventana móvil: Railway muestra
> el ciclo abierto, GitHub el período de facturación corriente, y las
> predictions de Replicate se paginan y eventualmente envejecen. **Un mes
> que no se snapshotea es irrecuperable.** Conviene un cron el día 2 de
> cada mes.

### Leer `complete`

`complete: false` significa que al menos una fuente no está configurada o
falló, así que `total_usd` es un **piso**, no el total. No dividas por
cantidad de videos ni cites el margen hasta que sea `true`.

Una fuente sin credenciales devuelve `status: "not_configured"` y
`amount_usd: null` — nunca `0`. Un `0` significaría "este proveedor salió
gratis", que es una mentira distinta.

---

## Costo por canción de un cliente (atribución)

Las secciones de abajo miden **cuánto se gastó**. Para saber **en nombre de
quién**, está `cost_attribution.py` + `scripts/umg_cost_report.py`, que
responden tres niveles: costo directo por canción, costo total del cliente con
infraestructura prorrateada, y a qué se fue cada dólar del negocio.

Tres hechos hacen que un `GROUP BY tenant_id` dé mal, y los tres están
verificados contra datos reales:

1. **La producción gestionada de UMG corre en STAGING, bajo cuentas del
   equipo.** 67 de las 68 entregas vigentes del portal `umg.genly.pro` tienen
   su job en la base de staging, propiedad de `tomas@epical.digital`,
   `agus77`, `default` y `omg` — ningún tenant `universal_*`. Atribuir por
   tenant se pierde casi toda la producción gestionada.
2. **Staging no es gratis.** Staging y prod comparten proyecto de GCP, bucket
   R2 y proyecto de Railway, así que ninguna factura se puede partir por
   entorno. El corte sale de las bases.
3. **La unidad facturable es la canción, no el job.** Una canción entregada
   arrastra ~2,4 jobs (variantes, re-renders, ediciones).

Además, `golden_render_bot` re-renderiza catálogo real para QA. Por eso la
clasificación **chequea tenants de CI antes que canciones** — al revés, cada
corrida de regresión se le facturaría al cliente. Hay tests que lo fijan.

### Uso

```bash
export DATABASE_URL_PROD='postgresql://...'
export DATABASE_URL_STAGING='postgresql://...'
python scripts/umg_cost_report.py --period 2026-07 --revenue 2000 \
  --invoices '{"gcp":199.53,"railway":126.02,"r2":18.84,"fixed":44}'
```

En el panel: `GET /admin/cost/umg` y `GET /admin/cost/business`. Necesitan
**`PEER_DATABASE_URL`** apuntando al otro entorno (en staging ya se reusa
`DELIVERIES_DATABASE_URL`, así que no hay que configurar nada). Sin ella los
endpoints responden igual pero marcan `single_environment: true` — nunca
reportan que el otro entorno costó $0.

### El denominador son las canciones ENTREGADAS

Es el mismo defecto que originó toda esta auditoría, un nivel más arriba, y es
fácil de reintroducir. Una canción que se trabajó y nunca salió **consumió
plata igual**: suma al numerador. Pero meterla en el denominador abarata
artificialmente el costo de entregar.

Medido en jun-2026: **51 canciones tocadas, 37 entregadas**. Dividir por 51
subestimaba el costo un **38%**. `umg.songs` son las entregadas;
`umg.songs_touched` es diagnóstico y nunca divide. Hay un test que lo fija.

### Resultado medido (jun-2026, facturas completas)

| | |
|---|---|
| Canciones de UMG **entregadas** | 37 |
| Tocadas sin entregar | 14 ($12,96 que se pagó igual) |
| Jobs asociados | 151 (4,08 por entregada) |
| **Costo directo de IA por canción** | **$3,42** |
| **Costo total por canción** (con infra prorrateada) | **$6,05** |
| Precio real por canción ($2.000 ÷ 37) | $54,05 |
| **Margen** | **88,8%** |

UMG fue el **56,1%** del gasto de IA; el resto fue I+D interno (42,7%) y
CI (1,1%).

> **Cotizá con el promedio, no con la mediana.** La plata que sale es la suma,
> y la cola de canciones re-generadas 10-20 veces es real. La mediana ($2,43)
> describe el caso típico, no el costo.

> ⚠️ **El 42,7% de "I+D interno" hay que mirarlo con desconfianza.** Con un
> solo cliente grande, buena parte del trabajo de desarrollo es corregir
> defectos que afectan a UMG. `classify_job` manda un job del equipo a I+D
> salvo que su canción esté en el portal, y `song_key` no normaliza acentos ni
> puntuación a propósito — así que un tipeo distinto mueve trabajo de UMG a
> I+D, nunca al revés. El sesgo es en una sola dirección.

## Precisión medida (validación 7-ago-2026)

Cada conector se corrió con las credenciales reales y se comparó contra la
factura del proveedor:

| Fuente | Período | Conector | Factura real | Error |
|---|---|---|---|---|
| **Railway** | jun-2026 | $126,02 | $124,54 | **+1,2%** ✅ |
| **R2** | jul-2026 | $26,30 | $26,49 | **−0,7%** ✅ |
| **OpenAI** | jul-2026 | $19,41 | $19,30 | **+0,6%** ✅ |
| Replicate | jun-2026 | $6,10 | $7,12 | −14% ⚠️ |
| GCP | — | sin export habilitado | $199,53 | — |
| GitHub | — | 404 (falta scope) | $0 facturado | — |

Los tres primeros están dentro del 1,2%. Replicate afloja porque usa una
tarifa de hardware mezclada (ver más abajo), pero es la línea más chica
del stack.

**Los dos que faltan son los que importan distinto:** GCP es el ~50% del
gasto total y hay que habilitar el export; GitHub factura $0 y no vale el
esfuerzo.

## Configurar cada proveedor

Todas las variables van en el servicio `api` de Railway. Cada fuente es
opcional: sin credenciales el panel muestra el resto igual.

### 1. Google Cloud — ~80% del gasto variable ⭐ prioridad

GCP no tiene un endpoint de "cuánto gasté el mes pasado". El camino
soportado es exportar la facturación a BigQuery (gratis, diario, por SKU)
y consultarla.

1. Consola de facturación → **Exportación de facturación** → habilitar
   *Costo detallado* hacia un dataset de BigQuery.
2. Crear una service account de **sólo lectura** (que no sea la misma que
   usa Vertex — el que lee facturas no debería poder gastar):
   - `roles/bigquery.jobUser` en el proyecto
   - `roles/bigquery.dataViewer` en el dataset
3. Variables:

```bash
GCP_BILLING_BQ_PROJECT=genly-prod
GCP_BILLING_BQ_DATASET=billing_export
GCP_BILLING_BQ_TABLE=gcp_billing_export_v1_XXXXXX_XXXXXX_XXXXXX
GCP_BILLING_SA_JSON='{"type":"service_account",...}'   # JSON inline
GCP_BILLING_PROJECT_IDS=genly-prod,genly-staging          # obligatorio
```

`GCP_BILLING_PROJECT_IDS` enumera los proyectos donde corre el workload de
GenLy, no necesariamente el proyecto que hospeda el dataset. Sin este scope el
conector queda incompleto: el export cubre toda la cuenta de facturación y
aceptarlo mezclaría otros proyectos en costos y tarifas.

> El export tarda ~24 h en empezar a poblar y **no es retroactivo**:
> los meses anteriores a habilitarlo no van a estar. Habilitalo ya aunque
> el resto quede para después.

**Bonus de alto valor:** una vez andando, etiquetar las llamadas a Vertex
con labels por `job_id` / `tenant_id` permite ver el costo real por
cliente contra la factura. Es el dato que te deja defender el precio con
UMG con números en vez de estimaciones.

### 2. Railway — hosting/compute

```bash
RAILWAY_API_TOKEN=...            # Account Settings → Tokens
RAILWAY_PROJECT_ID=...           # requerido, o RAILWAY_WORKSPACE_ID
```

⚠️ Tiene que ser un **token de cuenta**, no un *project token*: los
project tokens no pueden leer la query `usage`.

Acotar al proyecto o workspace exclusivo de Genly IA es obligatorio: la
cuenta tiene otros proyectos y en jun-2026 la diferencia fue **$124,54
(Genly) vs $135,25 (cuenta entera)**. Sin uno de esos scopes el conector queda
incompleto y no consulta la API.

**Railway no expone dólares por API.** El enum `MetricMeasurement` no
tiene ninguna medida de costo — sólo recursos crudos. El conector pide
`usage` (CPU, memoria, disco, backup, red) y lo valoriza con las tarifas
publicadas, todas overrideables por env (`RAILWAY_USD_PER_VCPU_MONTH`,
`RAILWAY_USD_PER_GB_MONTH`, `RAILWAY_USD_PER_EGRESS_GB`…).

El compromiso mínimo del plan se configura con
`RAILWAY_PLAN_MINIMUM_USD` (default `$20`) y sólo se aplica cuando el scope es
un workspace exclusivo de Genly. Con `RAILWAY_PROJECT_ID` el conector informa
únicamente el uso medido: el compromiso se cobra a nivel cuenta/workspace y no
hay una base confiable para imputarle al proyecto el top-up compartido. Ese
mínimo queda deliberadamente sin asignar; no se debe sumar como suscripción
fija porque podría duplicar gasto ya absorbido por otros proyectos.

> **La trampa de las unidades:** los recursos vuelven en *unidad-minutos*
> acumulados sobre la ventana, pero `NETWORK_TX_GB` es un flujo ya
> expresado en GB. Confundirlos es un error de ~700x. Hay un test que lo
> fija.

### 3. OpenAI — transcripción whisper-1

```bash
OPENAI_ADMIN_KEY=sk-admin-...
OPENAI_COST_LINE_ITEMS=whisper,gpt-4o-mini  # default; vacío = toda la organización
```

Tiene que ser una **admin key**, no la API key normal — la común no puede
leer facturación.

> **Filtrar es obligatorio.** La organización de OpenAI está compartida
> con otros proyectos: jul-2026 fueron **$567,43 en toda la org**
> (GPT-5.4, GPT-4.1, modelos de imagen, batch API) contra **$20,15 de
> whisper**, que es lo único que usa GenLy. Sin filtro el costo por video
> queda inflado 28x. El `breakdown` igual lista lo excluido, así que se ve
> qué quedó afuera y por qué el número no coincide con el titular del
> dashboard de OpenAI.

### 4. Cloudflare R2 — storage ⚠️ nunca se midió

```bash
CLOUDFLARE_API_TOKEN=...     # permiso: Account Analytics → Read
CLOUDFLARE_ACCOUNT_ID=...
R2_BUCKET=genly-media        # opcional
```

Cloudflare no expone una línea de factura por bucket, así que se lee el
uso medido y se valoriza con las tarifas publicadas (ajustables por env:
`R2_USD_PER_GB_MONTH`, `R2_USD_PER_M_CLASS_A/B`). Sale marcado
`is_estimate: true`.

> **Mirar esta línea de cerca.** Se guardan MP4 + vertical + thumbnail +
> masters ProRes por video, y todavía **no hay lifecycle rules** en el
> bucket. Es el único costo que sólo crece, y nunca estuvo en el modelo.

### 5. Replicate — whisperX / forced-align / demucs

Anda sin configurar nada: usa el `REPLICATE_API_TOKEN` que el pipeline ya
tiene. Como Replicate no tiene endpoint de facturación, se listan las
predictions de la ventana y se valorizan por tiempo de compute
(`REPLICATE_USD_PER_SECOND`, default blended). Sale `is_estimate: true`.

**Precisión medida** contra las facturas reales:

| Mes | Estimado | Facturado | Desvío |
|---|---|---|---|
| may-2026 | $4,90 | $3,67 | +33% |
| jun-2026 | $6,10 | $7,12 | −14% |
| jul-2026 | $3,60 | $1,82 (a mitad de mes) | consistente |

La tarifa default ($0,000225/s) queda dentro del **4% del promedio de los
dos meses cerrados** — está bien centrada. El desvío mes a mes es por
mezcla de modelos: demucs corre en hardware más caro que whisperX y una
sola tarifa mezclada no los distingue. Sirve para ver la magnitud y
detectar saltos, no para cuadrar al centavo. Es la línea más chica del
stack (~$5/mes), así que no vale la pena refinarla más.

### 6. GitHub — Actions ⚠️ no vale la pena

```bash
GITHUB_BILLING_TOKEN=ghp_...  # necesita el scope `user`
GITHUB_BILLING_ORG=...        # o GITHUB_BILLING_USER
GITHUB_BILLING_CYCLE_DAY=1    # sólo si el ciclo real empieza el día 1
```

**Factura $0.** En ago-2026 el uso bruto medido fue $21,93 y el descuento
incluido del plan fue $21,93 — neto cero. El único costo real de GitHub
es la suscripción Pro de $4/mes, que ya está en `fixed`.

Si igual lo querés conectar: en cuentas personales el PAT necesita el
scope **`user`**. `repo` + `admin:org` no alcanzan y el endpoint tira
**404** — un 404 acá significa scope faltante, no cuenta inexistente.
El endpoint reporta el **ciclo de facturación**, no el mes calendario. El
ciclo debe empezar el día 1 incluso cuando el total sea $0: con un corte a
mitad de mes, ese cero podría omitir minutos pagos de la primera quincena. Si
no está alineado, el conector muestra el valor observado como diagnóstico y
deja el total mensual incompleto porque la API no permite separar los meses.

### 7. Suscripciones fijas

Sin API que valga la pena. Default en código: Vercel Pro $20 + GitHub Pro
$4. Se cambian sin deploy:

```bash
FIXED_SUBSCRIPTIONS_JSON='{"vercel_pro":20,"github_pro":4}'
```

Railway no entra acá: su compromiso mínimo se trata dentro del conector de
Railway como se explica arriba. La clave legacy `railway_plan`, si todavía
existe en `FIXED_SUBSCRIPTIONS_JSON`, se interpreta como ese mínimo pero se
elimina de esta suma para no duplicarlo.

Estas suscripciones son la parte del costo unitario que **se amortiza**: a 65
videos/mes son $0,37/video, a 400/mes son $0,06. Dejarlas afuera es lo que hace
que los meses de bajo volumen parezcan baratos.

---

## Costo por TIPO de video (`lyric_veo` / `lyric_static` / `art_track`)

El contrato de Universal no es un precio único: 400 lyric videos/mes a $6
(con la condición de que la MITAD use fondo de foto fija en vez de Veo,
como palanca de costo) + 250 Art Tracks/mes a $4 (portada + waveform, sin
Veo, sin editor de letra). `GET /admin/cost/by-video-type?period=YYYY-MM`
(implementado en `cost_by_video_type.py`) es la primera vista que separa
el costo de estos 3 productos — hasta ahora solo existía agregado por
tenant/canción, lo que puede esconder que uno de los tres da pérdida
mientras los otros dos lo compensan.

Parámetros opcionales: `labor_rate_usd_per_hour` (default `10.0`, no hay
otra convención documentada en el repo) y `price_lyric_veo_usd` /
`price_lyric_static_usd` / `price_art_track_usd` — sin precio, el
endpoint devuelve costo nomás (nunca inventa un precio para calcular
margen).

**Cómo se clasifica un job** (no existe una columna `video_type`):

1. `render_params["art_track"] == True` → `art_track`. Lo persiste
   `main.py` antes de encolar el pipeline cuando el request trae
   `art_track=true`.
2. Si no, `render_params["background_ai_generated"]` (lo escribe
   `pipeline._background_source_is_ai()`): `True` → `lyric_veo`,
   `False` → `lyric_static`.
3. Si el campo no está (jobs previos a esa instrumentación) → `unknown`,
   contado aparte y **nunca** forzado a una de las 3 categorías
   facturables.

**Limitaciones conocidas:**

- `unknown` puede ser una porción significativa en meses viejos — el
  endpoint reporta `unknown.delivered_count` para que se vea la cobertura
  real en vez de asumir 100%.
- `labor_minutes_avg`/`labor_minutes_p50` de `art_track` habitualmente
  salen en `null`: ese flujo nunca abre el editor de letra (`App.jsx`:
  "background_file + art_track=true + empty segments. No lyrics editor"),
  así que no emite el evento `editor_approved` del que sale
  `active_edit_ms`. No es un hueco de telemetría — el producto no tiene
  esa mano de obra.
- Sólo lee el entorno de la sesión que recibe (no hace merge
  staging↔prod como `/cost/umg`). La producción gestionada de UMG corre
  en staging bajo cuentas del equipo — correrlo sólo contra prod
  subestima el volumen real de lyric videos.
- Es de solo lectura: agrega sobre `Job.render_params`, `AIProvenance` y
  `ProductEvent` ya existentes, no cambia nada del pipeline de render.

---

## Dónde está el desperdicio

`GET /admin/margin` ahora trae `waste`, y `cost_waste_breakdown()` acepta
`tenant_id` para verlo por cliente.

Medido en prod sobre los últimos 38 días (ago-2026):

```
Costo facturable total    $187,45
  → videos entregados      $83,90
  → DESPERDICIADO         $103,55   (55,2%)

Costo/entregado (real)     $3,91
Costo/entregado (piso)     $1,75   ← si se elimina el desperdicio
```

Las causas, verificadas en el código:

- **Previews descartados.** Cada cambio de opción en el wizard genera un
  fondo nuevo a $0,80 aunque el video no se renderice nunca.
- **Rechazos.** Un job rechazado quema ~5 llamadas Veo: el validador
  bloquea y la regeneración `policy-recovery` **fuerza un cache miss**.
- ~~**Retry del worker ×3** re-paga porque el prompt sale con
  `temperature=0.8`.~~ **CORREGIDO (ago-2026): era falso.** No existe ningún
  `temperature=0.8` en el código — los prompts se generan con 0.0-0.1 — y
  `queue_jobs.py:744-747` documenta que las fallas de Veo caen al gradient
  fallback sin re-lanzar, así que el `Retry` de RQ **no** reintenta Veo. El
  reclamo venía de un reporte no verificado y se propagó a la doc y a un
  comentario del código antes de que alguien lo chequeara.
- **Re-roll de escenas.** 5 por escena × 6 escenas = hasta $24 en un solo
  job, y no consume el presupuesto de 3 edits.

El cache de Veo **no baja el costo en régimen**: la key incluye
`artista|canción` a propósito, así que dos canciones nunca comparten
clip. Es un seguro de idempotencia contra retries, no una palanca.

---

## Recalibrar las tarifas

1. `POST /admin/cost/refresh?period=YYYY-MM` después de cerrar el mes y
   cumplirse el rezago de GCP.
2. `POST /admin/cost/calibrate-rates?period=YYYY-MM`. Requiere
   `PEER_DATABASE_URL` porque la factura compartida cubre staging y prod; sin
   el segundo entorno el endpoint se niega a guardar una tarifa sesgada.
3. Confirmar en `GET /admin/cost/rates?period=YYYY-MM` que las herramientas
   esperadas muestran `source: "factura"`. Usar
   `GET /admin/cost/reconcile?period=YYYY-MM` como control de deriva global.

No multipliques `COST_PER_CALL` por `calibration_factor`: cambiaría el fallback
de todos los períodos sin snapshot. Esa tabla se modifica sólo cuando cambia
deliberadamente la tarifa estimada de lista; la calibración operativa es
mensual y se guarda mediante el endpoint.
