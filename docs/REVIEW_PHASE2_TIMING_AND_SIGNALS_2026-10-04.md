# Fase 2: calibración del timing y evaluación de señales por línea (2026-10-04)

Sin cambios de producto. No se aplicó ningún parámetro. Base: `staging`
5124e710 (1.1.109) y la línea de base de la fase 1
(`docs/REVIEW_BASELINE_2026-10-04.md`).

## Resumen

1. **Los parámetros globales casi no mueven las ediciones de timing.** La
   mejor combinación (adelanto único de 80 ms y 250 ms de aire antes de la
   línea siguiente) elimina unas **75 de ~1.380** ediciones de inicio y fin
   en 100 jobs de staging: alrededor de **5 %**.
   - Las correcciones del operador son errores de cada línea, no un corrimiento
     común: la mediana es 0,26 s en inicios y 0,35 s en fines, con colas de
     varios segundos.
   - Un corrimiento global no puede absorberlas.
2. **Adelanto único: 80 ms.**
   - No cambia nada en el 70 % de los jobs (origen CTC).
   - En los jobs de origen WhisperX corre los inicios 80 ms más tarde. Sube los
     inicios dentro de ±100 ms de lo aprobado de 66 % a 86 %, o de 78 % a 83 %
     si se excluye un job que se generó sin adelanto.
   - Con tolerancia de 100 ms, baja las ediciones de inicio de esas líneas de
     78 a 36.
3. **No hay evidencia para cambiar el largo del hold.** La comparación entre
   agosto (hold de 0,25 s) y septiembre (0,5 s) no se sostiene contra el
   control: es criterio de revisión, no hold. Lo que el operador corrige es
   que **las líneas se toquen**. Cuando edita un fin, deja entre 0,21 s (p10)
   y 0,44 s (mediana) de aire antes de la siguiente; el pipeline deja 10 ms.
4. **El beat snap es irrelevante.**
   - Solo puede actuar sobre 26 líneas en 10 de 157 jobs.
   - En la muestra calibrable movió 4 líneas; el operador no dejó ninguna donde
     la puso el snap y ninguna volvió a su posición previa.
5. **Ninguna combinación de señales llega a la meta** (≥ 85 % de las líneas
   con error marcadas y < 30 % de las correctas marcadas de más).
   - Texto: 59–62 % de cobertura con 29–30 % de marcas de más, estable entre
     mitades de staging.
   - Timing: la cobertura es igual a la tasa de marcado, o sea que las señales
     no aportan información.
   - **Primero hay que persistir las señales que hoy se tiran (fase 5) antes
     de construir el filtro.**

## Datos y método

| Dataset | Jobs | Líneas | Con palabras | Calibrables* | Origen (jobs) | Roles |
|---|---|---|---|---|---|---|
| umg-gold-v1 (agosto, aprobado en portal) | 57 de 65 (8 sin salida de máquina) | 2.231 | 41 % | 893 | ctc 46, reconciled 4, whisperx 1, otros 6 | train 25, val 8, eval_holdout 24 |
| Staging UMG (fase 1, 10-sep → 4-oct) | 100 | 3.228 | 82 % | 1.867 | ctc 70, whisperx 15, reconciled 13, otros 2 | ninguno registrado |

\* Con palabras, emparejadas con la versión aprobada y con el mismo texto
normalizado.

**Reconstrucción**
- El inicio crudo es la primera palabra y el fin crudo, la última. Ninguna de
  las cuatro mutaciones mueve las palabras.
- Se re-simulan en el orden del emisor: adelanto de WhisperX, beat snap,
  adelanto de `lead_in.polish` y hold.
- Si la línea de máquina salió con menos adelanto que el modal de su job, su
  inicio se usa como piso: el fin del segmento anterior puede ir más allá de
  su última palabra.
- Fidelidad con los parámetros actuales, contra la máquina real:

| | Inicio dentro de 20 ms | Fin dentro de 20 ms |
|---|---|---|
| Staging, líneas CTC | 99 % | 95 % |
| Staging, líneas WhisperX | 92 % | 91 % |
| Gold | 81 % | 18 % |

El gold se generó en agosto con hold de 0,25 s y, en 4 jobs, con adelanto
de 0,4 s.

**Sesgo de anclaje (limita todo lo de abajo)**
- En las líneas que el operador no tocó, lo aprobado es exactamente lo que
  produjo el pipeline con los parámetros de ese momento. Cada dataset
  favorece sus propios parámetros: staging el hold de 0,5 s, el gold el de
  0,25 s.
- Por eso se informa además un estimador libre de anclaje: qué valor eligió
  el operador en las líneas que **sí** corrigió.

**Splits**
- El registro de roles solo cubre el gold, que tiene poca cobertura de
  palabras y señales.
- Para detectar sobreajuste en staging se usa además una partición fija 50/50
  por job (hash del `job_id`).

## (a) Calibración: % de líneas con inicio y fin dentro de ±100 ms

En la tabla, "inicio / fin" es el % de líneas dentro de ±100 ms. La columna
"Máquina real" es lo que salió del pipeline; el resto son simulaciones.

| Grupo (líneas) | Máquina real | Actual simulado (80+80 ms, hold 0,5, snap 80) | Adelanto único 80 ms | Adelanto único 0 ms | Único 80 + hold 0,25 | Único 80 + aire 250 ms |
|---|---|---|---|---|---|---|
| Staging (1.867) | 91 / 44 | 89 / 43 | 91 / 43 | 85 / 43 | 91 / 19 | 91 / **51** |
| Staging CTC (1.640) | 93 / 46 | 92 / 46 | 92 / 46 | 93 / 46 | 92 / 20 | 92 / **54** |
| Staging WhisperX (216) | 82 / 35 | 66 / 21 | **86** / 21 | 33 / 20 | 86 / 13 | 86 / 25 |
| Gold (893) | 93 / 72 | 69 / 22 | 73 / 21 | 74 / 8 | 73 / **62** | 73 / 23 |
| Estudio (2.663) | 92 / 53 | 82 / 36 | 85 / 36 | 82 / 31 | 85 / 33 | 85 / 41 |
| Vivo, inferido (97) | 90 / 64 | 90 / 44 | 90 / 43 | 75 / 36 | 90 / 38 | 90 / 47 |
| Gold train (246) | 98 / 58 | 28 / 6 | 38 / 6 | 36 / 4 | 38 / 39 | 38 / 12 |
| Gold val (43) | 93 / 81 | 0 / 0 | 40 / 0 | 40 / 0 | 40 / 0 | 40 / 14 |
| Gold eval_holdout (604) | 91 / 77 | 90 / 29 | 90 / 29 | 92 / 10 | 90 / 76 | 90 / 28 |

Curvas de un parámetro por vez, con los demás en el valor actual:

- **`LYRIC_LEAD_IN_S` en líneas CTC de staging** (% de inicios dentro de ±100 ms):

  | Adelanto (ms) | 0 | 20 | 40 | 60 | 80 | 100 | 120 | 160 | 180 | 200 |
  |---|---|---|---|---|---|---|---|---|---|---|
  | Inicios dentro de ±100 ms | 92,7 % | 92,8 % | 92,6 % | 92,5 % | 92,3 % | 91,0 % | 88,8 % | 88,5 % | 33,7 % | 6,0 % |

  Es plana entre 0 y 80 ms.
- **Adelanto total en líneas WhisperX de staging:** 80 ms → 85,7 %; 120 ms →
  68,5 %; 160 ms (el actual) → 65,7 %; 200 ms → 64,4 %.
- **Hold en CTC de staging:** el pico está en el valor que generó los datos
  (450–500 ms, 47 %). En el gold, el pico está en 200–250 ms (69 %). Es anclaje.
- **Aire mínimo antes de la línea siguiente** (CTC de staging, % de fines
  dentro de ±100 ms):

  | Aire (ms) | 10 (actual) | 100 | 200 | 250 | 300 |
  |---|---|---|---|---|---|
  | Fines dentro de ±100 ms | 46,3 % | 49,0 % | 50,6 % | 54,3 % | 55,7 % |

**Mejor combinación por grupo (inestable):**

| Grupo | Óptimo |
|---|---|
| Staging | WX 80, polish 160, hold 450 |
| Gold | WX 0, polish 160, hold 200 |
| WhisperX | WX 0, polish 80, hold 50 |
| Gold eval_holdout | polish 60, hold 200 |
| Gold train | polish 160, hold 200 |

- El óptimo elegido en un split se transfiere mal a los otros. Ejemplo: elegido
  en eval_holdout da 0,73 ahí y 0,13 en train.
- El óptimo "global" en ±100 ms es mayormente anclaje y ruido; por eso **no**
  se recomienda.

**Estimador libre de anclaje** (líneas que el operador corrigió, staging):

| Origen | Inicios corregidos | Adelanto elegido: mediana (p25 / p75) | Fines corregidos | Hold elegido (fin − última palabra): mediana (p25 / p75) |
|---|---|---|---|---|
| CTC | 129 | 0,00 s (−0,09 / 0,00) | 833 | 0,18 s (0,00 / 0,35) |
| WhisperX | 31 | 0,015 s (−0,10 / 0,04) | 123 | 0,01 s (−0,22 / 0,28) |

Cuando el operador corrige un inicio, lo pone en la primera palabra; ningún
adelanto calibrado lo reproduce. Para ±100 ms da igual cualquier adelanto
entre 0 y 80 ms.

## (b) Cuántas ediciones de timing desaparecen

- Ediciones de inicio y de fin por separado, en las líneas calibrables de
  staging (100 jobs). "Hoy" es máquina contra aprobado.
- "Tolerancia 100 ms": una línea que el operador aceptó solo cuenta como
  edición nueva si el valor simulado queda a más de 100 ms de lo aceptado.
- "Estricto": cualquier diferencia de 50 ms o más es edición.
- La comparación justa es contra "actual simulado" (el simulador agrega ~3 %
  de ruido).

| Staging | Inicios con edición (tolerancia 100 ms / estricto) | Fines con edición (tolerancia 100 ms / estricto) | Total con tolerancia 100 ms | Δ contra actual simulado |
|---|---|---|---|---|
| Hoy (máquina real) | 232 | 1.076 | 1.308 | — |
| Actual simulado | 278 / 280 | 1.103 / 1.105 | 1.381 | — |
| **Adelanto único 80 ms** | 236 / 393 | 1.103 / 1.108 | 1.339 | **−42 (−3,0 %)** |
| Único 80 + snap apagado | 236 / 393 | 1.103 / 1.108 | 1.339 | −42 |
| **Único 80 + aire 250 ms** | 236 / 393 | 1.070 / 1.087 | 1.306 | **−75 (−5,4 %)** |
| Único 80 + aire 200 ms | 236 / 393 | 1.102 / 1.117 | 1.338 | −43 |
| Único 80 + hold 0,25 | 236 / 393 | 1.612 / 1.624 | 1.848 | +467 |
| Adelanto único 0 ms | 292 / 1.714 | 1.104 / 1.193 | 1.396 | +15 |

- **WhisperX:** con el adelanto único de 80 ms, los inicios con edición pasan
  de 78 a 36 con tolerancia de 100 ms. En modo estricto suben de 78 a 190,
  porque los operadores aceptaron exactamente −160 ms en el 65 % de esas
  líneas.
- **Aire de 250 ms:** quita 135 ediciones de fin y crea 129 (tolerancia
  100 ms). El neto es chico porque el operador también acepta muchas líneas
  pegadas: el p10 del aire en fines no tocados es 10 ms.

**Veredicto:** la mejor combinación elimina ~5 % de las ediciones de timing,
unas 0,75 por job sobre ~13,8. No es una palanca de minutos. El adelanto
único se justifica por consistencia (dos mecanismos hacen lo mismo) y por
bajo riesgo, no por ahorro.

### Experimento natural del hold (y por qué no alcanza)

Jobs CTC por el hold con que se generaron, separados por el hueco hasta la
línea siguiente:

| Hueco hasta la siguiente | Hold 0,25 (gold, agosto) | Hold 0,5 (staging, septiembre) |
|---|---|---|
| ≤ 0,25 s (**control**: los dos holds dan la misma salida) | 17 % [12–23] | **76 %** [70–82] |
| 0,25–0,5 s (el hold cambia la salida) | 13 % [8–22] | 78 % [72–82] |
| > 0,5 s | 36 % [32–41] | 48 % [45–50] |

Entre corchetes, el IC 95 % de Wilson.

La diferencia en el control es igual a la de la franja donde el hold
importa: el efecto es del criterio de revisión (staging separa líneas
pegadas; agosto no), no del hold. **No se puede estimar el efecto del hold
sobre datos retrospectivos.**

## (c) Beat snap

- `BEAT_SNAP_ENABLED=1` en api, ShortWorker y BatchShortWorker, con ventana
  de 80 ms.
- Solo actúa sobre líneas sin ninguna palabra con score ≥ 0,5, y en el camino
  CTC el retime lo pisa.
- Alcance: 26 líneas en 10 de 157 jobs; 9 calibrables. Los beats se
  calcularon con `beat_snap.detect_beats` sobre el audio de esos 10 jobs,
  bajado de R2 de staging en solo lectura.
- El snap movió 4 líneas calibrables (2 con el adelanto único). El operador
  no dejó ninguna en la posición del snap y ninguna volvió a la posición
  previa: las movió a otro lado.
- **No ayuda ni estorba de forma medible.** Apagarlo quita la única mutación
  que depende del audio, sin efecto en las ediciones.

## (d) Valores recomendados y riesgo de aplicarlos solo a jobs nuevos

| Parámetro | Hoy | Recomendado | Evidencia | Riesgo |
|---|---|---|---|---|
| Adelanto de WhisperX (`LYRIC_LEAD_IN_MS`) | 80 ms (default) | **0** | WhisperX: inicios dentro de ±100 ms 66 % → 86 %; ediciones de inicio 78 → 36 (tolerancia 100 ms) | Bajo. En jobs WhisperX nuevos los inicios llegan 80 ms más tarde. El operador había aceptado −160 ms exactos en el 65 % de esas líneas, pero 80 ms está en el p10 de lo que suele corregir |
| Adelanto de `lead_in.polish` (`LYRIC_LEAD_IN_S`) | 0,08 | **0,08 (sin cambio)** | Plano entre 0 y 80 ms; es lo que aceptan en el 89 % de las líneas CTC | Ninguno |
| Hold (`LYRIC_HOLD_S`) | 0,5 | **sin cambio** | Anclado en los dos datasets; el experimento natural no pasa el control | — |
| Aire antes de la línea siguiente (`lead_in._MIN_GAP_S`, no configurable hoy) | 10 ms | **candidato 250 ms, solo con prueba prospectiva** | Fines dentro de ±100 ms 46 % → 54 %; neto −33 ediciones de fin (−3 %) | Medio. Toca el display de todas las líneas que hoy quedan pegadas; requiere bandera nueva |
| Beat snap (`BEAT_SNAP_ENABLED`) | 1 | **0** (opcional) | Sin efecto medible; 26 líneas en 10 de 157 jobs | Muy bajo |

**Si se aplica solo a jobs nuevos:**
- Los jobs aprobados conservan su timing: el render usa los segmentos
  aprobados con `preserve_approved_timing`.
- Una re-transcripción o un `/reanchor` de un job viejo sí tomaría los
  valores nuevos.
- El catálogo queda mixto: los videos WhisperX anteriores llevan 160 ms y los
  nuevos 80 ms. No hay riesgo de entrega, porque cada video es independiente.
- **Riesgo operativo:** `LYRIC_LEAD_IN_MS`, `LYRIC_LEAD_IN_S` y `LYRIC_HOLD_S`
  forman parte de `observability.runtime_timing_config`, que exige paridad
  entre api y todos los workers. Cambiar una variable en un solo servicio
  deja `/health` en `timing_config_mismatch`, como pasó el 1-oct. Hay que
  cambiarla en los 6 servicios de staging a la vez, o hacerlo en código
  (fase 5.4) con una bandera.
- Las mediciones antes/después de la fase 3 en adelante tienen que separar
  jobs generados antes y después del cambio.

## Evaluación de señales por línea

**Error real, por línea de máquina**
- **Texto:** la línea se borró, o su texto en minúsculas y sin puntuación
  cambió. Las tildes cuentan.
- **Timing:** mismo texto, pero el inicio o el fin aprobados quedan a más de
  150 ms del timing **calibrado** (adelanto único de 80 ms). Las líneas sin
  palabras se comparan con la máquina.
- Las líneas que el operador agregó (omisiones) no tienen línea de máquina
  que marcar y quedan fuera.

**Ventanas inseguras**
- Se leen de `editor_documents.machine_evidence.decisions.quality`, la
  evidencia congelada antes de la revisión (100 de 100 jobs de staging).
- Las de `jobs.transcription_quality` se recalculan después de la edición
  (motivo `operator_edited_segment`) y filtrarían la respuesta.
- El gold (agosto) no tiene evidencia congelada.

| | Líneas | Error de texto | Error de timing (> 150 ms) | Cualquiera |
|---|---|---|---|---|
| Staging | 3.228 | 957 (30 %) | 1.351 (42 %) | 2.308 (**71 %**) |
| Gold | 2.231 | 297 (13 %) | 936 (42 %) | 1.233 (55 %) |

Con umbral de 300 ms, los errores de timing en staging bajan a 919. El
veredicto no cambia.

**Cobertura de señales**

| Señal | Staging | Gold |
|---|---|---|
| `ctc_min_score`, `ctc_mean_score`, `alignment_score` | 68 % | 10 % |
| `recognition_score` | 39 % | 0 % |
| `provider_evidence.min_score` | 45 % | 0,2 % |
| `review` (verdadero) | 1,8 % | 2,5 % |
| `timing_validation` | **0 %** (nunca se persiste) | 0 % |
| Dentro de una ventana insegura congelada | 25 % | sin datos |

**Mejor señal sola** (umbral con < 30 % de marcas de más; todas las líneas):

| Señal | Texto: cobertura / de más | Timing: cobertura / de más | Tasa de marcado |
|---|---|---|---|
| `provider_min_score` < 0,84 | **50 % / 18 %** | 20 % / 30 % | 26 % |
| `recognition_score` < 0,88 | 42 % / 16 % | 18 % / 25 % | 22 % |
| `ctc_mean_score` < 0,85 | 34 % / 30 % | 28 % / 29 % | 31 % |
| `ctc_min_score` < 0,60 | 32 % / 27 % | 28 % / 29 % | 29 % |
| Ventana insegura | 29 % / 10 % | 14 % / 15 % | 15 % |
| `review` | 4 % / 1 % | 2 % / 2 % | 2 % |
| `timing_validation` | 0 / 0 | 0 / 0 | 0 % |

**Combinaciones OR de hasta tres señales**
- Elegidas en la mitad A de staging y medidas en la B.
- **Texto:** `recognition_score` < 0,72 OR `provider_min_score` < 0,29 OR
  ventana insegura. Mitad A: 62 % de cobertura y 29 % de marcas de más
  (marca el 40 %). Mitad B: 59 % / 30 %. Sin sobreajuste dentro de staging.
- **Timing:** lo mejor es 30 % / 29 % con una tasa de marcado del 29 %, o
  sea lo mismo que marcar al azar. Mitad B: 29 % / 36 %.
- **Cualquier error:** 55 % / 29 %; mitad B: 55 % / 41 %.
- **Gold y eval_holdout:** cobertura ~0–4 %. No es que la regla falle: las
  señales casi no existen en esas líneas.

**Ninguna combinación llega a ≥ 85 % de cobertura con < 30 % de marcas de
más**, para texto, para timing ni para cualquier error. Con 71 % de líneas
con error en staging, un filtro de "solo marcadas" escondería hoy al menos
4 de cada 10 errores de texto y casi todos los de timing. Hay que
**persistir antes las señales que se tiran (fase 5.2)** y volver a correr
esta evaluación:
- `agreement` del consenso;
- ratio de `text_mismatches` por línea;
- `coverage_warning`;
- aviso de línea truncada;
- mediana CTC;
- `timing_validation`, que hoy no llega al segmento.

## Reproducir

```bash
cd lyricgen/backend
# 1) beats (solo para jobs con líneas snappables; ver --beats)
# 2) calibración (~1 min) y líneas para la evaluación
DATABASE_URL=... python3.11 scripts/calibrate_display_timing.py \
    --gold-dir <riyadh>/.context/benchmarks/umg-gold-v1/cases \
    --beats /tmp/beats.json --json-out /tmp/calib.json --lines-out /tmp/lines.pkl
# 3) señales (umbral de timing parametrizable)
DATABASE_URL=... python3.11 scripts/evaluate_line_signals.py \
    --lines /tmp/lines.pkl --timing-error-ms 150 --json-out /tmp/signals.json
```

- Los dos scripts abren la base en solo lectura.
- `--lines-out` incluye texto de las letras: no se commitea.
- Tests: `tests/test_calibrate_display_timing.py`.
