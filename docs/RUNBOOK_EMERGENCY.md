# Runbook de emergencia — GenLy AI

## Si la app no responde

1. Verificá: https://genly-ai.up.railway.app/health → debe devolver `{"status": "ok", ...}`.
2. Si no responde:
   - Railway → servicio `api` → tab Deployments → ver si último deploy falló.
   - Logs en vivo → buscar stack trace.
3. Si los logs muestran error en arranque:
   - `git log --oneline -5` (en local)
   - `git revert HEAD --no-edit && git push` → revierte último commit, redeploya.

## Si los videos se quedan trabados en "queued" o "processing"

1. Verificá que el `worker` esté en Deployed (Railway → worker → Deployments).
2. Si es zombie (>1h en mismo step):
   - Railway → worker → Restart → fuerza redeploy
   - Espera 2 min → submit nuevo video → verifica que avanza.
3. Si los zombies se acumulan, marcalos como failed:
   ```sql
   -- via Railway → Postgres → Data tab:
   UPDATE jobs
   SET status='failed', error='zombie cleanup'
   WHERE status='processing'
     AND created_at < NOW() - INTERVAL '1 hour';
   ```

## Si UMG dice "rechacé un video y se contó contra mi cuota"

1. Verificá en `/usage` cuánto figura usado.
2. `SELECT status, COUNT(*) FROM jobs WHERE tenant_id='umg-argentina' GROUP BY status;`
3. Solo videos `status=done` deben contar. Si `rejected` cuenta, hay bug.
4. Mientras lo arreglás, podés ajustar manualmente:
   ```sql
   UPDATE users SET max_videos_per_day = max_videos_per_day + 1 WHERE username='...';
   ```

## Si el cap de 5 simultáneos bloquea cuando NO debería

1. `SELECT status, COUNT(*) FROM jobs WHERE tenant_id='umg-argentina' AND status IN ('queued','processing','pending_review');`
2. Si hay zombies viejos contando, limpialos con la query de arriba.
3. Si necesitás abrir el cap temporalmente, env var en api: `TENANT_BACKLOG_LIMIT=20` y redeployás.

## Si Veo (Vertex AI) tira invalid_scope o rate limit

1. Es Google del lado backend, no nuestro código.
2. Esperá 5 min, suele recuperarse.
3. Si persiste, Railway → api/worker → Variables → verificar
   `GOOGLE_APPLICATION_CREDENTIALS_BASE64` esté presente y válida.

## Rollback rápido

```bash
cd ~/VideoLyricsIA
git log --oneline -10                      # ver últimos commits
git revert <SHA> --no-edit                 # revertir uno específico
# o:
git reset --hard <SHA_BUENO> && git push --force-with-lease   # SOLO en emergencia
```

Railway redeploya en ~2 min. **Nunca usar `--force` sin avisar — riesgo de perder commits.**

## Contactos críticos

- Railway support: https://help.railway.app
- Cloudflare R2: dashboard → ticket
- Resend: dashboard → contacto
- Sentry: dashboard → ver últimos errores

## Lo que NO conviene hacer en emergencia

- Cambiar código en producción sin probar local primero.
- Subir caps al doble sin entender qué los activó.
- Desactivar `REQUIRE_REVIEW` (UMG cobraría por todo).
- Tocar la base sin backup.
