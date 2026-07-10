"""Sentinel config — todo por env vars (Railway service variables).

Requeridas:
  ANTHROPIC_API_KEY        para el Claude Code headless que investiga/implementa
  GITHUB_TOKEN             PAT con scope repo (push branch + gh pr create)
  TELEGRAM_BOT_TOKEN       bot de @BotFather
  TELEGRAM_ALLOWED_CHAT_IDS  ids separados por coma — ÚNICO gate de autorización
  SENTRY_CLIENT_SECRET     Client Secret de la Internal Integration de Sentry
                           (firma HMAC del webhook). Vacío = NO verificar (solo dev).
  REPO_URL                 https://github.com/<owner>/<repo>.git
"""

import os


def _int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, "") or default)
    except ValueError:
        return default


ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")
GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN", "")
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_ALLOWED_CHAT_IDS = {
    c.strip() for c in os.environ.get("TELEGRAM_ALLOWED_CHAT_IDS", "").split(",") if c.strip()
}
SENTRY_CLIENT_SECRET = os.environ.get("SENTRY_CLIENT_SECRET", "")

REPO_URL = os.environ.get("REPO_URL", "https://github.com/tometh22/VideoLyricsIA.git")
# Base de TODO PR que abre el agente. La regla del repo (CLAUDE.md) prohíbe
# PRs a main sin autorización humana explícita — el Sentinel la hard-codea.
PR_BASE_BRANCH = os.environ.get("PR_BASE_BRANCH", "staging")

# Dónde vive el clon del repo (Railway volume para sobrevivir deploys).
WORKDIR = os.environ.get("SENTINEL_WORKDIR", "/data")

# ¿Investigar apenas llega la alerta (True) o esperar el botón [Investigar]?
AUTO_INVESTIGATE = os.environ.get("AUTO_INVESTIGATE", "true").lower() == "true"

# Guardrails de gasto/carga.
MAX_CONCURRENT_RUNS = _int("MAX_CONCURRENT_RUNS", 1)
MAX_RUNS_PER_DAY = _int("MAX_RUNS_PER_DAY", 12)
ISSUE_COOLDOWN_HOURS = _int("ISSUE_COOLDOWN_HOURS", 6)
AGENT_TIMEOUT_SECONDS = _int("AGENT_TIMEOUT_SECONDS", 1800)  # 30 min por corrida
AGENT_MAX_TURNS = _int("AGENT_MAX_TURNS", 80)
CLAUDE_MODEL = os.environ.get("SENTINEL_CLAUDE_MODEL", "")  # vacío = default del CLI

DB_PATH = os.environ.get("SENTINEL_DB", os.path.join(WORKDIR, "sentinel.db"))
