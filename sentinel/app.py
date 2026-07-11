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
    # v1.1: adjuntar la cola de logs de Railway (Worker + api) al contexto —
    # es lo primero que un humano miraría; le ahorra al agente ir a ciegas.
    ctx = _alert_ctx.get(incident_id, inc["title"])
    try:
        import railway_logs
        if railway_logs.enabled():
            ctx = f"{ctx}\n\n{await railway_logs.context_for_investigation()}"
    except Exception:
        pass
    async with _run_sem:
        try:
            res = await agent.investigate(inc, ctx)
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

async def _run_chat_task(instruction: str, chat_id: str,
                         resume_session: str | None = None, deep: bool = False):
    """Tarea libre (/task, /deep o continuación por reply)."""
    import models
    await tg.send(f"🧠 Trabajando{' (modo profundo, varios agentes)' if deep else ''}… "
                  f"[{tg.esc(models.label(models.current(config.CLAUDE_MODEL)))}]",
                  chat_id=chat_id)
    async with _run_sem:
        try:
            res = await agent.run_task(instruction, resume_session=resume_session, deep=deep)
        except Exception as e:
            await tg.send(f"❌ La tarea falló: {tg.esc(str(e)[:300])}", chat_id=chat_id)
            return
    text = (res.get("text") or "(sin salida)")[:3500]
    msg_id = await tg.send(f"🧠 {tg.esc(text)}\n\n<i>Respondé a este mensaje "
                           f"para seguir la conversación.</i>", chat_id=chat_id)
    if msg_id and res.get("session_id"):
        store.map_tg_session(msg_id, res["session_id"])


async def _do_merge(pr_number: int, chat_id: str):
    import github_api
    ok, msg = await github_api.merge_pr(pr_number)
    await tg.send(("✅ " if ok else "❌ ") + tg.esc(msg), chat_id=chat_id)


async def _do_promote(chat_id: str):
    """Crea el PR staging→main y lo mergea al verde. SOLO llega acá tras la
    doble confirmación explícita del operador (su botón es la autorización
    humana a producción que exige la regla de branches)."""
    import github_api
    commits = await github_api.compare("main", config.PR_BASE_BRANCH)
    if not commits:
        await tg.send("staging y main ya están iguales — nada que promover.",
                      chat_id=chat_id)
        return
    num, url = await github_api.create_promotion_pr(
        "[PROD] Promoción staging→main (vía Sentinel, confirmada por operador)",
        "Promoción disparada desde Telegram con doble confirmación explícita.\n\n"
        + "\n".join(f"- {c}" for c in commits[:20]),
    )
    if not num:
        await tg.send(f"❌ No pude crear el PR de promoción: {tg.esc(url)}", chat_id=chat_id)
        return
    await tg.send(f"⏳ PR de promoción #{num} creado ({tg.esc(url)}) — mergeo "
                  f"a PRODUCCIÓN cuando el CI esté verde…", chat_id=chat_id)
    for _ in range(45):
        await asyncio.sleep(40)
        ok, msg = await github_api.merge_to_main(num)
        if ok:
            await tg.send(f"🚀 {tg.esc(msg)}", chat_id=chat_id)
            return
        if "verde" not in msg:  # error distinto a CI-pendiente → abortar
            await tg.send(f"❌ Promoción detenida: {tg.esc(msg)}", chat_id=chat_id)
            return
    await tg.send(f"⏰ El CI del PR #{num} no terminó a tiempo — quedó abierto, "
                  f"reintentá con /promote o mergealo desde GitHub.", chat_id=chat_id)


async def _on_callback(data: str, chat_id: str):
    action, _, raw = data.partition(":")
    if action == "promote_go":
        asyncio.create_task(_do_promote(chat_id))
        return
    if action == "merge_go":
        try:
            asyncio.create_task(_do_merge(int(raw), chat_id))
        except ValueError:
            pass
        return
    if action == "cancel":
        await tg.send("Cancelado.", chat_id=chat_id)
        return
    try:
        incident_id = int(raw)
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
    if t.startswith("/model"):
        import models
        arg = t[len("/model"):].strip()
        if not arg:
            cur = models.current(config.CLAUDE_MODEL)
            await tg.send(f"🧬 Modelo actual: <b>{tg.esc(models.label(cur))}</b>\n"
                          f"{models.options_text()}\nUso: /model opus | sonnet | haiku",
                          chat_id=chat_id)
            return
        resolved = models.set_model(arg)
        await tg.send(f"🧬 Modelo → <b>{tg.esc(models.label(resolved))}</b> "
                      f"(<code>{tg.esc(resolved)}</code>). Aplica desde la próxima tarea/investigación.",
                      chat_id=chat_id)
        return
    if t.startswith("/deep"):
        instr = t[len("/deep"):].strip()
        if not instr:
            await tg.send("Uso: /deep <auditoría grande> — lanza subagentes en paralelo.",
                          chat_id=chat_id)
            return
        asyncio.create_task(_run_chat_task(instr, chat_id, deep=True))
        return
    if t.startswith("/task"):
        instr = t[len("/task"):].strip()
        if not instr:
            await tg.send("Uso: /task <qué querés que investigue/audite/revise>",
                          chat_id=chat_id)
            return
        asyncio.create_task(_run_chat_task(instr, chat_id))
        return
    if t.startswith("/merge"):
        arg = t[len("/merge"):].strip().lstrip("#")
        if not arg.isdigit():
            await tg.send("Uso: /merge <número de PR> (solo PRs con base staging)",
                          chat_id=chat_id)
            return
        import github_api
        pr = await github_api.pr_info(int(arg))
        if not pr:
            await tg.send(f"PR #{arg} no existe.", chat_id=chat_id)
            return
        green, detail = await github_api.checks_state(pr["head"]["sha"])
        await tg.send(
            f"¿Mergear <b>#{arg}</b> — {tg.esc(pr.get('title',''))} → "
            f"<code>{tg.esc(pr.get('base',{}).get('ref','?'))}</code>?\n"
            f"CI: {tg.esc(detail)}",
            buttons=[[{"text": "✅ Confirmar merge", "callback_data": f"merge_go:{arg}"},
                      {"text": "Cancelar", "callback_data": "cancel:0"}]],
            chat_id=chat_id)
        return
    if t.startswith("/promote"):
        import github_api
        commits = await github_api.compare("main", config.PR_BASE_BRANCH)
        if not commits:
            await tg.send("staging y main ya están iguales.", chat_id=chat_id)
            return
        listing = "\n".join(f"• {tg.esc(c[:70])}" for c in commits[:15])
        await tg.send(
            f"⚠️ <b>ESTO VA A PRODUCCIÓN</b> (genly.pro — clientes reales).\n"
            f"Se promueve staging→main con:\n{listing}\n\n"
            f"El merge ocurre solo con CI verde.",
            buttons=[[{"text": "🚀 SÍ, A PRODUCCIÓN", "callback_data": "promote_go:0"},
                      {"text": "Cancelar", "callback_data": "cancel:0"}]],
            chat_id=chat_id)
        return
    if t.startswith("/logs"):
        import railway_logs
        svc = (t[len("/logs"):].strip() or "Worker")
        out = await railway_logs.tail(svc, 50)
        await tg.send(f"📜 <b>{tg.esc(svc)}</b>\n<pre>{tg.esc(out[-3000:])}</pre>",
                      chat_id=chat_id)
        return
    if t.startswith("/help") or t.startswith("/start"):
        await tg.send(
            "Soy el Sentinel de Genly — tu terminal de guardia.\n\n"
            "🚨 Automático: alertas de Sentry → investigo → te propongo PR a staging.\n\n"
            "/task <pedido> — investigá/auditá/revisá algo (conversación continuable por reply)\n"
            "/deep <pedido> — auditoría grande: lanza varios subagentes en paralelo\n"
            "/model [opus|sonnet|haiku] — ver o cambiar el modelo en caliente\n"
            "/merge <PR> — mergear un PR a staging (doble confirmación, CI verde)\n"
            "/promote — promover staging→main (PRODUCCIÓN, doble confirmación)\n"
            "/logs [servicio] — cola de logs de Railway (Worker, api, ShortWorker…)\n"
            "/incidents — últimos incidentes\n\n"
            "Reply a un diagnóstico = instrucciones para el fix.\n"
            "Reply a una respuesta de /task = seguir esa conversación.",
            chat_id=chat_id,
        )
        return
    if reply_to:
        session = store.session_for_tg_message(reply_to)
        if session:
            asyncio.create_task(_run_chat_task(t, chat_id, resume_session=session))
            return
        incident_id = store.incident_for_tg_message(reply_to)
        if incident_id:
            store.append_operator_note(incident_id, t)
            await tg.send(f"📝 Nota agregada al #{incident_id}. Se usará al implementar.",
                          chat_id=chat_id)
            return
    # Texto libre sin reply = tarea nueva (equivale a /task).
    if len(t) > 12 and not t.startswith("/"):
        asyncio.create_task(_run_chat_task(t, chat_id))
        return
    await tg.send("No entendí. /help para ver comandos.", chat_id=chat_id)


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
    store.init_sessions()
    store.init_settings()
    asyncio.create_task(tg.poll_updates(_on_callback, _on_text))
    logger.info("sentinel arriba — auto_investigate=%s base=%s",
                config.AUTO_INVESTIGATE, config.PR_BASE_BRANCH)
