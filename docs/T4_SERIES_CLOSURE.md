# Cierre de la serie T4

## Decisión

La serie de reglas T4 queda **congelada hasta alcanzar 200 canciones
capturadas con snapshot pre-humano, hipótesis por familia y aprobación**.
No se ejecutan nuevas variantes ni se habilitan mutaciones productivas antes
de ese hito. El selector de realineado y T4-95 comparten el mismo disparador de
200 canciones porque ambos atacan la identificación de ocurrencia y las
fronteras de timing; un único job los ejecutará juntos.

## Evidencia disponible

Las cuatro variantes ya probadas se midieron sobre el mismo replay histórico:
62 finales tempranos en la población objetivo y 239 eventos fuera del objetivo
como control. La mejora relativa es `(MAE_base - MAE_candidato) / MAE_base`;
los daños de control son eventos cuyo error empeoró más de 150 ms. `NO-GO`
significa que no se habilita una mutación; no significa que se descarte la
hipótesis para siempre.

| Variante congelada | Objetivo (n) | MAE objetivo base → candidato | Mejora objetivo | Control MAE base → candidato | Daños control >150 ms | Decisión |
|---|---:|---:|---:|---:|---:|---|
| `shadow-pitch-tail-rules-v1` | 62 | 3,434 → 3,200 s | +6,8% | 0,760 → 0,850 s | 39/239 | NO-GO |
| `shadow-pitch-tail-rules-v2` | 62 | 3,434 → 3,309 s | +3,6% | 0,760 → 0,817 s | 25/239 | NO-GO |
| `shadow-pitch-tail-rules-v3-fixed-padding` | 62 | 3,434 → 3,361 s | +2,1% | 0,760 → 0,794 s | 17/239 | NO-GO |
| `shadow-structural-t4-v1` | 62 | 3,434 → 3,434 s | 0,0% | 0,760 → 0,760 s | 0/239 | NO-GO / sin cambios |

Fuentes: `phase0/rotor-umg-v1/t4-selective-evaluation.json`,
`t4-selective-evaluation-v3-fixed-padding.json` y
`t4-structural-shadow-v1-evaluation.json`. Estos artefactos contienen 62
casos objetivo; los 95 finales del contrato T4 siguen siendo la población que
se debe volver a medir cuando el corpus llegue a 200 y existan deltas limpios.

### Lectura

Las cuatro variantes repiten la misma firma: la extensión mejora una fracción
del conjunto objetivo, pero el selector no distingue de forma estable cuándo
debe abstenerse y daña eventos fuera del objetivo. No hay señal suficiente para
otra regla manual. El próximo experimento, si el hito de 200 lo justifica, se
calibra con los deltas humanos nuevos y un selector de confianza; no se diseña
contra esta tabla aislada.

## Disparador único a 200

`REALIGN_SELECTOR_AUTORUN_ENABLED=1` y
`REALIGN_SELECTOR_TRIGGER_SONGS=200` crean un solo RQ job por bucket. El job
lleva `companion_triggers=["t4_95"]` en sus metadatos y auditoría, y el
executor `REALIGN_SELECTOR_EXECUTOR` debe ejecutar el selector y el replay T4-95
en la misma corrida. No existe un trigger T4 independiente ni un segundo RQ
job. Los IDs y buckets siguen siendo idempotentes ante reintentos.

Hasta ese momento T4 permanece en replay/documentación; `T4_95_REPLAY_ENABLED`
solo puede controlar el executor cuando llegue el hito, nunca adelanta el
disparador ni modifica una canción en producción.
