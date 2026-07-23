"""Telegram como canal de chat/autorización.

Por qué Telegram: accesible desde cualquier lado (celular incluido), botones
inline nativos para Aprobar/Rechazar, y long-polling — no hay que exponer ni
configurar un webhook público para el bot. La autorización es el allowlist
TELEGRAM_ALLOWED_CHAT_IDS: cualquier update de otro chat se ignora y se loguea.

WhatsApp (Twilio) puede sumarse después detrás de esta misma interfaz
(send/notify + callbacks) — se eligió Telegram primero porque no requiere
aprobación de templates ni cuenta Business.
"""

import asyncio
import html
import json
import logging

import httpx

import config
from logging_utils import redact

logger = logging.getLogger("sentinel.telegram")
# Telegram embeds its bot token in every request URL. Avoid the normal httpx
# INFO request line even when this module is imported outside app.py.
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)


def _api_url(method: str) -> str:
    return f"https://api.telegram.org/bot{config.TELEGRAM_BOT_TOKEN}/{method}"


def _authorized(chat_id) -> bool:
    ok = str(chat_id) in config.TELEGRAM_ALLOWED_CHAT_IDS
    if not ok:
        logger.warning("telegram: chat NO autorizado: %s", chat_id)
    return ok


async def send(text: str, buttons: list[list[dict]] | None = None,
               chat_id: str | None = None) -> int | None:
    """Manda a todos los chats del allowlist (o a uno). Devuelve el message_id
    del último envío (para mapear replies → incidente)."""
    targets = [chat_id] if chat_id else sorted(config.TELEGRAM_ALLOWED_CHAT_IDS)
    if not targets or not config.TELEGRAM_BOT_TOKEN:
        logger.info("telegram deshabilitado — %s", text[:120])
        return None
    payload: dict = {"parse_mode": "HTML", "disable_web_page_preview": True}
    if buttons:
        payload["reply_markup"] = json.dumps({"inline_keyboard": buttons})
    last_id = None
    async with httpx.AsyncClient(timeout=20) as client:
        for t in targets:
            try:
                r = await client.post(_api_url("sendMessage"),
                                      data={**payload, "chat_id": t, "text": text})
                body = r.json()
                if body.get("ok"):
                    last_id = body["result"]["message_id"]
                else:
                    logger.error("telegram sendMessage error: %s", redact(body))
            except Exception as e:  # red caída no debe tirar el servicio
                logger.error("telegram send falló: %s", redact(e))
    return last_id


def esc(s: str) -> str:
    return html.escape(s or "", quote=False)


async def poll_updates(handle_callback, handle_text):
    """Loop de long-polling. `handle_callback(data, chat_id)` para botones,
    `handle_text(text, chat_id, reply_to_message_id)` para mensajes."""
    if not config.TELEGRAM_BOT_TOKEN:
        logger.warning("TELEGRAM_BOT_TOKEN vacío — chat deshabilitado")
        return
    offset = 0
    async with httpx.AsyncClient(timeout=70) as client:
        while True:
            try:
                r = await client.get(_api_url("getUpdates"),
                                     params={"timeout": 50, "offset": offset})
                for upd in r.json().get("result", []):
                    offset = upd["update_id"] + 1
                    cq = upd.get("callback_query")
                    if cq:
                        chat_id = cq["message"]["chat"]["id"]
                        if _authorized(chat_id):
                            await client.post(_api_url("answerCallbackQuery"),
                                              data={"callback_query_id": cq["id"]})
                            await handle_callback(cq.get("data", ""), str(chat_id))
                        continue
                    msg = upd.get("message")
                    if msg and msg.get("text"):
                        chat_id = msg["chat"]["id"]
                        if _authorized(chat_id):
                            reply_to = (msg.get("reply_to_message") or {}).get("message_id")
                            await handle_text(msg["text"], str(chat_id), reply_to)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.error("telegram poll error: %s", redact(e))
                await asyncio.sleep(5)
