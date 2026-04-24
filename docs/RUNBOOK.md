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

RQ tiene `job_timeout=2700` (45 min). Si un job excede eso, RQ lo mata y lo
marca como `failed`. Se puede re-encolar manualmente con
`rq requeue -u $REDIS_URL --all`.

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
