# Piloto real de escucha activa — 17/11/2026

Se usaron audios originales y revisiones automáticas históricas de tres canciones.
Las respuestas del agente se guardaron antes de compararlas con las versiones
aprobadas. Los accesos de staging se cargaron temporalmente; no hubo escrituras
al producto, cambios en entregas ni despliegues. El procesamiento se ejecutó
localmente, con llamadas seriales a los proveedores configurados en staging.

| Canción | Cobertura con respuestas utilizables | Líneas con revisión dirigida | Propuestas de texto | Pares mejorados | Pares empeorados | Llamadas nuevas | Minutos de ejecución |
|---|---|---:|---:|---:|---:|---:|---:|
| Es Tan Facil | google: 99.4% / openai: 100.0% | 20 | 0 | 0 | 0 | 66 | 4.0 |
| Viaje Sin Rumbo | google: 75.1% / openai: 100.0% | 20 | 0 | 0 | 0 | 53 | 4.7 |
| En La Vereda Del Sol | google: 0.0% / openai: 100.0% | 20 | 0 | 0 | 0 | 32 | 1.1 |

“Pares” son grupos de líneas con correspondencia exacta en el historial, no
cantidad de reclamos del cliente. Se comparan palabras normalizadas contra la
referencia editorial aprobada. No se evaluó precisión de puntuación o de timing.
No tener propuestas no demuestra buena precisión: también deja errores sin resolver.

## Resultado operativo

El piloto produjo **cero propuestas de texto y cero ajustes de timing**.
De 120 grupos de líneas evaluables, 41 conservan diferencias léxicas frente a
la referencia aprobada. Los otros 79 coinciden léxicamente, lo que no certifica
que su timing sea correcto. No se demostró reducción de trabajo humano.

La prueba de doble proveedor quedó incompleta: Gemini tuvo cobertura utilizable
parcial en las dos primeras canciones y no se ejecutó en la tercera tras el
segundo límite de capacidad. La cobertura completa de Whisper en esa canción
no equivale a una revisión completa de dos fuentes.

No se recomienda activar aplicación automática con estos resultados. La próxima
hipótesis a probar es separar reconocimiento de texto y localización temporal:
los timestamps del modelo generativo están invalidando texto y mezclando frases
vecinas. Se requiere un método de correspondencia por ocurrencia que use el
alineador acústico sin convertir el texto condicionado en prueba de reconocimiento.

## Límites observados

- El criterio estricto de dos familias y dos contextos no autoriza cambios
  cuando las respuestas incluyen líneas vecinas o límites fuera del fragmento.
- Parte de las respuestas de Gemini sobre recortes cortos exceden la duración
  real; se rechazan, no se corrigen recortando artificialmente sus tiempos.
- La revisión dirigida se limitó a 20 líneas por canción; no representa una
  corrección exhaustiva de todo el catálogo de hallazgos.
- Vertex devolvió 429 al comenzar la segunda canción. Se detuvieron sus llamadas,
  se preservó la corrida parcial y se hizo un reintento explícito tras la espera.
  Las respuestas válidas de Whisper se reutilizaron sin comprarlas otra vez.
  Al repetirse el límite durante el reintento, la tercera canción se procesó
  únicamente con Whisper; no se siguió insistiendo sobre Vertex.
- El costo monetario no se obtuvo de facturación. Se guardaron uso disponible,
  cantidad de llamadas y tiempos; no se presupone costo cero.
- Estas canciones ya fueron utilizadas en investigaciones anteriores. Este lote
  es diagnóstico, no una evaluación independiente de generalización.

## Previews

Las previews muestran original histórico y borrador del agente con el mismo
master. Conservan los tiempos originales; no adoptan hipótesis CTC como tiempos
aprobados. Son videos locales de diagnóstico, no renders del producto ni
entregables del portal. Se comparó su audio decodificado con el master mediante
residuo normalizado; ese control no certifica que letra y sincronización sean correctas.

Los archivos privados (snapshots, respuestas, propuestas, comparaciones, clips y
previews) están en `.context/active-audio-three-song/`. Cada evaluación vincula
el hash de su informe de inferencia y cada control de preview vincula su video
con el master y con ese informe. Las credenciales temporales fueron eliminadas.

## Es Tan Facil

Referencia: 40 pares evaluables; errores léxicos antes/después: 13/13.

Decisiones pendientes: ambiguous_or_crossing_phrase: 5, missing_independent_contexts: 15.

Líneas candidatas fuera del cupo dirigido: 19. Referencias excluidas: {}.

## Viaje Sin Rumbo

Referencia: 59 pares evaluables; errores léxicos antes/después: 134/134.

Decisiones pendientes: missing_independent_contexts: 18, ambiguous_or_crossing_phrase: 2.

Líneas candidatas fuera del cupo dirigido: 35. Referencias excluidas: {"not_regression_target": 68, "source_segment_mismatch": 9}.

## En La Vereda Del Sol

Referencia: 21 pares evaluables; errores léxicos antes/después: 13/13.

Decisiones pendientes: missing_independent_contexts: 20.

Líneas candidatas fuera del cupo dirigido: 1. Referencias excluidas: {}.
