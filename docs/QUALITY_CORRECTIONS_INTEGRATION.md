# Correcciones y control de entrega: integración por cohorte

La autoaplicación guarda una revisión reversible; nunca renderiza, aprueba ni publica.
La segunda escucha genera hallazgos para revisión humana, no cambios de letra o timing.

## Activación

Aplicar primero la migración `c0e2f4a6b8d0` en la base de trabajos. No modifica
la base externa del portal. Desplegar API y workers compatibles antes de habilitar
las capacidades. Mantener un responsable de despliegue y el worker de calidad
con una réplica; las tareas acústicas no deben ejecutarse en el pool de render.

Todas las funciones nuevas nacen apagadas. `QUALITY_PILOT_JOB_IDS` es una lista
explícita de hasta 30 trabajos. No se deriva de una campaña que pueda crecer.

- `CHANGE_REQUEST_AUTOAPPLY_ENABLED=1`: además requiere las flags existentes
  `CHANGE_REQUEST_ASSIST_ENABLED=1` y `CHANGE_REQUEST_APPLY_ENABLED=1`.
- `CHANGE_REQUEST_OWNER_ENVIRONMENT`: debe coincidir exactamente con `ENVIRONMENT`.
- `CHANGE_REQUEST_AUTOAPPLY_SINCE`: fecha ISO con zona horaria. Sin fecha ni
  `CHANGE_REQUEST_AUTOAPPLY_REQUEST_IDS`, ningún pedido histórico se incluye.
- `QUALITY_PIPELINE_OBSERVE_ENABLED=1`: conserva reconocimiento anterior al filtro
  y registra la evaluación de referencia sin promover nuevas decisiones.
- `QUALITY_FULL_REVIEW_ENABLED=1`: segunda escucha en la cola existente
  `transcription_quality`; necesita `REVIEWER_ASSIST_CACHE_DIR` persistente.
- `QUALITY_DELIVERY_REVIEW_REQUIRED=1`: exige informe vigente y decisión con
  motivo en hallazgos acústicos o tramos sin análisis, al aprobar y publicar.

Se analiza toda la duración del master con ventanas de 24 segundos, solapamiento
6 segundos, dos proveedores y ningún reintento automático de proveedor. Los
resultados se cachean por audio/ventana/proveedor/modelo/prompt/configuración.
La voz separada se usa solo si ya está cacheada y su sincronía pasa la comprobación;
si no, se escucha el mix. No se genera un stem de pago para esta revisión.

## Operación

El reconciliador se programa cada 60 segundos en la cola de mantenimiento
`default`. La carga del worker puede retrasar su ejecución. El outbox existente
repone el calendario; la escritura está cercada por revisión y locks de base.
Dos pedidos abiertos del mismo trabajo requieren revisión para evitar aplicar
instrucciones contradictorias entre pedidos. Una propuesta humana se conserva.
Una propuesta automática interrumpida antes de aplicar puede retomarse.
Si el operador editó desde que se recibió el pedido, la aplicación automática
se abstiene y deja una explicación en la propuesta.

Las operaciones guardan la instrucción original, segmento anterior/resultante,
metadatos de origen, actor y revisión. No se aplica texto solo por proximidad:
la autoaplicación exige correspondencia textual exacta normalizada y única en
la ventana local. Diferencias de vocabulario sin texto anterior explícito siguen
como propuestas. El operador mantiene la posibilidad de aplicarlas tras escucharlas.

Los intervalos requieren duración conocida y no invadir otras tarjetas. Los
segmentos bloqueados, énfasis en mayúsculas y palabras dudosas del pedido quedan
para confirmar. Una eliminación que cruza una tarjeta no la recorta por adivinación.

`POST /admin/change-requests/{id}/proposals/{proposal}/undo` recibe
`base_revision` y crea otra revisión. Rechaza audio, metadatos o documento que
hayan cambiado desde la aplicación. El historial original queda preservado.
Las correcciones de sistema no se registran como ejemplos de edición humana.

## Evidencia y entrega

`Job.acoustic_review` guarda el informe separado del análisis heurístico para
que un worker no sobrescriba el resultado del otro. Se liga a revisión, segmentos
y audio. Al completarse, invalida un QC anterior: usar el botón de revalidación
sobre el render actual. Las decisiones acústicas no se heredan entre renders.
La huella SHA-256 del MP4 inspeccionado se vincula al ETag del objeto subido.
Antes de decidir, aprobar o publicar se vuelve a comprobar ese ETag: una
sustitución del archivo invalida la evidencia aunque conserve la misma URL.

El QC compara el audio decodificado del MP4 con el master completo; los casos
que no superan la comparación conservadora requieren escucha humana. No se
confunden con prueba objetiva de que el archivo sea incorrecto. El OCR informa
que inspecciona una muestra de fotogramas, no todo el video.

Para trabajos de la cohorte, un error objetivo no se puede cerrar desde el botón
“Revisado”. Requiere corregir y volver a ejecutar el control. Los hallazgos acústicos
y partes sin análisis necesitan una explicación del operador. Esto se aplica
al endpoint de aprobación, publicación directa y envío desde campañas.
También se comprueba en exportaciones a Drive (incluido el worker de envío),
subidas a YouTube y registro de entregas de campañas. La inspección detallada
corresponde al MP4 principal; no certifica los fotogramas de otros derivados.

### Secuencia del operador después de la segunda escucha

La tarjeta de control debe llevar al operador por estos pasos, en este orden:

1. **Actualizar QC** sobre el MP4 que se va a entregar. La escucha completa ya
   debe estar ligada al mismo audio y a la misma revisión de letra.
2. **Resolver cada hallazgo concreto.** Para una diferencia acústica, escuchar
   el fragmento con contexto y registrar qué se oyó y la decisión tomada. Un
   error objetivo requiere corregir el contenido o el render y volver a medir;
   no se acepta como resuelto con una explicación genérica.
3. **Revisar los huecos de cobertura.** Escuchar y dejar constancia de cada
   tramo que algún proveedor no pudo analizar. La tarjeta debe mostrar los
   intervalos faltantes por proveedor, incluida la introducción y el cierre.
4. **Aprobar la revisión comprobada.** La aprobación y la publicación deben
   referirse al mismo MP4, identidad de audio, revisión, metadatos y decisiones.
   Si cualquiera cambia, el flujo vuelve a actualizar QC antes de continuar.

El operador que entrega es responsable de escuchar y registrar la resolución
de los hallazgos. El responsable de calidad mantiene el criterio de clasificación
de hallazgos y revisa falsos negativos durante el piloto. La publicación sigue
siendo una acción explícita del operador. El registro por hallazgo incluye
identificador, tramo, resultado, motivo, persona que decidió y huella de la
revisión y el MP4; el informe también conserva cobertura y evidencia por
proveedor. No se cierra el control mientras haya fallos objetivos, decisiones
acústicas faltantes, huecos sin revisión, o un informe o archivo desactualizado.

## Diagnóstico y piloto

`backend/scripts/audit_quality_integration.py --job-ids ID,ID --output /ruta/privada.json`
consulta las bases configuradas en modo de solo lectura y exporta solicitudes,
historial, evidencia automática y actividad. Informa datos faltantes; no genera
una medición de precisión o ahorro a partir de datos inexistentes. El informe
contiene letras privadas y se guarda con permisos 0600.

El corpus de regresión conserva los 17 pedidos originales del 8 de noviembre.
No es un conjunto de evaluación independiente. Para el piloto, preparar un
manifiesto con `masters` (30 objetos: `job_id`, `master_id`, `audio_sha256`,
`kind` live/studio y `kind_verified_by`), `development_job_ids`,
`development_audio_sha256` y `frozen_config_sha256`. Validar con
`--validate-pilot archivo.json --effective-config config-piloto.json --output resultado.json`.
El validador compara la huella congelada con el archivo real, exige hashes SHA-256
válidos para los audios y requiere el conjunto de desarrollo para excluirlo.
Requiere 15 live y 15 studio e identidades únicas.

El informe de minutos existente (`report_reviewer_minutes.py`) usa eventos de
actividad, no tiempo entre timestamps. Comparar minutos activos, renders,
reversiones y hallazgos de Universal por revisión/cohorte. La revisión humana
incluye tramos sin alertas para estimar omisiones del detector. Treinta temas
no autorizan correcciones acústicas automáticas; se mantienen los requisitos
de calibración previos.

Antes de analizar el primer master, guardar la configuración efectiva como un
archivo JSON de configuración y calcular SHA-256 sobre sus bytes. Registrar esa
huella en el manifiesto y con cada resultado. Comparar la huella calculada con
`frozen_config_sha256` antes de aceptar una canción; si difiere, detener la
evaluación, explicar el cambio y comenzar una nueva cohorte congelada. Un hash
presente pero no comparado con la configuración realmente ejecutada no prueba
que la configuración haya permanecido fija. Guardar versión de API, workers,
portal, parser, modelos y parámetros de escucha junto con la configuración.

Por canción, registrar minutos de actividad del operador desde eventos de
edición, con intervalos de inactividad excluidos; cantidad de renders del
pedido; operaciones automáticas aplicadas y deshechas; duración y cobertura
exitosa de cada proveedor; segmentos pendientes por falla; hallazgos acertados
y descartados; y decisiones humanas ligadas a la huella publicada. El evento de
reversión debe enlazarse a la operación original y clasificar el motivo (texto,
tiempo, aparición equivocada, metadatos u otro). No tratar segundos transcurridos
entre la apertura y el cierre como tiempo activo.

Para medir omisiones, después de cerrar el control un revisor escucha también
tramos elegidos al azar de toda la canción donde el sistema no alertó, incluidos
silencios aparentes, huecos de letra e introducción y final. Registrar cada
hallazgo nuevo como falso negativo con audio, tramo, revisión, MP4 y huella de
configuración. Informar cantidad y tipo, junto con el número de tramos negativos
escuchados; no presentar «cero falsos negativos» si no se revisaron segmentos
sin alertas. Reportar aparte llamadas fallidas, cobertura faltante, costo
observado y demora adicional. Sólo usar montos facturados cuando haya un dato
real de facturación; el consumo de tokens o duración no es una factura.

El cierre de cada canción requiere: manifestación y configuración concordantes;
audio y MP4 identificados; cobertura completa o escucha humana registrada de
cada hueco; todos los fallos objetivos corregidos y recontrolados; hallazgos
acústicos decididos con motivo; aprobación y publicación ligadas a ese render;
y evaluación de zonas sin alertas registrada. El piloto se cierra después de
las 30 canciones, sin cambiar la configuración durante la cohorte, y con un
informe que muestre resultados por tipo de canción (en vivo/estudio), precisión,
omisiones, minutos activos, renders, reversiones, cobertura y demora. Una
configuración modificada requiere una nueva evaluación; no se mezclan resultados
de distintas configuraciones como si fueran la misma prueba.

Rollback: apagar cada flag nueva por separado. No borrar evidencia ni revertir
una migración con actores de sistema existentes asignándolos a un usuario falso.
No cambiar punteros de entregas publicadas durante la desactivación.

## Estado de verificación de esta implementación

La inspección de Railway del 10 de noviembre de 2026 fue de solo lectura.
Producción mostraba API y workers principales en `38c86ea`, y BatchWorker en
`403df`; staging mostraba servicios en `3cba59`. Es una fotografía del momento,
no una comprobación permanente de igualdad de versiones. Se guardó el inventario
privado en `.context/quality-runtime-inventory.json` y las flags observadas en
`.context/quality-runtime-flags.json`, sin credenciales.

El corpus versionado reproduce el texto de los 17 pedidos. No incluye todavía
un paquete completo de audio exacto, salida automática previa, historial y corte
entregado de cada uno. Por eso las regresiones del parser no constituyen una
medición de los errores del reconocimiento ni de los minutos ahorrados.

El 10 de noviembre se exportaron en modo de solo lectura los 17 trabajos de
staging asociados a esos pedidos. Cada uno tiene un SHA de audio registrado,
evidencia de máquina, historial del editor, entrega y actividad del operador.
La plantilla privada `.context/quality-pilot-prep.json` conserva esos metadatos
sin copiar letra ni comentarios. Los latidos del editor dan una cota inferior
de 464 minutos activos sumados entre los 17 trabajos; no mide el tiempo pensando
fuera del editor. No hay eventos de render registrados para calcular el conteo
por pedido. El parser separó 103 instrucciones en 55 reemplazos de texto, 39
intervalos, 5 inicios, 2 cambios de metadatos, 1 eliminación y 1 caso para
revisión manual. Esto clasifica pedidos, no confirma errores del reconocimiento:
para eso hay que contrastarlos con el audio. Las cuatro flags nuevas consultadas
en el servicio API están sin configurar y siguen apagadas por defecto.

El esquema de staging sigue en `f4a6c8e0b2d4`: no tiene la columna de revisión
acústica. La migración de esta implementación aún no se aplicó. El informe
privado `.context/umg-staging-audit.json` contiene la evidencia técnica sin
texto de letras ni comentarios; la consulta no escribió datos en staging.

Se probaron aplicación/reversión, identidad del sistema, idempotencia, colisiones,
protecciones, cobertura parcial, fallos de proveedor, caché incompatible,
comparación de audio con MP4 sintético y controles de aprobación/publicación.
También se ejecutaron pruebas de interfaz, recorridos de navegador con servicios
simulados, build del portal y upgrade/downgrade de la migración en SQLite.

Pendiente de la puesta en marcha: completar el paquete de los 17 casos y la
medición inicial, aplicar/verificar la migración en PostgreSQL de staging,
ejecutar CI desde checkout limpio, coordinar versiones de API/worker/portal y
congelar el manifiesto de 30 masters nuevos antes de habilitar las flags. No se
ejecutaron llamadas pagas de reconocimiento ni publicaciones de clientes como
prueba de esta implementación.
