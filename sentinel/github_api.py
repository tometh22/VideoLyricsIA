"""GitHub REST (httpx) para las operaciones de chat: merge y promote.

Se usa la REST API directa (no `gh`) para no depender de un CLI dentro del
loop async. Guardrails duros acá, no solo en el prompt:
  - merge_pr() se NIEGA si la base del PR no es PR_BASE_BRANCH (staging).
  - create_promotion_pr() es la ÚNICA función que toca main, y el caller
    (app.py) la gatea con doble confirmación explícita del operador — ese
    botón ES la autorización humana que exige la regla de branches.
"""

import logging

import httpx

import config

logger = logging.getLogger("sentinel.github")

_REPO = config.REPO_URL.rstrip("/").removesuffix(".git").split("github.com/")[-1]
_API = f"https://api.github.com/repos/{_REPO}"


def _headers() -> dict:
    return {
        "Authorization": f"Bearer {config.GITHUB_TOKEN}",
        "Accept": "application/vnd.github+json",
    }


async def pr_info(number: int) -> dict | None:
    async with httpx.AsyncClient(timeout=20) as c:
        r = await c.get(f"{_API}/pulls/{number}", headers=_headers())
    return r.json() if r.status_code == 200 else None


async def checks_state(sha: str) -> tuple[bool, str]:
    """(verde, resumen) de los check-runs backend/frontend del commit."""
    async with httpx.AsyncClient(timeout=20) as c:
        r = await c.get(f"{_API}/commits/{sha}/check-runs",
                        headers=_headers(), params={"per_page": 50})
    runs = [x for x in r.json().get("check_runs", [])
            if x["name"] in ("backend", "frontend")]
    if not runs:
        return False, "sin check-runs backend/frontend todavía"
    parts = {x["name"]: (x.get("conclusion") or x.get("status")) for x in runs}
    green = all(v == "success" for v in parts.values())
    return green, ", ".join(f"{k}={v}" for k, v in sorted(parts.items()))


async def merge_pr(number: int, method: str = "squash") -> tuple[bool, str]:
    """Mergea un PR SOLO si su base es PR_BASE_BRANCH y el CI está verde."""
    pr = await pr_info(number)
    if not pr:
        return False, f"PR #{number} no existe"
    if pr.get("state") != "open":
        return False, f"PR #{number} está {pr.get('state')}"
    base = pr.get("base", {}).get("ref")
    if base != config.PR_BASE_BRANCH:
        return False, (f"PR #{number} tiene base '{base}' — /merge solo opera "
                       f"sobre {config.PR_BASE_BRANCH}. Producción va por /promote.")
    green, detail = await checks_state(pr["head"]["sha"])
    if not green:
        return False, f"CI no está verde ({detail})"
    async with httpx.AsyncClient(timeout=30) as c:
        r = await c.put(f"{_API}/pulls/{number}/merge", headers=_headers(),
                        json={"merge_method": method})
    if r.status_code == 200:
        return True, f"PR #{number} mergeado a {base} ({detail})"
    return False, f"merge falló ({r.status_code}): {r.json().get('message','')}"


async def compare(base: str, head: str) -> list[str]:
    """Commits en head que no están en base (títulos)."""
    async with httpx.AsyncClient(timeout=20) as c:
        r = await c.get(f"{_API}/compare/{base}...{head}", headers=_headers())
    if r.status_code != 200:
        return []
    return [c_["commit"]["message"].splitlines()[0]
            for c_ in r.json().get("commits", [])]


async def create_promotion_pr(title: str, body: str) -> tuple[int | None, str]:
    """Crea el PR staging→main. NO lo mergea — eso lo hace el flujo /promote
    tras la confirmación y con CI verde."""
    async with httpx.AsyncClient(timeout=30) as c:
        r = await c.post(f"{_API}/pulls", headers=_headers(), json={
            "title": title, "body": body,
            "head": config.PR_BASE_BRANCH, "base": "main",
        })
    if r.status_code == 201:
        d = r.json()
        return d["number"], d["html_url"]
    return None, f"({r.status_code}) {r.json().get('message','')}"


async def merge_to_main(number: int) -> tuple[bool, str]:
    """Merge del PR de promoción (base main). SOLO llamable desde el flujo
    /promote doblemente confirmado; exige CI verde igual que merge_pr."""
    pr = await pr_info(number)
    if not pr or pr.get("state") != "open":
        return False, f"PR #{number} no está abierto"
    if pr.get("base", {}).get("ref") != "main" or pr.get("head", {}).get("ref") != config.PR_BASE_BRANCH:
        return False, "solo se promueve staging→main"
    green, detail = await checks_state(pr["head"]["sha"])
    if not green:
        return False, f"CI no está verde ({detail})"
    async with httpx.AsyncClient(timeout=30) as c:
        r = await c.put(f"{_API}/pulls/{number}/merge", headers=_headers(),
                        json={"merge_method": "merge"})
    if r.status_code == 200:
        return True, f"PR #{number} MERGEADO A MAIN — producción deployando ({detail})"
    return False, f"merge falló ({r.status_code}): {r.json().get('message','')}"
