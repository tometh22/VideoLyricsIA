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
