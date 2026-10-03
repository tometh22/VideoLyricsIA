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

Los valores válidos son `argentina` y `chile`. La UI envía además
`"async_publish": true`: el endpoint devuelve `202` y un `operation_id`, y
el operador sigue el resultado por `GET /admin/delivery-operations/{id}`.
Esto permite preparar archivos ProRes grandes sin agotar el tiempo de la
petición web. Los clientes anteriores pueden seguir usando la respuesta
sincrónica durante la transición. El estado del job conserva
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
filas vencidas y limpia sus archivos de salida. Las copias inmutables que
reemplaza una publicación permanecen ocho días, porque una URL firmada antes
del reemplazo puede seguir vigente siete días. Una copia compartida por los
dos portales permanece mientras cualquiera de ellos la publique. Nunca se
elimina `inputs/`, porque el editor y los reintentos necesitan el audio fuente.

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

El editor mantiene los nombres de salida habituales. Al publicar, el backend
congela el corte aprobado en keys `published/{tenant}/{job_id}/{hash}/...` y
guarda un manifiesto por destino. El portal firma las keys del manifiesto; una
edición posterior no altera una descarga ya anunciada. Una nueva publicación
actualiza la misma fila del portal, con una revisión y fecha nuevas.

Los tres pasos de una corrección son ahora explícitos:

1. **Editar** desde el pedido de cambios (Admin → Operación → Pedidos de
   cambio) o desde la campaña. Pedir el re-render marca las publicaciones
   activas del job como `stale_since`: el portal deja de presentar la descarga
   como final mientras los archivos se están reemplazando.
2. **Verificar** cada pedido de cambio contra el video final aprobado. El
   operador confirma las instrucciones detectadas y el comentario completo.
   La verificación queda ligada al fingerprint del corte; otra edición la
   invalida. Un pedido sin verificación sigue abierto.
3. **Publicar** con `Enviar a UMG` / `Publicar actualización`. El backend
   comprueba los archivos y congela el corte antes de cambiar el manifiesto:
   - **corte nuevo** → sube `published_revision`, sella `content_updated_at`,
     da de baja la aprobación anterior y cierra solo los pedidos verificados
     para ese mismo corte, con `resolution_source="publication"`;
   - **mismo corte** → conserva la revisión y la aprobación del cliente.
4. **El cliente revisa** la versión nueva. `awaiting_review` la distingue de una
   ya aprobada.

Las filas históricas sin manifiesto todavía apuntan a keys mutables. Durante
una edición se suspenden las URLs nuevas de esas filas. El backfill de
manifiestos es conservador: solo congela automáticamente filas cuya identidad
publicada coincide de forma comprobable con el render actual. Las URLs viejas
ya firmadas pueden vivir hasta siete días tras el cambio; hay que considerar
esa ventana durante el despliegue.

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

`mark_deliveries_stale` marca **todas** las filas activas del job. Con
manifiestos, cada portal conserva descargable su corte publicado mientras el
operador prepara una corrección. Publicar limpia la ventana solo en el destino
elegido; el otro queda marcado como desactualizado hasta que se publique allí
también. Los dos destinos pueden compartir la misma copia inmutable.
