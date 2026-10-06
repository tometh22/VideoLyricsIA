# Incidente 2026-10-05: /health de staging en down por paridad de timing

**Causa:** al encender `LYRIC_MIN_GAP_AB_ENABLED` agregué la referencia a la variable compartida servicio por servicio. La API de Railway dio timeout después de 2 de los 6 servicios (api y Worker), y redesplegué igual. La variable forma parte de `timing_config_parity`, así que `/health` pasó a `down` (503). Después, el redeploy de BatchWorker falló en el push de la imagen y hubo que relanzarlo.

**Impacto:** 37 minutos en `down`, de 14:42 a 15:19 UTC, solo en staging; producción no se tocó. Un revisor (ricardo.tapia) trabajó en 2 canciones UMG durante toda la ventana y no perdió nada: 435 autosaves exitosos, 0 fallidos y 1 aprobación completada. No había transcripciones en curso. El healthcheck de Railway (`/health/deploy`) solo marcó `degraded` y no bloqueó deploys; el monitor de uptime solo vigila producción.

**Qué cambia:** las 7 claves de la paridad de timing quedan como variables compartidas, ya referenciadas en los 6 servicios, así que cualquier cambio futuro es una sola escritura. Antes de redesplegar se verifican las 6 referencias. No hay deploys a staging en el horario del operador sin aviso previo y OK explícito. `/health` muestra el valor efectivo y el origen de cada variable de timing (PR aparte).
