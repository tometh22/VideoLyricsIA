# Escuchar una línea con contexto

La acción **Escuchar 2 s antes** de cada fila reproduce desde
`max(0, line.start - 2)`. Se puede repetir y no cambia texto, tiempos ni
confirmaciones. El timestamp sigue reproduciendo desde el inicio exacto.
Sin audio o durante Sync, la acción no está disponible.

## Telemetría

`editor_line_context_played` registra la reproducción iniciada correctamente;
un rechazo de `audio.play()` no cuenta. No genera además `editor_seek`:
los dos nombres distinguen escucha con contexto de navegación convencional.
Ambos llevan posición anterior real del audio y destino en milisegundos,
sesión, revisión durable conocida y `unsaved_changes`.

- `line_context=target`: el gesto eligió explícitamente esa línea.
- `line_context=selected`: había una línea seleccionada al hacer un seek libre;
  **no afirma que el destino pertenezca a esa línea**.
- `line_context=none` y `review_marker=unknown`: no hay contexto de línea.
- `review_marker`: `review`, `quality_window`, `both` o `none` describe el
  estado visible en ese momento, no el documento final ni el diagnóstico acústico.
- `line_id` es el `_id` local que utiliza el delta auditado cuando se persiste;
  puede reasignarse al hidratar. `segment_id`, cuando existe, conserva la
  identidad de merge. Usar también revisión, índice de línea e inicio capturado
  para validar asociaciones; nunca unir sesiones sólo por `line_id`.
- La acción nueva añade `requested_lead_in_ms=2000` y el contexto efectivo
  (por ejemplo, 1000 en una línea que empieza al segundo 1).

Los campos son escalares acotados; no se envían letra ni audio. El backend
acepta la revisión entre tenants de administradores, como el editor, y mantiene
el aislamiento de usuarios comunes. El evento conserva el tenant/usuario del
actor; para campaña se une por `job_id`. Recibir telemetría no crea un documento,
adquiere un lock ni modifica la actividad del job. El cliente inspecciona
`accepted/rejected` y reporta el primer fallo por editor a observabilidad,
sin interrumpir la edición ni reenviar eventos con riesgo de duplicarlos.

## Antes/después en staging

En el análisis del 14-sep, 97 canciones aprobadas por Agus tenían cero seeks
persistidos porque su tenant difería del propietario. El histórico no permite
estimar seeks/corrección, origen o latencia: **NA, no cero**. Confirmar recepción
con usuario administrador de otro tenant antes de interpretar adopción.

La métrica primaria es minutos activos por línea aprobada **por campaña**:
sumar gaps positivos de hasta 25 s entre latidos de todas las sesiones del
mismo usuario/job, hasta aprobar; denominador de la versión aprobada. Mantener
esa fórmula en ambos períodos y comparar por buckets de esfuerzo anteriores
a editar. No comparar medianas crudas por día: la cola entrega fáciles primero.
Referencias solicitadas: AR 0,18 min/línea; CL 0,34. Registrar cohorte y método
exactos, ya que restringir a Agus y eliminar imputación inicial cambia valores.

Para seeks manuales por corrección, unir por sesión/job/identidad validada y
revisión, excluyendo asociaciones ambiguas o cambios locales sin correspondencia.
El audit fecha el guardado: llamar a esa latencia **seek→guardado**, no
seek→primera tecla. Una comparación antes/después de seeks requiere una ventana
prospectiva con telemetría reparada. `search` también incluye navegación visual:
su descenso no demuestra por sí solo reducción de búsqueda auditiva.

Esta entrega prepara un PR contra **staging únicamente**. No acredita ahorro
medido, despliegue ni cambios de producción.
