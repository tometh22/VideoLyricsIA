"""Tests del Sentinel — lo crítico: firma del webhook, parseo de los dos
shapes de Sentry, dedupe/cooldown, gate de autorización de Telegram y
guardrails en los prompts (base staging, nunca main)."""

import hashlib
import hmac
import importlib
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(__file__))

os.environ.setdefault("SENTINEL_WORKDIR", tempfile.mkdtemp())
os.environ["SENTINEL_DB"] = os.path.join(tempfile.mkdtemp(), "t.db")
os.environ["TELEGRAM_ALLOWED_CHAT_IDS"] = "111,222"

import config  # noqa: E402
importlib.reload(config)
import sentry_webhook  # noqa: E402
import store  # noqa: E402
import prompts  # noqa: E402
import telegram  # noqa: E402

store.init()


def test_signature_verification():
    secret = "s3cr3t"
    body = b'{"data":{}}'
    good = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    assert sentry_webhook.verify_signature(body, good, secret)
    assert not sentry_webhook.verify_signature(body, "deadbeef", secret)
    assert not sentry_webhook.verify_signature(body, None, secret)
    # sin secret configurado (solo dev) pasa
    assert sentry_webhook.verify_signature(body, None, "")


def test_parse_both_sentry_shapes():
    issue_shape = {"data": {"issue": {
        "id": 42, "title": "CalledProcessError", "culprit": "pipeline.run_edit_pipeline",
        "level": "error", "web_url": "https://sentry.io/x"}}}
    a = sentry_webhook.parse_alert(issue_shape)
    assert a["issue_id"] == "42" and a["culprit"] == "pipeline.run_edit_pipeline"

    event_shape = {"data": {"event": {
        "issue_id": 7, "title": "Boom", "level": "error", "web_url": "u"}}}
    b = sentry_webhook.parse_alert(event_shape)
    assert b["issue_id"] == "7" and b["title"] == "Boom"

    assert sentry_webhook.parse_alert({"data": {}}) is None


def test_dedupe_cooldown():
    iid = store.create_incident("sentry-1", "t", "c", "error", "u")
    hit = store.recent_incident_for_issue("sentry-1", cooldown_hours=6)
    assert hit and hit["id"] == iid
    assert store.recent_incident_for_issue("sentry-OTRO", cooldown_hours=6) is None
    # cooldown de 0 horas = ventana vacía → no dedupea
    assert store.recent_incident_for_issue("sentry-1", cooldown_hours=0) is None


def test_incident_lifecycle_and_notes():
    iid = store.create_incident("sentry-2", "t2", "", "error", "")
    store.update_incident(iid, status="diagnosed", diagnosis="RC")
    store.append_operator_note(iid, "usá el patrón X")
    store.append_operator_note(iid, "y no toques Y")
    inc = store.get_incident(iid)
    assert inc["status"] == "diagnosed"
    assert "patrón X" in inc["operator_note"] and "no toques Y" in inc["operator_note"]

    store.map_tg_message(9001, iid)
    assert store.incident_for_tg_message(9001) == iid
    assert store.incident_for_tg_message(1) is None


def test_telegram_authorization_gate():
    assert telegram._authorized(111)
    assert telegram._authorized("222")
    assert not telegram._authorized(999)


def test_prompt_guardrails_pin_staging_never_main():
    inv = prompts.investigate_prompt("{}", "staging")
    imp = prompts.implement_prompt("diag", "", "sentinel/1-fix", "staging")
    for p in (inv, imp):
        assert "PROHIBIDO" in p and "main" in p
        assert "staging" in p
        assert "NUNCA mergees" in p
    assert "--base staging" in imp
