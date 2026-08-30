# Realineado jerárquico — resultado al 2026-08-29

La tesis de ocurrencia quedó parcialmente confirmada: las anclas únicas matan
la mayor parte de los saltos grandes, pero todavía no convierten el timing en
una categoría automática completa.

## Comparación limpia: mismas 18 canciones

- Alineador stem anterior: p50 20 ms, p90 499 ms, 61 fronteras con error >2 s.
- Híbrido jerárquico: p50 20 ms, p90 320 ms, 18 fronteras con error >2 s.
- Mejora relativa p90: 35,9%.
- Reducción de saltos >2 s: 43/61, o 70,5%.
- Costo: cobertura selectiva; 526 líneas alineadas frente a 623.

## Cohorte disponible

- 26/41 stems están locales; 25 canciones pudieron puntuarse y una se abstuvo
  por no tener dos anclas duras.
- Cobertura: 85,2% (CI 78,8%–90,9%).
- p50: 20 ms (CI 20–40 ms).
- p90: 481 ms (CI 320–760 ms): `NO_GO` contra el gate de 250 ms.
- Líneas dentro de ±150 ms en ambos bordes: 43,1%.

## ZTLR sin proyecciones infladas

- Histórico: 55,3%.
- 86,1% es el techo matemático si las 476 líneas solo-timing se resolvieran;
  no es un resultado experimental.
- En las 25 canciones medidas hay 240 líneas solo-timing; el replay reproduce
  63 dentro de ±150 ms.
- Sumadas sin dañar las 856 líneas ya zero-touch, el límite inferior medido
  sobre las 41 sería 59,4%.

## Cola y revisión

Sobre las 25 canciones, la descomposición retrospectiva de los flags da:

- texto: 94 líneas, 18,5 s/canción;
- timing resuelto por replay: 60 flags, 13,9 s/canción;
- timing todavía humano: 215 líneas, 45,5 s/canción;
- falsos flags/zero-touch: 382 líneas, 63,1 s/canción.

Estos buckets usan gold para diagnóstico y no pueden retirar trabajo del
revisor en producción por sí solos. El cuello siguiente es un selector de
confianza que separe timing seguro y falsos flags; no otro alineador global.

La localización acústica previa de anclas se probó en tres stress cases. Bajó
el p90 descriptivo 2,64 s→2,03 s, pero siguió muy lejos del gate y duplicó el
costo de CTC; queda estacionada, no se corre sobre los 41.
