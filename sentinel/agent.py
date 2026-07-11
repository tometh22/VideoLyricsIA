"""Runner del agente: corre Claude Code headless (`claude -p`) sobre un
worktree del repo, en dos fases.

- investigate(): worktree de solo-lectura sobre origin/main (el código que
  CORRE en prod — la alerta vino de ahí). Tools acotadas a lectura.
- implement(): worktree nuevo con branch `sentinel/<id>-<slug>` desde
  origin/{PR_BASE_BRANCH} (la regla del repo: los fixes van a staging),
  tools de edición + bash habilitadas, y `gh` autenticado para abrir el PR.

Guardrails de runtime (además de los del prompt):
- timeout duro por corrida (AGENT_TIMEOUT_SECONDS)
- --max-turns acota el loop del modelo
- el worktree se descarta al final — nada persiste fuera del branch pusheado
"""

import asyncio
import json
import logging
import os
import re
import shutil
import subprocess
import time

import config
import prompts

logger = logging.getLogger("sentinel.agent")

REPO_DIR = os.path.join(config.WORKDIR, "repo")


def _run(cmd: list[str], cwd: str | None = None, timeout: int = 300, env: dict | None = None):
    return subprocess.run(
        cmd, cwd=cwd, timeout=timeout, capture_output=True, text=True,
        env={**os.environ, **(env or {})},
    )


def ensure_repo():
    """Clon (una vez) + fetch. El token va por header para no persistirlo
    en .git/config del volumen."""
    auth = []
    if config.GITHUB_TOKEN:
        b64 = subprocess.run(
            ["python3", "-c",
             "import base64,sys;print(base64.b64encode(('x-access-token:'+sys.argv[1]).encode()).decode())",
             config.GITHUB_TOKEN],
            capture_output=True, text=True,
        ).stdout.strip()
        auth = ["-c", f"http.extraheader=AUTHORIZATION: basic {b64}"]
    if not os.path.isdir(os.path.join(REPO_DIR, ".git")):
        os.makedirs(config.WORKDIR, exist_ok=True)
        r = _run(["git", *auth, "clone", "--filter=blob:none", config.REPO_URL, REPO_DIR],
                 timeout=600)
        if r.returncode != 0:
            raise RuntimeError(f"git clone falló: {r.stderr[-400:]}")
    r = _run(["git", *auth, "fetch", "origin", "main", config.PR_BASE_BRANCH],
             cwd=REPO_DIR, timeout=300)
    if r.returncode != 0:
        raise RuntimeError(f"git fetch falló: {r.stderr[-400:]}")


def _worktree(ref: str, name: str, branch: str | None = None) -> str:
    path = os.path.join(config.WORKDIR, "wt", name)
    shutil.rmtree(path, ignore_errors=True)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    cmd = ["git", "worktree", "add", "--force"]
    if branch:
        cmd += ["-b", branch]
    cmd += [path, ref]
    r = _run(cmd, cwd=REPO_DIR, timeout=120)
    if r.returncode != 0:
        raise RuntimeError(f"worktree falló: {r.stderr[-400:]}")
    return path


def _cleanup_worktree(path: str):
    _run(["git", "worktree", "remove", "--force", path], cwd=REPO_DIR, timeout=60)
    shutil.rmtree(path, ignore_errors=True)


def _claude(prompt: str, cwd: str, allowed_tools: str,
            resume_session: str | None = None) -> dict:
    """Corre `claude -p` headless. Devuelve {'text', 'json', 'session_id'}.

    `resume_session`: retoma una sesión previa del CLI (conversación
    continuada desde Telegram: el operador responde a un mensaje del agente
    y el hilo sigue con TODO el contexto anterior)."""
    cmd = [
        "claude", "-p", prompt,
        "--output-format", "json",
        "--max-turns", str(config.AGENT_MAX_TURNS),
        "--allowedTools", allowed_tools,
        "--permission-mode", "acceptEdits",
    ]
    if resume_session:
        cmd += ["--resume", resume_session]
    if config.CLAUDE_MODEL:
        cmd += ["--model", config.CLAUDE_MODEL]
    env = {
        "ANTHROPIC_API_KEY": config.ANTHROPIC_API_KEY,
        # gh usa GH_TOKEN; git push usa el remote del worktree (mismo header
        # de auth aplicado por url insteadOf abajo en implement()).
        "GH_TOKEN": config.GITHUB_TOKEN,
        "GITHUB_TOKEN": config.GITHUB_TOKEN,
    }
    r = _run(cmd, cwd=cwd, timeout=config.AGENT_TIMEOUT_SECONDS, env=env)
    if r.returncode != 0 and not r.stdout.strip():
        raise RuntimeError(f"claude CLI falló (rc={r.returncode}): {r.stderr[-600:]}")
    session_id = None
    try:
        out = json.loads(r.stdout)
        text = out.get("result") or ""
        session_id = out.get("session_id")
    except (json.JSONDecodeError, AttributeError):
        text = r.stdout
    # El prompt pide que el último mensaje sea SOLO un JSON — lo extraemos
    # con tolerancia (el modelo a veces lo envuelve en ```json fences).
    m = re.search(r"\{.*\}", text, re.DOTALL)
    parsed = None
    if m:
        try:
            parsed = json.loads(m.group(0))
        except json.JSONDecodeError:
            parsed = None
    return {"text": text, "json": parsed, "session_id": session_id}


async def investigate(incident: dict, alert_context: str) -> dict:
    """Fase 1 — solo lectura sobre el código de prod (origin/main)."""
    def _sync():
        ensure_repo()
        wt = _worktree("origin/main", f"inv-{incident['id']}")
        try:
            return _claude(
                prompts.investigate_prompt(alert_context, config.PR_BASE_BRANCH),
                cwd=wt,
                allowed_tools="Read,Grep,Glob,Bash(git log:*),Bash(git show:*),Bash(git grep:*)",
            )
        finally:
            _cleanup_worktree(wt)
    return await asyncio.to_thread(_sync)


def _slug(title: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")
    return s[:40] or "fix"


async def implement(incident: dict) -> dict:
    """Fase 2 — implementa + push + PR a PR_BASE_BRANCH. Solo tras aprobación humana."""
    branch = f"sentinel/{incident['id']}-{_slug(incident['title'])}-{int(time.time()) % 100000}"

    def _sync():
        ensure_repo()
        wt = _worktree(f"origin/{config.PR_BASE_BRANCH}", f"impl-{incident['id']}", branch=branch)
        try:
            # Auth de push scoped al worktree (no persiste en el clon base).
            _run(["git", "config", "user.name", "genly-sentinel"], cwd=wt)
            _run(["git", "config", "user.email", "sentinel@genly.pro"], cwd=wt)
            if config.GITHUB_TOKEN:
                url = config.REPO_URL.replace(
                    "https://", f"https://x-access-token:{config.GITHUB_TOKEN}@"
                )
                _run(["git", "remote", "set-url", "origin", url], cwd=wt)
            return _claude(
                prompts.implement_prompt(
                    incident.get("diagnosis") or "",
                    incident.get("operator_note") or "",
                    branch, config.PR_BASE_BRANCH,
                ),
                cwd=wt,
                allowed_tools=(
                    "Read,Grep,Glob,Edit,Write,"
                    "Bash(git:*),Bash(gh pr create:*),Bash(gh pr view:*),"
                    "Bash(python3:*),Bash(pytest:*),Bash(npm:*),Bash(npx:*),Bash(node:*),"
                    "Bash(ruff:*),Bash(ls:*),Bash(cat:*)"
                ),
            )
        finally:
            _cleanup_worktree(wt)
    return await asyncio.to_thread(_sync)


# ---------------------------------------------------------------------------
# Tareas ad-hoc desde el chat (/task) — v1.1
# ---------------------------------------------------------------------------

_TASKSPACE = None  # worktree persistente para tareas (las sesiones retomadas
                   # necesitan el mismo cwd; se refresca a origin/main por tarea)


def _ensure_taskspace() -> str:
    global _TASKSPACE
    path = os.path.join(config.WORKDIR, "wt", "taskspace")
    if _TASKSPACE and os.path.isdir(path):
        _run(["git", "fetch", "origin", "main", config.PR_BASE_BRANCH],
             cwd=path, timeout=300)
        _run(["git", "reset", "--hard", "origin/main"], cwd=path, timeout=60)
        return path
    ensure_repo()
    shutil.rmtree(path, ignore_errors=True)
    _run(["git", "worktree", "prune"], cwd=REPO_DIR, timeout=60)
    r = _run(["git", "worktree", "add", "--force", "--detach", path, "origin/main"],
             cwd=REPO_DIR, timeout=120)
    if r.returncode != 0:
        raise RuntimeError(f"taskspace falló: {r.stderr[-300:]}")
    _TASKSPACE = path
    return path


async def run_task(instruction: str, resume_session: str | None = None) -> dict:
    """Tarea libre del operador (auditar, revisar, investigar, testear algo
    puntual) — SOLO lectura sobre el código, con conversación continuable
    (resume_session). Nunca edita ni pushea: para eso está el flujo de
    incidentes con aprobación explícita."""
    def _sync():
        ws = _ensure_taskspace()
        return _claude(
            prompts.task_prompt(instruction, config.PR_BASE_BRANCH)
            if not resume_session else instruction,
            cwd=ws,
            allowed_tools=(
                "Read,Grep,Glob,"
                "Bash(git log:*),Bash(git show:*),Bash(git grep:*),Bash(git diff:*),"
                "Bash(ls:*),Bash(cat:*),Bash(python3 -c:*),Bash(gh pr view:*),Bash(gh pr diff:*)"
            ),
            resume_session=resume_session,
        )
    return await asyncio.to_thread(_sync)
