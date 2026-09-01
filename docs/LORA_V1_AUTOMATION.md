# LoRA v1 y disparadores de evaluación

## Alcance

La autorización de entrenamiento con audio de catálogo se expresa mediante
`CATALOG_AUDIO_TRAINING_ENABLED=1`.  El valor opcional
`CATALOG_AUDIO_TRAINING_AUTHORIZATION_ID` es una referencia no secreta para
auditoría; no contiene ni sustituye credenciales.  El modelo solicitado es
`openai/whisper-large-v3-turbo`.

La preparación local (`scripts/prepare_lora_v1.py`) materializa las 498
muestras, más los pares históricos que se pasen con `--historical-pairs`. La
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

## Evaluación y gates

`scripts/evaluate_lora_v1.py` consume predicciones separadas de baseline y
candidato y produce WER/CER por canción, particiones fácil/difícil, bootstrap
por canción e intervalo de confianza, y una curva dato→mejora. No se acepta un
WER global como único resultado. Los reportes mantienen
`runtime_replacement_allowed=false`.

`scripts/evaluate_t4_95.py` evalúa únicamente la población de 95 finales
tempranos y un control de no-daño. Es replay-only, informa mejora de target y
daños de control (>150 ms), y nunca muta timestamps. La extensión T4 no entra
en producción por este script.

## Triggers sin duplicados

Después de cada captura de corrección y en un reconciliador periódico, el
worker cuenta jobs distintos con snapshot pre-humano y aprobación. Con:

* `LORA_V1_AUTORETRAIN_ENABLED=1` y `CORPUS_RETRAIN_EVERY_SONGS=100` solicita
  un run en cada bucket de 100.
* `REALIGN_SELECTOR_AUTORUN_ENABLED=1` y
  `REALIGN_SELECTOR_TRIGGER_SONGS=200` solicita el selector.
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
  --output /ruta/privada/lora-v1-run \
  --validate-only
```

La corrida de GPU se ejecuta sólo con `--validate-only` aprobado, el flag de
autorización activo y un executor/GPU real. Ninguna clave se guarda en el
repositorio, en Murmur ni en estos reportes.
