# Segmentación de líneas, validación del motivo repetido y estimado de partir/unir (2026-10-05)

No hay cambios de producto. Los scripts son de solo lectura:

- `scripts/diagnose_segmentation.py`
- `scripts/simulate_motif_ctc_splice.py`

**Muestra:** los mismos 100 jobs UMG de staging de las fases 1 y 2. Para no
cargar la base durante el horario del operador, el diagnóstico de
segmentación se corrió sobre la copia local del 4-oct.

## 1. Segmentación

### a) Qué hace el operador con cada línea de máquina

Texto de máquina y texto aprobado se alinean palabra por palabra.

| Origen | Líneas | Igual | Unida | Partida | Partida y unida | Borrada o reescrita |
|---|---|---|---|---|---|---|
| Todas | 3.228 | **77,5 %** | **10,5 %** | 3,5 % | 3,3 % | 5,2 % |
| CTC | 2.149 | 82,3 % | 9,1 % | 2,4 % | 2,9 % | 3,3 % |
| WhisperX | 476 | 53,8 % | **19,1 %** | 6,3 % | 7,8 % | 13,0 % |
| WhisperX reconciliado | 546 | 81,0 % | 9,7 % | 2,9 % | 0,9 % | 5,5 % |

El operador **une tres veces más de lo que parte**. En WhisperX, casi la
mitad de las líneas cambia de estructura.

### b) Duración y palabras por línea

| | p25 | Mediana | p75 | p90 |
|---|---|---|---|---|
| Duración, máquina | 2,1 s | 3,2 s | 4,4 s | 5,7 s |
| Duración, aprobada | 2,2 s | 3,2 s | 4,3 s | 5,5 s |
| Palabras, máquina | 3 | 5 | 7 | 9 |
| Palabras, aprobada | 3 | 5 | 7 | 9 |

El largo típico no cambia. El operador no tiene otro "tamaño ideal" de línea:
corrige cortes puntuales.

### c) Dónde corta y dónde une el operador

| | Cantidad | Pausa acústica en ese punto: mediana (p25 / p75 / p90) | Referencia |
|---|---|---|---|
| Cortes que **agregó** (213; 172 con palabras medibles) | 213 | 0,10 s (0,02 / 0,46 / 0,86) | Pausas internas que **no** cortó: 0,04 s (p90 0,14) |
| Cortes del pipeline que **eliminó** al unir (249; 218 medibles) | 249 | 0,26 s (0,12 / 0,60 / 1,16) | Bordes del pipeline que **respetó**: 0,96 s (p25 0,50) |

- **Cuando parte:**
  - el 68 % de los cortes cae en un signo de puntuación;
  - el 46 % separa un fragmento que se repite en la canción (estribillo);
  - el 19 % coincide con una rima.

  La línea que parte es larga: mediana de 9 palabras y 5,0 s (p75: 13 palabras y 5,9 s). La pausa ayuda, pero no decide: la mitad de los cortes cae en pausas de 0,1 s o menos.
- **Cuando une:** el pipeline había cortado en una pausa corta (0,26 s de
  mediana) y el operador la ignora. Los bordes que respeta tienen pausas
  mucho mayores (0,96 s).

### d) Guía de estilo (`docs/UMG_GUIA_ESTILO_LETRAS.md`)

La guía no fija largo ni máximo de palabras. Sobre la estructura dice
únicamente "Líneas de largo parecido; repeticiones cortas por separado"
(línea 38), y la marca como no automatizada.

El operador la sigue: la variación del largo dentro de cada canción
(coeficiente de variación de palabras por línea) baja de 0,46 en la máquina a
0,38 en lo aprobado (medianas), y el 46 % de sus cortes aísla repeticiones.

### e) Simulación de los parámetros de corte

La métrica es el porcentaje de líneas con los dos bordes en un borde
aprobado. Se simularon 69 jobs con palabras medibles (2.088 líneas).

| Regla | Mejor combinación | Coincidencia |
|---|---|---|
| **Actual** (lo que salió del pipeline) | — | **74,6 %** |
| `WHISPERX_MAX_LINE_S` (3–8 s), aplicado sobre las líneas actuales | 8 s | 72,6 % (con 3 s: 26,3 %) |
| Corte de `post_reconcile` (palabras 5/7/9 × duración 2/3/4 s × pausa 0,4/0,7/1,0 s) | 9 palabras, 2 s, 1,0 s | 74,3 % |
| `phrase_segmenter` sobre las líneas actuales (27 combinaciones) | target 7, max 13, 6 s | 71,0 % |
| `phrase_segmenter` sobre toda la canción | target 8, max 11, 4 s | 30,9 % |
| Extra: **unir** vecinas con pausa corta | pausa < 0,4 s, ≤ 12 palabras, ≤ 7 s | 77,0 % |

- **Ninguna combinación de los parámetros actuales supera a lo que ya sale.**
  Todas esas reglas solo agregan cortes, y el operador sobre todo une. Con la
  mejor combinación de los parámetros actuales desaparecen **0** ediciones de
  "texto más timing grande".
- **La regla inversa, unir con pausa corta, tampoco sirve.** Con pausa menor a
  0,3 s reproduce 68 de las 352 uniones del operador (19 %), pero borra 93
  bordes que el operador había dejado. Con 0,4 s: 83 uniones acertadas y 147
  bordes borrados. **No recomiendo tocar parámetros de corte.**

## 2b. Motivo corto repetido: CTC en toda la canción, timing actual dentro del motivo

Son los 20 jobs que hoy no salen por CTC por ese motivo. Los 20 se alinearon
con el stem; el motivo ocupa 86 de 841 líneas (10 %).

| Líneas fuera del motivo | Líneas | Inicio a ±150 ms: hoy / CTC | Fin a ±150 ms: hoy / CTC |
|---|---|---|---|
| Todas | 680 | 61,5 % / 60,2 % | 26,2 % / 25,4 % |
| Mismo texto | 379 | 77,6 % / 76,3 % | 31,7 % / 31,4 % |

**Sin mejora medible.**

- Hay un sesgo a favor de hoy: en las líneas que el operador no tocó, lo
  aprobado es lo que salió del pipeline.
- La simulación de CTC usa el adelanto único de 80 ms; lo que salió hoy, el
  doble de 160 ms.

Con datos retrospectivos no se puede demostrar que convenga. Si se quiere
probar, sería con una prueba prospectiva como la del aire mínimo. Hoy no
recomiendo rutear estos jobs.

## 3. Estimado: partir y unir con reparto de timing por palabra

**Lo que ya existe en staging:**

- Partir con Enter en la posición del cursor y con "Dividir" usa
  `planSegmentSplit`: reparte las palabras entre las dos mitades con sus
  tiempos reales (fd8d225e, 2-oct).
- "Unir con la línea siguiente" concatena las palabras de ambas.

**Lo que falta, con su peso en los datos:**

| Hueco | Peso | Horas |
|---|---|---|
| Líneas **sin** palabras: al partir se reparte por proporción de caracteres; al unir se descartan las palabras de los dos lados | El 19 % de los cortes que agregó el operador y el 12 % de sus uniones no tienen palabras utilizables; el 50 % de las líneas WhisperX no tiene palabras (CTC: 0,05 %) | 8–16 h si el pipeline guarda las palabras de WhisperX reconciliado. 24–40 h si se alinea a pedido con CTC por línea (misma infraestructura que el realineado: worker propio y latencia) |
| **Palabras viejas después de corregir el texto**: si el operador corrige y después parte, las palabras ya no coinciden con el texto y el corte cae al reparto por caracteres | Es el caso de "texto más timing grande" | 6–10 h: rearmar las palabras sobre el texto nuevo, conservando los tiempos de las palabras que no cambiaron e interpolando las nuevas |
| Unir cuando solo un lado tiene palabras: hoy las descarta | — | 3–5 h: conservar e interpolar |
| Medición: evento por corte y unión con el modo usado (palabras o caracteres) | — | 2–4 h |
| **Total** | | **19–35 h** (o 35–60 h con alineado a pedido) |

**Riesgos:**

- **Líneas sin timestamps por palabra:** sin palabras no hay reparto real.
  Interpolar puede dejar el corte a cientos de ms del canto. Hay que marcar la
  línea resultante como "timing aproximado" para que el operador la escuche.
- **WhisperX sin palabras:** hoy pesa el 50 % en ese origen. Guardar las
  palabras en el pipeline es el arreglo de fondo, pero toca el pipeline.
- **Beneficio incierto.** Partir es el 3,5 % de las líneas y unir el 10,5 %.
  Unir ya conserva el timing cuando hay palabras. El ahorro real depende de
  cuánto del tiempo de esas líneas es timing y cuánto es escuchar y decidir.
  Antes de construir conviene medir con un evento cuántas veces se parte sin
  palabras.
