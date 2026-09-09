# Revisión acústica integral — desarrollo, 2026-09-05 ART

## Resultado y decisión

Se ejecutó escucha real sobre **289,4133 / 289,4133 segundos** de Bersuit,
Hecho en Buenos Aires, no solo ventanas con flags. **La capacidad de reparar
varios finales defectuosos todavía no quedó demostrada.** La alternativa
espectral se descarta para uso operativo: acerca cuatro extensiones históricas,
pero aleja siete y no reproduce ninguna de esas catorce dentro de ±150 ms.
No se habilita otra capa ni se ajustan umbrales después de ver los resultados.

| Medida | Observado |
|---|---:|
| Duración total / escuchada por cada modelo | 289,4133 / 289,4133 s |
| Ventanas, duración máxima, solapamiento | 16 / 24 s / 6 s |
| Llamadas reales / fallos de proveedor | 32 / 0 |
| Líneas recorridas offline / protegidas en esa copia rev0 | 41 / 0 |
| Líneas protegidas en snapshot vigente, intactas | 38 |
| Frase exacta localizada en una ocurrencia superpuesta por ASR | 26 / 41 |
| Hipótesis CTC obtenidas / fallos de herramienta | 41 / 0 |
| Candidatos espectrales / cambio material ≥150 ms | 27 / 19 |
| Sin candidato: ancla inestable / sin límite observable | 12 / 2 |
| Timing aceptado por selector / aplicado a originales | 0 / 0 |
| Comparadores históricos operativos / gold limpio | 21 / 0 |
| De esos 21: extensión / reducción respecto de rev0 | 14 / 7 |
| Nuevas propuestas textuales con dos familias y ocurrencia | 1 |
| Eventos potencialmente fuera de cartel, con duplicados por solapamiento | 30 |
| Omisiones de letra certificadas | 0 |
| Escucha / análisis CTC + espectral | 133,415 / 35,304 s |
| Uso facturable informado por Whisper | 380 s |
| Gemini: tokens de entrada / salida | 11.868 / 3.644 |
| Costo monetario observado | No confirmado; no hay factura adjunta |

Los 7 finales inferiores a rev0 NO se atribuyen de nuevo al revisor: son
diferencias operativas cuya autoría causal no está limpia. No se usan como
éxitos de reparación de finales tempranos. Una línea sin cambios tampoco se
considera correcta.

## Separación de entrada y evaluación

`prepare` verificó revisión original 0 y hash
`b4fa3779670af7acc4d92b4aa0ca1d82eba4b6e556c956b73dee5697d0270c27`.
Exportó un archivo allowlist sin documentos actuales ni correcciones posteriores.
`listen` y `analyze` solo abren ese archivo. Los modelos reciben únicamente
audio: sin nombre, artista, Excel, baseline ni corrección humana. El CTC sí
recibe el texto prehumano, declarado como condicionamiento, no certificación.

La predicción quedó congelada antes de `evaluate`:

- Commit de análisis: `feeb9da2367e4a99d476406a9d70b0711eae6fdc`.
- Input SHA256: `cec1792a24972a5d8c7d657bcc12d04391a45b7fb6c0de38b4f39ef0b1a429ef`.
- Predicción SHA256: `2b913530d3a7d1d7ae626833c9885977df1a763c4bb7e669478e050fbd7f9022`.

Después se compararon líneas locked con el mismo texto y diferencia de end
mayor a 150 ms. Es una evaluación **de desarrollo**, no independiente ni gold
limpio: el historial anterior al fix de auto-trim no fue readjudicado. No se
tocaron documentos actuales, aprobaciones, cola, flags ni despliegue.

## Método y cobertura

Modo offline independiente de `quality_jobs`: no límite de cuatro flags y no
condición de ausencia de propuestas nativas. Ventanas 0–24, 18–42, …,
270–289,413333; se guardan WAV, SHA, offset, request/model/usage y respuesta.
Whisper-1 y Gemini-2.5-flash recibieron **la misma mezcla original**, no se
transfirieron tiempos del stem. Los prompts y modelos de escucha se reutilizan.
Se computa unión de intervalos con respuesta válida por proveedor, no suma
de ventanas solapadas. No hubo intervalos fallidos; sí interpretaciones pendientes.

Las 41 frases se cotejaron con reconocimiento libre, sin depender de flags.
26 tienen coincidencia lexical exacta en una única ocurrencia con superposición
temporal; eso no certifica su límite. Las otras 15 siguen como discrepancia de
reconocimiento/asociación, no se convierten en líneas correctas por CTC.
Los 30 eventos externos a cartel son hipótesis con palabras o vocalización de
Gemini, no inferencias de letra omitida a partir de energía. Incluyen eventos
solapados duplicados y timestamps de modelo inciertos.

Como control adicional, los timestamps libres de Whisper se conservan como
hipótesis, no se elige el más cercano al revisor. Whisper público usa
[atención y DTW](https://github.com/openai/whisper/blob/main/whisper/timing.py),
pero no se presume que la implementación privada de la API sea idéntica.
En la última frase devolvió palabras de duración cero: no sirve como certificado
del final sostenido.

## Alternativa dirigida y etapas

Fallo investigado: CTC puede emitir la última palabra y terminar su alineación
antes del sonido sostenido. Alternativa: seguir la **forma log-mel del sonido
terminal**, no pitch estable. Ancla últimos 120 ms; primer cambio persistente
80 ms; coseno mínimo 0,90; búsqueda máxima 2 s, limitada por siguiente ocurrencia.
Estos parámetros se congelaron antes del contraste humano. No hay suma fija
de tiempo, puente de huecos ni ajuste posterior para acercarse al target.

Es un proxy espectral, **no un reconocedor fonético**. Instrumentos, consonantes,
variación de timbre y reverberación pueden cambiar el espectro sin terminar
la palabra, o mantenerlo después. No se inventa atribución de voz principal.

| Etapa | Diagnóstico |
|---|---|
| Frase/ocurrencia | 26 coincidencias exactas ASR, 15 sin localización lexical suficiente |
| Generación | 27 cambios de espectro candidatos, 12 anclas inestables, 2 sin límite |
| Comparación con CTC | Mejora parcial en 4/14 extensiones históricas, ninguna dentro de ±150 ms |
| Selector | 41 abstenciones; los 27 candidatos carecen de atribución de voz y final fonético |
| Defecto restante | El candidato útil falta o no está certificado; no hay evidencia de falsos rechazos del selector |

El schema legacy del selector también recibe sync=false porque no se certificó
mix/stem; aquí no se usa stem. Eso no es la causa suficiente de rechazo: aun
con reloj nativo admitido, siguen faltando voz objetivo y límite fonético.
No se modificó el gate para ocultar esa limitación.

## Comparación congelada

El baseline es rev0, no el CTC recién ejecutado. Acercarse al CTC no equivale
a mejorar el documento. Las medianas son solo de casos con candidato y no
deben compararse como si compartieran denominador.

| Conjunto / método | N | Con candidato | Más cerca que rev0 | Más lejos | Dentro ±150 ms |
|---|---:|---:|---:|---:|---:|
| 21 diferencias operativas / CTC | 21 | 21 | 5 | 15 | 5 |
| 21 diferencias operativas / espectral | 21 | 14 | 6 | 8 | 1 |
| 14 extensiones / CTC | 14 | 14 | 0 | 13 | 0 |
| 14 extensiones / espectral | 14 | 11 | 4 | 7 | 0 |

### Ejemplos, índices humanos 1-based

| Línea | Rev0 end | CTC nuevo | Espectral | Histórico | Resultado |
|---|---:|---:|---:|---:|---|
| 7 | 55,150 | 54,900 | 55,172 | 55,4379 | Mejora de solo 22 ms; error restante 266 ms, no reparación resuelta |
| 17 | 127,610 | 127,340 | 128,062 | 128,8529 | Acerca 452 ms; frase exacta no localizada, error restante 791 ms |
| 34 | 238,490 | 238,220 | 239,042 | 239,7476 | Acerca 552 ms; error restante 706 ms |
| 10 | 85,590 | 84,460 | 84,902 | 85,8903 | Empeora 688 ms frente a rev0 |
| 41 | 283,520 | 280,580 | 280,752 | 284,1059 | Frase reconocida; CTC local pierde sustain ya incluido en rev0; espectral empeora 2,768 s |

La última línea tuvo contexto **277,3–289,4133**, que contiene el target
histórico. No se explica por haber recortado el clip a end+2. El espectro cambia
a 280,752, mucho antes del final histórico; ampliar más la búsqueda no corrige
ese primer cambio falso. No afirmamos haber separado auditivamente voz/coro/reverb.

En Luciano se reutilizó la alineación original-mix 175,5–184,3, con prefijos
estables y protección contra el salto a otra repetición. Baseline179,32;
CTC178,82; la alternativa se abstuvo por ancla inestable. **Sin extensión ni
daño aplicado**, pero no se convierte ese caso en gold de corrección humana.

La escucha integral produjo además una propuesta textual nueva en línea13,
fuera del caso exitoso anterior. Dos familias respaldan el reemplazo mínimo;
la frase propuesta se localiza en 99,00–104,82 (ventana90–114). Permanece
como propuesta offline, no como corrección humana validada ni cambio vigente.

## Reproducción, evidencia y límites

Python3.11.15, entorno dedicado `.context/venvs/shadow-py311/bin/python`.
Artefactos privados: `.context/reviewer-shadow-artifacts/integral-v2/`:
`input.json`, `listening.json`, `window-*.wav`, `requests/`, `line-*.json`,
`predictions-frozen.json`, `freeze.json`, `evaluation.json`, `summary.json`,
`luciano-control.json`. Conservan éxitos parciales, fracasos y discrepancias.

Pruebas locales: **145 aprobadas, 2 omitidas por requerir PostgreSQL**,
incluyendo 12 específicas del modo integral (cobertura, repeticiones, proxy
espectral sintético, aislamiento prehumano e integridad del congelamiento).
JUnit: `integral-v2/tests.xml`. Se usó una SQLite nueva en directorio temporal;
ninguna base compartida. No hubo cambios de frontend en este hito.

Desde el worktree del PR, para un directorio NUEVO:

```sh
PYTHONPATH=lyricgen/backend "$REVIEWER_PYTHON" lyricgen/backend/scripts/review_integral_audio.py prepare --root "$REVIEWER_ARTIFACTS" --output "$REVIEWER_RUN"
PYTHONPATH=lyricgen/backend "$REVIEWER_PYTHON" lyricgen/backend/scripts/review_integral_audio.py listen --root "$REVIEWER_ARTIFACTS" --output "$REVIEWER_RUN"
HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1 PYTHONPATH=lyricgen/backend "$REVIEWER_PYTHON" lyricgen/backend/scripts/review_integral_audio.py analyze --root "$REVIEWER_ARTIFACTS" --output "$REVIEWER_RUN"
PYTHONPATH=lyricgen/backend "$REVIEWER_PYTHON" lyricgen/backend/scripts/review_integral_audio.py control --root "$REVIEWER_ARTIFACTS" --output "$REVIEWER_RUN"
PYTHONPATH=lyricgen/backend "$REVIEWER_PYTHON" lyricgen/backend/scripts/review_integral_audio.py evaluate --root "$REVIEWER_ARTIFACTS" --output "$REVIEWER_RUN"
PYTHONPATH=lyricgen/backend "$REVIEWER_PYTHON" lyricgen/backend/scripts/report_reviewer_integral.py --directory "$REVIEWER_RUN"
```

`listen` requiere credenciales existentes y consume hasta32 llamadas. No se
recompra automáticamente un intento de resultado desconocido. Los archivos de
salida son exclusivos, no se sobreescriben. No usar un directorio existente
para repetir fases finalizadas. No hay nueva UI ni petición de anotación manual.

**Siguiente bloqueo técnico concreto:** distinguir continuidad de la propia
vocal/consonante de cambios de mezcla, no sumar otra constante ni flexibilizar
el selector. Este experimento ya permite medir ese fallo sobre audio completo,
pero todavía no demuestra varios finales reparados ni ahorro humano.
