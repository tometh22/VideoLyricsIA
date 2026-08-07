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
facturados (factor 1,22; la diferencia es staging + precios reales de
tokens). **Si ese factor se mueve mucho, las tarifas de `COST_PER_CALL`
quedaron viejas** — y como la atribución por tenant hereda el error, una
tabla silenciosamente incorrecta es peor que no tener ninguna.

---

## Endpoints

| Endpoint | Para qué |
|---|---|
| `POST /admin/cost/refresh?period=YYYY-MM` | Consulta cada proveedor y persiste el snapshot. **Correr al menos una vez cuando cierra el mes.** |
| `GET /admin/cost/real?period=YYYY-MM` | Costo facturado por proveedor. `live=true` consulta las APIs sin guardar. |
| `GET /admin/cost/unit-economics?period=…&price_per_video_usd=13.5` | **El número para cotizar**: costo real ÷ videos entregados, más margen. |
| `GET /admin/cost/reconcile?period=…` | Modelado vs facturado + factor de calibración. |
| `GET /admin/margin` | Dashboard modelado, ahora con `waste` y `row_quality`. |

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
```

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
RAILWAY_PROJECT_ID=...           # opcional pero recomendado
```

⚠️ Tiene que ser un **token de cuenta**, no un *project token*: los
project tokens no pueden leer la query `usage`.

Acotar al proyecto Genly IA importa: la cuenta tiene otros proyectos y en
jun-2026 la diferencia fue **$124,54 (Genly) vs $135,25 (cuenta entera)**.

**Railway no expone dólares por API.** El enum `MetricMeasurement` no
tiene ninguna medida de costo — sólo recursos crudos. El conector pide
`usage` (CPU, memoria, disco, backup, red) y lo valoriza con las tarifas
publicadas, todas overrideables por env (`RAILWAY_USD_PER_VCPU_MONTH`,
`RAILWAY_USD_PER_GB_MONTH`, `RAILWAY_USD_PER_EGRESS_GB`…).

> **La trampa de las unidades:** los recursos vuelven en *unidad-minutos*
> acumulados sobre la ventana, pero `NETWORK_TX_GB` es un flujo ya
> expresado en GB. Confundirlos es un error de ~700x. Hay un test que lo
> fija.

### 3. OpenAI — transcripción whisper-1

```bash
OPENAI_ADMIN_KEY=sk-admin-...
OPENAI_COST_LINE_ITEMS=whisper   # default; vacío = toda la organización
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
```

**Factura $0.** En ago-2026 el uso bruto medido fue $21,93 y el descuento
incluido del plan fue $21,93 — neto cero. El único costo real de GitHub
es la suscripción Pro de $4/mes, que ya está en `fixed`.

Si igual lo querés conectar: en cuentas personales el PAT necesita el
scope **`user`**. `repo` + `admin:org` no alcanzan y el endpoint tira
**404** — un 404 acá significa scope faltante, no cuenta inexistente.
El endpoint reporta el **ciclo de facturación**, no el mes calendario.

### 7. Suscripciones fijas

Sin API que valga la pena. Default en código: Vercel Pro $20 + GitHub Pro
$4 + Railway plan $20. Se cambian sin deploy:

```bash
FIXED_SUBSCRIPTIONS_JSON='{"vercel_pro":20,"github_pro":4,"railway_plan":40}'
```

Son chicas pero son la parte del costo unitario que **se amortiza**: a 65
videos/mes son $0,68/video, a 400/mes son $0,11. Dejarlas afuera es lo que
hace que los meses de bajo volumen parezcan baratos.

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
- **Retry del worker ×3.** El comentario en `queue_jobs.py` asume que el
  cache de R2 salva el reintento. **Es falso**: el prompt se genera con
  `temperature=0.8`, así que cada retry produce un hash distinto y se
  vuelve a pagar entero.
- **Re-roll de escenas.** 5 por escena × 6 escenas = hasta $24 en un solo
  job, y no consume el presupuesto de 3 edits.

El cache de Veo **no baja el costo en régimen**: la key incluye
`artista|canción` a propósito, así que dos canciones nunca comparten
clip. Es un seguro de idempotencia contra retries, no una palanca.

---

## Recalibrar las tarifas

1. `POST /admin/cost/refresh?period=YYYY-MM` con el mes cerrado.
2. `GET /admin/cost/reconcile?period=YYYY-MM`.
3. Si `calibration_factor` se fue lejos de ~1,2, multiplicar las tarifas
   de `COST_PER_CALL` en `provenance.py` por el factor y actualizar el
   test `test_veo_fast_rate_matches_invoice_reconciliation`.

La comparación es imperfecta a propósito y el endpoint lo aclara: la
ventana del modelo son días corridos, la factura es mes calendario, y la
factura de GCP incluye staging mientras que el modelo mide sólo prod.
Sirve para detectar deriva, no para cuadrar al centavo.
