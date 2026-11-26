# Investigación de borradores vacíos — 10 nov 2026

Se consultaron las bases de staging y del portal mediante transacciones REPEATABLE READ, READ ONLY, con timeout de 15 segundos. No se modificaron pedidos, letras, aprobaciones ni publicaciones.

## Alcance y evidencia

- Últimos 30 pedidos de cambios del portal: 25 videos distintos y 3882 checkpoints conservados. Ninguno contiene un array de segmentos vacío.
- En los 15 pedidos más recientes, los documentos actuales del editor y el job tienen letra no vacía y coinciden en cantidad de líneas y revisión. Esto descarta un vaciado actual en esa muestra, no certifica cada edición histórica ni la exactitud de la letra.
- Se leyeron 827 eventos de hoy de la muestra (sin truncamiento al límite de 1200). No hay eventos editor_autosave_failed. La telemetría no registra la apertura/elección del diálogo de recuperación: no permite contar en cuántos navegadores apareció.
- La captura corresponde a Lejos De Vos — Alejandro Lerner, job 35cd85eefff5: coinciden el texto de la primera línea, inicio 19.3819, fin 21.3259 y revisión 273.
- Agus aprobó revisión 273 con 67 líneas el 10-nov a las 12:03:10.860 UTC. La captura fecha el borrador local a las 12:03:15.561 UTC (4.7 segundos después). Luego Agus guardó 274 y aprobó 275 a las 16:08 UTC, ambas con 67 líneas.

| Video con pedido reciente | Revisión consultada | Líneas | Último editor |
|---|---:|---:|---|
| Como Un Bolu | 362 | 55 | ricardo.tapia |
| Corazón Herido | 43 | 43 | tomas@epical.digital |
| Dileando Con Un Alma | 296 | 39 | agus77 |
| Hecho Mierda | 459 | 95 | tomas@epical.digital |
| Hey Hey My My | 18 | 19 | soniaca24 |
| Inocencia Perdida | 41 | 44 | agus77 |
| Joya + Guinda + Fuego | 27 | 30 | tomas@epical.digital |
| La Bolsa | 130 | 61 | soniaca24 |
| Lejos De Vos | 275 | 67 | agus77 |
| Megagilda Mix | 486 | 94 | agus77 |
| Mientras Miro Las Nuevas Olas | 7 | 36 | agus77 |
| Roca De Ibiza | 42 | 23 | ricardo.tapia |
| Ser Humano Junto A Los Mios | 24 | 31 | ricardo.tapia |
| V.S.D. | 238 | 37 | agus77 |
| ¿Por qué no se van? | 41 | 39 | agus77 |

## Bug reproducido y corrección

App libera la entrada de segmentsStore tras aprobar/salir. useJobSegments expone [] al suscriptor aún montado cuando falta esa entrada. LyricsEditor puede conservar isDirty=true durante esa transición y guardar el array vacío como borrador con base_segments y base_revision válidos. No hay una acción humana de eliminar líneas en esta reproducción.

Las pruebas de evict y evictAll fallaron antes del fix: crearon exactamente {segments: [], base_segments: [...], base_revision: 273}. Este hallazgo supera la hipótesis inicial de un borrador vacío sin base: la limpieza interna también genera copias vacías CON base válida, por lo que validar base_segments no alcanza.

El editor ahora comprueba que la entrada siga viva antes de escribir borradores o rescatar al cerrar, deshabilita autosave sobre la ausencia de entrada y no permite que una reconciliación tardía resucite la entrada liberada. Un array vacío que sí pertenece a una entrada viva sigue siendo una eliminación recuperable. Las pruebas también conservan cambios pendientes al desmontar normalmente.

Cambio en .context/editor-ux-implementation (rama fix/editor-visible-times), junto con la corrección del diálogo. No se hizo merge ni deploy.

Verificación: 62 tests aprobados en 7 archivos: recuperación, persistencia, store, autosave, undo, ciclo de aprobación y salida segura. Logs de reproducción y validación en .context/draft-incident-20261110/.

## Qué se puede concluir

Existe una causa reproducible de falsas alarmas compatible con la captura y el momento de aprobación. No hay evidencia de vaciado de letras en servidor en esta muestra. No se accedió al localStorage ni a una grabación de sesión del navegador de Agus: no se puede atribuir de manera definitiva cada aparición al mismo bug ni descartar cambios legítimos sin guardar en otros borradores.

El commit 71f0e529 (6-nov) habilitó recuperar arrays vacíos para proteger eliminaciones intencionales; antes quedaban apartados como incompatibles. Esto explica por qué un defecto de limpieza podía permanecer oculto y ahora mostrar un aviso. La API staging consultada está en 3cba59f8, desplegada el 8-nov, e incluye ese commit; no se verificó independientemente la fecha de despliegue del frontend.
