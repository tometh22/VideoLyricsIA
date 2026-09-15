# Campañas: revisión de experiencia y cambios

## Resultado

El recorrido de lyric videos tiene cuatro pasos visibles: **Letras → Generar videos → Revisar videos → Entregables**. La búsqueda y el contexto de retorno acompañan el trabajo entre pantallas. Contrato y trazabilidad quedan disponibles como herramientas secundarias.

| Problema observado | Cambio |
| --- | --- |
| Buscadores con reglas diferentes y sensibles a tildes/orden | Búsqueda por palabras en cualquier orden, sin tildes, sobre título, artista, código, archivo e identificador; misma lógica en listado y asignación de siguiente canción. |
| Cambiar de pantalla pierde la búsqueda y la posición | Contexto en URL: campaña, búsqueda, filtros y página; enlaces de edición y retorno lo conservan. |
| Lista de 300 canciones extensa y explicaciones repetidas | Páginas de 25, navegación arriba y detalles de revisión plegables; filas adaptadas a móvil. |
| Cola general elige una campaña implícitamente | Selector visible de campaña y búsqueda; conserva la campaña elegida al volver. |
| Seleccionar todos incluye aprobados ocultos por búsqueda | Las acciones masivas trabajan sólo sobre el resultado filtrado. Cambiar filtros limpia la selección. |
| Generación mezclada con configuración | Paso propio; se reutiliza el estilo guardado y se abre la configuración cuando hace falta. Sólo se generan letras aprobadas seleccionadas. |
| Aprobar exige cerrar reproductor, confirmar y abrir otro | Acción **Aprobar y siguiente**, limitada al video actual y al filtro vigente; aprobación normal del backend, sin override administrativo. |
| Diálogos pueden quedar desplazados por transformaciones del layout | Se montan en el documento, con foco y navegación de teclado contenidos y altura acotada a pantalla. |
| Envío sólo informa un mensaje inicial | Seguimiento de operación, resultado parcial, selección de fallidos y acceso al portal; el enlace conserva el identificador al recargar. |
| Reintentar tras una respuesta perdida puede duplicar la solicitud | La confirmación conserva la clave de idempotencia para el mismo destino y selección durante el reintento. |
| Recargas frecuentes también descargan fondos | Actualización de estado cada 30 segundos, sólo visible y sin acción en curso; fondos se consultan al abrir configuración. |
| Vista previa de entregas art-track usa GET contra ruta POST | Corregido el método y el mensaje de resultado que no se mostraba; cubierto con prueba. |

## Alcance y límites

- El rediseño de cuatro pasos corresponde a campañas de **lyric videos**. El importador de art tracks conserva su flujo específico.
- La mejora del editor interno que se prepara en `feat/editor-unified-review` sigue separada. Aquí se corrigen entradas, salidas y continuidad de revisión; no se reemplazan letra, tiempos ni audio del cliente.
- La interfaz no acelera el render del proveedor ni certifica la calidad de las letras. La reducción de tiempo operativo todavía debe medirse con uso real.
- Ninguna aprobación ni entrega real fue ejecutada al validar. Las pruebas de navegador usan datos y respuestas simulados.
- Release de staging 1.1.40, basado en `56602592` (1.1.39). Sin migraciones.

## Validación

Suite completa de interfaz, pruebas específicas de búsqueda/cola/selección/aprobación/reintento y pruebas backend de búsqueda y campañas. Pruebas de navegador cubren 300 canciones, escritorio y móvil, búsqueda, paginación, generación con estilo guardado, editor y regreso, aprobación consecutiva y envío del subconjunto correcto con recarga del seguimiento.

Los resultados exactos y capturas se guardan en `.context/campaign-workflow-evidence/RESULTADO.md` del workspace principal.
