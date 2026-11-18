# Línea de base de revisión humana — fase 1 (2026-10-04)

Objetivo del programa: bajar los minutos de revisión por video sin tocar la
calidad entregada. Esta fase no cambia el producto: mide, verifica el doble
adelanto y compara producción con staging.

Código auditado: `staging` 5124e710 (1.1.109). Producción despliega `main`
5168a4b7, con árbol idéntico (ver §3).

Reproducir (solo lectura; abre la base con `default_transaction_read_only=on`):

```bash
cd lyricgen/backend
DATABASE_URL=... python3.11 scripts/report_review_baseline.py \
    --label staging-umg --tenant universal_music --limit 100 \
    --json-out /tmp/staging.json --rows-out /tmp/staging-rows.jsonl
DATABASE_URL=... python3.11 scripts/report_review_baseline.py --label production
```

`--rows-out` incluye títulos: no se commitea. Tests: `tests/test_report_review_baseline.py`.

## Método

- **Jobs**: los últimos 100 por fecha de la **primera** versión `is_approved`
  de `editor_versions`. Se excluyen los tenants de pruebas automáticas
  (`preflight_*`, `golden_render_bot`, `e2e*`, smoke, etc.). En staging eran
  60 de los 100 más recientes. Staging queda 100 % `universal_music`.
  Producción tiene solo 58 jobs aprobados en total, el último el 2026-09-09:
  se usan los 58.
- **Minutos activos**: latidos `editor_activity_heartbeat` con el criterio de
  `report_reviewer_minutes.active_seconds` (huecos ≤ 25 s, todas las
  sesiones, por revisor y sumados por job). Se cortan en la primera
  aprobación (+60 s). Lo posterior se informa aparte como corrección post-aprobación.
- **Ediciones**: diff entre la *versión de máquina* y la primera versión
  aprobada, con el mismo emparejador de líneas del AuditLog
  (`training_corpus._match_rows`).
  - La versión de máquina es la última `transcription`/`migration` anterior
    a la primera versión humana. Así, la canonicalización que corre al abrir
    el editor no cuenta como edición humana.
  - Se informa también el diff crudo contra la versión 0.
  - Umbral de timing: 50 ms, igual que `TIMING_NOISE_THRESHOLD_S`.
- **Estudio/vivo**: `transcription_quality.metrics.is_live` está en 2 de 100
  jobs de staging y en 0 de prod. Para el resto se usa la regex de
  `main._looks_live` sobre título y archivo, marcada como **inferida**.

## 1. Línea de base

### Minutos activos por job

| Entorno | Grupo | Jobs (con latidos) | Mediana hasta aprobar | p90 | Mediana total (incluye post-aprobación) | p90 total | Min/línea |
|---|---|---|---|---|---|---|---|
| Staging UMG (10-sep → 4-oct) | Todos | 100 (97) | **8,6** | 20,6 | 10,6 | 23,2 | 0,25 |
| | Estudio | 97 (94) | 8,6 | 21,0 | 10,5 | 23,3 | 0,25 |
| | Vivo (inferido) | 3 (3) | 16,5 | 18,8 | 16,5 | 18,8 | 0,45 |
| Prod (11-ago → 9-sep) | Todos | 58 (45) | 4,3 | 18,8 | **10,1** | 24,5 | 0,12 |
| | Estudio | 48 (36) | 1,3 | 15,9 | 6,8 | 22,0 | 0,05 |
| | Vivo (inferido) | 10 (9) | 10,1 | 18,8 | 17,1 | 39,2 | 0,27 |

Lectura:

- **Staging es la referencia.** En prod, Chile (35 de 58 jobs) aprobaba el
  cliente primero (`sebastian.vargas@umusic.com`, mediana de 0,3 min antes
  de aprobar). La revisión real ocurría después de la aprobación: 249 min
  post-aprobación en 58 jobs. Por eso, en prod solo la columna "total" es
  comparable.
- **Post-aprobación en staging**: 246 min en 42 de los 100 jobs (correcciones
  posteriores a la primera aprobación).
- **Quién aprobó en staging**: `agus77` 85, `tomas@epical.digital` 11 y la
  cuenta de lote `batch-universal-staging` 3 (no es revisión humana).
- **El "~15 min" de partida no aparece en los datos.** La mediana medida es
  8,6–10,6 min. Los latidos son cota inferior: no cuentan pensar con el
  editor cerrado.

### Ediciones (máquina → primera aprobación)

| Entorno | Grupo | Jobs comparables | **Sin ninguna edición** | Sin edición de texto/altas/bajas | Sin edición de timing | % líneas con texto editado | % con timing editado (inicio / fin) | % altas | % bajas | Mediana por job: texto / timing / altas / bajas |
|---|---|---|---|---|---|---|---|---|---|---|
| Staging UMG | Todos | 100 | **4 %** (crudo vs v0: 3 %) | 16 % | 4 % | 30,5 % | 65,2 % (28,2 / 62,8) | 8,1 % | 6,1 % | 7,5 / 21 / 0 / 1 |
| | Estudio | 97 | 4,1 % | 16,5 % | 4,1 % | 30,0 % | 65,5 % | 8,1 % | 6,0 % | 7 / 21 / 0 / 1 |
| | Vivo | 3 | 0 % | 0 % | 0 % | 45,4 % | 54,6 % | 9,3 % | 9,3 % | 13 / 16 / 3 / 1 |
| Prod | Todos | 54 | 3,7 % (crudo: 1,9 %) | 51,9 % | 3,7 % | 13,1 % | 28,0 % (9,7 / 26,5) | 3,2 % | 0,9 % | 0 / 7 / 0 / 0 |
| | Estudio | 45 | 4,4 % | 57,8 % | 4,4 % | 9,1 % | 26,4 % | 1,6 % | 0,7 % | 0 / 6 / 0 / 0 |
| | Vivo | 9 | 0 % | 22,2 % | 0 % | 34,7 % | 36,6 % | 12,3 % | 1,9 % | 9 / 11 / 1 / 0 |

Lectura:

- **El timing domina.** En staging se tocan 2,8 veces más líneas por timing
  que por texto, y lo que más se toca es el **fin** de línea: 62,8 % de las
  líneas.
- **Qué pasa con los finales** (líneas con el mismo texto, staging):
  - El operador mueve el fin en el 61 % de los casos.
  - Entre las movidas, la mediana es **−0,20 s**, y el 49 % recorta entre
    0,15 y 0,5 s.
  - Es consistente con deshacer parte del hold de 0,5 s (`LYRIC_HOLD_S=0.5`).
  - No estaba en el plan: ver "Riesgos".
- **Migraciones al abrir**: 59 de los 100 jobs de staging tienen al menos una
  revisión `migration`, y en 25 hay una *después* de que el humano empezó a
  editar. En 50 jobs la versión de máquina que vio el operador es una
  `migration`, no la versión 0. El impacto por línea se mide en la fase 5,
  como pide el plan.
- **Prod**: 12 de 58 jobs no tienen versión 0 (`transcription`) porque son
  documentos legacy. 4 quedan sin diff comparable.

## 2. Doble adelanto (~160 ms)

**Confirmado en el código y en los datos.**

En el camino WhisperX hay dos adelantos y los dos restan del `start` de la
línea; ninguno mueve las palabras:

1. `whisperx_transcribe._apply_lead_in`, con `LYRIC_LEAD_IN_MS` por defecto
   en 80 ms (`whisperx_transcribe.py:997`, llamado en `:1214`).
2. `lead_in.polish` dentro de `main._snap` (`main.py:7529`), con
   `LYRIC_LEAD_IN_S=0.08`, que está seteada en todos los servicios de los dos
   entornos.

Cuando CTC retimea (`main.py:6810`), los inicios se recalculan desde los
onsets y se aplica un solo adelanto.

### Adelanto en la versión de máquina (primera palabra − inicio de línea)

| Entorno | `timing_source` | Líneas | Mediana | En 0,06–0,10 s (un adelanto) | En 0,14–0,18 s (doble) | ≈0 |
|---|---|---|---|---|---|---|
| Staging UMG | ctc_align (70 jobs) | 2.148 | 0,08 | **95,8 %** | 0 % | 0 % |
| | whisperx (15 jobs) | 476 | **0,16** | 5,3 % | **70,6 %** | 7,4 % |
| | whisperx_reconciled (13) | 29 | 0,08 | 37,9 % | 37,9 % | 0 % |
| Prod | ctc_align (39) | 1.347 | 0,08 | 89,6 % | 5,0 % | 0 % |
| | whisperx (4) | 137 | 0,16 | 8,8 % | 51,1 % | 23,4 % |
| | whisperx_reconciled (7) | 128 | 0,00 | 6,2 % | 3,9 % | 82,8 % |

La dispersión dentro de whisperx (≈0 y 0,005–0,10) es esperable por dos motivos:

- Los clamps: ningún adelanto pisa el fin de la línea anterior.
- El beat-snap de ±80 ms, que está **prendido** (ver §3).

### Corrimiento de inicio que hizo el operador (máquina → aprobada, mismo texto)

| Entorno | Origen | Líneas | Movidas ≥ 50 ms | Mediana de las movidas | p25 / p75 de las movidas |
|---|---|---|---|---|---|
| Staging UMG | ctc_align | 1.469 | 10,7 % | +0,08 s | −0,13 / +0,26 |
| | whisperx | 173 | 17,9 % | **+0,22 s** | +0,13 / +0,80 |
| | whisperx_reconciled | 356 | 37,6 % | +0,61 s | +0,17 / +2,09 |
| Prod | ctc_align | 1.184 | 2,5 % | +0,18 s | −0,30 / +1,73 |
| | whisperx | 44 | 25 % | −0,52 s | −2,47 / −0,15 |

- **La mayoría de las líneas no se toca:** la mediana de todas es 0. En
  staging, cuando el operador mueve una línea de origen whisperx, la corre
  hacia **después**, que es la dirección que corrige un adelanto de más. Pero
  la magnitud está dominada por errores mayores, no por 80 ms.
- **Efecto visible:** en staging, el 15 % de los jobs (origen whisperx)
  tiene el 71 % de sus líneas unos 160 ms antes de la voz, el doble de lo
  calibrado. Otro 13 % (whisperx_reconciled) tiene así 4 de cada 10 líneas. `lead_in.py` documenta que 120 ms ya se percibía
  como "la línea cae antes que la voz".
- **Fade:** ningún job de las dos muestras tiene `lyric_transition="fade"`
  guardado. En todo el histórico son 13 en staging y 24 en prod. El
  corrimiento de 75–150 ms del render de edición no aplica a esta muestra.

Corrección propuesta (fase 5.4, sujeta a decisión): un solo adelanto
configurable, sin tocar el render de jobs ya aprobados.

## 3. Producción vs staging en las 7 áreas

**El código es el mismo.** `main` (5168a4b7) contiene a `staging` (5124e710)
más 42 commits de merge y espejo, y `git diff origin/staging origin/main`
da vacío. La promoción del 3-oct (#1485) llevó 1.1.109 a producción. Railway
prod despliega `main` en 5168a4b7 en api, Worker, ShortWorker y
quality-worker. Producción no tiene BatchWorker ni BatchShortWorker.

Lo que difiere son las **variables** y los **datos**.

| Área | Variable | Staging (api/Worker/Short/quality) | Prod | Efecto |
|---|---|---|---|---|
| 1 Medición | `REVIEWER_TIMING_CAPTURE_ENABLED` | 1/·/·/· | — | Prod no captura `lyrics.prospective_timing` |
| 2 Señales | `QUALITY_OPERATOR_SUGGESTIONS_ENABLED` | 1/·/·/1 | — | Prod no muestra propuestas de calidad |
| 3 Editor | `EDITOR_V2_GLOBALLY_ENABLED` | — (staging lo habilita por entorno) | 1/1/1/· | Mismo editor en los dos |
| 3 Editor | `LYRIC_REVIEW_MODE` | observe/·/·/· | observe en los 4 | Igual en la práctica (observe) |
| 5 Referencia | `APPROVED_TEXT_REUSE_ENABLED`, `CAMPAIGN_LRCLIB_CANDIDATE_ENABLED` | ·/·/1/· (y BatchShort) | — | Prod no reusa letra aprobada ni busca lrclib en campañas |
| 6 QC | `DELIVERY_QC_MODE` | observe | — (default off) | — |
| 6 QC | `DELIVERY_QC_GATES_OFF` | — | 1 | Prod apaga los gates en cualquier entorno |
| 6 QC | `DELIVERY_QC_STAGING_GATES_OFF`, `…_UMG_STAGING_REVIEW_BYPASS` | 1 en api | — | Staging los apaga con su interruptor propio |
| 6 QC | `DELIVERY_QC_OCR_ENABLED` | 1 | — | OCR de no-UMG solo en staging |
| Otros | `LANGUAGE_REVIEW_ADVISORY` | — | 1 | Prod no bloquea por idioma |
| Otros | `CHANGE_REQUEST_INTERPRETER_ENABLED` | 1 en api | — | Interpretación de pedidos con IA solo en staging |

Son iguales en los dos entornos: `LYRIC_LEAD_IN_S=0.08`, `LYRIC_HOLD_S=0.5`,
`CTC_ALIGN_ENABLED=1`, `FORCED_ALIGNER_ENABLED=1`, `ANCHOR_LYRICS_ENABLED=1`,
`BEAT_SNAP_ENABLED=1` (api y ShortWorker), `TRANSCRIPTION_QUALITY_MODE=observe`,
`REPETITION_RECONCILE`/`PHRASE_SEGMENTER`/`WORD_VOTE`/`ADLIB_CONSENSUS`/
`LLM_SEGMENT`/`LINE_TEXT_CORRECT`=1, `GAP_RESCUE`/`CHORUS_SNAP`=0 y
`KARAOKE_FA_RETIME_ENABLED` sin setear (apagado).

**Datos**: la base de prod no tiene aprobaciones desde el 9-sep. El trabajo
UMG gestionado corre en staging (tenant `universal_music`).

**Recomendación**: no hace falta converger el código. Producción no sirve
como línea de base porque no tiene tráfico de revisión; las mediciones de
este programa se hacen en staging. Las banderas nuevas del plan quedan
apagadas en prod. Riesgo: cualquier promoción futura de staging lleva el
código de estas fases a prod con las banderas en su default (apagadas). Hay
que revisar la tabla de variables en ese momento, como en la promoción del
1-oct.

## Contradicciones con la auditoría

1. **`BEAT_SNAP_ENABLED=1`** en api y ShortWorker, en los dos entornos. La
   auditoría lo daba como apagado (default del código: 0). Corre en
   `main._snap` antes del lead y corre inicios hasta ±80 ms
   (`BEAT_SNAP_WINDOW_MS`) en líneas sin palabras confiables (score < 0,5).
   Es una tercera fuente de corrimiento de inicios previa a la revisión.
2. `LLM_SEGMENT_ENABLED`, `LINE_TEXT_CORRECT_ENABLED` y `LIVE_NO_HINT_ENABLED`
   están en 1 en los dos entornos. La auditoría informó los defaults del
   código (0). `LLM_SEGMENT` igual queda bloqueado por el gate de mutación.
   `LINE_TEXT_CORRECT` solo actúa cuando `lyrics_source == "gemini"`, y su
   relación con el gate no está verificada.
3. **Producción ya no es otra línea de código**: desde el 3-oct es el mismo
   árbol que staging.
4. Del "corpus de 23 canciones": se confirmó que vive fuera del repo (lo
   mismo que en la auditoría). Este dato se usa en la fase 2.

## Riesgos vistos que no estaban en el plan

- **El hold de 0,5 s genera trabajo.** Es la edición de timing más
  frecuente: se recorta el fin en el 61 % de las líneas, mediana −0,20 s.
  Bajarlo o derivarlo del fin del canto podría ahorrar más minutos que
  cualquier ajuste de inicio. No se toca sin decisión; afecta el render, como
  el adelanto.
- **Las migraciones al abrir son frecuentes**: 59 de los 100 jobs. Refuerza
  la fase 5.1.
- **Muestra de vivos chica**: 3 jobs en staging. Las conclusiones por vivo
  son débiles hasta que se persista `is_live` para todos los jobs.
- **"Aprobado" no significa lo mismo en los dos entornos**: en prod lo
  aprobaba el cliente antes de la revisión del operador. Para comparar entre
  entornos hay que usar minutos totales.
