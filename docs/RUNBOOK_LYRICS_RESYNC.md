# Runbook: Re-sincronizar lyrics sobre videos aprobados

Cuándo usar este flujo: el video ya pasó por revisión (`status=done`) y el background es correcto, pero las letras están desfasadas respecto del audio. Permite corregir los timings **sin regenerar el background** (no se paga Veo) y **sin re-subir el MP3**.

## Precondiciones

- El job está en `status ∈ {done, pending_review, rejected}`.
- El background fue cacheado en R2 (`bg_r2_key_cached` está poblado — todos los jobs aprobados lo tienen).
- El job todavía tiene `edits_remaining > 0` (máximo 3 ediciones por job).
- Si el job ya fue publicado en YouTube, el sistema avisará: el archivo en la plataforma se actualizará pero el video en YouTube **no se reemplaza** (la API de YouTube no permite reemplazar el archivo, solo metadata).

## Pasos

1. **Abrir el job**
   - Dashboard → click en el job afectado, o History → click en la fila.
   - Verificar que el preview reproduce con el sync mal y que el background visualmente está OK.

2. **Abrir el editor completo**
   - En `JobDetail`, baja al panel "¿Necesitás ajustes?".
   - Para jobs en `done` el único modo disponible es **Lyrics** (los modos Typography y Background solo se ofrecen en `pending_review`).
   - Click en **Lyrics** → se abre un overlay full-screen con el **mismo editor que usás en el wizard de upload**, ahora cargando el MP3 fuente via signed R2 URL para playback.

3. **Corregir los timings**
   - **Reproducir el audio** mientras editás (botón play o tecla Space) para escuchar y ajustar.
   - **Offset global** (lo más común): si todas las líneas están corridas por el mismo delta, abrir "Mover toda la canción" y usar los presets `±125ms`, `±250ms`, `±500ms` o el slider de `±1000ms`. El cambio se aplica a todo el timeline.
   - **Sync mode (tap-anchor)**: tocá Space mientras reproduce para anclar cada línea al tiempo de playback actual. Útil cuando el desfase es irregular o estirado.
   - **Edición línea-a-línea**: editá `start`/`end` por línea con el clamping a vecinos.
   - El editor parte de `segments_json` (la transcripción original guardada), así que no hay que re-transcribir.

4. **Submit**
   - Click en el botón de re-render.
   - El job pasa a `status=editing` y el progress avanza a `video → short → thumbnail`. Demora ~3 minutos.
   - Si el job está publicado en YouTube, aparece un confirm pidiendo opt-in explícito al "drift" (YouTube quedará desincronizado con el archivo en plataforma).

5. **Re-aprobar**
   - El job termina en `status=pending_review` (workflow estricto UMG).
   - Ir al panel de revisión, reproducir el nuevo cut y aprobar o rechazar.

## Trazabilidad

Cada re-sync deja tres entries en `audit_log`:

| action | escrito por | cuándo |
|--------|-------------|--------|
| `job.edit_request` | API (`main.py`) | al disparar `/edit` — guarda los segments nuevos, `edit_count`, `user_id` del operador. |
| `job.edit_completed` | worker (`pipeline.py`) | al terminar con éxito — guarda duración, versión archivada, lista de archivos actualizados. |
| `job.edit_failed` | worker (`pipeline.py`) | si el render explota — guarda el error. |

Consulta típica para auditar todos los re-sync de un job:

```sql
SELECT created_at, action, detail
FROM audit_log
WHERE detail->>'job_id' = '<job_id>'
  AND action LIKE 'job.edit_%'
ORDER BY created_at;
```

## Versiones anteriores en R2

Antes de sobreescribir un deliverable (`video.mp4`, `short.mp4`, etc.) el sistema copia el archivo previo a `{key}.v{N}` donde `N = edit_count`. Por ejemplo:

```
outputs/<tenant>/<job_id>/lyric_video.mp4       ← versión vigente (post-edit N)
outputs/<tenant>/<job_id>/lyric_video.mp4.v1    ← archivo previo al edit 1 (original)
outputs/<tenant>/<job_id>/lyric_video.mp4.v2    ← archivo previo al edit 2
```

El job persiste el índice en `job.previous_versions` (JSONB):

```json
[
  {
    "version": 1,
    "edit_type": "lyrics",
    "archived_at": "2026-05-14T14:00:00+00:00",
    "keys": {
      "video": "outputs/.../lyric_video.mp4.v1",
      "short": "outputs/.../short.mp4.v1",
      "thumbnail": "outputs/.../thumbnail.jpg.v1"
    }
  }
]
```

**Para restaurar una versión anterior** (operación manual de admin):

1. Identificar el `archived` key desde `job.previous_versions`.
2. Server-side copy de vuelta: `aws s3 cp s3://<bucket>/<archived_key> s3://<bucket>/<current_key>` (o equivalente Cloudflare R2).
3. No hace falta tocar `segments_json` si solo querés volver al video viejo — el archivo bytes ya lleva las lyrics viejas.

## Troubleshooting

| Síntoma | Causa probable | Acción |
|---------|----------------|--------|
| 400 "Lyrics edit requires the job to be done, pending_review, or rejected" | Job en `processing`, `editing`, `error`, `validation_failed`. | Esperar a que termine el render actual o resolver el error antes. |
| 400 "No cached background available" | `bg_r2_key_cached` está vacío (job pre-cache, muy antiguo). | Usar `edit_type=background` para regenerar Veo + cachear. |
| 400 "Maximum edit limit (3) reached" | Job ya tiene 3 ediciones. | Aprobar o rechazar; si hace falta otra corrección, crear un nuevo job. |
| 409 "youtube_already_published" | El job ya está en YouTube. | Confirmar en el diálogo si estás OK con que YouTube quede desincronizado; alternativa: re-subir como video nuevo (flujo aparte). |
| Worker tarda más de 5 min | Render colgado o worker muerto. | Reaper detecta jobs `editing` con `editing_started_at > 30 min` y los marca `error`. Re-intentar entonces. |

## Costo

- **Lyrics edit**: $0 en Veo (background cacheado se reutiliza). Solo CPU del worker (~3 min).
- **Background edit**: ~$0.90 (Veo + content validation). No aplica acá.
- **Typography edit**: $0. Tampoco aplica a este flujo.

## Fuera de scope (no resuelve este flujo)

- **Reemplazo en YouTube**: la API de YouTube no permite reemplazar el archivo de un video subido. Si hace falta corregir un video ya publicado en YouTube se debe subir como video nuevo (operación separada, no automatizada).
- **Re-transcripción**: si el problema es que las palabras transcritas están mal (no los timings), usar el editor línea-a-línea para corregir el texto, o crear un nuevo job.
- **Cambio de fondo aprobado**: si el cliente decide después de aprobar que el background no le gusta, usar `edit_type=background` (cuesta Veo).
