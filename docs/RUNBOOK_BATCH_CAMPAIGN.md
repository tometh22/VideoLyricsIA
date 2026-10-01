# Runbook: campaña batch de 1000 canciones

Este flujo mantiene RunPod apagado. Usa la flota actual, limita Demucs a dos
predicciones globales y alimenta transcripción en olas acotadas. Una canción
fallida queda marcada en el manifest; no detiene las demás.

## 1. Configurar staging

En `api`, `Worker` y `ShortWorker`:

```text
BATCH_CAMPAIGN_SCOPES=universal_music
BATCH_USER_BACKLOG_LIMIT=50
BATCH_TENANT_BACKLOG_LIMIT=50
BATCH_DAILY_VOLUME_CAP=1200
DEMUCS_MAX_CONCURRENT=2
DEMUCS_LEASE_TTL_S=1500
DEMUCS_SLOT_WAIT_MAX_S=900
```

`BATCH_CAMPAIGN_SCOPES` acepta `tenant_id` o `billing_group`. Mantenerlo vacío
deja los límites históricos sin cambios. No subir los límites globales de
clientes retail.

El token debe pertenecer al tenant/billing group de la campaña; no usar un
admin de otro tenant sólo para saltear límites. La cuenta operadora debe tener
plan `unlimited`, al menos 1000 créditos, o
`allow_overage=true`. El runner consulta `/auth/me`, `/usage` y
`/batch/capacity` y aborta antes de crear el primer job si algo no alcanza.

## 2. Canary de 30

Congelar deploys de workers durante la ventana. Ejecutar:

```bash
cd lyricgen/backend
python3.11 universal_batch.py \
  --folder "/ruta/al/lote" \
  --expected-count 30 \
  --wave-size 30 \
  --canary-size 30 \
  --concurrency 5 \
  --token "$STAGING_BATCH_TOKEN"
```

El manifest queda en `.context/universal-batch-manifest.json` y se escribe de
forma atómica después de cada transición. Para continuar tras reiniciar la
laptop:

```bash
python3.11 universal_batch.py ... --resume
```

Para crear intentos nuevos únicamente para las canciones terminalmente
fallidas:

```bash
python3.11 universal_batch.py ... --resume --retry-errors
```

Gate del canary:

- 30/30 llegan a `pending_review`.
- cero `vocal_sep_degraded` en `ai_provenance`.
- cero canciones matadas por `reaper.killed_transcription`.
- cola de Demucs estable; `queue_time_ms` no crece ola contra ola.
- ninguna ola supera 50 pendientes por tenant.

## 3. Escalado

Después del canary, correr 100 canciones con olas de 30. Si el gate se
mantiene, procesar el resto con los mismos límites. No encolar las 1000 de una
vez: el tamaño de ola es un guardrail operativo, no un parámetro de velocidad.
Si quedan menos de 30 lugares porque los revisores todavía no drenaron
`pending_review`, el runner espera y vuelve a consultar `/batch/capacity`; no
crea una ola parcial ni convierte el 429 en fallos falsos.

El runner se detiene por defecto al completar el canary. Después de revisar el
gate, continuar el mismo manifest con:

```bash
python3.11 universal_batch.py ... --resume --continue-after-canary
```

Para el lote completo:

```bash
python3.11 universal_batch.py \
  --folder "/ruta/al/lote" \
  --expected-count 1000 \
  --wave-size 30 \
  --canary-size 30 \
  --concurrency 5 \
  --token "$STAGING_BATCH_TOKEN"
```

Ese comando procesa 30 y se detiene. Reanudar con
`--resume --continue-after-canary` únicamente después de aprobar el gate.

## 4. Rollback

Detener el runner no cancela trabajos ya aceptados; sólo deja de crear la ola
siguiente. Para cerrar la ventana de campaña, vaciar
`BATCH_CAMPAIGN_SCOPES`. Si el proveedor de Demucs se degrada, bajar
`DEMUCS_MAX_CONCURRENT=1`; ponerlo en `0` desactiva el semáforo y vuelve al
comportamiento anterior.
