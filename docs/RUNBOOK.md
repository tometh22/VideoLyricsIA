# VideoLyricsIA — Production Runbook

Este documento describe cómo operar VideoLyricsIA en producción para clientes
tipo UMG. Léelo antes del primer lanzamiento y tenelo abierto durante las
primeras semanas.

## Topología

```
[Usuario] → Caddy / nginx → API (uvicorn, 2 workers)
                         ↓
                  PostgreSQL (state) ─┐
                         ↓            │
                      Redis  ←→  Worker × 3 (rq)
                         ↓
                 Cloudflare R2 (masters)
                         ↓
                      Sentry (errores)
```

- `api`: recibe uploads, encola jobs, sirve `/status`, redirige `/download`
  a signed URLs de R2.
- `worker.py`: corre los jobs en procesos separados. Precarga Whisper turbo
  al arrancar.
- PostgreSQL: estado canónico de usuarios, jobs, facturas, provenance,
  audit log, tokens de password reset y email verification. Hard dependency
  — sin Postgres el API no arranca (`init_db()` corre en startup).
- Redis: cola de jobs (RQ). Dos colas con prioridad: `enterprise` y
  `default`. Si falta, `queue_jobs.py` cae a `threading.Thread` (solo dev).
- R2: storage permanente de los masters (egress gratis). Si falta, los
  deliverables se sirven desde disco local (slow para archivos grandes).
- Sentry: errores + contexto. Gated por `SENTRY_DSN`.

## Primera vez que se hace deploy

1. Clonar el repo y ubicarse en la branch de producción.
2. Copiar `lyricgen/backend/.env.example` a la raíz como `.env`.
3. Generar JWT_SECRET fuerte: `openssl rand -base64 32`.
4. Configurar `DATABASE_URL` (formato `postgresql://user:pass@host:5432/db`).
   En docker-compose la base local viene en el servicio `db`.
5. Configurar `REDIS_URL` (en compose: `redis://redis:6379/0`).
6. Crear bucket en Cloudflare R2 y llenar `R2_*`.
7. Crear proyecto Sentry → copiar DSN.
8. Subir `vertex_credentials.json` a `./secrets/`.
9. `docker compose up -d --build` (levanta `db`, `redis`, `api`, `worker`).
10. La primera vez, `init_db()` corre en el evento startup del API y crea
    las tablas + el admin default (`admin` / valor de `ADMIN_PASSWORD`).
11. Chequear `curl http://localhost:8000/health` — debe devolver
    `{"status":"ok", "redis":"up", "r2":"configured", "disk_free_gb": >0, ...}`.
12. Rotar el admin: crear uno nuevo con tu password,
    ```bash
    docker compose exec api python -c "
    from database import SessionLocal
    from auth import create_user
    db = SessionLocal()
    create_user(db, 'tu_usuario', 'tu_password_fuerte', role='admin')
    db.close()"
    ```
    y desactivar el default desde el AdminPanel UI o con
    ```bash
    docker compose exec api python -c "
    from database import SessionLocal, User
    db = SessionLocal()
    db.query(User).filter(User.username=='admin').update({'is_active': False})
    db.commit(); db.close()"
    ```

## Cosas que van a fallar y qué hacer

### PostgreSQL caído

`init_db()` corre en startup, así que un Postgres caído impide arrancar el
API. Síntoma: `uvicorn` muere con `OperationalError: could not connect`.

```
docker compose restart db
docker compose logs -f db          # confirmar que aceptó conexiones
docker compose restart api worker  # re-arrancar lo que dependa de la DB
```

Si la corrupción es real (`pg_dump` falla), restaurar desde el último
backup:

```
gunzip -c backups/genly-YYYYMMDD.sql.gz | \
  docker compose exec -T db psql -U genly -d genly
```

Mientras Postgres está caído, los workers también frenan: usan
`SessionLocal` para escribir el estado de los jobs.

### Redis caído

```
docker compose restart redis
```

RQ detecta la desconexión, los jobs en cola no se pierden, los jobs en curso
cuando Redis volvió son re-encolados automáticamente por `failure_ttl`.

### Un worker se colgó

```
docker compose restart worker
```

En Railway: dashboard → servicio Worker → botón **Restart**.

RQ tiene `job_timeout=2700` (45 min). Si un job excede eso, RQ lo mata y lo
marca como `failed`. Se puede re-encolar manualmente con
`rq requeue -u $REDIS_URL --all`.

### Job parece colgado en la UI (barra de progreso quieta)

**No mires los logs primero.** moviepy entra en código C durante el
compositing y NO emite líneas — la barra UI quieta + logs silentes es
la apariencia normal de un render lento, no de un hang. La señal
real de progreso vive en la tabla `jobs`:

```
echo "SELECT progress, current_step, status, EXTRACT(EPOCH FROM (NOW() - created_at)) AS age_s
      FROM jobs WHERE job_id='<JOB_ID>';" | railway connect Postgres -e production
```

Resultados típicos:
- `progress` cambia entre dos chequeos a 30 s → **vivo**, dejarlo correr
- `progress` quieto > 10 min en `current_step IN (video, short)` →
  moviepy se trabó (más probable con backgrounds .mp4 que requieren
  palindrome loop). Cancelá y reintentá con un asset .jpg de la
  biblioteca (Ken Burns, mucho más rápido y estable):
  ```
  echo "UPDATE jobs SET status='error', error='moviepy hang during <step>'
        WHERE job_id='<JOB_ID>';" | railway connect Postgres -e production
  ```
  Después restart Worker en Railway dashboard.
- `progress` quieto > 15 min en `whisper`, `lyrics` o `validation` →
  causa distinta; ahí sí mirá `railway logs --service Worker` por una
  excepción Python.

Atajo: `python scripts/diagnose_job.py <JOB_ID>` o `--watch` para que
poll'ee y avise transición.

### Veo 3 devuelve 429 en masa

Esperar — el pipeline ya reintenta hasta 5 veces con backoff. Si el problema
persiste más de 1 h:
1. Ver cuota en la consola de GCP (Vertex AI quotas).
2. Si estás a punto de agotarla, pausá la cola:
   ```
   docker compose exec api python -c "from queue_jobs import _init_redis; r,d,e=_init_redis(); d.empty(); e.empty()"
   ```
   y abrí ticket de soporte con Google.

### Disco lleno

El endpoint `/health` avisa cuando el disco baja de 10 GB libres (status
`degraded`). Mientras tanto:
1. `docker system prune -f` para liberar imágenes viejas.
2. Borrar outputs locales que ya subieron a R2: los archivos en `outputs/*/`
   con su correspondiente key en R2 son seguros de borrar.

### Cliente reporta rechazo en QC de UMG

1. Correr `ffprobe -show_streams -show_format` sobre el master reportado.
2. Comparar con la función `_validate_umg_master` en `pipeline.py` — si
   coincide con todas las checks, el warning de UMG es el esperado
   (confirmado por Santi).
3. Si algún campo NO coincide, revisar Sentry el día del render para ver
   si hubo algún warning silenciado.

### JWT_SECRET comprometido

1. `openssl rand -base64 32` → nuevo valor.
2. Cambiar en `.env` y `docker compose up -d api`.
3. Todos los tokens emitidos quedan inválidos, cada usuario debe
   re-loguearse.

## Métricas a revisar cada día

- Sentry: ¿errores nuevos en las últimas 24 h?
- `GET /admin/queue` (como admin): ¿queue depth razonable?
- `GET /health`: ¿disco libre > 20 GB, Redis up, R2 configurado?
- GCP billing console: costos de Vertex AI dentro de lo presupuestado.

## Checklist go/no-go antes de mandar a UMG

- [ ] `.env` tiene `ENV=prod` y `JWT_SECRET` es un valor generado (no el default).
- [ ] `CORS_ORIGINS` restringido al dominio del frontend.
- [ ] Admin default (`admin` / `genly2026`) rotado o borrado.
- [ ] `R2_*` configurado, bucket creado, upload de prueba exitoso.
- [ ] `SENTRY_DSN` activo y errores llegan a tu dashboard.
- [ ] `vertex_credentials.json` montado en `./secrets/`, permisos 600.
- [ ] `curl /health` devuelve `status: ok`.
- [ ] Test de carga: 10 uploads en paralelo, todos terminan sin errores.
- [ ] Test de delivery: 1 master real enviado a Santi → pasa QC.
- [ ] Backup de PostgreSQL configurado (cron diario):
      ```
      docker compose exec -T db pg_dump -U genly genly | gzip > \
        backups/genly-$(date +%Y%m%d).sql.gz
      ```
      Retención mínima: 30 días. Validar restore una vez al mes.
- [ ] Backup de R2 (versionado del bucket activado o snapshot mensual a
      otra región).

## Contactos

- **Cliente ancla:** Santi (Universal Music) — confirmó verbalmente que el
  warning QC por H.264 en la cadena es aceptado y procesan igual.
- **Soporte Runway** (si migramos de Veo): `support@runwayml.com`.
- **GCP billing alert:** umbral recomendado $500/mes.

---

## UMG Master smoke test (run before each delivery week)

The UMG ProRes render path has a real spec test fixture and a smoke test that
verifies end-to-end ffmpeg + moviepy + ffprobe behavior. Run them before
shipping a batch of UMG masters, especially after dependency updates.

```bash
cd lyricgen/backend

# Fast unit tests (~seconds; runs in CI by default)
./venv/bin/pytest tests/test_render_spec.py tests/test_validate_umg_master.py -v

# End-to-end smoke (real ProRes render, ~20s; deselected in CI by default)
./venv/bin/pytest -m umg_smoke -v
```

The smoke test exercises:
- ProRes 422 HQ at 1080p / 24 fps
- Fractional fps (23.976 → `24000/1001` rational, R1 fix)
- Deterministic font selection for UMG profile (same `job_dir` → same font)
- ffprobe validation chain (codec, profile, dimensions, fps, color, container)

If any smoke test fails, **do not deliver to UMG until the failure is
diagnosed**. Common causes after dep updates:

1. moviepy / ffmpeg version mismatch breaking ProRes encoding flags.
2. `_validate_umg_master` regression after adding a new field check.
3. Missing fonts in `lyricgen/assets/fonts/` causing fallback to "Arial".

## Incident response — Sentry pages or uptime alert at 2am

5-line checklist before escalating:

1. **Scope:** is it one tenant or all? Check `/admin/cost` with `?since_days=1`
   — if only one tenant has spent today, the issue is likely tenant-specific.
2. **Queue depth:** check the running pipeline count via `docker compose
   logs worker --tail=50`. If 0 processing, restart the worker:
   `docker compose restart worker`.
3. **External APIs:** check Veo / Gemini / Whisper status:
   - Vertex AI status: <https://status.cloud.google.com/>
   - If Veo is down, the library-fallback path should kick in automatically;
     verify by checking `/admin/provenance` for `tool_name=library-fallback`
     records in the last hour.
4. **Disk space:** `df -h` — if `/outputs` or the R2 cache hits >90%, run the
   intermediate cleanup: `docker compose exec backend python -c "from
   pipeline import _cleanup_local_intermediates; import os; [
   _cleanup_local_intermediates(os.path.join('outputs', d)) for d in
   os.listdir('outputs') ]"`.
5. **Status page:** if the issue exceeds 15 minutes of impact, post to the
   public status page (or send a single email to the named UMG contact)
   acknowledging the issue, ETA, and current actions.

If none of the above resolves: read the most recent Sentry stack trace
end-to-end before changing any code. Don't restart-loop the worker.

---

## UMG ProRes delivery — failure-mode audit (2026-05-07)

End-to-end audit of every plausible failure mode for UMG ProRes
exports, what's already protected, what's not, and pre-launch
checks. Re-read this before sending the first batch to UMG.

### Things already protected

| Risk | Protection | Reference |
|---|---|---|
| DB connection saturation under 5 concurrent jobs | `pool_size=20, max_overflow=10` | `database.py:42-44` |
| Vertex Veo 429 / RESOURCE_EXHAUSTED | 5 attempts, exponential backoff 60→300s | `pipeline.py:2707` |
| R2 multipart upload timeout / transient failure | `max_attempts=5, mode=adaptive`, 30s connect, 120s read | `storage.py:42-46` |
| R2 upload of multi-GB masters slow | 64 MB chunks × 20 concurrent threads | `storage.py:50-61` |
| Worker RAM (24 GB Railway) | ProRes peak ~3 GB; sobra 8× | Railway plan |
| Empty / whitespace lyric rows | Frontend filter + backend filter + `_make_text_clip` early-return | `pipeline.py:3320-3321`, `LyricsEditor.jsx:431-435` |
| Job timeout for UMG ProRes | 90 min for `umg`/`both`, 45 min for `youtube` | `queue_jobs.py:18-23, 75-80` |
| Soft cap simultaneous jobs per tenant | 5 max; rest queue | task #56 |
| R2 input MP3 cleanup | Auto-delete > 30 days | task #53 |
| UMG master spec compliance | `_validate_umg_master` runs after every render | `pipeline.py:3393` |
| Subtitle overlap on render | Clamp `end` to `next.start - 50ms` | `pipeline.py:3577-3593` |
| Title duplication on render | Pick ONE strategy (centered drop OR top-third), never both | `pipeline.py:3585-3640` |

### Things NOT yet protected — pre-launch checklist

| # | Risk | Action required | Owner |
|---|---|---|---|
| 1 | Worker disk efímero < 10 GB on Railway plan | Verify Railway worker plan has ≥10 GB ephemeral disk (Pro plan or higher). UMG master + intermediates can hit ~3-4 GB tmp. | Tomi |
| 2 | R2 multipart upload incomplete if worker dies | Configure Cloudflare R2 lifecycle rule "abort incomplete multipart > 1 day". 5 min in Cloudflare console, no code change. | Tomi |
| 3 | UMG QC rejects color metadata `unknown` | If rejected, post-process with `ffmpeg -metadata` to write explicit `color_primaries=bt709, color_trc=bt709`. Currently the `colr` atom is written but ffprobe doesn't surface the stream-level tags. | Tomi (only if UMG flags) |
| 4 | UMG asks for ProRes 4444 (profile 4) instead of 422 HQ (profile 3) | Add profile 4 option to listbox. RenderSpec already supports it. | Tomi (only if UMG asks) |
| 5 | Vertex token expires mid-render-storm (>60min) | Improbable; refresh-on-call already in `_get_genai_client()`. Monitor first UMG batch logs. | Tomi |

### Things I cannot test from local — confirm in prod

- **R2 multipart upload of real 1.5 GB file**: local has no R2 creds suitable for the prod bucket. First UMG job in prod is the verification.
- **3 workers parallel uploading 1 GB each** (3 GB total in flight): Railway egress saturation. If first batch is slow, increase worker concurrency cap or batch size.
- **R2 5xx errors during multi-GB upload**: boto3 retries 5×; if all fail the job lands in error with `s3_keys` not set. Manual recovery required.

### Failure modes by category

**Hard fails (job ends in error)**:
- Vertex AI 5xx persistent → Veo gen fails after 5 retries
- R2 endpoint unreachable → upload step throws
- `_validate_umg_master` returns errors (e.g. wrong bitrate) → master rejected
- moviepy / ffmpeg crash on weird audio (very rare; corrupt file)

**Silent fails (job marks done, but output broken)**:
- ⚠️ R2 upload appears successful but file is corrupted (network silently drops bytes)
  - **Mitigation**: post-render verify ffprobe each deliverable, assert size + duration + codec match expected before marking job `done`. **TODO** — see task #133.
- Color metadata stripped silently (only fails downstream UMG QC)

**Performance / queue issues** (not failures, but UX):
- 5+ simultaneous bursts → 2 wait in queue ~10 min each. By design.
- Veo cold cache → first batch of new bgs hits Vertex Veo for every video. Subsequent reuses are free via R2 cache.

### Pre-flight check before sending first UMG delivery

1. **Render one test track** with `delivery_profile=both` end-to-end on prod.
2. **Download the .mov** via the master tab in JobDetail.
3. **Open in QuickTime + DaVinci** to confirm playback + color + audio.
4. **Run `ffprobe -show_streams`** locally on the downloaded file. Confirm:
   - codec_name=prores, profile=HQ
   - 1920x1080, 24/1 fps
   - pix_fmt=yuv422p10le, bits_per_raw_sample=10
   - audio: pcm_s24le, 48000 Hz, 24-bit, 2 channels
5. **Confirm with UMG** the file passes their QC before scaling to 200/month.

### Recovery procedures

**Job stuck in `processing` > 90 min**:
1. Check Railway worker logs for the job_id.
2. Run `python scripts/diagnose_job.py <job_id>` → shows current_step + progress + age.
3. If stuck on `umg_master` step, render likely OOMing or disk full. Check Railway metrics.
4. Cancel job via DELETE /jobs/{id} (works on processing). Operator re-uploads.

**Master file corrupted (operator reports playback fails)**:
1. Check R2 bucket for the object; download and ffprobe locally.
2. If corrupted, delete the s3_key from the Job row and re-render via `/jobs/{id}/render-master` (when implemented) or full re-submit.

**R2 storage growing unexpectedly**:
1. Run `aws s3 ls --recursive s3://lyricgen/ | awk '{sum+=$3} END {print sum/1024/1024/1024 " GB"}'`.
2. Look for incomplete multipart uploads: `aws s3api list-multipart-uploads --bucket lyricgen`.
3. Abort them: `aws s3api abort-multipart-upload --bucket lyricgen --key X --upload-id Y`.
4. Long-term: configure R2 lifecycle rule (item #2 above).
