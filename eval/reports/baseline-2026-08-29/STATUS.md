# Cierre de Fase 0 y arranque del flywheel — 2026-08-29

## Resumen ejecutivo

`baseline-2026-08-29` quedó **certificado** y content-addressed. Contiene 65
entregas (62 originadas en staging, 3 en producción), 65 audios verificados por
SHA-256 y cinco controles adversariales aprobados contra el feed read-only que
alimenta `umg.genly.pro`. El crudo histórico se divide sin mezclar: 23 `exact`,
18 `reconstructed`, 16 `estimated` y 8 `none`. Los 2.935 cambios legacy de texto
conservan tanto el valor anterior como el nuevo.

La cohorte fuerte de 41 canciones da WER main **5,78%** (bootstrap por canción,
CI95 **3,52–8,42%**) y error absoluto de final p90 **750 ms** (CI95
**511–1.067 ms**). La sensibilidad que incluye las 16 estimadas da WER **8,12%**
(CI95 **5,80–10,79%**) y final p90 **840 ms** (CI95 **667–1.080 ms**). Las ocho
sin crudo están excluidas de toda comparación histórica.

La repetición no autoriza un módulo propio en la cohorte fuerte: **6,05%** de
error por palabra repetida frente a **5,53%** por palabra única. El 50,4% del
share original era efecto del denominador. En timing humano hay 747 correcciones
directamente observadas de final: 494 hacia antes y 253 hacia después; el patrón
parcial anterior de 385/227 queda refutado por la extracción completa.

## Flywheel y gates

| Bloque | Resultado | Decisión |
|---|---|---|
| T4 LightGBM, CV por canción | 729 correcciones observadas; MAE 3.581 ms; 3,7% dentro de ±150 ms | **NO_GO**; 0 propuestas al editor |
| Predictor de error | AUC 0,708; CI95 0,644–0,783 | **NO_GO**; no ordena la cola de Agus |
| LoRA large-v3-turbo | 498 muestras ≤25 s; split 52/13 canciones; leave-artist-out 14 canciones | Preparado, **bloqueado por política**; cero audio enviado y USD 0 gastados |
| Taxonomía de 454 errores | Qwen 3.5 local; cero egreso | **Provisional** hasta validar 30 casos humanos |
| 8 entregas sin crudo | Whisper large-v3-turbo local; WER 34,13%; 120 líneas omitidas y 134 inventadas | `historical:false`; sensibilidad separada, no altera 41/57 |

La taxonomía provisional asigna: `otro` 46,9%, homófono/par fonético mínimo
15,6%, interjección 11,9%, palabra en otro idioma 11,5%, contracción oral 9,3%,
nombre propio 2,6%, segmentación 2,0% y jerga/lunfardo 0,2%. Antes de la
validación humana no autoriza módulos de inferencia. Si las proporciones se
confirman, sólo rescoring fonético, manejo de interjecciones y soporte
multilingüe superan el umbral del 10%; consistencia de estribillo, nombres,
segmentación y jerga quedan descartados por ahora.

## Decisión provisional de Fase 2

| Bucket/palanca | Contribución | Estado |
|---|---:|---|
| Rescoring fonético para pares mínimos | 15,6% | Espera validación humana de 30 |
| Manejo específico de interjecciones | 11,9% | Espera validación humana de 30 |
| Soporte de palabras en otro idioma | 11,5% | Espera validación humana de 30 |
| Contracciones orales | 9,3% | No justificado hoy (<10%) |
| Consistencia de estribillo | repetidas 6,05% vs únicas 5,53% | No justificado |
| Nombres, segmentación y jerga | 2,6% / 2,0% / 0,2% | No justificados |
| `otro` | 46,9% | No autoriza un módulo; requiere reducir el bucket |

## Gates pendientes explícitos

1. Completar `error_taxonomy_validation_30.csv`; sin eso la tabla semántica no
   es definitiva.
2. Resolver la contradicción de política antes de LoRA o Batch externo: el
   compliance publicado afirma que GenLy no entrena con datos del cliente y que
   el audio no sale de la infraestructura. El harness no permite convertir esa
   contradicción en un upload silencioso.
3. Las ocho canciones sin crudo ya recibieron baseline local
   `historical:false`: WER corpus 34,13%, final p90 2.247 ms y comienzo p90
   3.740 ms. Se reportan aparte y nunca alteran retrospectivamente las 41/57.

Bersuit queda como stress test vivo separado: con la misma normalización su WER
es **67,74%**, no 0,68 bajo otra métrica. Es un outlier real y no reemplaza el
baseline UMG.
