# Ediciones de timing grandes: qué tienen en común (2026-10-05)

No hay cambios de producto. El script es
`lyricgen/backend/scripts/diagnose_large_timing_edits.py`, de solo lectura.
Usa los 100 jobs de staging de la fase 1 y compara las líneas de máquina
emparejadas con la primera aprobación.

Los grupos analizados:

| Grupo | Líneas | Qué incluye |
|---|---|---|
| Fines movidos más de 500 ms | 895 | Diferencia estrictamente mayor a 500 ms |
| Inicios movidos 50 ms o más | 932 | Todas las ediciones de inicio |
| Texto editado | 755 | Texto normalizado distinto entre la máquina y lo aprobado |
| Todas las líneas emparejadas | 3.038 | Referencia |

Una segunda corrida, horas después, dio 898, 948 y 763 líneas, porque la
ventana de los últimos 100 jobs aprobados se movió. Los resultados casi no
cambian: 74,8 % del texto editado viene con timing mayor a 500 ms, 20,1 s
por línea, y el techo con ±1, ±2 y ±4 s queda en 47, 67 y 86 %. Las tablas
son de la primera corrida.

El diagnóstico anterior contaba 955 fines movidos de 500 ms o más. Las 60
líneas de diferencia se movieron exactamente 500 ms: el operador sacó el hold
completo.

## e) Líneas sin timestamps por palabra en la versión de máquina

| Grupo | Total | CTC | WhisperX | Otros |
|---|---|---|---|---|
| Fines movidos más de 500 ms | 26,7 % | 0,2 % (1 de 455) | 49,1 % (195 de 397) | 100 % (43) |
| Inicios movidos | 29,1 % | 0,2 % (1 de 444) | 52,1 % (237 de 455) | 100 % (33) |
| Texto editado | 18,1 % | 0,3 % | 34,6 % | 100 % |
| Todas (referencia) | 16,8 % | 0,05 % | 50,3 % | 100 % |

- **Dentro de cada origen, faltar palabras no aumenta la probabilidad de una
  edición grande.** En WhisperX, el 49 % de las líneas con edición grande no
  tiene palabras, igual que el 50 % de todas sus líneas.
- **El total sube porque WhisperX está sobrerrepresentado.** Tiene el 30 % de
  las líneas, pero el 44 % de los fines movidos más de 500 ms y el 49 % de los
  inicios movidos.

## f) Ediciones de timing y ediciones de texto en la misma línea

| | Con edición de texto | Referencia (todas las líneas) |
|---|---|---|
| Fines movidos más de 500 ms | **47,7 %** | 24,9 % |
| Inicios movidos | **57,5 %** | 24,9 % |

- **La inversa es más fuerte.** De las 755 líneas con texto editado, el
  **74,4 %** tiene además una edición de timing mayor a 500 ms, en el inicio o
  en el fin.
- **Corregir solo el texto es raro:** 54 líneas en 100 jobs.

## g) Estribillos y texto repetido

Se mide qué parte de cada grupo tiene un texto normalizado que aparece 2 o
más veces en la canción.

| Grupo | Texto repetido |
|---|---|
| Fines movidos más de 500 ms | 42,1 % |
| Inicios movidos | 36,4 % |
| Texto editado | 27,6 % |
| Todas (referencia) | 43,6 % |

Las líneas repetidas **no** están sobrerrepresentadas entre las ediciones
grandes. Entre las de texto están subrepresentadas.

## h) Tiempo del operador por línea

No hay eventos por línea: los latidos traen la revisión, no la línea. El
tiempo activo (huecos de 25 s o menos entre latidos, todas las sesiones)
entre dos versiones guardadas consecutivas se reparte entre las líneas que
cambiaron en esa versión. Cada una se lleva a su línea aprobada con el
emparejador del AuditLog. En total quedan atribuidos 733 minutos.

| Tipo de línea | Líneas | Con tiempo atribuido | Mediana | p75 | Total |
|---|---|---|---|---|---|
| **Timing > 500 ms + texto** | 562 | 545 | **20,3 s** | 36,7 s | **259 min (35 %)** |
| Timing > 500 ms, sin texto | 538 | 524 | 5,6 s | 16,5 s | 131 min (18 %) |
| Solo texto | 54 | 53 | 19,0 s | 51,6 s | 29 min (4 %) |
| Timing de 50 a 500 ms, solo timing | 906 | 855 | 4,7 s | 7,8 s | 139 min (19 %) |
| Sin editar | 839 | 271 | 0,5 s | 0,7 s | 9 min |

- **Una línea con texto y timing grande cuesta unas 4 veces más** que una con
  timing grande sola. Suman el 35 % del tiempo atribuido, unos 2,6 minutos
  por job.
- **El costo viene de la combinación.** Corregir solo el texto cuesta lo mismo
  (19 s), pero casi nunca pasa sin que haya que mover el timing.

## i) Realinear la línea al guardar una corrección de texto

La regla que acordaste para el punto i se cumple: el 74 % de las ediciones de
texto viene con timing mayor a 500 ms. Sigue la estimación.

**Techo con una ventana fija.** Mide qué parte de las líneas con timing
mayor a 500 ms más texto tiene el timing aprobado dentro de la ventana de
máquina ± margen:

| Margen | ±1 s | ±2 s | ±4 s |
|---|---|---|---|
| Timing > 500 ms + texto (563) | **48 %** | 68 % | 86 % |
| Timing > 500 ms, sin texto (548) | 53 % | 75 % | 89 % |

Con el margen de 1 s que pedías, el alineado no puede encontrar la posición
correcta en más de la mitad de los casos. La propuesta debería usar 2 s, o
ampliar la ventana si el score queda bajo.

**Qué se reutiliza**

- **Alineado por ventana:** `ctc_align.align_structural_window`. Ya alinea
  texto contra una ventana de audio con el modelo CTC aprobado y devuelve
  inicio y fin por palabra con su score. Hoy exige entre 2 y 8 líneas y una
  ventana de 2 a 45 s; hace falta una variante de 1 línea (unas 60 líneas
  nuevas, sin tocar la existente).
- **Stem y modelo:** la caché del stem (`vocal_sep.separate_vocals(...,
  cache_only=True)`) y la carga del modelo (`ctc_align._load_model`).
- **Ejecución:** `/reanchor` ya corre el CTC local en la api y tiene modo
  asíncrono (`async_mode`, 202 + consulta de estado), y la misma ruta sirve.
- **Interfaz de propuesta:** `LyricReviewPanel` ya muestra ítems de propuesta
  que se aceptan con Enter y se descartan con ⌫.

**Esfuerzo estimado:** 32 a 48 horas.

| Pieza | Horas |
|---|---|
| Backend: variante de 1 línea, endpoint `POST /editor/{job}/lines/{id}/realign` que devuelve la propuesta sin escribir, umbral de score, tests | 12–16 |
| Frontend: al confirmar una corrección de texto se pide la propuesta; tarjeta "inicio–fin propuestos, Enter acepta, Esc descarta"; evento de telemetría | 12–16 |
| Ejecución: api o worker, stem en caché, concurrencia con el lock de CTC | 4–8 |
| Medición: tasa de aceptación y segundos por corrección antes y después | 4–8 |

**Riesgos**

- **Líneas repetidas.** Con texto idéntico y la ventana ampliada a ±2–4 s, el
  CTC puede engancharse a la repetición vecina. Aunque las repetidas no están
  sobrerrepresentadas (punto g), la ventana más grande que hace falta aumenta
  el riesgo. Mitigación:
  - recortar la ventana en los bordes de las líneas vecinas con el mismo
    texto;
  - proponer solo cuando la ventaja de score sobre la segunda mejor posición
    sea clara.
- **Líneas sin voz.** El alineado forzado siempre devuelve algo, incluso sobre
  instrumental o público. Hay que declinar con score bajo (umbral sobre
  `ctc_mean_score` o `ctc_lr`, calibrado con estos 563 casos) y mostrar "sin
  propuesta" en vez de una posición inventada.
- **Latencia (estimada, no medida).** El modelo (wav2vec2-large) tarda de 10
  a 30 s en cargar en frío y alinear una ventana de 5–10 s en caliente tarda
  de 0,5 a 2 s en CPU.
  La primera vez por job hay que bajar el stem. En la api suma memoria justo
  después del recorte de memoria de 1.1.102. Conviene el modo asíncrono, con
  la propuesta llegando unos segundos después de guardar.
- **Idioma.** El modelo es para español. En jobs en inglés el score cae y la
  propuesta debería declinar sola.
- **Techo.** Aun con un alineado perfecto, ±2 s cubre el 68 %. El resto son
  líneas reubicadas por más de 2 s que siguen siendo manuales.
