# Timing no confiable a nivel canción (20-nov-2026)

Banco: 220 canciones aprobadas en staging (6.728 líneas). "Línea mal ubicada" =
el operador movió el inicio más de 1 s respecto de la primera palabra de la
máquina. "Canción mala" = más del 25 % de sus líneas mal ubicadas: 32
canciones, 354 de las 725 líneas malas.

## Qué hay detrás de esas líneas
De las 725 líneas, en las 32 canciones malas sólo 66 son las mismas palabras
corridas en el tiempo (+25 corridas cortas). El resto es letra que la máquina no
puso donde el operador la dejó: 107 con otras palabras a más de 10 s, 77 sin
palabras de máquina ahí, 48 repeticiones del estribillo. Es decir: gran parte
de "timing mal" es texto faltante o mal oído, y re-alinear no lo arregla.

## Detector (`song_timing_trust.py`)
Cinco señales de la canción, regresión logística ajustada en el banco:
mediana del score por palabra, segundos de canto sin cartel, proporción de
líneas con más de 1,2 s por palabra, palabras del testigo whisper-1 fuera de
todo cartel y parecido con la transcripción de control de Gemini. La fuente de
timing, las palabras apretadas y `timing_validation.flagged` no separan (AUC
0,41–0,64 y 0,54).

Validación cruzada 5x20 (AUC 0,75):

| umbral | canciones marcadas | precisión | recall | sanas marcadas |
|---|---|---|---|---|
| 0,50 | 20 % | 0,33 | 0,46 | 16 % |
| **0,75 (usado)** | 16 % | 0,34 | 0,39 | 12,7 % |
| 1,00 | 13 % | 0,37 | 0,33 | 9,3 % |

En la muestra de ajuste: marca 40, precisión 0,38, recall 0,47. Al azar la
precisión sería 0,15. Por línea: dentro de las canciones malas, las 3 líneas
más sospechosas fueron líneas movidas por el operador el 48 % de las veces (al
azar 29 %; AUC de línea 0,63). Es una señal de prioridad, no de certeza.

## Re-alineado con letra oficial (medición con audio, local, US$0)
Demucs + CTC locales sobre las 38 canciones accesibles con letra oficial
(planilla, lrclib). Se copia a cada línea de la máquina el tiempo de SUS
palabras en el alineado contra la letra oficial; el texto no cambia.
30 midieron, 7 declinó el CTC, 1 línea apretada.

- Aplicado en 18 canciones (494 líneas): líneas malas 24 -> 20; 5 arregladas, 1
  roto; líneas a menos de 0,3 s del humano 449 -> 449.
- En las canciones malas el re-alineado se aplicó en 1 de 8 medibles y no
  arregló ninguna línea (5 -> 5). Las otras: el texto oficial no cubre las
  líneas (otra versión), el CTC declinó o la atestación no lo certifica.
- Conclusión: con estos datos no acerca las palabras a lo que dejó el humano
  donde importa. Por eso `SONG_TIMING_TRUST_MODE` queda en `observe` y `apply`
  no se recomienda.

## Riesgos
Precisión 0,34: dos de cada tres avisos son canciones que el operador dejó
casi igual. Banco chico (32 positivos), un solo corte, sin muestra fuera del
banco.
