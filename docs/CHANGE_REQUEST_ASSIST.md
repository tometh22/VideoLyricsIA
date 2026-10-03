# Asistente de pedidos de cambio

## Objetivo

Convertir pedidos del portal de entregas (inicialmente UMG Argentina) en una
propuesta revisable dentro de Operación. El flujo funciona aun sin letra
oficial: solo propone texto escrito explícitamente por el cliente y nunca usa
un modelo para inventar cuál debería ser la letra.

## Flujo de operación

1. En **Operación → Pedidos de cambio**, abrir `Analizar pedido`.
2. Revisar juntos el pedido original, el diff por línea y la vista previa de
   la letra completa resultante. La vista previa se actualiza al seleccionar o
   deseleccionar cambios y no escribe en el editor.
3. Las instrucciones de timing, estructura, fondo o audio se muestran como
   revisión manual y no tienen botón de aplicación.
4. Ajustar el texto propuesto si hace falta, guardar el ajuste y aplicar
   únicamente las filas seleccionadas. Mientras haya un ajuste sin guardar,
   la aplicación queda bloqueada para que el resultado aplicado no difiera de
   la vista previa.
5. Abrir el editor, escuchar el tramo, re-renderizar y aprobar el corte final.
6. En el pedido, verificar cada instrucción detectada y confirmar que el
   comentario completo quedó atendido en ese corte. Esto registra la evidencia
   y todavía no marca el pedido como resuelto.
7. Publicar la nueva versión en el destino del pedido. Una publicación del
   mismo corte verificado y posterior a la revisión observada por UMG lo
   marca automáticamente como resuelto, con su revisión y fecha. Si la
   verificación terminó justo después de publicar, confirmar de nuevo esa
   versión completa el cierre. Aplicar una propuesta o reenviar el corte
   reclamado no equivale a entregarlo al cliente.

Cuando la revisión del editor o el audio cambia, la propuesta queda obsoleta y
debe recalcularse. La API liga la vista previa a la misma revisión/hash que la
aplicación, por lo que nunca muestra una letra vieja como si todavía pudiera
aplicarse. Una aplicación parcial también se recalcula sobre la nueva revisión
para trabajar los pendientes.

La verificación final guarda versión del parser y hash del comentario. Si una
versión posterior detecta instrucciones que antes se omitían, esa verificación
queda obsoleta y el operador debe revisar el pedido completo otra vez. El
parser v6 conserva apóstrofos y signos, limita el alcance de repeticiones y
reconoce restricciones visuales explícitas como banderas o armas. Las órdenes
de insertar antes de otra frase, las referencias a un punto anterior y las
explicaciones de timing quedan para revisión contextual; no se convierten en
reemplazos de letra por una coincidencia textual parcial.

El listado admin devuelve además `workflow`: fase, próxima acción, acciones
permitidas, bloqueos y evidencia de revisión. El Centro de Correcciones usa
esta proyección del servidor para habilitar Verificar y Publicar, y permite
filtrar por etapa. El endpoint que ejecuta cada comando vuelve a validar la
revisión y los controles dentro de su transacción; la proyección sirve para
guiar al operador, no reserva el corte frente a ediciones concurrentes.

## Qué automatiza

- listas de `timestamp + letra correcta`, con o sin `debe decir`
  (`0:13 Dicen que soy lo peor`);
- reemplazos antes/después (`cambiar "…" por "…"`);
- la misma corrección en todas las ocurrencias exactas indicadas por el
  cliente;
- eliminación determinística de puntos finales, con confirmación para líneas
  bloqueadas;
- prevención adicional en Delivery QC: fragmentación, inconsistencias entre
  repeticiones y tarjetas que terminan antes del último timestamp de palabra.

Sin letra oficial, las comparaciones dudosas, el timing, la estructura de
líneas, el fondo y la identidad del audio son solo señales de revisión.

## Seguridad e integridad

- Solo administradores pueden generar, editar, aplicar o descartar propuestas.
- Cada propuesta está ligada al hash del pedido, revisión y hash de segmentos,
  revisión de audio e identidad del archivo cuando está disponible.
- La escritura usa el historial normal del editor (`reason=change_request`),
  control optimista de concurrencia e idempotencia.
- Cada decisión queda en `AuditLog`; las métricas guardan categorías y conteos,
  no la letra del cliente.
- El pedido externo vive en la base de entregas; la propuesta vive en la base
  local y revalida pedido, entrega y job antes de aplicar.

## Activación

Las dos capacidades nacen apagadas:

```bash
CHANGE_REQUEST_ASSIST_ENABLED=1
CHANGE_REQUEST_APPLY_ENABLED=1
```

Activación recomendada:

1. Habilitar solo `CHANGE_REQUEST_ASSIST_ENABLED` y revisar propuestas sin
   aplicar durante una muestra inicial.
2. Medir propuestas útiles, intervenciones manuales y falsos positivos.
3. Habilitar `CHANGE_REQUEST_APPLY_ENABLED` para aplicación confirmada por un
   operador.

No hay autoaplicación ciega. Incluso los cambios determinísticos pasan por la
selección del operador y por el ciclo editor → render → publicación.

## API administrativa

- `POST /admin/change-requests/{id}/proposals`
- `GET /admin/change-requests/{id}/proposals/current`
- `PATCH /admin/change-requests/{id}/proposals/{proposal_id}`
- `POST /admin/change-requests/{id}/proposals/{proposal_id}/apply`
- `POST /admin/change-requests/{id}/proposals/{proposal_id}/dismiss`
- `POST /admin/change-requests/{id}/verify`

La migración `e6a8c0d2f4b6` crea `change_request_proposals`. Antes de activar las
flags, ejecutar la migración por el procedimiento habitual y verificar que
`alembic current` coincide con `alembic heads`.
