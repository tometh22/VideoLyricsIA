# Contrato del cliente UMG Argentina y Chile

La API es dueña de la revisión publicada y firma URLs de los archivos de su
manifiesto. El frontend del portal debe conservar esa identidad mientras el
usuario revisa el video. Este contrato sirve para coordinar el repositorio del
portal con el despliegue del backend; no implica que el frontend externo ya se
haya actualizado.

## Listado

`GET /api/deliveries/items` con `X-Portal-Token` y `X-Portal-Id` (`argentina`
o `chile`) devuelve `songs[].versions[]`. Cada versión expone:

- `delivery_id`, `revision`, `manifest_hash`, `content_updated_at`;
- `files[]` con `type`, `url`, `available` y `size`;
- `approved_at`, `awaiting_review`, `updating`, `download_paused` y pedidos.

`manifest_hash` identifica los archivos exactos que se mostraron. El cliente
no debe reconstruir una key R2 a partir del Job. Si `download_paused` es
verdadero o el formato tiene `available: false`, no ofrecer esa descarga.
`updating` informa que hay cambios en preparación; el manifiesto anterior
permanece publicado hasta que se confirme el nuevo.

## Aprobación del cliente

Al hacer clic en Aprobar, enviar la revisión y el manifiesto que el usuario
vio en el listado:

```json
POST /api/deliveries/{delivery_id}/approve
{
  "expected_revision": 2,
  "expected_manifest_hash": "<hash recibido en el listado>"
}
```

La API devuelve 409 `delivery_revision_changed`, `delivery_manifest_changed`
o `delivery_update_pending` si esa vista ya no describe el corte aprobable.
En ese caso, refrescar el listado, mostrar qué revisión reemplazó a la
anterior y pedir que UMG revise el nuevo video antes de aprobar. No repetir
automáticamente el POST con valores nuevos. Un segundo clic sobre la misma
revisión aprobada es idempotente.

El backend acepta temporalmente la llamada sin `expected_*` por compatibilidad
con el cliente anterior. El canary debe comprobar que **ambos** frontends ya
envían esos campos antes de ampliar la publicación; luego se podrá exigirlos
para entregas con manifiesto.

## Prueba de contrato antes de ampliar el rollout

En un sandbox aislado por destino: listar una versión, abrir y descargar cada
formato disponible, publicar una revisión nueva, confirmar que el enlace viejo
conserva el corte previo, intentar aprobar con la revisión/hash anteriores
(409) y aprobar después de volver a revisar el nuevo corte. Comprobar también
el rechazo de archivos ausentes y la visibilidad de un pedido resuelto. El
backend tiene pruebas de contrato; falta la prueba contra el frontend real de
cada dominio.
