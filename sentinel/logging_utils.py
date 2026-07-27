"""Central logging hardening for the Sentinel process.

HTTP clients may include request URLs and authorization headers in normal or
exception logging. Telegram embeds the bot credential in the URL itself, so
redaction must happen after the full record (including tracebacks) is rendered.
"""

from __future__ import annotations

import logging
import os
import re
from collections.abc import Iterable


_SECRET_ENV_NAMES = (
    "ANTHROPIC_API_KEY",
    "GITHUB_TOKEN",
    "GH_TOKEN",
    "TELEGRAM_BOT_TOKEN",
    "SENTRY_CLIENT_SECRET",
    "RAILWAY_PROJECT_TOKEN",
    "RAILWAY_API_TOKEN",
)

_TOKEN_PATTERNS = (
    # Telegram bot tokens in URLs or exception messages.
    re.compile(r"(?i)(api\.telegram\.org/bot)[^/\s]+"),
    re.compile(r"\b\d{6,}:[A-Za-z0-9_-]{20,}\b"),
    # Common auth header renderings from httpx/httpcore and application logs.
    re.compile(r"(?i)(authorization[\"']?\s*[:=]\s*[\"']?(?:bearer|basic)\s+)[^\s,;\"']+"),
    re.compile(r"(?i)(project-access-token[\"']?\s*[:=]\s*[\"']?)[^\s,;\"']+"),
)


def configured_secrets() -> tuple[str, ...]:
    """Return non-empty credentials known to this process."""
    return tuple(
        value for name in _SECRET_ENV_NAMES
        if (value := os.environ.get(name, ""))
    )


def redact(value, secrets: Iterable[str] | None = None) -> str:
    """Remove known and structurally recognizable secrets from ``value``."""
    text = str(value)
    for secret in secrets if secrets is not None else configured_secrets():
        if secret:
            text = text.replace(secret, "[REDACTED]")
    text = _TOKEN_PATTERNS[0].sub(r"\1[REDACTED]", text)
    text = _TOKEN_PATTERNS[1].sub("[REDACTED]", text)
    text = _TOKEN_PATTERNS[2].sub(r"\1[REDACTED]", text)
    text = _TOKEN_PATTERNS[3].sub(r"\1[REDACTED]", text)
    return text


class SecretRedactingFilter(logging.Filter):
    """Redact message arguments before third-party handlers see a record."""

    def filter(self, record: logging.LogRecord) -> bool:
        record.msg = redact(record.msg)
        if isinstance(record.args, dict):
            record.args = {
                key: redact(value) if isinstance(value, str) else value
                for key, value in record.args.items()
            }
        elif isinstance(record.args, tuple):
            # Preserve numeric/object args so format strings such as ``%d``
            # keep their original semantics. The final formatter redacts the
            # fully rendered line (including object strings and tracebacks).
            record.args = tuple(
                redact(value) if isinstance(value, str) else value
                for value in record.args
            )
        return True


class RedactingFormatter(logging.Formatter):
    """Final defense that also redacts rendered exception tracebacks."""

    def format(self, record: logging.LogRecord) -> str:
        return redact(super().format(record))


def configure_logging() -> None:
    """Install process-wide safe logging before importing HTTP clients."""
    handler = logging.StreamHandler()
    handler.addFilter(SecretRedactingFilter())
    handler.setFormatter(RedactingFormatter("%(asctime)s %(name)s %(message)s"))
    logging.basicConfig(level=logging.INFO, handlers=[handler], force=True)

    # httpx logs complete request URLs at INFO. Telegram's credential is part
    # of that URL; WARNING removes the primary leak, redaction is defense-in-depth.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
