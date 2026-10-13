# Plan: realinear la línea después de corregir su texto (`REALIGN_ON_TEXT_EDIT`)

**Para qué.** El 74 % de las líneas con texto corregido tiene además una
edición de timing mayor a 500 ms. Esas líneas cuestan 20 s de mediana y suman
el 35 % del tiempo del operador (`docs/REVIEW_LARGE_TIMING_EDITS_2026-10-05.md`).

**Disparador.** Cuando el guardado (autosave o explícito) de una revisión
cambia el texto normalizado de una línea, el editor pide una propuesta para
esa línea:

- una sola propuesta en curso por línea;
- si la línea se vuelve a editar, se cancela la pendiente;
- no se dispara por cambios de timing ni por líneas nuevas.

**Cálculo.** CTC con el modelo aprobado sobre la ventana `[inicio − 1 s, fin + 1 s]` del stem vocal, con el texto corregido.

- **Origen del stem:** caché de R2, con clave derivada de `input_audio_sha256`, sin bajar la mezcla. Si no hay stem, se usa la mezcla y queda registrado.
- **Si el texto se repite en la canción,** la ventana se recorta en los bordes de las líneas vecinas con el mismo texto. Nunca se busca en toda la canción.
- **Código:** una variante de una línea de `ctc_align.align_structural_window`. La existente no se toca.
- **Dónde corre:** en una cola `realign` del ShortWorker, donde el modelo ya está cargado. La API encola y devuelve 202; el editor consulta el resultado.

**Resultado.** Una propuesta de inicio y fin nuevos, visible en la línea, que el operador acepta con Enter o descarta con Esc.

- Nunca se aplica sola.
- Aceptarla guarda una revisión normal del editor, igual que si el operador hubiera movido la línea a mano.
- **No se muestra propuesta en tres casos:** el score queda bajo el umbral, la ventana no tiene voz, o los dos bordes se moverían menos de 50 ms.

**Latencia objetivo: menos de 3 s.**

| Paso | Medido en M1 Pro (CPU) | Railway (estimado) |
|---|---|---|
| Alineado de una línea, modelo en caliente | 0,3 s de mediana (emisión 0,25–0,31 s) | 0,6–1,2 s |
| Encolar + tomar el job + consulta del editor | — | 0,5–1 s |
| **Total en caliente** | | **~1,5–2,5 s** |
| Primer alineado del proceso | 3,3 s | 5–10 s |
| Carga del modelo en frío | 4,7 s | 10–20 s |
| Bajar stem/mezcla de 36 MB desde R2 | 9,8 s (desde mi red) | sin medir |

- **En frío no se llega a 3 s.** Por eso, al abrir el editor se encola un precalentamiento: bajar el stem a caché local y dejar el modelo cargado.
- Sin precalentamiento, la primera propuesta del job tardaría entre 10 y 30 s.
- La latencia real en Railway se mide en staging antes de encender la bandera. Si no baja de 3 s, te aviso con el número y la causa.

**Bandera.** `REALIGN_ON_TEXT_EDIT`, apagada por defecto.

- No entra en la paridad de timing ni en la huella de la flota.
- Con la bandera apagada, el endpoint no existe y el editor no pide nada.

**Medición.**

- **Eventos:**
  - `editor_realign_proposal_shown`, `_accepted`, `_dismissed` y `_declined`, con latencia y motivo;
  - el porcentaje de aceptadas y descartadas se calcula sobre las mostradas, y el de declinadas sobre las pedidas.
- **Antes y después:** minutos por job y ediciones de timing mayores a 500 ms en líneas con texto corregido, con `report_review_baseline.py` y `diagnose_large_timing_edits.py`. La comparación es contra los 100 jobs de la línea de base.
- **Confusor:** la prueba A/B del aire mínimo también mueve los fines. Conviene encender `REALIGN_ON_TEXT_EDIT` cuando esa prueba llegue a 30 jobs aprobados por brazo, o estratificar el resultado por brazo.

**Riesgos y qué pasa en cada caso.**

- **Línea sin voz** (instrumental, público, silencio). El alineado forzado siempre devuelve algo.
  - Se declina si el score medio de tokens o `ctc_lr` queda bajo el umbral.
  - El umbral se calibra antes de encender, sobre los 571 casos históricos con texto y timing mayor a 500 ms.
  - El editor muestra "sin propuesta" y la línea no cambia.
- **Texto muy distinto al audio de la ventana** (la línea pertenece a otro lugar, o el texto corregido no se canta ahí).
  - El score cae y se declina.
  - Techo medido: el timing aprobado cae dentro de ±1 s solo en el 47–48 % de estas líneas. En la otra mitad, lo esperable es declinar.
  - Peor caso: una propuesta plausible pero equivocada dentro de la ventana. Por eso se exige margen de score y el operador decide siempre.
- **Scores CTC bajos en toda la canción** (vivo, mucho ruido, idioma distinto del español, porque el modelo es en español).
  - Se declina por línea.
  - Si la canción no está en español, la función se apaga para ese job.
  - Si las declinaciones superan un umbral de la canción, se dejan de pedir propuestas en ese job.
- **Carga en los workers.** Las propuestas compiten con las transcripciones por el lock de CTC. La cola `realign` tiene prioridad propia y concurrencia 1 por proceso.

**Esfuerzo.** Entre 32 y 48 h: backend 12–16, editor 12–16, ejecución y precalentamiento 4–8, medición 4–8. No se implementa hasta que apruebes el plan.
