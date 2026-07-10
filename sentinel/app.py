"""Sentinel — agente de guardia para Genly.

Flujo:
  Sentry (Internal Integration webhook) → POST /webhooks/sentry
    → dedupe/cooldown/límite diario
    → aviso por Telegram
    → (AUTO_INVESTIGATE) el agente investiga en un checkout de prod
    → diagnóstico al chat con botones [Abrir PR a staging] [Descartar]
    → SOLO con aprobación humana implementa y abre el PR (base staging,
      hard-codeado — la regla del repo prohíbe PRs a main)
    → link del PR al chat. El merge SIEMPRE lo hace un humano.

Comandos de chat: /incidents, /help. Responder (reply) a un mensaje de
diagnóstico agrega esa instrucción al incidente antes de implementar.
"""

import asyncio
import json
import logging

from fastapi import FastAPI, Request, Response

import agent
import config
import sentry_webhook
import store
import telegram as tg

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(message)s")
logger = logging.getLogger("sentinel")

app = FastAPI(title="genly-sentinel")

_run_sem = asyncio.Semaphore(config.MAX_CONCURRENT_RUNS)
# contexto crudo de la alerta por incidente (en memoria alcanza: si el
# proceso reinicia antes de investigar, la próxima alerta del issue re-crea
# el incidente pasado el cooldown).
_alert_ctx: dict[int, str] = {}


# ---------------------------------------------------------------------------
# Fases del agente
# ---------------------------------------------------------------------------

async def _investigate(incident_id: int):
    inc = store.get_incident(incident_id)
    if not inc:
        return
    store.update_incident(incident_id, status="investigating")
    async with _run_sem:
        try:
            res = await agent.investigate(inc, _alert_ctx.get(incident_id, inc["title"]))
        except Exception as e:
            logger.exception("investigación falló")
            store.update_incident(incident_id, status="failed")
            await tg.send(f"❌ <b>#{incident_id}</b> investigación falló: {tg.esc(str(e)[:300])}")
            return
    d = res.get("json") or {}
    diagnosis = json.dumps(d, ensure_ascii=False, indent=2) if d else res["text"][-1500:]
    store.update_incident(incident_id, status="diagnosed", diagnosis=diagnosis)
    text = (
        f"🔍 <b>Diagnóstico #{incident_id}</b> — {tg.esc(inc['title'])}\n\n"
        f"<b>Causa raíz:</b> {tg.esc(d.get('root_cause', res['text'][:400]))}\n"
        f"<b>Confianza:</b> {tg.esc(d.get('confidence', '?'))} · "
        f"<b>Impacto:</b> {tg.esc(d.get('impact', '?'))}\n"
        f"<b>Fix propuesto:</b> {tg.esc(d.get('proposed_fix', '?'))}\n\n"
        f"Respondé a este mensaje para agregar instrucciones antes del PR."
    )
    msg_id = await tg.send(text, buttons=[[
        {"text": "✅ Abrir PR a staging", "callback_data": f"pr:{incident_id}"},
        {"text": "🗑 Descartar", "callback_data": f"drop:{incident_id}"},
    ]])
    if msg_id:
        store.map_tg_message(msg_id, incident_id)


async def _implement(incident_id: int):
    inc = store.get_incident(incident_id)
    if not inc or inc["status"] not in ("diagnosed", "failed"):
        await tg.send(f"⚠️ #{incident_id} no está en estado 'diagnosed' (está: {inc and inc['status']})")
        return
    store.update_incident(incident_id, status="implementing")
    await tg.send(f"🔧 <b>#{incident_id}</b> implementando el fix y abriendo PR a "
                  f"<code>{config.PR_BASE_BRANCH}</code>…")
    async with _run_sem:
        try:
            res = await agent.implement(inc)
        except Exception as e:
            logger.exception("implementación falló")
            store.update_incident(incident_id, status="failed")
            await tg.send(f"❌ <b>#{incident_id}</b> implementación falló: {tg.esc(str(e)[:300])}")
            return
    d = res.get("json") or {}
    pr_url = d.get("pr_url", "")
    if pr_url:
        store.update_incident(incident_id, status="pr_open", pr_url=pr_url)
        await tg.send(
            f"🚀 <b>#{incident_id}</b> PR abierto (base <code>{config.PR_BASE_BRANCH}</code>):\n"
            f"{tg.esc(pr_url)}\n\n"
            f"<b>Resumen:</b> {tg.esc(d.get('summary', ''))}\n"
            f"El merge lo hacés vos — el Sentinel nunca mergea."
        )
    else:
        store.update_incident(incident_id, status="failed")
        await tg.send(f"❌ <b>#{incident_id}</b> no pudo abrir el PR. "
                      f"Motivo: {tg.esc(d.get('blocked') or res['text'][-400:])}")


# ---------------------------------------------------------------------------
# Chat (botones + texto)
# ---------------------------------------------------------------------------

async def _on_callback(data: str, chat_id: str):
    action, _, raw_id = data.partition(":")
    try:
        incident_id = int(raw_id)
    except ValueError:
        return
    if action == "inv":
        asyncio.create_task(_investigate(incident_id))
    elif action == "pr":
        asyncio.create_task(_implement(incident_id))
    elif action == "drop":
        store.update_incident(incident_id, status="dismissed")
        await tg.send(f"🗑 #{incident_id} descartado.", chat_id=chat_id)


async def _on_text(text: str, chat_id: str, reply_to: int | None):
    t = text.strip()
    if t.startswith("/incidents"):
        rows = store.list_incidents(10)
        if not rows:
            await tg.send("Sin incidentes.", chat_id=chat_id)
            return
        lines = [
            f"#{r['id']} [{r['status']}] {tg.esc(r['title'][:70])}"
            + (f" → {tg.esc(r['pr_url'])}" if r["pr_url"] else "")
            for r in rows
        ]
        await tg.send("\n".join(lines), chat_id=chat_id)
        return
    if t.startswith("/help") or t.startswith("/start"):
        await tg.send(
            "Soy el Sentinel de Genly. Recibo alertas de Sentry, investigo y, "
            "con tu OK, abro PRs a staging (nunca a main, nunca mergeo).\n\n"
            "/incidents — últimos incidentes\n"
            "Respondé (reply) a un diagnóstico para sumar instrucciones al fix.",
            chat_id=chat_id,
        )
        return
    if reply_to:
        incident_id = store.incident_for_tg_message(reply_to)
        if incident_id:
            store.append_operator_note(incident_id, t)
            await tg.send(f"📝 Nota agregada al #{incident_id}. Se usará al implementar.",
                          chat_id=chat_id)
            return
    await tg.send("No entendí. /help para ver comandos; para instruir un fix, "
                  "respondé (reply) al mensaje del diagnóstico.", chat_id=chat_id)


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------

@app.get("/health")
async def health():
    return {
        "ok": True,
        "auto_investigate": config.AUTO_INVESTIGATE,
        "pr_base": config.PR_BASE_BRANCH,
        "runs_today": store.runs_today(),
        "telegram": bool(config.TELEGRAM_BOT_TOKEN and config.TELEGRAM_ALLOWED_CHAT_IDS),
        "sentry_sig": bool(config.SENTRY_CLIENT_SECRET),
    }


@app.post("/webhooks/sentry")
async def sentry_hook(request: Request):
    body = await request.body()
    sig = request.headers.get("sentry-hook-signature")
    if not sentry_webhook.verify_signature(body, sig, config.SENTRY_CLIENT_SECRET):
        return Response(status_code=401)
    try:
        payload = json.loads(body)
    except json.JSONDecodeError:
        return Response(status_code=400)

    alert = sentry_webhook.parse_alert(payload)
    if not alert or not alert["issue_id"]:
        return {"ok": True, "skipped": "payload sin issue"}

    prev = store.recent_incident_for_issue(alert["issue_id"], config.ISSUE_COOLDOWN_HOURS)
    if prev:
        return {"ok": True, "skipped": f"cooldown (incidente #{prev['id']})"}
    if store.runs_today() >= config.MAX_RUNS_PER_DAY:
        await tg.send(f"⚠️ Alerta de Sentry recibida pero se alcanzó el tope diario "
                      f"({config.MAX_RUNS_PER_DAY} corridas): {tg.esc(alert['title'])}\n"
                      f"{tg.esc(alert['url'])}")
        return {"ok": True, "skipped": "tope diario"}

    incident_id = store.create_incident(
        alert["issue_id"], alert["title"], alert["culprit"], alert["level"], alert["url"]
    )
    _alert_ctx[incident_id] = sentry_webhook.compact_context(payload)

    header = (f"🚨 <b>#{incident_id}</b> {tg.esc(alert['level'] or 'error')} en prod\n"
              f"<b>{tg.esc(alert['title'])}</b>\n{tg.esc(alert['culprit'])}\n"
              f"{tg.esc(alert['url'])}")
    if config.AUTO_INVESTIGATE:
        await tg.send(header + "\n\n🔎 Investigando…")
        asyncio.create_task(_investigate(incident_id))
    else:
        msg_id = await tg.send(header, buttons=[[
            {"text": "🔎 Investigar", "callback_data": f"inv:{incident_id}"},
            {"text": "🗑 Ignorar", "callback_data": f"drop:{incident_id}"},
        ]])
        if msg_id:
            store.map_tg_message(msg_id, incident_id)
    return {"ok": True, "incident_id": incident_id}


@app.on_event("startup")
async def startup():
    store.init()
    asyncio.create_task(tg.poll_updates(_on_callback, _on_text))
    logger.info("sentinel arriba — auto_investigate=%s base=%s",
                config.AUTO_INVESTIGATE, config.PR_BASE_BRANCH)
