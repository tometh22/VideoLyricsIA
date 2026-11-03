# Realineado tras corregir el texto: simulación sobre los casos históricos (2026-10-05)

No hay cambios de producto. Las piezas usadas:

- `ctc_align.align_line`: alinea una línea en una ventana. Es nueva y no se usa en producción.
- `scripts/simulate_realign_windows.py`: la simulación.

**Casos:** 602 líneas de los últimos 100 jobs UMG de staging que tienen el texto
corregido y un timing movido más de 500 ms. Las 73 canciones tenían el stem
vocal en caché en R2.

**Método:** se alinea el texto aprobado con CTC sobre la ventana de la máquina
± margen y se compara con el inicio y el fin aprobados. Cada propuesta cae en
una de tres categorías:

| Categoría | Criterio |
|---|---|
| Correcta | Inicio y fin a ±150 ms o menos |
| Plausible pero equivocada | Pasa el umbral de score, pero inicio o fin quedan a más de 500 ms |
| Cercana | El resto |

## Tabla (porcentaje sobre los 602 casos)

| Ventana | Umbral de score | Correcta | Cercana | Plausible pero equivocada | Sin propuesta |
|---|---|---|---|---|---|
| ±1 s | 0 | 5,8 % | 14,3 % | 79,9 % | 0 % |
| ±1 s | 0,6 | 4,8 % | 8,8 % | 33,7 % | 52,7 % |
| ±1 s | 0,8 | 3,3 % | 4,5 % | 12,1 % | 80,1 % |
| ±2 s | 0 | 6,0 % | 11,5 % | 82,6 % | 0 % |
| ±2 s | 0,6 | 4,5 % | 9,0 % | 45,2 % | 41,4 % |
| ±2 s | 0,8 | 3,5 % | 5,3 % | 16,3 % | 74,9 % |
| ±4 s | 0 | 4,6 % | 7,8 % | 87,5 % | 0 % |
| ±4 s | 0,6 | 4,5 % | 7,3 % | 56,0 % | 32,2 % |
| ±4 s | 0,8 | 3,5 % | 4,8 % | 23,6 % | 68,1 % |
| **Adaptativa (1 → 2 → 4 s, sin la de 4 s si el texto se repite a ±8 s)** | 0,6 | 6,5 % | 12,0 % | 52,0 % | 29,6 % |
| Adaptativa | 0,7 | 6,0 % | 9,8 % | 37,4 % | 46,8 % |
| Adaptativa | 0,8 | 5,5 % | 7,8 % | 22,3 % | 64,5 % |

## Por qué da así

1. **El 70 % de los casos no es una corrección de palabras dentro de la línea.**
   Solo 182 de los 602 casos conservan la cantidad de palabras ±1. El resto son
   líneas partidas, unidas o reescritas, y el timing aprobado corresponde a
   otra línea, no a la de máquina corregida. En las 182 correcciones de
   palabras, la ventana de ±1 s acierta igual solo el 6 % sin umbral, y el
   3,3 % con umbral 0,8.
2. **El timing aprobado cae fuera de la ventana en más de la mitad de los
   casos.** Dentro de ±1 s está el 47,7 %.
3. **El fin no coincide aunque el inicio sí.** Cuando el timing aprobado está
   dentro de ±1 s:
   - el inicio queda a ±150 ms en el 43 % de las propuestas, contra 30 % de la
     máquina;
   - ambos bordes, solo en el 10 %.

   El fin aprobado sigue el criterio del operador (aire y hold), no el fin
   acústico. Aplicar a la propuesta el adelanto de 80 ms y un hold de 0 a 0,5 s
   no lo mejora.
4. **El score no separa propuestas buenas de malas.** Al subir el umbral, las
   correctas casi no cambian (5,8 % → 3,3 %) y lo que baja es todo lo demás. Con
   el umbral que deja las equivocadas en 22 %:
   - se muestra una propuesta en el 35,5 % de los casos;
   - de lo que se muestra, el 63 % está equivocado y el 15 % es correcto.

## Umbral recomendado

**Ninguno.** No hay umbral que haga útil la propuesta: en el mejor punto, de cada 7 propuestas mostradas, 1 es correcta y 4 están equivocadas. La recomendación es **no encender** `REALIGN_ON_TEXT_EDIT` con este diseño.

Lo que sí sale de los datos:

- **El costo de "texto más timing grande" viene sobre todo de reestructurar
  líneas** (partir, unir o reescribir), no de corregir palabras. La ayuda debe
  ir a esas operaciones, por ejemplo repartiendo el timing por palabras al
  partir una línea, y no a realinear una línea fija.
- **Una variante acotada podría servir:** proponer solo el inicio, en
  correcciones de la misma cantidad de palabras. Hoy acierta el 43 % contra el
  30 % de la máquina, sobre una base chica (97 casos). Habría que validarla
  antes de construir nada.

## Qué determina que un job salga por WhisperX y no por CTC

Muestra: los últimos 100 jobs UMG aprobados. Hay 28 sin CTC: 15 WhisperX, 11 WhisperX reconciliado y 2 `synced_scaffold`.

| Causa | Jobs | De dónde sale | ¿Se puede rutear a CTC sin riesgo? |
|---|---|---|---|
| Motivo corto repetido: `short_repeated_motif_runs` declina el CTC global de toda la canción | **20 (71 %)** | Persistido: `unsafe_windows` con motivo `ctc_short_repeated_motif`. Ninguno de los 72 jobs CTC lo tiene | **No tal cual.** Es el guardia contra el caso de "Pa Pa Pa". Variante posible, a validar offline: CTC en toda la canción y el timing de la cascada solo dentro de las ventanas del motivo. La regla actual también se dispara con estribillos cortos que no son cantos (solo mira la primera palabra de líneas de 1 a 4 palabras) |
| El texto no se reconoce como español (`guess_text_lang`): inglés 2, "unknown" 2 | 4 | Reconstruido del texto | **Inglés: no**, el modelo es en español. **"unknown": sí, con poco riesgo,** si el idioma resuelto del job es español; el piso de mediana de score (0,35) sigue atajando un idioma equivocado |
| Menos de 3 líneas | 1 | Reconstruido | No, es parte del contrato del motor |
| Anclado por catálogo u operador: el CTC timeó la canción pero la etiqueta quedó `whisperx` | 1 | `pre_anchor_present` y scores CTC en las líneas | No hace falta rutear: es solo la etiqueta. Se corrige con `set_timing_source` en el anclado |
| Motivo solo en el log: colapso, mediana baja, error del stem | 2 | Log de Railway | No. Uno es Aimogasta (18dc85ecd8d6), cuyo candidato CTC declinado estaba mal |

Conclusión: el único arreglo de ruteo seguro hoy es el idioma "unknown" con el
job en español (2 jobs). El motivo repetido explica el 71 %, y bajarlo sin
validación reintroduce el error que el guardia previene. El paso siguiente
sería una validación offline del empalme (CTC fuera del motivo, cascada
adentro) sobre estos 20 jobs.
