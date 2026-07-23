"""Tests del Sentinel — lo crítico: firma del webhook, parseo de los dos
shapes de Sentry, dedupe/cooldown, gate de autorización de Telegram y
guardrails en los prompts (base staging, nunca main)."""

import hashlib
import hmac
import importlib
import io
import logging
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
import logging_utils  # noqa: E402

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


def test_logging_redacts_telegram_urls_and_auth_headers():
    bot_token = "123456789:abcdefghijklmnopqrstuvwxyzABCDE"
    api_token = "railway-super-secret-token"
    raw = (
        f"POST https://api.telegram.org/bot{bot_token}/getUpdates "
        f"Authorization: Bearer {api_token} "
        f"Project-Access-Token={api_token}"
    )
    safe = logging_utils.redact(raw, secrets=(bot_token, api_token))
    assert bot_token not in safe
    assert api_token not in safe
    assert safe.count("[REDACTED]") >= 3


def test_logging_formatter_redacts_exception_traceback(monkeypatch):
    token = "123456789:abcdefghijklmnopqrstuvwxyzABCDE"
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", token)
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(logging_utils.RedactingFormatter("%(message)s"))
    test_logger = logging.getLogger("sentinel.redaction-test")
    test_logger.handlers = [handler]
    test_logger.propagate = False
    test_logger.setLevel(logging.ERROR)
    try:
        raise RuntimeError(f"https://api.telegram.org/bot{token}/getUpdates")
    except RuntimeError:
        test_logger.exception("request failed")
    assert token not in stream.getvalue()
    assert "[REDACTED]" in stream.getvalue()


def test_sentry_context_is_redacted_before_agent_handoff(monkeypatch):
    secret = "railway-context-super-secret"
    monkeypatch.setenv("RAILWAY_API_TOKEN", secret)
    context = sentry_webhook.compact_context({
        "exception": {"value": f"request failed with Bearer {secret}"},
    })
    assert secret not in context
    assert "[REDACTED]" in context


def test_railway_log_tail_redacts_context(monkeypatch):
    import asyncio
    import railway_logs

    secret = "railway-log-super-secret"
    monkeypatch.setenv("RAILWAY_API_TOKEN", secret)
    monkeypatch.setattr(railway_logs, "enabled", lambda: True)

    async def fake_latest(_service):
        return "deployment-1"

    async def fake_gql(_query, _variables):
        return {"data": {"deploymentLogs": [{
            "timestamp": "2026-07-23T10:00:00Z",
            "message": f"Authorization: Bearer {secret}",
        }]}}

    monkeypatch.setattr(railway_logs, "_latest_deployment_id", fake_latest)
    monkeypatch.setattr(railway_logs, "_gql", fake_gql)
    context = asyncio.run(railway_logs.tail("api"))
    assert secret not in context
    assert "[REDACTED]" in context


def test_http_client_info_logging_is_disabled():
    assert logging.getLogger("httpx").getEffectiveLevel() >= logging.WARNING
    assert logging.getLogger("httpcore").getEffectiveLevel() >= logging.WARNING


def test_prompt_guardrails_pin_staging_never_main():
    inv = prompts.investigate_prompt("{}", "staging")
    imp = prompts.implement_prompt("diag", "", "sentinel/1-fix", "staging")
    for p in (inv, imp):
        assert "PROHIBIDO" in p and "main" in p
        assert "staging" in p
        assert "NUNCA mergees" in p
    assert "--base staging" in imp


# ─── v1.1: merge/promote gates, sesiones, task prompt ────────────────────────

def test_merge_pr_refuses_non_staging_base():
    """El guardrail duro de /merge: código, no prompt. Un PR con base main
    debe rechazarse SIEMPRE — producción solo va por /promote (doble confirm)."""
    import inspect
    import github_api
    src = inspect.getsource(github_api.merge_pr)
    assert "config.PR_BASE_BRANCH" in src
    assert "/promote" in src  # el mensaje redirige al flujo correcto
    m2m = inspect.getsource(github_api.merge_to_main)
    # merge_to_main exige exactamente staging→main y CI verde.
    assert '"main"' in m2m and "checks_state" in m2m


def test_promote_requires_double_confirmation():
    """El flujo /promote NUNCA ejecuta directo: manda botón de confirmación
    explícita ('ESTO VA A PRODUCCIÓN') y solo el callback promote_go dispara."""
    import inspect, app
    on_text = inspect.getsource(app._on_text)
    assert "ESTO VA A PRODUCCIÓN" in on_text
    assert "promote_go" in on_text
    # _do_promote solo se lanza desde el callback, no desde /promote directo.
    assert "_do_promote" not in on_text.split("/promote")[1].split("/logs")[0].replace("promote_go", "")
    on_cb = inspect.getsource(app._on_callback)
    assert "promote_go" in on_cb and "_do_promote" in on_cb


def test_task_sessions_roundtrip():
    store.init_sessions()
    store.map_tg_session(555, "sess-abc")
    assert store.session_for_tg_message(555) == "sess-abc"
    assert store.session_for_tg_message(556) is None


def test_task_prompt_is_read_only_and_phone_sized():
    p = prompts.task_prompt("auditá el reaper", "staging")
    assert "SOLO LECTURA" in p
    assert "NO podés editar" in p
    assert "PROHIBIDO" in p  # guardrails de branches presentes también acá


def test_railway_logs_disabled_gracefully(monkeypatch):
    import railway_logs
    monkeypatch.setattr(railway_logs, "_PROJECT_TOKEN", "")
    monkeypatch.setattr(railway_logs, "_API_TOKEN", "")
    assert not railway_logs.enabled()


# ─── v1.2: modelo cambiable + deep tasks ─────────────────────────────────────

def test_model_override_precedence():
    import models
    store.init_settings()
    # alias → id
    assert models.resolve("opus") == "claude-opus-4-8"
    assert models.resolve("haiku") == "claude-haiku-4-5-20251001"
    assert models.resolve("fable") == "claude-fable-5"
    # id desconocido pasa tal cual (el CLI valida)
    assert models.resolve("claude-x-99") == "claude-x-99"
    # override del store gana al env default
    models.set_model("sonnet")
    assert models.current("claude-opus-4-8") == "claude-sonnet-5"
    # sin override, cae al env default
    store.set_setting("claude_model", "")
    assert models.current("claude-opus-4-8") in ("claude-opus-4-8", "")


def test_deep_task_prompt_asks_for_subagents():
    import prompts
    p = prompts.task_prompt("auditá todo el pipeline", "staging", deep=True)
    assert "SUBAGENTES" in p and "Task" in p
    shallow = prompts.task_prompt("mirá esto", "staging", deep=False)
    assert "SUBAGENTES" not in shallow


def test_run_task_deep_enables_task_tool():
    import inspect, agent
    src = inspect.getsource(agent.run_task)
    # deep=True agrega el spawner de subagentes y sube max_turns
    assert '",Task"' in src or '+= ",Task"' in src
    assert "max_turns=200 if deep" in src


def test_readonly_tasks_can_query_github_but_not_mutate():
    """El /task read-only debe poder LISTAR PRs/issues (gh pr list, etc.) y ver
    el remoto (git ls-remote), pero NO mutar (sin gh pr create/merge/comment,
    git push, ni gh api que puede POSTear)."""
    import inspect, agent
    src = inspect.getsource(agent.run_task)
    for read in ("gh pr list:*", "git ls-remote:*", "gh pr checks:*",
                 "gh issue list:*", "gh run view:*"):
        assert read in src, f"falta comando de lectura {read}"
    # NO debe HABILITAR (Bash(...)) ninguna mutación (los nombres pueden
    # aparecer en comentarios; lo que cuenta es que no estén como tool).
    for mutate in ("Bash(gh pr create", "Bash(gh pr merge", "Bash(gh pr comment",
                   "Bash(git push", "Bash(gh api"):
        assert mutate not in src, f"NO debe habilitar mutación: {mutate}"


def test_investigate_can_query_github():
    import inspect, agent
    src = inspect.getsource(agent.investigate)
    assert "gh pr list:*" in src and "gh issue list:*" in src
    # investigación es read-only: sin herramientas de escritura
    assert "Bash(gh pr create" not in src and "Bash(git push" not in src

def test_railway_logs_supports_bearer_api_token(monkeypatch):
    import importlib, railway_logs
    monkeypatch.setenv("RAILWAY_PROJECT_TOKEN", "")
    monkeypatch.setenv("RAILWAY_API_TOKEN", "acct-xyz")
    monkeypatch.setenv("RAILWAY_PROJECT_ID", "p")
    monkeypatch.setenv("RAILWAY_ENVIRONMENT_ID", "e")
    importlib.reload(railway_logs)
    assert railway_logs.enabled()
    assert railway_logs._auth_headers() == {"Authorization": "Bearer acct-xyz"}
    # el project token tiene precedencia si ambos están
    monkeypatch.setenv("RAILWAY_PROJECT_TOKEN", "proj-abc")
    importlib.reload(railway_logs)
    assert railway_logs._auth_headers() == {"Project-Access-Token": "proj-abc"}
