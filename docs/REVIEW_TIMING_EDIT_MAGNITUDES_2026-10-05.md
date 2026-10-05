# Magnitud de las ediciones de timing: insumo para una tolerancia de revisión (2026-10-05)

No hay cambios de producto. El script es
`lyricgen/backend/scripts/report_timing_edit_magnitudes.py`, de solo lectura y
con los mismos datos que la fase 2. Compara la versión de máquina contra la
aprobada, línea por línea emparejada. Una edición es una diferencia de 50 ms o
más en un borde, el mismo umbral que la fase 1.

Las dos cohortes:

- **Agosto**: umg-gold-v1, 57 jobs aprobados en el portal (48 en agosto y 9 en
  julio). Lo aprobado es la versión entregada, así que puede incluir
  correcciones posteriores a la primera aprobación.
- **Septiembre**: los 100 jobs de staging de la fase 1, con primera
  aprobación entre el 10-sep y el 4-oct.

## Porcentaje de ediciones por debajo de cada magnitud

| Cohorte | Borde | Ediciones | < 100 ms | < 150 ms | < 200 ms | < 300 ms | < 500 ms |
|---|---|---|---|---|---|---|---|
| Septiembre | Inicio | 930 | 15,3 % | 21,5 % | 26,3 % | 34,2 % | 42,4 % |
| Septiembre | Fin | 2.067 | 2,8 % | 8,1 % | 14,5 % | 33,8 % | 53,8 % |
| Agosto | Inicio | 241 | 9,1 % | 12,5 % | 17,8 % | 27,0 % | 37,8 % |
| Agosto | Fin | 608 | 4,6 % | 8,6 % | 13,0 % | 22,9 % | 36,8 % |

En agosto hay 16 casos con la salida de máquina estimada. Sin ellos quedan 41
jobs, y los inicios bajo 150 ms son el 5,5 % y los fines el 9,3 %.

## Líneas y jobs

| | Septiembre | Agosto |
|---|---|---|
| Líneas editadas, sin contar las altas | 2.402 | 846 |
| Líneas cuya única edición fue de timing y menor a 150 ms | 116 (**4,8 %** de las editadas) | 58 (6,9 %) |
| La misma cifra, sobre las líneas con edición de timing | 5,4 % | 8,6 % |
| Altas (líneas que agregó el operador) | 267 | 56 |
| Jobs sin ninguna edición de timing | 4 % | 7 % |
| Jobs que quedarían sin edición de timing con tolerancia de 150 ms | 4 % | 7 % |
| Jobs que quedarían sin edición de timing con tolerancia de 500 ms | 4 % | 12 % |

## Lectura

- **Las ediciones chicas son minoría.** En septiembre, solo el 8 % de las
  ediciones de fin y el 21 % de las de inicio están por debajo de 150 ms. El 46 %
  de las de fin supera los 500 ms.
- **Una tolerancia de 150 ms ahorraría poco.** Afectaría a menos del 5 % de
  las líneas editadas, y no deja ningún job de septiembre sin ediciones de
  timing: todos los que tienen alguna tienen al menos una de 500 ms o más.
- **Las ediciones chicas que hay están en los inicios.** El 15 % de las
  ediciones de inicio queda debajo de 100 ms. Es consistente con la fase 2:
  el adelanto sobrante en las líneas WhisperX.
- **Los fines se corrigen entre 200 y 500 ms.** Es el rango del aire que el
  operador deja antes de la línea siguiente. Ese rango lo mide la prueba
  prospectiva de `LYRIC_MIN_GAP_MS`, no una tolerancia.
