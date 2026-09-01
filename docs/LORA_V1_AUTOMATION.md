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
worker cuenta jobs distintos con snapshot pre-humano y aprobación. Con:

* `LORA_V1_AUTORETRAIN_ENABLED=1` y `CORPUS_RETRAIN_EVERY_SONGS=100` solicita
  un run en cada bucket de 100.
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

## Enlace opcional en staging (fail-closed)

El puente de runtime ya está conectado en los caminos API multipart, legacy y
worker. Está apagado por defecto y no puede activarse desde un payload de job.
Para un entorno de staging con el artefacto montado fuera del repositorio:

```sh
LORA_V1_FAMILY_ENABLED=1
LORA_V1_EVAL_REPORT=/secure-mounted/lora-v1/evaluation.json
LORA_V1_ADAPTER_PATH=/secure-mounted/lora-v1/adapter
```

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
