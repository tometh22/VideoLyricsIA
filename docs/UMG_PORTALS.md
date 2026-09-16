# Portales de entregables UMG

Los portales de Argentina y Chile usan el mismo build estático de
`/Users/tomi/genly-deliveries`. El frontend identifica el dominio actual y
envía `X-Portal-Id` (`argentina` o `chile`) junto con `X-Portal-Token`.

## Alcance

- `umg.genly.pro`: mantiene compatibilidad con el listado histórico de
  entregas de Argentina. Las filas históricas sin destino explícito se
  interpretan como Argentina.
- `umgchile.genly.pro`: lista las entregas cuyo destino explícito es Chile.
  El tenant de origen (staging o producción) no limita una entrega que un
  administrador haya enviado a Chile.
- Ambos portales permiten descargar, previsualizar, aprobar, deshacer la
  aprobación, rechazar, pedir cambios y abrir `app.genly.pro/videos/{job_id}/edit-lyrics`.
- La edición requiere sesión en la aplicación principal. Al guardar una
  corrección, el operador debe volver a publicar la versión desde GenLy.

## Envío desde cuentas admin

En el detalle de un video aprobado, `Enviar a UMG` permite elegir Argentina o
Chile. El backend conserva una fila independiente por destino, de modo que el
mismo `job_id` puede estar publicado en ambos portales. Reenviar al mismo
destino actualiza esa fila sin duplicarla.

La API acepta el destino explícito (si se omite, queda Argentina por
compatibilidad):

```http
POST /admin/deliveries/from-job/{job_id}
Content-Type: application/json

{"portal_id":"argentina"}
```

Los valores válidos son `argentina` y `chile`. El estado del job conserva
`is_in_umg_portal` para clientes antiguos y agrega `umg_portals` con todos los
destinos activos.

## Configuración del backend

El backend conserva el token existente de Argentina:

```text
DELIVERY_PORTAL_TOKEN=<password compartido actual>
```

Opcionalmente se puede configurar un token distinto para Chile:

```text
DELIVERY_PORTAL_TOKEN_CHILE=<password chile>
```

Si no se define el token específico de Chile, el primer rollout acepta el
token compartido existente. Para aislamiento
criptográfico completo, definir `DELIVERY_PORTAL_TOKEN_CHILE` antes de entregar
el acceso.

La retención de entregables es de 60 días por defecto:

```text
DELIVERY_RETENTION_DAYS=60
```

El reaper ejecuta el sweep una vez por día bajo lock multi-réplica. Oculta las
filas vencidas y borra únicamente los cinco nombres de salida publicados en
R2. Nunca elimina `inputs/`, porque el editor y los reintentos necesitan el
audio fuente. Un fallo de R2 deja la fila elegible para reintento.

## Vercel y DNS

El proyecto `genly-deliveries` contiene el build compartido. `umg.genly.pro`
ya apunta al despliegue actualizado y `umgchile.genly.pro` está agregado como
dominio del proyecto. Para activarlo, crear en el DNS autoritativo de
`genly.pro`:

```text
A  umgchile.genly.pro  76.76.21.21
```

Luego verificar:

```bash
dig +short umgchile.genly.pro
vercel domains inspect umgchile.genly.pro
```

## Ciclo de una corrección

El portal no guarda el archivo ni una URL congelada: reconstruye la key de R2
`{tenant}/{job_id}/{nombre}` y la firma en cada request, y el render escribe en
esa misma key. Una corrección llega al cliente sin link nuevo —lo que queremos—
y, hasta 2026-09-15, sin ningún rastro: misma fila, misma fecha, misma pastilla
verde de "aprobado" sobre un corte que nunca vio.

Los tres pasos de una corrección son ahora explícitos:

1. **Editar** desde el pedido de cambios (Admin → Operación → Pedidos de
   cambio) o desde la campaña. Pedir el re-render marca las publicaciones
   activas del job como `stale_since`: el portal deja de presentar la descarga
   como final mientras los archivos se están reemplazando.
2. **Publicar** con `Enviar a UMG` / `Publicar actualización`. El backend
   compara el `render_fingerprint` actual contra el publicado:
   - **distinto** → sube `published_revision`, sella `content_updated_at`,
     **da de baja la aprobación del portal** (el cliente vuelve a ver Aprobar /
     Rechazar), invalida el cache de tamaños y **cierra los pedidos pendientes**
     de esa entrega con `resolution_source="publication"`;
   - **igual** → es un reenvío: la versión y la aprobación no se tocan.
3. **El cliente revisa** la versión nueva. `awaiting_review` la distingue de una
   ya aprobada.

### Por qué el gate no pregunta a R2 por el ProRes

`umg_master.mov` / `umg_short.mov` se transcodifican aparte, después del MP4.
Tras un edit, el **.mov PRE-EDIT sigue en su key** y contesta el HEAD: con la
sola prueba de existencia el gate daba OK y el portal entregaba el master viejo
al lado del MP4 nuevo (incidente 2026-08-03; vuelto a ver el 2026-09-15 en la
entrega 289 de Chile). El oráculo es la fila del job: `run_edit_pipeline` borra
`s3_keys["umg_master"]` al invalidar y el prewarm la reescribe recién cuando el
master fresco está arriba.

`delivery_freshness.prores_pending()` exige la conjunción que sólo cumple un job
recién editado — `previous_versions` con entradas, la fuente (`video`/`short`)
trackeada y el derivado ausente. Medido contra producción el 2026-09-15, la
condición floja ("falta la key") matcheaba ~40 de 215 publicaciones activas, casi
todas nunca editadas: jobs anteriores al tracking de keys y jobs cuya key se
perdió con la escritura mayorista de `s3_keys` previa al 2026-05-26. Marcarlas
habría bloqueado toda publicación y encolado ~40 transcodes de varios GB.

### Detectar entregas que sirven un corte viejo

```sql
-- En la DB de los JOBS (staging para las campañas UMG), cruzando contra los
-- job_id con entrega activa en la DB del portal (producción).
SELECT job_id, artist, song_title, edit_count
FROM jobs
WHERE s3_keys ? 'video' AND NOT (s3_keys ? 'umg_master')
  AND jsonb_typeof(previous_versions) = 'array'
  AND (umg_spec IS NOT NULL OR delivery_profile IN ('umg','both'));
```

Los jobs de las campañas viven en la base de **staging** mientras las filas
`deliveries` viven en **producción** (`DELIVERIES_DATABASE_URL`). Por eso un
endpoint del portal de prod que necesite el Job —como `prepare-prores`— no
resuelve esos `job_id`: hay que encolar desde staging.

### Un job publicado en los dos portales

`mark_deliveries_stale` marca **todas** las filas activas del job, porque el
re-render reemplaza los archivos que sirven las dos. Publicar limpia la ventana
sólo en la fila que se publicó: la del otro portal queda marcada y, en cuanto el
render termina, aparece en la campaña como **Portal desactualizado** (su
fingerprint ya no coincide). Eso es correcto —ese portal sigue entregando un
corte que nadie aprobó— y se resuelve publicando también ahí. Si la decisión es
no publicar en el otro portal, la fila queda visible en ese estado a propósito:
no hay un camino en el que el aviso se limpie solo sin que alguien decida.

## Auditoría diaria de integridad

`delivery_integrity.audit_active_deliveries()` corre una vez por día dentro del
ciclo del reaper, bajo el mismo lock de un solo runner que el barrido de
retención. Reporta tres cosas y no arregla ninguna:

| hallazgo | qué significa |
|---|---|
| `phantom` | la fila anuncia un `file_type` cuyo objeto no está en R2. El cliente ve un entregable que no se puede descargar |
| `outdated` | el render del job cambió después de publicarse: el portal sirve algo que nadie aprobó |
| `in_flight_too_long` | fila marcada "aplicando cambios" hace más de `DELIVERY_STALE_ALERT_HOURS` (24 por defecto). Sólo publicar limpia ese flag |

Lo que deliberadamente **no** es un hallazgo: una fila sin
`published_render_fingerprint` (no hay con qué comparar), un job que vive en
otro entorno (las entregas de campaña viven en la DB del portal y sus jobs en
la de staging), y un fallo de red de R2 (un falso positivo entrena a ignorar
la alerta). Los dos primeros se cuentan en `unevaluable`.

No arregla nada por diseño. El 2026-09-15 una fila decía "desactualizado"
sobre bytes que ya estaban corregidos, y otra decía "todo en orden" sobre un
master de un corte anterior: **medir el artefacto antes de tocarlo**.

```bash
# Verificar un entregable a mano: ffprobe sobre la URL firmada lee sólo los
# headers y cuesta nada. Comparar SIEMPRE contra el MP4 fuente.
ffprobe -v error -select_streams v:0 \
  -show_entries stream=codec_tag_string,width,height,r_frame_rate,pix_fmt \
  -show_entries format=duration -of default=noprint_wrappers=1 "<url firmada>"
# master correcto de este catálogo: apch (ProRes 422 HQ), 1920x1080,
# yuv422p10le, y fps + duración IGUALES a la fuente. Un fps distinto al de la
# fuente es conversión de framerate, que el QC manual de UMG rechaza.
```
