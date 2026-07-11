"""Estado persistente del Sentinel — SQLite en el volumen.

Un incidente = un issue de Sentry que disparó el webhook. Ciclo de vida:
  new → investigating → diagnosed → implementing → pr_open
                      ↘ dismissed            ↘ failed
El dedupe/cooldown vive acá: un mismo sentry_issue_id no re-dispara una
investigación hasta pasadas ISSUE_COOLDOWN_HOURS (re-alertas de Sentry por
el mismo issue escalando no deben quemar corridas del agente).
"""

import sqlite3
import time
from contextlib import contextmanager

import config

_SCHEMA = """
CREATE TABLE IF NOT EXISTS incidents (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  sentry_issue_id TEXT NOT NULL,
  title TEXT NOT NULL,
  culprit TEXT DEFAULT '',
  level TEXT DEFAULT '',
  url TEXT DEFAULT '',
  status TEXT NOT NULL DEFAULT 'new',
  diagnosis TEXT DEFAULT '',
  operator_note TEXT DEFAULT '',
  pr_url TEXT DEFAULT '',
  created_at REAL NOT NULL,
  updated_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_incidents_issue ON incidents(sentry_issue_id, created_at);
-- message_id de Telegram -> incidente, para que una respuesta (reply) a un
-- mensaje del bot se adjunte como instrucción del operador a ese incidente.
CREATE TABLE IF NOT EXISTS tg_messages (
  message_id INTEGER PRIMARY KEY,
  incident_id INTEGER NOT NULL
);
"""


@contextmanager
def _db():
    conn = sqlite3.connect(config.DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init():
    with _db() as db:
        db.executescript(_SCHEMA)


def recent_incident_for_issue(sentry_issue_id: str, cooldown_hours: int) -> dict | None:
    """El incidente más nuevo para este issue dentro de la ventana de cooldown."""
    cutoff = time.time() - cooldown_hours * 3600
    with _db() as db:
        row = db.execute(
            "SELECT * FROM incidents WHERE sentry_issue_id=? AND created_at>=? "
            "ORDER BY created_at DESC LIMIT 1",
            (sentry_issue_id, cutoff),
        ).fetchone()
    return dict(row) if row else None


def create_incident(sentry_issue_id: str, title: str, culprit: str, level: str, url: str) -> int:
    now = time.time()
    with _db() as db:
        cur = db.execute(
            "INSERT INTO incidents (sentry_issue_id, title, culprit, level, url, "
            "status, created_at, updated_at) VALUES (?,?,?,?,?,'new',?,?)",
            (sentry_issue_id, title, culprit, level, url, now, now),
        )
        return cur.lastrowid


def get_incident(incident_id: int) -> dict | None:
    with _db() as db:
        row = db.execute("SELECT * FROM incidents WHERE id=?", (incident_id,)).fetchone()
    return dict(row) if row else None


def update_incident(incident_id: int, **fields):
    if not fields:
        return
    fields["updated_at"] = time.time()
    keys = ", ".join(f"{k}=?" for k in fields)
    with _db() as db:
        db.execute(f"UPDATE incidents SET {keys} WHERE id=?", (*fields.values(), incident_id))


def append_operator_note(incident_id: int, note: str):
    inc = get_incident(incident_id)
    if not inc:
        return
    joined = (inc["operator_note"] + "\n" + note).strip() if inc["operator_note"] else note
    update_incident(incident_id, operator_note=joined)


def runs_today() -> int:
    cutoff = time.time() - 24 * 3600
    with _db() as db:
        row = db.execute(
            "SELECT COUNT(*) c FROM incidents WHERE created_at>=? AND status NOT IN ('new','dismissed')",
            (cutoff,),
        ).fetchone()
    return row["c"]


def list_incidents(limit: int = 10) -> list[dict]:
    with _db() as db:
        rows = db.execute(
            "SELECT * FROM incidents ORDER BY created_at DESC LIMIT ?", (limit,)
        ).fetchall()
    return [dict(r) for r in rows]


def map_tg_message(message_id: int, incident_id: int):
    with _db() as db:
        db.execute(
            "INSERT OR REPLACE INTO tg_messages (message_id, incident_id) VALUES (?,?)",
            (message_id, incident_id),
        )


def incident_for_tg_message(message_id: int) -> int | None:
    with _db() as db:
        row = db.execute(
            "SELECT incident_id FROM tg_messages WHERE message_id=?", (message_id,)
        ).fetchone()
    return row["incident_id"] if row else None


# ─── Sesiones de conversación (/task continuable) — v1.1 ────────────────────

def init_sessions():
    with _db() as db:
        db.executescript("""
        CREATE TABLE IF NOT EXISTS tg_sessions (
          message_id INTEGER PRIMARY KEY,
          session_id TEXT NOT NULL
        );""")


def map_tg_session(message_id: int, session_id: str):
    if not (message_id and session_id):
        return
    with _db() as db:
        db.execute("INSERT OR REPLACE INTO tg_sessions (message_id, session_id) VALUES (?,?)",
                   (message_id, session_id))


def session_for_tg_message(message_id: int) -> str | None:
    with _db() as db:
        row = db.execute("SELECT session_id FROM tg_sessions WHERE message_id=?",
                         (message_id,)).fetchone()
    return row["session_id"] if row else None
