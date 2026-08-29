# Baseline local de las 8 entregas sin crudo

Estas ocho entregas no conservan salida cruda histórica. Se generó una
hipótesis reproducible con Whisper large-v3-turbo local y se marcó
`historical:false`; no modifica los resultados certificados de las cohortes
41/57.

| Métrica | Resultado |
|---|---:|
| Canciones | 8 |
| WER corpus | 34,13% |
| CER medio por canción | 31,06% |
| Líneas omitidas | 120 |
| Líneas inventadas | 134 |
| Error p90 de inicio | 3.740 ms |
| Error p90 de final | 2.247 ms |

No hay timestamps por palabra en esta corrida rápida, por lo que no se
publica una métrica de timing palabra-a-palabra.
