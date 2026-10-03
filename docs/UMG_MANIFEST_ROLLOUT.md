# Despliegue del manifiesto UMG

Esta versión cambia el contrato entre staging, el worker y la base compartida
de los portales. Una sola persona debe coordinar migración, orden de deploy y
verificación. `staging` es operativo para campañas UMG: usa
`DELIVERIES_DATABASE_URL` hacia una base que también sirve a los portales
reales.

## Orden

1. Revisar que las ramas de los otros agentes estén integradas y que el CI de
   un checkout limpio pase. La release actualiza juntos `VERSION`,
   `package.json` y `CHANGELOG.md`.
2. Confirmar backup y propietario de la base compartida. Ejecutar primero el
   **dry run** de `scripts/expand_shared_portal_manifest_schema.py`; aplicar
   sus columnas aditivas en la base del portal solo con el propietario de la
   migración. No estampar la revisión Alembic de staging en esa base.
3. Aplicar `alembic upgrade head` en la base de jobs de staging. La revisión
   `c2e4f6a8b0d1` debe ser la única cabeza. Desplegar API y workers de la
   misma versión. El lector del portal anterior sigue funcionando porque
   conserva los campos conocidos y la API firma las keys del manifiesto.
4. Ejecutar el dry run de `scripts/backfill_delivery_manifests.py`. Aplicar
   por lotes solo las filas con fingerprint publicado y ETag del video aprobado
   que coincidan con el corte actual. Las filas históricas sin ambas pruebas
   se omiten aunque el job no registre ediciones.
   Las demás quedan para conciliación manual; nunca inventar qué corte aprobó
   UMG. No borrar snapshots viejos durante esta ventana.
   Ejecutar `scripts/audit_portal_manifests.py` en modo lectura: `failed`
   indica un archivo o identidad incorrecta; `unknown` indica legado o un
   problema de consulta y requiere revisión, nunca se cuenta como sano.
   Antes de activar la limpieza de snapshots, ejecutar
   `scripts/preview_delivery_retention.py`: informa filas que se ocultarían y
   objetos candidatos a borrado sin cambiar la base ni R2.
5. Probar una entrega **sintética** por Argentina y Chile: publicar, descargar
   cada formato ofrecido, editar y comprobar que la URL vieja conserva el
   corte viejo, publicar el nuevo, comprobar revisión/fecha/aprobación y
   resolución de un pedido verificado. Probar también un archivo ausente y
   un fallo de worker. Usar un portal/sandbox aislado para las pruebas que
   mutan datos.
6. Desplegar el cliente del portal con `expected_revision` y
   `expected_manifest_hash` al aprobar. Hasta entonces el backend acepta el
   cliente anterior por compatibilidad, aunque la protección de concurrencia
   para una pestaña antigua no es completa. Verificar la versión visible del
   frontend de ambos dominios antes de ampliar la publicación.

## Límite temporal de las URLs antiguas

Las URLs firmadas antes del cambio podían apuntar a nombres mutables y duran
hasta siete días. El manifiesto protege las URLs nuevas. La limpieza conserva
ocho días las copias reemplazadas. No atribuir una aprobación histórica a una
revisión nueva sin comprobar el corte.

## Recuperación

Si falla una copia, el puntero publicado queda en el corte anterior y la
operación conserva el error. El botón puede reintentarse; el manifiesto usa
keys deterministas por contenido. Si el esquema compartido aún no está
ampliado, mantener el escritor nuevo deshabilitado y desplegar una versión
compatible. No revertir el esquema aditivo ni volver a un lector que firme
keys mutables para entregas con manifiesto.

La fila de `deliveries` y el recibo de la operación registran el commit del
portal. Una caída posterior del `AuditLog` local se registra como error de
auditoría, sin transformar una publicación o aprobación ya confirmada en un
fallo para el operador. Conciliar esos errores del log antes de cerrar la
ventana de release.
