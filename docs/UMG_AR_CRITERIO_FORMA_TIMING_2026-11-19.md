# UMG Argentina: criterio de forma y timing (medido 19-11-2026)

Banco: 201 canciones con pedidos de cambio (sep–nov), versión entregada (la que
vio UMG) contra la versión final aceptada. Solo cifras agregadas; los datos
viven fuera del repo.

## Qué eran los puntos "la pantalla ya tenía esas palabras"

La clasificación anterior (palabras sin tildes, como conjunto, en ±4/8 s) contaba
191 puntos como "forma" (190 de Argentina). Al comparar línea entregada contra
final, más la mitad eran texto:

| Qué cambió de verdad | puntos (de 191) |
|---|---|
| Tilde (llevás, allá, él...) | 38 |
| Palabras (repetición, "y"/"en", palabra cambiada) | 24 |
| Puntuación/mayúsculas/paréntesis | 19 |
| Línea agregada (repetición faltante) | 8 |
| Recorte de una palabra sobrante / sin cambio visible | 7 |
| **Segmentación** (partir 34, mover el corte 8, unir 3) | 45 |
| **Timing** (inicio 12, fin 16, ambos 22) | 50 |

Sumando los 168 puntos de timing (112 "el revisor lo movió y quedó mal", 56 "quedó
el de la máquina"), los 359 puntos de forma/timing son: sólo fin 108, sólo inicio 27, inicio y fin 49,
segmentación 50, texto 125. 344 son de Argentina.

## Reglas encontradas

1. **"Desde X hasta Y" es un intervalo exacto.** 139 puntos lo usan. La final
   empieza en [X, X+1) en 106 (76 %) y termina en [Y, Y+1) en 107 (77 %). La
   entregada terminaba antes de Y en 87.
2. **La línea dura lo que dura la voz (nota sostenida incluida).** 116 pedidos de
   alargar el fin (112 de una sola línea). Mediana +1,27 s (p90 +3,3). La entregada
   terminaba en la última palabra (mediana 0,00 s después; p90 0,50). La final
   termina en mediana 1,44 s después de esa palabra. En las líneas aceptadas sin
   pedido, la mediana es +0,27 s y el 75 % tiene ≤0,5 s. Ningún hold fijo copia esto:
   1,5 s resolvería 36/112 pero movería el 44 % de las líneas aceptadas. El fin
   depende del audio.
3. **Una línea que termina en la última palabra se reclama 7 veces más.** En
   Argentina, las líneas sin hold que nacieron en una re-escritura del servidor
   (versión `migration`, donde cae el re-anclado) tienen un 8,0 % de pedidos de
   alargar (35 de 435). Con el hold de 0,5 s, un 1,1 % (8 de 719). De los 47
   pedidos de alargar cuyo fin se puede rastrear, 39 terminaban en la última
   palabra y 35 venían de esa re-escritura.
4. **No quedarse en pantalla cuando no se canta.** 41 pedidos de acortar
   (mediana −1,8 s). La entregada llegaba casi hasta la línea siguiente (aire
   mediano 0,11 s) y la final deja 1,1 s. La final termina 1,8 s antes de la última
   palabra que tenía la entregada: las palabras estaban mal alineadas. Es un error
   del alineador, no una regla de presentación.
5. **Entrar con la voz.** 51 pedidos de atrasar el inicio (mediana +1,2 s) y 24 de
   adelantarlo (−0,7 s). Al atrasar, la entregada estaba pegada a la línea anterior
   (aire mediano 0,05 s) y la final empieza 1,25 s después de la primera palabra
   que tenía: otra vez son palabras mal alineadas. El adelanto que UMG acepta es el
   actual: primera palabra − inicio = 0,08 s de mediana (`LYRIC_LEAD_IN_MS=80`).
   Nada pide más lead-in.
6. **Un cartel = una frase cantada.** 50 puntos sobre 46 líneas. Las partidas tienen
   44 caracteres de mediana (aceptadas: 25, p90 41) y duran 5,0 s. Pero 18 de las 34
   no superan 8 palabras ni 42 caracteres. Son frases cortas con una pausa
   adentro, por ejemplo un nombre suelto antes de la frase. En 9 de los 12 cortes
   medibles, UMG corta en la pausa más larga (mediana 0,35 s) y en 7 hay puntuación
   antes del corte. Una regla de "pausa ≥0,5 s" capta 5 de 12, pero también 157
   líneas aceptadas de Argentina (7 %). No es una regla precisa.
7. **Nunca dejar colgado un artículo o preposición** (una línea que termina en "para" o "una"). Solo
   3 de las 46 líneas, y aparece en el 1,3 % de las aceptadas. Es la regla
   `open_ended_line` que ya tiene `display_segmentation`.
8. **Voces de fondo entre paréntesis.** 28 puntos agregan paréntesis (coros,
   respuestas, recitados).

## Qué contradice el sistema

- **El re-anclado no aplica el hold** (`_maybe_anchor_align` y
  `_reanchor_execute` no pasan por `lead_in.polish`): cada línea re-alineada se
  apaga en el último fonema. Es la causa de la regla 3. Corregido detrás de
  `REANCHOR_HOLD_MODE` (este PR).
- **`STABLE_PITCH_TAIL_ENABLED=0`** (en la paridad de la flota): es justo el
  mecanismo que alarga hasta el fin de la nota sostenida (regla 2). No se puede
  medir sin audio en este banco. Propuesta: backtest con los stems de los 112
  pedidos y, si pasa, cargarlo como variable compartida en los 6 servicios. No
  se toca por env desde acá.
- **`LYRIC_HOLD_S=0,5`**: compatible con lo que UMG acepta (75 % de las líneas
  aceptadas). Subirlo no resuelve la regla 2 (ver números arriba). No proponemos
  cambiar el valor.
- **`display_segmentation` (8 palabras / 42 caracteres)**: el tope coincide con el
  p90 de lo aceptado, pero el criterio de UMG es la pausa, no el largo. El
  planner propone cortar 3 de las 34 líneas pedidas y 53 líneas aceptadas de
  Argentina. Ni bajar a 7 / 36 ayuda (4 de 34, 87 aceptadas). No hay cambio claro.
- **Lead-in**: sin contradicción.

## Antes/después de `REANCHOR_HOLD_MODE=apply` (offline)

Simulado sobre la versión entregada, en las líneas que nacieron en una
re-escritura del servidor y terminan en la última palabra:

- Pedidos de alargar el fin: 32 de 112 tocados, los 32 quedan más cerca del fin
  aceptado. A ≤0,5 s del fin final: 8 → 17.
- Pedidos de acortar: alarga 1 de 41.
- Líneas aceptadas sin pedido que cambian: Argentina 397 de 3231 (12,3 %, +0,39 s de
  media, máx +0,5), Chile 20 de 1493 (1,3 %). Quedan con el mismo hold que UMG
  acepta en el resto. Afecta a 35 canciones.

Rollout sugerido: `observe` en staging (la respuesta de `/reanchor` y el audit
`lyrics.reanchor` traen `hold`). Después, `apply` en staging, antes de producción.
El flag sólo lo lee la API (el re-anclado corre ahí) y no entra en
`runtime_timing_config`.

## Borrador de pregunta a UMG Argentina

> Hola, para dejar de hacerles repetir pedidos queremos fijar dos criterios con
> ustedes:
>
> 1. **Fin de cada línea.** En [canción A, m:ss] y [canción B, m:ss] nos pidieron
>    que la línea siga hasta que termina la nota. ¿La regla es "la línea queda hasta
>    que termina la voz, aunque haya silencio antes de la siguiente"? Cuando hay un
>    instrumental largo, ¿prefieren que la pantalla quede vacía o que la última
>    línea quede hasta la siguiente?
> 2. **Cortes de línea.** En [canción C, m:ss] separaron una palabra suelta (un
>    nombre) de la frase que sigue, aunque la línea era corta. ¿Cortamos siempre
>    donde el cantante hace una pausa, aunque la frase entre en una pantalla? ¿Hay un
>    largo máximo por línea que prefieran?
>
> Gracias.

(Los ejemplos concretos están en el reporte interno del 19-11. No se copian acá.)
