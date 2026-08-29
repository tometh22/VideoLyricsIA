# Zero-touch report

## Métrica norte

- **ZTLR histórico:** 55.3% (856/1547; CI por canción 47.9%–63.0%).
- Trabajo residual: 476 líneas solo timing, 78 solo texto y 52 ambas.
- Minutos históricos: **no medibles**; el sistema viejo no persistía tiempo activo del editor. El before/after real sale del timer nuevo.

## Experimento central: texto confirmado → re-alineación

- **mix/current_xlsr/acoustic_raw:** 31 canciones, p50 172 ms, p90 1583 ms, ±150 ms en 18.0%, ZTLR proyectado 17.4%; **NO_GO**.
- **mix/mms_fa/display_lightgbm:** 41 canciones, p50 375 ms, p90 4447 ms, ±150 ms en 15.0%, ZTLR proyectado 13.4%; **NO_GO**.
- **mix/xlsr_ipa/display_lightgbm:** 41 canciones, p50 2526 ms, p90 38203 ms, ±150 ms en 0.2%, ZTLR proyectado 0.2%; **NO_GO**.
- **stem/current_xlsr/display_robust_global_median:** 18 canciones, p50 20 ms, p90 499 ms, ±150 ms en 50.9%, ZTLR proyectado 46.7%; **NO_GO**.

## Encontrar el residuo

- La unión OOF actual llega a 95.1% de recall, pero selecciona 1304/1509 líneas, 61.0% del audio.
- Agus escucharía **140 s/canción** en vez de 229 s. Aún es demasiado: T7/auto-consistencia/VAD deben reducir falsos flags sin bajar de 95%.

## Reducir errores

- **MSS-ALT (piloto; GO_REPLAY):** 1 canción, WER nativo 19.6%, RMS-VAD 15.5%, mejora relativa 21.2%.
- A2 (large-v2) y A3 (coherencia/rescoring) quedan pendientes hasta cerrar el replay A1 sobre la cohorte.
- Repetición A4 permanece descartada por prerrequisito: el error por palabra en líneas repetidas no supera materialmente al de líneas únicas.

No se entregó timing aprobado a ningún alineador, la calibración perceptual es leave-one-song-out y no hubo cambios en staging/producción.
