# genly-sentinel

Agente de guardia: recibe alertas de Sentry de producción, **investiga la causa
raíz con Claude Code headless** sobre el código de prod, te manda el diagnóstico
por **Telegram**, y —solo con tu aprobación por botón— **implementa el fix y abre
un PR a `staging`**. Nunca toca `main`, nunca mergea: eso queda siempre en manos
humanas (regla de `CLAUDE.md`).

Motivación: el incidente UMG del 9-jul se reportó por WhatsApp y se empezó a
investigar 8 horas después. Con el Sentinel, el diagnóstico llega al chat en
minutos y el fix queda a un botón de distancia.

```
Sentry alert ──▶ /webhooks/sentry ──▶ dedupe/cooldown/tope diario
                                        │
                                        ▼
                              Telegram: 🚨 alerta + "investigando…"
                                        │  (claude -p, checkout de origin/main,
                                        ▼   tools de SOLO lectura)
                              Telegram: 🔍 diagnóstico
                                [✅ Abrir PR a staging] [🗑 Descartar]
                                        │  ← reply con texto = instrucciones extra
                                        ▼  (solo si apretás ✅)
                              claude -p en worktree de origin/staging,
                              branch sentinel/<id>-<slug>, tests, push,
                              gh pr create --base staging
                                        │
                                        ▼
                              Telegram: 🚀 link del PR (mergeás vos)
```

## Setup (una vez)

### 1. Bot de Telegram
1. Hablarle a `@BotFather` → `/newbot` → guardar el **token**.
2. Mandarle cualquier mensaje al bot desde tu Telegram.
3. Obtener tu chat id: `curl https://api.telegram.org/bot<TOKEN>/getUpdates`
   → `result[0].message.chat.id`.

### 2. Sentry Internal Integration
En Sentry: **Settings → Developer Settings → New Internal Integration**:
- Webhook URL: `https://<sentinel>.up.railway.app/webhooks/sentry`
- Permisos: Issue & Event **Read**.
- ✅ "Alert Rule Action" habilitado.
- Guardar el **Client Secret** (firma del webhook).

Después, en **Alerts → Create Alert Rule** (proyecto backend, env production):
- WHEN: "A new issue is created" (y otra regla: "seen more than 3 times in 5 minutes")
- THEN: "Send a notification via <tu integración>".

### 3. Servicio en Railway
Nuevo servicio desde este repo, **Root Directory = `sentinel/`** (usa el
Dockerfile). Agregar un **Volume montado en `/data`**. Variables:

| Var | Valor |
|---|---|
| `ANTHROPIC_API_KEY` | key de Anthropic (el agente corre con tu cuenta API) |
| `GITHUB_TOKEN` | PAT fine-grained: repo `VideoLyricsIA`, permisos Contents RW + Pull requests RW |
| `TELEGRAM_BOT_TOKEN` | de BotFather |
| `TELEGRAM_ALLOWED_CHAT_IDS` | tu chat id (coma-separado si son varios) |
| `SENTRY_CLIENT_SECRET` | de la Internal Integration |
| `REPO_URL` | `https://github.com/tometh22/VideoLyricsIA.git` |

Opcionales: `AUTO_INVESTIGATE` (default `true`), `MAX_RUNS_PER_DAY` (12),
`ISSUE_COOLDOWN_HOURS` (6), `AGENT_TIMEOUT_SECONDS` (1800), `AGENT_MAX_TURNS`
(80), `SENTINEL_CLAUDE_MODEL` (vacío = default del CLI), `PR_BASE_BRANCH`
(`staging` — no lo cambies sin leer CLAUDE.md).

### 4. Probar sin esperar un incidente real
```bash
curl -X POST https://<sentinel>.up.railway.app/webhooks/sentry \
  -H 'Content-Type: application/json' \
  -d '{"data":{"issue":{"id":"999","title":"[TEST] ZeroDivisionError in pipeline.run_edit_pipeline",
       "culprit":"pipeline.run_edit_pipeline","level":"error","web_url":"https://sentry.io/test"}}}'
```
(si `SENTRY_CLIENT_SECRET` está seteado, el request sin firma da 401 — para el
smoke test podés quitarlo momentáneamente o firmar el body con HMAC-SHA256).

## Guardrails
- **Autorización**: solo chats en `TELEGRAM_ALLOWED_CHAT_IDS`; todo lo demás se ignora.
- **Prod intocable**: base del PR hard-codeada a `staging`; el prompt además
  lo prohíbe; el agente nunca mergea.
- **Gasto acotado**: 1 corrida concurrente, tope diario, cooldown por issue,
  timeout duro de 30 min y `--max-turns` por corrida.
- **Firma del webhook**: HMAC-SHA256 con el Client Secret de Sentry — sin
  firma válida, 401 (si el secret está configurado).
- **Aislamiento**: cada corrida en un `git worktree` descartable; la fase de
  investigación solo tiene tools de lectura.

## Comandos del chat
- `/incidents` — últimos 10 incidentes con estado y PR.
- `/help` — ayuda.
- **Reply** a un mensaje de diagnóstico → esa instrucción se pasa al agente
  cuando implemente ("usá el patrón X", "no toques Y").

## Roadmap corto
- WhatsApp (Twilio) como segundo canal detrás de la misma interfaz.
- Comando `/approve-merge` con doble confirmación para mergear el PR a staging.
- Adjuntar los últimos logs de Railway al contexto de investigación.

## Tests
```bash
cd sentinel && python3 -m pytest test_sentinel.py -q
```
