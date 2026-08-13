"""Modelo activo del Sentinel, cambiable en caliente desde Telegram.

Orden de precedencia:
  1. override en store (settings['claude_model']) — /model desde el chat
  2. SENTINEL_CLAUDE_MODEL (env var de Railway)
  3. default del CLI (si ambos vacíos, no se pasa --model)

Aliases cómodos para el teléfono: 'opus' | 'sonnet' | 'haiku' → id completo.
El operador puede pasar un alias o el id exacto; cualquier string se acepta
(el CLI valida), pero los aliases cubren el 99% del uso "más pro / menos pro".
"""

import store

ALIASES = {
    "opus": "claude-opus-4-8",
    "sonnet": "claude-sonnet-5",
    "haiku": "claude-haiku-4-5-20251001",
    "fable": "claude-fable-5",
    # atajos de "potencia"
    "max": "claude-opus-4-8",
    "pro": "claude-opus-4-8",
    "fast": "claude-haiku-4-5-20251001",
    "mid": "claude-sonnet-5",
}

# Etiqueta legible para el chat.
LABEL = {
    "claude-opus-4-8": "Opus 4.8 (máx capacidad)",
    "claude-sonnet-5": "Sonnet 5 (equilibrado)",
    "claude-haiku-4-5-20251001": "Haiku 4.5 (rápido/barato)",
    "claude-fable-5": "Fable 5",
}


def resolve(name: str) -> str:
    """alias → id; un id/string desconocido se devuelve tal cual."""
    return ALIASES.get(name.strip().lower(), name.strip())


def current(env_default: str = "") -> str:
    """El modelo efectivo: override del store, si no el env default."""
    return store.get_setting("claude_model") or env_default or ""


def set_model(name: str) -> str:
    """Persiste el override. Devuelve el id resuelto."""
    resolved = resolve(name)
    store.set_setting("claude_model", resolved)
    return resolved


def label(model_id: str) -> str:
    return LABEL.get(model_id, model_id or "(default del CLI)")


def options_text() -> str:
    return ("Modelos: <b>opus</b> (Opus 4.8) · <b>sonnet</b> (Sonnet 5) · "
            "<b>haiku</b> (Haiku 4.5) · <b>fable</b> (Fable 5). "
            "También podés pasar un id exacto.")
