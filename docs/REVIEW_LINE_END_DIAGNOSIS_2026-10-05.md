# Diagnóstico de los fines de línea (2026-10-05)

No hay cambios de producto. El script es
`lyricgen/backend/scripts/diagnose_line_ends.py`, de solo lectura. Usa los
100 jobs de staging de la fase 1 (UMG, primera aprobación entre el 10-sep y
el 4-oct). Una edición de fin es una diferencia de 50 ms o más entre el fin
de máquina y el aprobado.

## a) Cómo termina cada línea de máquina

| Fin de la línea de máquina | CTC (2.149) | WhisperX (1.022) | Otros (57) | Total (3.228) |
|---|---|---|---|---|
| Última palabra + hold de 0,5 s | **64,0 %** | 21,3 % | — | 49,4 % |
| Estirado a "inicio siguiente − 10 ms" porque no entraba el hold | 24,0 % | 11,9 % | — | 19,8 % |
| Estirado a "inicio siguiente − 10 ms", línea sin palabras | — | 20,6 % | 93,0 % | 8,1 % |
| Estirado más allá del hold | 0,3 % | — | — | 0,2 % |
| En la última palabra, sin hold | 4,9 % | 13,4 % | — | 7,5 % |
| Sin palabras, no estirado | 0,0 % | 29,4 % | 3,5 % | 9,4 % |
| Otro / última línea | 6,7 % | 3,4 % | 3,5 % | 5,6 % |

Lo que muestra la tabla:

- **Los fines no se estiran hasta la siguiente línea por defecto.** Siguen al
  canto: fin de la última palabra más el hold.
- **Solo se pegan a la siguiente línea cuando el hueco es menor que 0,5 s.**
  Ahí el hold se recorta a "siguiente − 10 ms". Pasa en el 24 % de las líneas
  CTC.
- **El único caso que se estira siempre son las líneas sin palabras.** Están
  en jobs WhisperX y en otros orígenes.

## b) Ediciones de fin mayores a 500 ms (955)

- **Dirección:** 59 % extiende y 41 % recorta.
- **Hold implícito** (fin aprobado − fin de la última palabra), sobre 700 líneas con palabras:
  - p10 −3,36 s, p25 −0,78 s, mediana 0,58 s, p75 1,96 s, p90 3,83 s.
  - Solo CTC: mediana 0,13 s, con p25 −0,73 s y p75 1,72 s.
- **Aire antes de la línea siguiente:** mediana 1,31 s (p25 0,60 s).

**No hay un hold estable.** El operador mueve el fin por segundos en las dos
direcciones. Es reubicar la línea, porque las palabras o el límite entre
líneas estaban mal alineados. No es un problema de presentación.

## c) Ediciones de fin entre 200 y 500 ms (813)

| | CTC | WhisperX | Total |
|---|---|---|---|
| Recortes | 88 % | 77 % | 85 % |
| Hold implícito: mediana (p25 / p75) | 0,148 s (0,000 / 0,241) | 0,017 s (−0,220 / 0,174) | 0,128 s (−0,031 / 0,241) |
| Aire antes de la siguiente: mediana (p25) | 0,38 s (0,30) | 0,39 s (0,31) | 0,38 s (0,30) |

En las líneas con el mismo texto, el hold implícito da mediana 0,128 s
(p25 −0,028 / p75 0,241).

Para comparar, en las ediciones de 50 a 200 ms el hold implícito tiene
mediana 0,315 s (p25 0,08 / p75 0,37) y el aire, mediana 0,29 s.

**Es otro fenómeno que el de (b).** Son recortes de presentación: el
operador lleva el fin cerca de la última palabra y deja unos 0,3–0,4 s de
aire. Pero el hold que elige depende de la banda (0,13 s en una, 0,32 s en la
otra), así que tampoco es un valor único.

## d) La regla "fin = última palabra + hold, nunca más allá de siguiente − aire"

Hay que leerla con cuidado, porque el diagnóstico la respalda solo en parte:

- **(a)** muestra que el fin ya es "última palabra + hold" y que se estira
  solo por el clamp.
- **(b)** no muestra un hold estable.

Por eso la simulación usa los valores de (c): hold 0,128 s y aire 0,301 s
(el p25 del aire). La última línea queda intacta, igual que en `apply_hold`.

| Regla (sobre 3.027 líneas emparejadas) | Ediciones de fin | Desaparecen | Aparecen | Neto |
|---|---|---|---|---|
| Hoy (máquina real) | **2.067** | — | — | — |
| Regla actual simulada (hold 0,5, aire 10 ms) | 2.096 | 6 | 35 | +29 (ruido del simulador) |
| **Regla de los datos (hold 0,128, aire 0,301)** | 2.709 | **221 (10,7 %)** | **863** | **+642** |
| Hold 0,25 + aire 0,30 | — | 308 | 861 | +553 |
| Hold 0,5 + aire 0,30 (= brazo B) | 2.032 | 242 | 207 | **−35** |
| Hold 0,5 + aire 0,25 | — | 187 | 197 | +10 |

Con tolerancia de 100 ms, en la que una línea aceptada solo cuenta como
edición nueva si queda a más de 100 ms de lo aprobado, los netos son casi
iguales: +642 y −45.

**Lectura**

- **La regla saca el 10,7 % de las ediciones de fin, pero crea 863 nuevas.**
  Las crea en líneas que hoy el operador acepta tal como vienen en "última
  palabra + 0,5 s": 779 en CTC.
- **El resultado es la cota peor.** Asume que el operador va a volver a
  corregir cualquier fin que no sea el que aceptó, y por el anclaje eso no se
  sabe.
- **Cualquier hold menor a 0,5 s crea más de 800 ediciones** contra ese
  criterio.
- **La única variante que no empeora en la retrospectiva es mantener el hold
  y separar las líneas pegadas:** aire de 300 ms, neto −35. Es el brazo B.

## Recomendación para la prueba prospectiva

Los datos no respaldan la regla del punto (d) lo suficiente: el hold de (b)
no es estable, y el neto retrospectivo es +642. Siguiendo tu criterio, la
prueba va con **dos brazos**:

- **A:** el comportamiento actual.
- **B:** `LYRIC_MIN_GAP_MS=300`.

La prueba prospectiva es la única forma de saber si el operador aceptaría un
hold más corto. Si querés probarla igual, el brazo C sería hold 0,13 s con
aire de 300 ms. Arriesga crear hasta 863 ediciones de fin cada 100 jobs si el
operador mantiene su criterio actual.
