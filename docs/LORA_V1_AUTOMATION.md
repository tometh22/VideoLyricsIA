# LoRA v1 y disparadores de evaluación

## Alcance

La autorización de entrenamiento con audio de catálogo se expresa mediante
`CATALOG_AUDIO_TRAINING_ENABLED=1`.  El valor opcional
`CATALOG_AUDIO_TRAINING_AUTHORIZATION_ID` es una referencia no secreta para
auditoría; no contiene ni sustituye credenciales.  El modelo solicitado es
`openai/whisper-large-v3-turbo`.

La preparación local (`scripts/prepare_lora_v1.py`) materializa las 498
muestras, más los pares históricos que se pasen con `--historical-pairs`.
Los pares históricos se incorporan únicamente si llevan `complete=true` y
conservan evidencia de máquina y deltas íntegros. Los exports legacy que no
cumplen el invariante quedan contabilizados como `historical_pairs_rejected` y
no se usan para entrenar.

cohorte exacta de 23 canciones queda marcada `eval_only` y se excluye del
entrenamiento para evitar fuga. Se generan splits por canción y
leave-artist-out. La ejecución no sube audio.

El entrenamiento (`scripts/train_lora_v1.py`) valida primero los paths y la
política; si faltan dependencias o executor devuelve un reporte bloqueado. El
adaptador entrenado es siempre `additional_consensus_family`. No hay reemplazo
del Whisper base: la sustitución requiere dos evaluaciones consecutivas con
CI por canción y una decisión explícita posterior.

Para producir las dos entradas del replay se ejecuta
`scripts/run_lora_v1_inference.py` dos veces: una sin `--adapter` (baseline) y
otra con el directorio `adapter` de la corrida. Luego
`scripts/evaluate_lora_v1.py` calcula WER por canción, las particiones
fácil/difícil, bootstrap e intervalo de confianza. Una corrida marcada
`trained_uncalibrated` o un smoke test nunca se puede pasar a
`lora_family.load_verified_family`; para enlazar el artefacto validado se pasa
`--training-report run_report.json` al evaluador. El gate de esta etapa sólo
autoriza añadir la familia al consenso; nunca habilita reemplazo.

## Evaluación y gates

`scripts/evaluate_lora_v1.py` consume predicciones separadas de baseline y
candidato y produce WER/CER por canción, particiones fácil/difícil, bootstrap
por canción e intervalo de confianza, y una curva dato→mejora. No se acepta un
WER global como único resultado. Los reportes mantienen
`runtime_replacement_allowed=false`.

`scripts/evaluate_t4_95.py` evalúa únicamente la población de 95 finales
tempranos y un control de no-daño. Es replay-only, informa mejora de target y
daños de control (>150 ms), además de media/CI bootstrap y desglose por
canción. La extensión T4 no entra en producción por este script. La serie de
variantes ya probadas y su cierre están en
[`docs/T4_SERIES_CLOSURE.md`](T4_SERIES_CLOSURE.md); no se agregan variantes
antes de 200 canciones capturadas.

## Triggers sin duplicados

Después de cada captura de corrección y en un reconciliador periódico, el
worker cuenta jobs distintos con snapshot pre-humano y aprobación. Para LoRA,
además exige al menos un delta humano real de texto, timing o reordenamiento;
aprobar sin corregir no infla la población supervisada. Con:

* `LORA_V1_AUTORETRAIN_ENABLED=1` y `CORPUS_RETRAIN_EVERY_SONGS=100` solicita
  un run en cada bucket de **100 canciones corregidas y aprobadas**, siempre que
  haya al menos 20 artistas distintos (`LORA_RETRAIN_MIN_DISTINCT_ARTISTS=20`).
  El executor recibe obligatoriamente `song_and_artist_disjoint`, reserva como
  mínimo 20 canciones de evaluación de 5 artistas completamente ausentes de
  train/validation, y conserva además separación por canción. Si cualquiera de
  esos mínimos falta, el trigger queda bloqueado y no entrena.
* `REALIGN_SELECTOR_AUTORUN_ENABLED=1` y
  `REALIGN_SELECTOR_TRIGGER_SONGS=200` solicita un único job que incluye el
  selector y el companion `t4_95`; T4 no tiene un trigger independiente.
* `AGENT_D1_AUTORUN_ENABLED=1` y `AGENT_D1_TRIGGER_SONGS=100` solicita D1.

Cada bucket tiene un RQ id determinista y una fila `AuditLog`; un retry o
reinicio no lo duplica. Los executors se configuran como paths absolutos,
`LORA_V1_EXECUTOR`, `REALIGN_SELECTOR_EXECUTOR` y `AGENT_D1_EXECUTOR`. Si no
están montados, el job queda `blocked_executor_missing`, sin declarar éxito
ni tocar producción. La cola es `transcription_quality` y requiere
`TRANSCRIPTION_QUALITY_QUEUE_ENABLED=1`.

Para el primer run local:

```sh
python3 lyricgen/backend/scripts/prepare_lora_v1.py \
  --golden /ruta/al/golden \
  --output /ruta/privada/lora-v1-prep \
  --expected-samples 498
python3 lyricgen/backend/scripts/train_lora_v1.py \
  --manifest /ruta/privada/lora-v1-prep/samples.jsonl \
  --historical-pairs /ruta/privada/lora-v1-prep/historical_pairs.jsonl \
  --output /ruta/privada/lora-v1-run \
  --validate-only
```

Si un export histórico completo tiene audio en almacenamiento local, se
puede pasar además `--historical-audio-map` con un JSON
`{"job_id":"/ruta/audio.wav"}`. Sin ese mapa los exports SQL no se usan para
entrenar: contienen letras aprobadas, pero deliberadamente no contienen
audio, y el comando los contabiliza como `rejected_missing_audio`.

La corrida de GPU se ejecuta sólo con `--validate-only` aprobado, el flag de
autorización activo y un executor/GPU real. Ninguna clave se guarda en el
repositorio, en Murmur ni en estos reportes.

## Resultado LoRA v1 (2026-09-01)

El replay de v1 sobre la cohorte canónica exacta de 23 canciones produjo:

* Whisper-large-v3-turbo aislado sobre las 168 ventanas: WER 25,66%.
* La misma inferencia con el adaptador LoRA: WER 22,42%, una mejora relativa
  de 12,61% (3,24 puntos porcentuales absolutos).
* El bootstrap por canción dio una diferencia de −3,24 pp, IC 95%
  [−0,93; +8,41] pp; 11 canciones mejoraron, una quedó igual y 11
  empeoraron.

El 25,66% no contradice el baseline certificado de aproximadamente 6,9%:
el primero mide el modelo ASR aislado sobre ventanas preparadas, sin consenso
ni post-procesos del pipeline; el segundo mide la salida end-to-end ya
certificada (consenso, filtros, alineación y correcciones). Son condiciones y
unidades de evaluación distintas. La cohorte canónica no contiene canciones
`difficult`, por lo que la partición difícil de v1 queda sin observaciones y
no habilita ninguna conclusión sobre esa cola.

Por ese IC y la regresión en 11 canciones, v1 queda únicamente como familia
adicional del consenso. `lora_family.load_verified_family` exige un reporte
completo de evaluación y verifica el SHA-256 del adaptador; nunca permite
reemplazar Whisper base. El reemplazo requeriría dos evaluaciones consecutivas
con CI por canción.

## V2: política fijada para el trigger de 100 canciones corregidas

El diagnóstico direccional sobre las 18 canciones `reconstructed` (WER
31,10% → 10,68%, −65,67% relativo; 15 mejoran, una igual y dos empeoran;
IC bootstrap de la diferencia [+4,37; +46,20] pp) es la justificación para
dar más peso a la cola difícil en v2. Es evidencia de dirección, no un gate:
esas canciones no son una cohorte `exact` histórica comparable.

Cuando `CORPUS_RETRAIN_EVERY_SONGS=100` dispare v2:

* el entrenamiento repite las muestras marcadas `difficulty=difficult` a
  razón 3:1 (`--difficulty-oversample 3`); validación y leave-artist-out se
  seleccionan antes y nunca se sobremuestrean;
* la evaluación incluye la cohorte canónica de 23 más todas las canciones
  nuevas `raw_quality=exact` disponibles (`--additional-exact-cohort`), con
  CI bootstrap por canción y partición fácil/difícil;
* el adaptador sigue entrando como familia adicional. Dos evaluaciones
  consecutivas con CI son requisito para cualquier decisión de reemplazo.

El script de entrenamiento deja en `run_report.json` el factor solicitado,
las filas difíciles y las filas efectivas. El evaluador deja explícitos los
conteos de la cohorte canónica y de las canciones exactas nuevas, evitando que
un WER de una condición aislada se confunda con el baseline end-to-end.

## Enlace opcional en staging (fail-closed)

El puente de runtime ya está conectado en los caminos API multipart, legacy y
worker. Está apagado por defecto y no puede activarse desde un payload de job.
Para un entorno de staging con el artefacto montado fuera del repositorio:

```sh
LORA_V1_FAMILY_ENABLED=1
LORA_V1_EVAL_REPORT=/secure-mounted/lora-v1/evaluation.json
LORA_V1_ADAPTER_PATH=/secure-mounted/lora-v1/adapter
```

En staging también puede usarse el puente privado R2, cuando no hay volumen
Railway disponible:

```sh
LORA_V1_FAMILY_ENABLED=1
LORA_V1_EVAL_REPORT_R2_KEY=runtime/lora-v1/evaluation.json
LORA_V1_ADAPTER_R2_PREFIX=runtime/lora-v1/adapter/
LORA_V1_RUNTIME_CACHE_DIR=/tmp/genly-lora-v1
```

El proceso descarga reporte y adapter atómicamente, comprueba el SHA-256 del
`adapter_model.safetensors` declarado por el reporte y continúa con Whisper si
alguna pieza falta o no coincide. Las credenciales R2 son variables del
servicio; nunca se guardan en el repo, en el reporte ni en Murmur.

`LORA_V1_ADAPTER_PATH` sólo cambia dónde se leen los bytes; el reporte sigue
siendo la autoridad y su SHA-256 debe coincidir. Si falta el montaje, la
dependencia opcional o el límite de audio, la familia se abstiene y el job
continúa con Whisper. La inferencia registra únicamente telemetría acotada en
`postpass_stats.lora_family`; no reemplaza ni muta `segments` directamente.
Cuando produce palabras, `targeted_consensus` la trata como una familia
independiente y sólo puede seleccionarla si supera su consenso existente.

## Diagnóstico reconstructed (no-gate)

Las 18 canciones `raw_quality=reconstructed` se evalúan aparte con
`scripts/diagnose_lora_v1.py`. El comando etiqueta el resultado como
`diagnostic_non_gate`, conserva bootstrap por canción y fuerza
`runtime_replacement_allowed=false`; una mejora en esas canciones sirve para
decidir el sobremuestreo de v2, pero no puede promover el adaptador.

La primera corrida (122 ventanas, 2026-09-01) dio WER 31,10% → 10,68%:
65,67% de mejora relativa, con diferencia bootstrap por canción de 20,42 pp
(IC 95% [4,37; 46,20] pp); 15 canciones mejoraron, una quedó igual y dos
empeoraron. Es una señal direccional fuerte de que el adaptador ayuda más en
material reconstruido/no canónico que en la cohorte exacta, pero no es un gate:
el manifest clasifica `reconstructed` como `easy` para entrenamiento y no hay
una partición `difficult` histórica comparable. La etiqueta correcta para
calibración de v2 es, por ahora, `raw_quality=reconstructed`, no “difícil”.

## Segunda evaluación en staging: sombra LoRA con/sin

Cada replay de `targeted_consensus` que tenga una hipótesis LoRA atestada
calcula una comparación apareada sin volver a llamar a ASR. En la misma
ventana se ejecuta el consenso con el testigo `lora` y se repite quitándolo;
el resultado se persiste en `transcription_quality.retry.lora_shadow`
(y en los diagnósticos `postpass_stats.targeted_consensus` cuando el replay
es adoptado):

* `comparisons`, `with_consensus` y `without_consensus` describen la población;
* `lora_contributed_lines` cuenta líneas en las que la decisión ganadora usa
  LoRA;
* `new_consensus_lines` es la métrica principal: consenso con LoRA que no
  existe sin LoRA;
* `lost_consensus_lines` vigila regresiones de la familia adicional.

Los contadores se agregan por canción durante las primeras 30–50 canciones
reales. El adaptador no muta la salida por sí solo: sigue siendo una familia
adicional, y una sugerencia sólo llega al editor si pasa el consenso vigente.
El reporte operativo es de solo lectura: `python
lyricgen/backend/scripts/report_lora_shadow.py --limit 50 --json`.

## Router de dificultad por desacuerdo LoRA↔base (piloto 2026-09-02)

El piloto offline combina las 23 canciones canónicas y las 18 reconstruidas.
Para cada canción calcula la distancia de edición ponderada entre las dos
secuencias de hipótesis, ignorando mayúsculas y puntuación. El WER se usa sólo
para etiquetar dificultad durante la evaluación; el score de runtime no lee
letras de referencia (`runtime_uses_gold=false`).

Resultado reproducible con
`scripts/pilot_lora_disagreement_router.py`: 41 canciones, 27 con WER base
>10%, AUC **0,971** (bootstrap 95%: **0,918–1,000**) y correlación de Pearson
**0,900**. Supera el gate exploratorio AUC ≥0,70, pero sigue siendo un piloto:
debe recalibrarse con leave-one-song-out sobre canciones nuevas antes de
ordenar automáticamente una cola.

En runtime, `transcription_worker` persiste el score gold-free en
`transcription_quality.metrics.difficulty_router` cuando ambas familias están
disponibles. Si falta cualquiera de los testigos, el campo no se escribe y el
router se abstiene. El score es una señal de semáforo, no una autorización de
autocorrección.

## Léxico por artista

El flujo de calidad ya recupera hasta 120 términos de canciones aprobadas del
mismo artista (excluyendo el job actual), los registra como
`artist_lexicon_terms` y los usa como `initial_prompt` únicamente en las
ventanas de consenso. No se incorporan hipótesis crudas ni referencias del
propio tema, y la decisión final sigue requiriendo dos familias acústicas.
La bandera independiente `ARTIST_LEXICON_RAG_ENABLED=1` queda preparada en
staging; apagada, el flujo no consulta el catálogo. La evaluación
leave-one-song-out y el gate de −10% WER para artistas con al menos dos temas
quedan pendientes de la cohorte real; no se activa como reemplazo global antes
de medirlos.
