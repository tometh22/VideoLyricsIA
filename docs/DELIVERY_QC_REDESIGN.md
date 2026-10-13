# Rediseño del control de calidad y preflight de entregas a UMG

Estado: **propuesta para decidir** (2026-10-01). Hoy el control está apagado en producción
(`DELIVERY_QC_GATES_OFF=1`) y en staging (`DELIVERY_QC_STAGING_GATES_OFF=1`). Nada de esto cambia el comportamiento
actual hasta que una fase se active con su bandera. Análisis hecho leyendo el código de staging `271ce4c9`; no se
ejecutaron pruebas ni se tocó ningún entorno. Lo que no se pudo verificar está marcado.

## 1. Qué pasa hoy

**Cuándo bloquea** (`delivery_readiness_gate`, `delivery_qc_runtime.py`):

- Con los interruptores apagados: nunca bloquea; los reportes se siguen generando.
- Para un trabajo "de entrega UMG" (perfil `umg`/`both`, `umg_spec`, ProRes listo, archivos maestros UMG o clase `batch`),
  publicar siempre exige las tres cosas: reporte COMPLETO y vigente (`fresh_preflight_required`), ningún FAIL abierto
  (`open_fail`) y las **8 recordatorias manuales firmadas** (`manual_review_required`).
- Para el resto, solo bloquea con `DELIVERY_QC_MODE=enforce` (por defecto está apagado).
- Aparte hay otro bloqueo al aprobar: el 409 `language_review_unresolved`.

**Qué revisa de verdad:**

| Chequeo | Tipo | Estado real |
|---|---|---|
| contenedor, audio y duración del MP4 (ffprobe) | automático | real; la duración tolera max(0,5 s; 1 %) |
| spec de entrega | automático | en la práctica solo compara fps (los demás campos del spec nunca se guardan) y contra el MP4 intermedio, no el ProRes |
| línea de tiempo (rango inválido, fuera del asset) | automático | real, FAIL |
| punto final de una línea (`LYRIC_TERMINAL_PERIOD`) | automático | cosmético pero bloquea |
| veredicto de calidad de la transcripción | automático | real; puede bloquear |
| ortografía, fragmentación, repeticiones | automático | solo WARN |
| metadatos de título/artista/versión y referencia | automático | **inalcanzables**: el runtime nunca les pasa el dato |
| OCR de título y letra (Gemini, hasta 24 cuadros) | automático | solo WARN; si no puede, queda NOT_RUN |
| video negro, congelado, sonoridad | automático | **solo un stub** que se abstiene, aun con la bandera |
| 8 recordatorias manuales | manual | se inyectan siempre, sin importar los hallazgos, y el gate las trata como bloqueantes |

## 2. Causas de la fricción

1. **El reporte nace vencido.** El QC corre en el worker antes de cerrar el job; `completed_at`, `render_params` y el
   estado se escriben después, y `completed_at` y `render_params` forman parte de la huella. Cada render queda "vencido" y
   pide una revisión manual. (Deducido leyendo el código; no se reprodujo, y las pruebas actuales usan `completed_at=None`
   y no lo detectarían.)
2. **La huella es mucho más amplia que el archivo.** Incluye `umg_spec`, la calidad de la transcripción, el nombre de
   archivo, la clase de trabajo e incluso la variable `DELIVERY_QC_MODE`. Habilitar ProRes, reanalizar la calidad o cambiar
   el modo invalida todos los reportes sin que el MP4 haya cambiado. La huella de render de `delivery_freshness` ya es una
   identidad sana.
3. **La revisión corre dentro de la petición HTTP** (baja el video de R2, calcula sha256, ffprobe y OCR de hasta 90 s), y el
   botón de publicar la llama antes de cada clic.
4. **La firma es frágil:** el `report_id` cambia con cada decisión y el servidor rechaza el anterior con 409.
5. **Las 8 recordatorias no prueban ninguna falla**, pero hay que firmar las 8.
6. **Cosas cosméticas o inverificables bloquean** (el punto final, el veredicto de calidad, un posible falso FAIL de fps).
7. **Hay varios bloqueos distintos** que se suman: QC, idioma, ProRes pendiente, `prores_required`, almacenamiento.

## 3. Qué se puede reutilizar y qué falta

Existe: ffprobe de streams, duración y spec; `_verify_deliverables` (tamaño, códec, duración ±2 s); `_validate_umg_master`
para ProRes (códec, perfil, dimensiones, fps racional, pix_fmt, color; borra masters malos); chequeos de existencia en R2;
`render_fingerprint` y `prores_pending`; el chequeo de línea de tiempo; el OCR; el endpoint y la UI de atestación de un
clic; y `/health` ya informa los interruptores.

Falta: detección de **negro, congelado y silencio** (no hay `blackdetect`, `freezedetect`, `silencedetect` ni
`volumedetect` en todo el backend), una verificación real de que la letra se renderizó, un chequeo audio-vs-video, guardar
el resultado de la validación de ProRes y verificar el archivo que realmente se sirve.

## 4. Propuesta

**Clasificación de los chequeos**

- **BLOQUEA (falla real):** archivo ausente o corrupto (sin stream de video o audio); video más corto o largo que el audio
  fuera de tolerancia; ProRes que no pasa `_validate_umg_master`; negro o congelado por encima de X % de la duración;
  audio en silencio; `INVALID_LYRIC_RANGE`; `LYRIC_OUTSIDE_ASSET`; letra no renderizada (solo cuando el OCR esté calibrado).
- **AVISA (se muestra, nunca bloquea):** todo lo de texto y ortografía, solapes, diferencias de OCR, el punto final
  (mejor quitarlo solo al renderizar) y el veredicto de calidad (el control de idioma ya cubre ese riesgo).
- **INFORMA:** lo que no se pudo evaluar y las 8 recordatorias, que pasan a ser una lista "qué mirar" imprimible y dejan de ser
  issues.

**Una sola atestación humana.** Se guarda `attestation = {usuario, momento, huella_de_render}` y vale mientras la huella de
render no cambie: habilitar ProRes, reanalizar la calidad o reenviar el mismo corte **no** la invalidan; un render o edición
nuevo sí. Configurable por cuenta (obligatoria por defecto para UMG). Se elimina el `report_id` como ficha.

**Ejecución automática e idempotente.** El QC pasa a un paso posterior al render (un trabajo propio en la cola), con la huella
de render más el etag/tamaño del objeto en R2 como clave (sin sha256 de archivos de varios GB). Publicar **lee el reporte
guardado**; si falta, lo encola y responde 202 `preparing_qc` con el mismo sondeo que ya usa ProRes. El OCR corre aparte y no
bloquea hasta medir su precisión.

**Datos, API y pantalla.** `jobs.delivery_qc` versión 2 (`fingerprint`, `checks[]` con `level` y `status`, `attestation`,
`decision`: lista / bloqueada / falta atestación; se mantiene legible la versión 1). Un endpoint
`GET /jobs/{id}/delivery-readiness` reemplaza las llamadas dispersas a `delivery_readiness_gate`. Un solo panel con **un botón
principal que cambia según el estado**: "Publicar en UMG Argentina/Chile", "Confirmar que revisé este corte y publicar" o
"Corregir: <falla real>", con el destino a la vista y los avisos plegados debajo.

**Despliegue.** Una bandera `DELIVERY_QC_V2=off|shadow|enforce`: `off` deja todo como hoy; `shadow` calcula v2 junto a v1 y
compara decisiones en logs y `/health`; `enforce` usa v2, y mientras `DELIVERY_QC_GATES_OFF=1` v2 tampoco bloquea. Producción
se queda con los controles apagados hasta que los datos de `shadow` en staging muestren la tasa de falsos bloqueos.

## 5. Plan en PRs chicos (cada uno publicable solo y detrás de su bandera)

1. **Arreglar la huella:** usar la huella de render + el archivo, sin `mode`, `umg_spec` ni calidad; el gate acepta la vieja y
   la nueva. Solo esto elimina la mayoría de los `fresh_preflight_required`. Pruebas a actualizar:
   `test_delivery_qc_runtime`, `test_deliveries` (`_signed_umg_qc_report`), `test_delivery_qc_decisions`; agregar una que
   verifique que un QC corrido antes de cerrar el job sigue vigente.
2. **QC asincrónico posterior al render**, con clave de idempotencia (`test_delivery_qc_runtime`, `test_qc_preview_fencing`).
3. **Módulo de chequeos objetivos:** negro, congelado y silencio con filtros de ffmpeg; guardar la validación de ProRes;
   corregir la comparación del spec. Pruebas nuevas con clips sintéticos.
4. **Reclasificar** (bloquea / avisa / informa), eliminar las 8 issues y una sola atestación atada a la huella. Rompe pruebas de
   gate en varios archivos del backend y de paneles en el frontend; se actualizan en el mismo PR.
5. **Endpoint `/delivery-readiness`** y manejo de `preparing_qc`; quitar la revisión previa a publicar en `JobDetail.jsx`.
6. **Panel de un solo botón** en `JobDetail` y en el panel de cambios, con Playwright.
7. **Comparación en sombra** en staging, procedimiento para producción y retiro de los bypass de staging.

No se ven afectadas `test_staging_gates_off` ni `test_production_gate_switches` mientras los interruptores de apagado sigan
funcionando.

## 6. Decisiones que necesito (con el valor que recomiendo)

1. ¿La atestación humana es obligatoria para UMG? **Sí, una vez por render, con interruptor por cuenta.**
2. ¿Se puede pasar por encima de un BLOQUEA? **Solo un admin, con motivo y registro** (como ya hace `approve_job`).
3. Umbrales: ¿cuánto negro o congelado, cuánto silencio, qué tolerancia de duración? **Empezar flojo** (más de 5 % de la
   duración en negro o congelado, silencio de más de 3 s, duración dentro de 1 s) y ajustar con datos.
4. ¿Un OCR ausente o fallido bloquea? **No: AVISA** hasta medir su precisión.
5. ¿El punto final se quita solo al renderizar o solo se avisa? **Quitarlo solo**, y avisar si la línea está fija.
6. ¿Un veredicto de calidad "inseguro" bloquea la publicación? **No: AVISA**; el control de idioma sigue siendo el único bloqueo
   de idioma.
7. ¿Habilitar ProRes o reenviar el mismo corte exige otra revisión? **No.**
8. ¿Cada portal (AR/CL) pide su propia atestación? **Una por render, válida para los dos.**

Con esas respuestas (o con los valores recomendados) las fases 1 a 3 se pueden empezar ya, porque no cambian lo que ve el
operador: arreglan la huella, mueven el cálculo y agregan chequeos objetivos que arrancan solo en modo `shadow`.
