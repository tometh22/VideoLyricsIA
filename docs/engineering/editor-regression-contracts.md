# Contratos del editor que la revisión de campañas debe conservar

La interfaz nueva debe cambiar cómo se accede a las acciones, no perder los contratos del editor de staging. Aprendizajes de la segunda auditoría del PR #1570:

| Contrato | Regresión que evita | Prueba |
| --- | --- | --- |
| Identidad estable distinta para cada copia; orden temporal coherente antes de canonizar | Una copia puede sobrevivir, pero desplazar por accidente líneas vecinas cuando sus rangos se solapan | `LyricsEditorCampaignParity.test.jsx`; `editor-collaboration-real.spec.js` con API/PostgreSQL y vecinos solapados |
| Editar una línea no autoriza cambiar las demás | Corregir un estribillo debe ofrecer propagación explícita; nunca aplicarla silenciosamente ni ocultar la opción | `LyricsRepeatPropagation.test.jsx`; `LyricsEditorCampaignParity.test.jsx` |
| Las sugerencias de referencia son optativas y específicas de la línea | Ocultar el botón individual obliga a aplicar todas o reescribir; la referencia tampoco es una verdad automática | `LyricsEditorCampaignParity.test.jsx`; `referenceSuggestions.test.js` |
| Aviso de timing no validado visible en la línea afectada | Un aviso agregado no permite saber qué tramo debe escuchar la revisora | `LyricsEditorCampaignParity.test.jsx` |
| Dividir/agregar/eliminar conserva la posición de trabajo; deshacer restaura documento y selección | Volver a la primera línea obliga a buscar otra vez dentro del audio | `LyricsEditorCampaignParity.test.jsx` |
| Backspace sólo une una línea ya vacía | Eliminar una palabra no debe borrar ni fusionar la línea | `LyricsEditorCampaignParity.test.jsx`; tests históricos de `LyricsEditor.test.jsx` |
| Unir conserva la extensión completa de ambos rangos; una línea nueva tiene duración positiva dentro del audio | Truncar una armonía o crear una fila más allá de la canción | `LyricsEditorCampaignParity.test.jsx` |
| Texto avanzado se publica antes de blur y usa el documento canónico | Espacios bloqueados, pérdida al navegar o borrador visual desactualizado | `LyricsTimeline.test.jsx`; `lyrics-editor.spec.js`; E2E real |
| La lista sólo desplaza su propio contenedor | `scrollIntoView` también movía la página y alejaba el campo de edición, especialmente en móvil | `CampaignLyricsReview.jsx`; recorrido de campaña |
| Una identidad eliminada deja de estar en edición inline | Una restauración posterior no debe reabrir un input fantasma ni bloquear eliminar | `LyricsTimeline.test.jsx` |
| Los eventos de teclado de texto llegan al reloj de actividad, sin activar controles de reproducción | `stopPropagation` global ocultaba actividad; el atributo de la timeline clasificaba texto como timing | `LyricsTimeline.test.jsx`; `editorTaskClock.test.js` |
| Cambiar entre diseño y letra espera los guardados correspondientes | La revisión dedicada no debe quitar tipografía ni perder texto pendiente | `campaign-creative.spec.js` |
| Persistencia no se demuestra con localStorage ni con una respuesta PATCH aislada | Comprobar API, PostgreSQL, recarga, historial, concurrencia y recuperación | `test_editor_documents.py`; `editor-collaboration-real.spec.js`; `reviewer-real.spec.js` |

## Verificación antes de integrar

- Vitest completo, build con i18n es/en/pt y chequeo de escrituras HTTP.
- E2E de editor, navegación/cambio de canción, diseño y recuperación.
- E2E con API real y PostgreSQL descartable; únicamente el transporte de audio/waveform es sintético.
- CI completo del commit exacto actualizado con staging. Si staging avanza, actualizar el PR y esperar de nuevo.

No se escriben canciones de campaña para probar. No se cambian umbrales de calidad. La métrica de resultado sigue siendo minutos activos por línea y por campaña; mejorar la atribución de texto en el reloj no demuestra por sí solo ahorro de tiempo.
