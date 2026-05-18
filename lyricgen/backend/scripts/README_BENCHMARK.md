# Lyrics quality benchmark harness

Mide cuantitativamente cuánto mejoran (o no) las modificaciones al
pipeline de transcripción **antes de cualquier deploy** a staging.

## Por qué existe

Las "mejoras de calidad AI" suenan bien sin medirse. Este harness
toma N jobs aprobados por el operador (cuya `segments_json` es el
ground-truth de lo que UMG considera shippable) y compara el output
de cualquier iteración del pipeline contra ese ground-truth.

Output: un Markdown report con WER (texto) + AOO (timing) per-job y
agregados, más un veredicto de ship/no-ship según los thresholds del
plan (`/Users/tomi/.claude/plans/ok-y-en-stanging-resilient-engelbart.md`).

## Workflow

### 1. Setup (una sola vez)

```bash
cd lyricgen/backend
source venv/bin/activate
pip install jiwer  # WER scoring
```

Variables de entorno necesarias:

```bash
export DATABASE_URL='postgresql://...prod...'   # para bajar ground truth
export R2_ACCESS_KEY_ID='...'
export R2_SECRET_ACCESS_KEY='...'
export R2_ENDPOINT_URL='https://....r2.cloudflarestorage.com'
export R2_BUCKET='genly-deliverables'
export OPENAI_API_KEY='...'                      # Whisper API
export GOOGLE_APPLICATION_CREDENTIALS='/path/to/vertex.json'  # Gemini
```

### 2. Curar el dataset

Editar `scripts/benchmark_jobs.txt` con los `job_id`s (uno por línea)
de 5-10 jobs aprobados en prod. Recomendado: variedad de canciones
(longitud, género, "limpitas" vs "difíciles").

```bash
python scripts/build_benchmark_dataset.py
```

Esto crea `benchmark/dataset/<job_id>/` con:
- `audio.wav` — input audio bajado de R2
- `ground_truth.json` — segments_json post-aprobación (lo que el
  operador firmó como shippable)
- `metadata.json` — artist, song_title, etc.

Tamaño típico: ~30-50 MB por canción, queda fuera de git
(`benchmark/dataset/` está en `.gitignore`).

### 3. Baseline run

Corre el pipeline tal cual está hoy, sin las mejoras Tier 1.

```bash
python scripts/run_pipeline_local.py
```

Salida: `benchmark/dataset/<job_id>/baseline_output.json` por canción.
Tarda ~1-2 min por canción (mayormente Whisper API).

### 4. Tier 1 run

Activa los nuevos helpers gateados por env flags y corre de nuevo.

```bash
export ENABLE_TIER1=1
python scripts/run_pipeline_local.py
```

O activar individualmente para atribuir un delta a un solo helper:

```bash
export VALIDATE_SEGMENTS=1  # solo el flagging AI
# o
export POLISH_TEXT=1        # solo el corrector ortográfico Gemini
python scripts/run_pipeline_local.py
```

Salida: `benchmark/dataset/<job_id>/improvement_output.json`.

### 5. Score y reporte

```bash
python scripts/score_benchmark.py
# o guardar:
python scripts/score_benchmark.py --out BENCHMARK_REPORT.md
```

El reporte trae:
- Tabla per-job: WER baseline → tier1, AOO baseline → tier1, composite
- Agregados (medias + deltas porcentuales)
- Veredicto automático según los thresholds del plan

### 6. Decidir

| Reporte dice... | Acción |
|---|---|
| WER ↓ ≥30% **AND** AOO ↓ ≥40% | ✅ Ship Tier 1 a staging |
| Solo una métrica ↓ ≥30% | 🟡 Ship parcial — solo el helper responsable |
| Ambas ↓ <15% | ❌ No ship — re-tune antes |
| Empeora alguna | ❌❌ Bug. Investigar antes de cualquier deploy |

## Costo aproximado

- Build dataset: $0 (solo descargas R2 + DB query)
- Baseline run: ~$0.01-0.05 por canción (Whisper API solo, sin Gemini extra)
- Tier 1 run: ~$0.02-0.10 por canción (Whisper + 3 muestras de validate + 1 Gemini polish)
- Total para 10 canciones, ambos runs: ~$0.30-1.50

## Archivos

- `build_benchmark_dataset.py` — descarga audios + ground-truth de prod
- `run_pipeline_local.py` — corre el pipeline standalone, guarda output
- `pipeline_runner.py` — wrapper sin FastAPI alrededor de la cascada
- `score_benchmark.py` — calcula WER + AOO + reporte Markdown
- `benchmark_jobs.txt` — lista editable de job_ids
- `../pipeline.py` — donde viven `_validate_segments_against_audio` y
  `_polish_segments_text` (los helpers Tier 1), gateados por env flags

## Limitaciones conocidas

- El dataset es Spanish-only por construcción (UMG). Adaptar
  `pipeline_runner.transcribe_local()` para multi-idioma si se
  amplía el corpus.
- `_fetch_lrclib` se llama con `db=None` → sin cache lrclib local.
  Cada run hace HTTP fresco a lrclib.net. Marginal pero notable si
  vas a iterar 50 veces el mismo dataset.
- WER mezcla todo el texto en un string — no diferencia entre "una
  línea entera mal" y "una palabra mal en cada línea". Mirar el
  detalle por job si una canción tiene WER alta.
