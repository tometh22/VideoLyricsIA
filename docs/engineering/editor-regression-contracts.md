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

## Tercera auditoría: concurrencia y límites

- **Deshacer es local:** conciliar un documento remoto debe trasladar sus cambios independientes al historial; descartar snapshots ambiguos antes que sobrescribir otra sesión. Preservar metadatos vigentes de filas no modificadas. Restaurar una versión o aplicar un reemplazo autoritativo limpia el historial anterior.
- **Identidad antes que índice:** una corrección remota puede renovar el `_id` visual; la selección debe seguir el `segment_id` y su ocurrencia legacy.
- **Vacío también es un borrador:** eliminar todas las líneas sin conexión requiere recuperación explícita, no descarte silencioso del array vacío.
- **No salir después de un guardado fallido:** cualquier `ok:false` bloquea la transición a diseño/salida, no sólo un conflicto de revisión. El texto permanece visible y se puede reintentar.
- **Propuestas diferidas no son permiso permanente:** revalidar fuente y destinos al aplicar una corrección repetida. No pisar texto editado después de abrir la propuesta.
- **Dividir conserva el padre y las vecinas:** ordenar antes de canonizar; cada mitad debe caber en el rango original y cumplir los 10 ms de persistencia. Si no hay lugar para dos mitades, no generar una operación ficticia.
- **La onda usa segundos reales:** los picos ocupan duración de audio × zoom, nunca el ancho del viewport ni la extensión de una letra fuera del audio. La metadata de waveform permite dibujar mientras carga el audio.
- **Mover no es redimensionar:** conservar duración de líneas cortas válidas. Mantener por separado las restricciones del gesto de resize.
- **Entrar a avanzado conserva contexto:** seleccionar y revelar horizontalmente la línea elegida, sin romper multiselección ni volver a seleccionar después de limpiar.
- **Reanudar significa reanudar:** quitar la suspensión temporal del seguimiento, no apagarlo.

Pruebas: `LyricsEditorPersistenceAudit3.test.jsx`, `rebaseEditorHistory.test.js`, `LyricsEditorStructureAudit3.test.jsx`, `LyricsTimelineInteractionAudit3.test.jsx`, `editor-audit-three.spec.js` y el recorrido real de colaboración con undo después del merge.
- **Una operación masiva, un paso de deshacer:** dividir varias líneas filtra IDs duplicados/no divisibles y registra una sola instantánea (`BulkSplitAudit3.test.jsx`).

## Cuarta auditoría: operaciones en vuelo

- Salir espera las ediciones que llegaron durante el guardado. Si siguen llegando cambios tras los intentos acotados, conserva el editor abierto. Una aprobación mantiene su snapshot explícito y exige nueva confirmación si cambia.
- Una transición de salida sólo corre una vez. Una petición de un editor desmontado no puede navegar; las respuestas de siguiente/aprobación verifican identidad de canción y ruta antes de cambiar la pantalla.
- Mientras se aprueba, el editor no acepta cambios ni atajos de edición; un fallo recupera la interacción y el foco, manteniendo un estado accesible fuera de la región bloqueada.
- Escape descarta el gesto de arrastre o selección antes de soltar el puntero, sin persistir ni generar deshacer. El límite físico del audio es independiente del ancho visual mínimo; no puede invertir la dirección de un movimiento.
- El foco recibido antes de los datos se aplica cuando llega la identidad, sin reactivar una selección limpiada ante cambios de texto.
- La guía opcional invalida escucha/decisión cuando cambia el rango, aunque conserve ID. Sus picos se posicionan por tiempo absoluto, también en recortes fraccionarios y clips cortos.
- El preview karaoke sólo reutiliza alineación si los tokens actuales coinciden con los de la evidencia, con la misma normalización básica que el render. No altera los word-stamps originales ni cambia el fallback histórico.

Pruebas nuevas: archivos `*Audit4.test.*`, `editor-navigation-races.spec.js` y `editor-approval-race.spec.js`. La prueba de flush conserva además el caso de dos guardados en cola y una corrección remota independiente.
- Al terminar una validación, el foco del panel de observaciones o del diálogo de renglones largos tiene prioridad sobre devolverlo al botón Aprobar. Ambos recorridos están cubiertos por E2E de teclado.

## Quinta auditoría: estados de canción y gestos reales

- La aprobación pertenece al editor que la inició. Desmontarlo durante flush, acknowledgement o conciliación impide iniciar/reintentar una aprobación desde otra pantalla. Un éxito explícito ya confirmado conserva telemetría de su sesión; una respuesta abandonada no se atribuye a la siguiente canción.
- Un arrastre no autoriza sobrescribir tiempos recibidos durante el gesto. Cambiar identidades o rangos invalida el preview y libera captura, incluyendo vecinos usados por ripple/colisiones. Cambios sólo de texto o metadata no cancelan el gesto.
- Seleccionar con mouse deja los atajos de timeline disponibles sin un paso manual de foco. Los inputs inline conservan espacios, cursor y Delete como edición de texto.
- La revisión dedicada corresponde a canciones en transcripción/revisión, con identidad no nula. Editar un video de campaña ya renderizado conserva el wizard previo; `null === null` nunca abre diseño implícitamente.
- Una campaña sin asignación creativa puede guardar su primer diseño con revisión cero. Abrir/hidratar no escribe; editar persiste y una revisión obsoleta no pisa el diseño nuevo.

Pruebas: `LyricsTimelineInteractionAudit5.test.jsx`, `LyricsEditorApprovalLifecycleAudit5.test.jsx`, `editor-approval-unmount-audit5.spec.js`, `editor-keyboard-audit-five.spec.js`, `campaign-postrender-audit-five.spec.js`, `campaign-workspace-audit-five.spec.js` y contrato backend de primer diseño en `test_campaign_creative.py`. La cobertura de teclado usa clics reales antes de los atajos; la lista de campaña se verifica también con lector automatizado de accesibilidad y viewport angosto.

## Sexta auditoría y criterio de cierre

- La aprobación usa el contenido y las identidades del snapshot que efectivamente se guardó. Si una conciliación incorpora texto o tiempos diferentes, requiere otra confirmación; los cambios equivalentes de metadata no bloquean el flujo.
- Restaurar conserva primero el trabajo actual en un checkpoint, espera la escritura en vuelo e invalida los guardados viejos en cola. Un resultado superseded no reactiva autosave. El bloqueo incluye atajos, aprobación, salida y cierre del historial; un fallo devuelve la interacción sin borrar la letra.
- Leer el historial materializa su respuesta y libera la transacción antes de retornar, también ante errores. Consultar versiones en paralelo no puede retener locks hasta el teardown y bloquear el event loop. La creación lazy sigue persistiendo.
- Editar un video o crear una variante conserva la identidad de ruta y canción durante todos los awaits. Una respuesta tardía no navega, no limpia otra revisión ni abre confirmaciones en la pantalla siguiente.
- Un gesto de diseño pertenece a su línea de origen: cambiar la línea activa durante reproducción cancela move/resize/rotate y libera captura. Cambios de texto que mantienen la identidad no cancelan el gesto.
- Los límites del audio prevalecen aunque la siguiente línea esté fuera de rango. Un desplazamiento en bloque nunca invierte el sentido solicitado para corregir un outlier existente; permite volver hacia el rango válido.

Pruebas: `*Audit6.test.*`, `editor-route-lifecycle-audit6.spec.js` y restauración durante autosave con API/PostgreSQL en `editor-collaboration-real.spec.js`.

Cierre: revisión cruzada independiente sin nuevos defectos reproducibles en los contratos modificados, pruebas de regresión completas, persistencia con API/PostgreSQL y CI del commit exacto. Esto establece evidencia para integrar en staging; no demuestra ausencia absoluta de bugs ni reemplaza medir el uso real por línea/campaña.
