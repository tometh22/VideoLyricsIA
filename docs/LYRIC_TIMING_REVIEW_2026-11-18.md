# Avisos de sincronización en la Revisión rápida (2026-11-18)

Código: `lyricgen/backend/lyric_timing_review.py` (puro, sin I/O) y la regla
`_rule_voice_timing` de `lyric_review.py`. Medición:
`lyricgen/backend/scripts/measure_lyric_timing_review.py` (datos privados,
fuera del repo). Costo de la medición: US$ 0, sin audio ni APIs.

## Qué avisa

| Aviso | Condición | Arreglo de un clic |
|---|---|---|
| Entra antes de la voz | WhisperX ubica la primera palabra ≥ 0,6 s después del inicio y lo confirma el testigo whisper-1; o, sin testigo, la primera palabra CTC está estirada (≥ 0,8 s) con ese comienzo adentro y son ≥ 1,0 s sin voz | Inicio = voz − 0,1 s |
| Línea en pantalla sin voz | Lo mismo, con ≥ 2 s (o ≥ la mitad de la línea) sin que se cante | Inicio = voz − 0,1 s |
| Entra tarde | CTC y WhisperX coinciden (±0,3 s) en que la primera palabra se canta ≥ 0,5 s antes de la línea | Inicio = voz − 0,1 s |
| Se va antes de que termine la voz | WhisperX y el testigo siguen oyendo la última palabra ≥ 1 s después del fin; CTC, si la tiene, no lo contradice (±0,5 s) | Fin = voz + 0,15 s |

- Ningún arreglo pisa a la vecina: inicio ≥ fin anterior + 0,05 s, fin ≤
  inicio siguiente − 0,05 s (lo mismo que `retime` del editor). Si no hay
  lugar para mover al menos 0,3 s, no hay aviso.
- Nunca cambia el texto ni los cortes, y nunca cambia tiempos solo.
- El editor no aplica el arreglo si la línea ya no está donde la vio el
  servidor (`expect_start`/`expect_end`, ±0,05 s).
- No se avisa en líneas de una sola palabra (0 de 11 avisos así eran
  correcciones) ni sin WhisperX de canción entera.
- Las palabras CTC de la línea sólo cuentan si son exactamente su texto y
  están cerca de la línea (una persona pudo reescribirla o moverla).
- La línea se ubica en cada oído con un alineamiento por texto que prefiere
  la repetición más cercana en el tiempo (un coro repetido dentro de la
  ventana hacía parecer la línea corrida varios segundos).

**Obligatorio (inequívoco)** = el testigo whisper-1 confirma lo que miden los
alineadores. CTC y WhisperX usan el mismo modelo acústico (wav2vec2 español):
su acuerdo solo no prueba nada, así que sin el testigo el aviso es sugerencia.
El testigo nunca da el tiempo (su reloj es grueso), sólo confirma que la voz
todavía no empezó o todavía sigue.

## Flag

`LYRIC_REVIEW_TIMING_MODE` = `observe` (por defecto) | `enforce` | `off`.

- `observe`: los avisos aparecen como sugerencias; no frenan nada.
- `enforce`: además los inequívocos frenan "Aprobar" (como el resto de la
  Revisión rápida, sólo donde `LYRIC_REVIEW_MODE` la hace obligatoria).
- `off`: no se calculan.

Por qué `observe` por defecto: sobre las versiones que UMG aceptó deja 0,20
avisos por canción (35 de 201 canciones con alguno), ninguno bloquea ni
mueve tiempos, y así se valida en el preview de staging sin tocar variables
de Railway. `enforce` queda para cuando una tanda real confirme que los
revisores aceptan la mayoría.

## Por qué `_rule_timing` casi no marcaba

1. **Estaba desconectada.** El 30-09-2026 (3fe23b94) se sacó la llamada de
   `build_review` porque sobre 298 canciones aprobadas daba 52 avisos y
   acertaba 1 pedido. Desde entonces no corría.
2. **Medía con el reloj equivocado.** Exigía que el testigo whisper-1 y las
   palabras de la máquina coincidieran (±0,4 s) en la última o primera
   palabra. Los tiempos por palabra de whisper-1 están interpolados, y en las
   canciones con WhisperX las palabras de la máquina son las mismas que el
   testigo debería contradecir. Sobre estos datos: marca 6 de 79 puntos de
   timing (control 2,5 %) con 0,44 avisos por canción aceptada; casi lo mismo
   en la entregada (99) que en la final (89): no distingue lo que UMG pidió.

La regla vieja se borró; la nueva la reemplaza.

## Medición

Datos: 201 canciones con pedidos de UMG (sep-nov 2026; 139 Argentina, 62
Chile), letra ENTREGADA, versión FINAL aceptada y los pedidos. Cada pedido se
parte en puntos; un punto es "de timing" si habla de sincronizar, alargar,
adelantar, atrasar, entrar antes/tarde, durar, coincidir con la voz, etc.
(79 con tiempo citado), o "Desde X hasta Y" (Argentina, 187). Un punto queda
**marcado** si algún aviso sobre la entregada cae en una línea que contiene
el tiempo citado (±1,5 s) o propone un tiempo a ±1,5 s. **Control**: los
mismos tiempos corridos ±50 s.

| | Puntos | Marcados | Control ±50 s |
|---|---|---|---|
| Timing (todos los avisos) | 79 | **16 (20,3 %)** | 3,4 % |
| Timing (sólo obligatorios) | 79 | **15 (19,0 %)** | 3,4 % |
| "Desde X hasta Y" | 187 | 19 (10,2 %) | 10,8 % |
| Regla vieja `_rule_timing`, timing | 79 | 6 (7,6 %) | 2,5 % |

| | Entregada | Final aceptada |
|---|---|---|
| Avisos | 79 (0,39/canción) | **42 (0,21/canción)** |
| Obligatorios | 75 | **41 (0,20/canción)** |
| Puntos en el panel (agrupados por coro) | 73 | 40 (35 canciones de 201) |

- De los 79 avisos sobre la entregada, **47 (59 %)** coinciden con lo que
  después corrigió el operador (mismo borde, mismo sentido, ≥ 0,25 s); 37
  quedaron a ±0,6 s del tiempo final.
- Por tipo (aciertos / no corregidos en la entregada; avisos en la final):
  entra antes 19/12, 16; se va antes 20/14, 20; entra tarde 4/2, 2; sin voz
  4/2, 4.
- Los aciertos se concentran: 20 de "se va antes" vienen de 8 canciones.
- Mitades por hash del job (los umbrales se eligieron sobre todo el
  conjunto): timing 26 % vs control 5 % y 14 % vs 2 %; final 0,23 y 0,19
  avisos por canción.
- **"Desde X hasta Y" no supera al azar.** Esos puntos son casi siempre
  reacomodos de texto (unir o partir líneas) o líneas que UMG quiere hasta un
  tiempo que los alineadores no ven: en la mayoría de los fines que pidió
  alargar, la última palabra de CTC/WhisperX ya terminaba donde terminaba la
  línea. Es la
  nota sostenida: el alineado forzado corta la vocal larga. Eso necesita el
  audio (`timing_review_suggestions.py`, pitch sobre el stem, detrás de
  `QUALITY_TIMING_OPERATOR_SUGGESTIONS_ENABLED`, que corre en el worker y
  hoy está apagado en staging); no se puede ver con lo guardado.
- Lo que no se detecta: pedidos de "sincronizar mejor desde X a Y" sin un
  borde concreto, cortes de la voz que los alineadores no oyen, líneas sin
  evidencia de WhisperX (~20 % de las líneas) y la duración de las notas
  largas.

Los avisos sobre versiones finales cuentan como falsos positivos porque UMG
las aceptó, pero no todos lo son: hay casos con las tres fuentes de acuerdo
(una primera palabra estirada más de 1 s sobre el instrumental) que UMG no
pidió corregir.

## Umbrales que se probaron

Con todo lo demás fijo (puntos de timing marcados / avisos por canción
aceptada; elegido: inicio 0,6 s y fin 1,0 s → 20 % / 0,21):

| Umbral | Marcados | Avisos en finales |
|---|---|---|
| Entra antes ≥ 0,4 s | 23 % (control 7 %) | 0,38 |
| Entra antes ≥ 0,8 s | 18 % | 0,16 |
| Entra antes ≥ 1,0 s | 14 % | 0,14 |
| Se va antes ≥ 0,6 s | 20 % | 0,34 |
| Se va antes ≥ 0,8 s | 20 % | 0,24 |
| Se va antes ≥ 1,5 s | 19 % | 0,14 |

Las variantes sin testigo (CTC+WhisperX solos) acertaban 6 correcciones con
20 avisos en finales para el inicio y 1 con 8 para el fin: el inicio sólo se
conserva con ≥ 1 s sin voz y como sugerencia; el fin sin testigo se descartó.
