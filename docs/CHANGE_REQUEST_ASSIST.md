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
3. Las instrucciones ambiguas de timing, estructura o audio se muestran completas
   para revisión manual. Los pedidos visuales compatibles ofrecen un prompt de
   fondo revisable; no generan automáticamente ni eliminan las restricciones.
4. Ajustar el texto propuesto si hace falta, guardar el ajuste y aplicar
   únicamente las filas seleccionadas. Mientras haya un ajuste sin guardar,
   la aplicación queda bloqueada para que el resultado aplicado no difiera de
   la vista previa.
5. Escuchar y revisar la letra guardada, confirmar el render desde Cambios o el
   editor, revisar el video resultante y publicar explícitamente. Un corte
   corregido a mano también puede publicarse cuando su revisión está comprobada;
   no hace falta volver a aplicar una propuesta pendiente.
6. Aplicar, renderizar, publicar y cerrar son hechos distintos. Una publicación
   vinculada explícitamente al pedido revisado puede cerrarlo; publicar desde
   campañas no certifica automáticamente todos los pedidos pendientes. El cierre
   manual requiere motivo y no prueba que exista un video nuevo.

Cuando la revisión del editor o el audio cambia, la propuesta queda obsoleta y
debe recalcularse. La API liga la vista previa a la misma revisión/hash que la
aplicación, por lo que nunca muestra una letra vieja como si todavía pudiera
aplicarse. Una aplicación parcial también se recalcula sobre la nueva revisión
para trabajar los pendientes.

## Qué automatiza

- texto literal explícito entre comillas con timestamp, con o sin `debe decir`
  (`0:13 "Dicen que soy lo peor"`);
- reemplazos antes/después (`cambiar "…" por "…"`);
- la misma corrección en todas las ocurrencias exactas indicadas por el
  cliente;
- eliminación determinística de puntos finales, con confirmación para líneas
  bloqueadas;
- prevención adicional en Delivery QC: fragmentación, inconsistencias entre
  repeticiones y tarjetas que terminan antes del último timestamp de palabra.

El parser v6 no convierte texto sin comillas o ambiguo en letra automáticamente:
conserva todo el pedido, incluidas continuaciones sin minuto, y pide revisión.
Las frases completas explícitas pueden sugerir una unión de fragmentos cuando
los límites y el contexto son inequívocos. No se infiere timing nuevo sin evidencia.

## Seguridad e integridad

- Solo administradores pueden generar, editar, aplicar o descartar propuestas.
- Cada propuesta está ligada al hash del pedido, revisión y hash de segmentos,
  revisión de audio e identidad del archivo cuando está disponible.
- PATCH/APPLY y la regeneración de fondo requieren el hash de la propuesta
  realmente mostrada. Un 409 requiere refrescar y revisar, nunca reintento ciego.
- Las decisiones de QC pertenecen a un informe concreto y no se trasladan a un
  render distinto. Evidencia desconocida o heurística no se presenta como PASS.
- Si se pierde la respuesta de una escritura, la interfaz muestra resultado
  desconocido y consulta el estado; no asegura éxito ni fracaso sin recibo.
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
