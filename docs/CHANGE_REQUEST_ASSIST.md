# Asistente de pedidos de cambio

## Objetivo

Convertir pedidos del portal de entregas (inicialmente UMG Argentina) en una
propuesta revisable dentro de Operación. El flujo funciona aun sin letra
oficial: solo propone texto escrito explícitamente por el cliente y nunca usa
un modelo para inventar cuál debería ser la letra.

## Flujo de operación

1. En **Operación → Pedidos de cambio**, abrir `Analizar pedido`.
2. Revisar el diff por línea. Las instrucciones de timing, estructura, fondo o
   audio se muestran como revisión manual y no tienen botón de aplicación.
3. Ajustar el texto propuesto si hace falta y aplicar únicamente las filas
   seleccionadas.
4. Abrir el editor, escuchar el tramo, re-renderizar y publicar la nueva
   versión por el flujo existente.
5. El pedido sigue pendiente hasta que la publicación lo cierre. Aplicar una
   propuesta no equivale a entregarla al cliente.

Cuando la revisión del editor o el audio cambia, la propuesta queda obsoleta y
debe recalcularse. Una aplicación parcial también se recalcula sobre la nueva
revisión para trabajar los pendientes.

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

La migración `e6a8c0d2f4b6` crea `change_request_proposals`. Antes de activar las
flags, ejecutar la migración por el procedimiento habitual y verificar que
`alembic current` coincide con `alembic heads`.
