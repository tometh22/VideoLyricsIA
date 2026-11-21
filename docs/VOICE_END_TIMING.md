# Fin de línea = fin de la voz (`VOICE_END_TIMING_MODE`)

Regla, medición y parámetros: docstring de `lyricgen/backend/voice_end_timing.py`.
Backtest reproducible: `lyricgen/backend/scripts/backtest_voice_end_timing.py` (sólo lectura).

## Precedencia con `VOCAL_CONTINUITY_MODE` y `STABLE_PITCH_TAIL_ENABLED`

Conflicto encontrado: en staging `VOCAL_CONTINUITY_MODE=apply` está sólo en api,
ShortWorker y BatchShortWorker (no está en la paridad), y la cola de pitch sólo
corre si continuity está `off` (`transcription_worker.py`, `_medir_cobertura_final`).
Así, hoy el fin de línea depende de qué servicio tomó el job.

Decisión: la regla nueva es dueña única del fin de línea cuando está en `apply`.

| `VOICE_END_TIMING_MODE` | vocal continuity | cola de pitch |
|---|---|---|
| `off` | como hoy | como hoy |
| `observe` | como hoy (la regla sólo mide, sobre el resultado final) | como hoy |
| `apply` | se omite (`superseded_by_voice_end_timing`) | se omite (`superseded_by_voice_end_timing`) |

Donde la regla no tiene evidencia (sin stem cacheado, ancla inaudible) queda el
hold de `lead_in` (0,5 s). Como el modo está en la paridad, el comportamiento
deja de depender del servicio. En `apply` no tiene sentido dejar los otros dos
encendidos; se pueden apagar después de verificar.

Re-anclado: corre después de `REANCHOR_HOLD_MODE` y sólo sobre las líneas que ese
re-anclado re-timeó (no bloqueadas). Con evidencia del stem, el fin lo pone la
voz (pisa el hold); sin evidencia queda el hold.
Reprocesos sobre una revisión existente y documentos aprobados reutilizados: sólo `observe`.

## Despliegue (no tocar env desde el PR)

1. Cargar `VOICE_END_TIMING_MODE` (empezar en `observe`) como variable COMPARTIDA y
   referenciarla en los 6 servicios (api, Worker, ShortWorker, quality-worker,
   BatchWorker, BatchShortWorker): `scripts/railway_share_pipeline_config.py`
   ya incluye la clave en `TIMING_KEYS`. Hacerlo ANTES de desplegar el código.
2. Desplegar; verificar `/health` (`timing_config_parity.match`).
3. `observe` -> revisar `transcription_quality.voice_end_timing` en jobs nuevos -> `apply`.
