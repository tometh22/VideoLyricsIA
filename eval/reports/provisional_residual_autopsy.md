# Autopsia provisional del residuo

Esta corrida usa las 41 salidas `exact+reconstructed` del snapshot local. El
traceback global `main_only` reproduce exactamente el baseline: **454 edits /
7.862 palabras = 5,77% WER**. Bootstrap por canción: CI95 **3,52–8,41%**.
El final p90 es **750 ms**, CI95 por canción **511–1.067 ms**.

| Dimensión | Bucket | Errores | % del residuo |
|---|---|---:|---:|
| tipo | sustitución | 197 | 43,4% |
| tipo | omisión de palabra | 130 | 28,6% |
| tipo | inserción de palabra | 127 | 28,0% |
| posición | interior de línea | 235 | 51,8% |
| posición | primera palabra | 134 | 29,5% |
| posición | última palabra | 85 | 18,7% |
| clase | común o jerga sin resolver | 398 | 87,7% |
| clase | interjección | 48 | 10,6% |
| clase | posible nombre propio | 8 | 1,8% |
| contexto | línea repetida | 228 | 50,2% |
| contexto | línea única | 226 | 49,8% |

Con el denominador correcto, las líneas repetidas tienen **228/3.788 = 6,02%**
de error y las únicas **226/4.074 = 5,55%**. La diferencia es pequeña: el share
de 50,2% se explica principalmente porque casi la mitad de las palabras está
en repeticiones, no porque los estribillos fallen mucho más. Por ahora no se
justifica priorizar consistencia de estribillo. El bucket dominante provisional
es sustitución. Esto todavía no autoriza una variante: los idiomas figuran
`unknown` porque el snapshot anterior no los derivaba.

Los diffs inicial→final producen 612 cambios de timing: 227 hacia más tarde y
385 hacia más temprano, magnitud absoluta p50 **310 ms** y p90 **2.390 ms**.
Todos son derivados, no la secuencia completa observada. La dirección y las
magnitudes definitivas se recalculan sobre todas las versiones/audits cuando
estén las credenciales.
