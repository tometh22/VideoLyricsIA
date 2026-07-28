"""High-frequency uptime ping — alerts when prod is unreachable.

Runs every ~5 min from .github/workflows/uptime.yml (vs daily_smoke which
runs once a day). Deliberately lightweight: it ONLY checks that the public
prod /health/deploy endpoint answers HTTP 200 (rollout-safe — see _probe).
No DB scan, no Veo, no heavy deps — so it keeps working even when Railway's
DB or workers are sick, and it runs on GitHub's infra (not Railway, not the
operator's laptop), so it survives the exact outages it's meant to catch.

Uses /health/deploy (not strict /health) on purpose: strict /health 503s
during every rolling deploy, and since this ping is part of the commit's
check suite that Railway's "Wait for CI" gates on, a false red here BLOCKS
prod deploys (deadlock, audit 2026-07-28). /health/deploy stays green during
rollouts and only 503s on a real DB/Redis outage.

Why this exists: 2026-05-20 Railway had two edge outages. The operator
found out both times because the UMG contact (Santi) messaged "no funciona
la web". The goal here is to flip that — get pinged in ~1 min so the
operator gets ahead of the client.

Transient-blip guard: a single failed request during a rolling deploy must
NOT page anyone. We require 3 consecutive failures (~20 s apart) before
declaring an outage.

On confirmed outage: emails ALERT_EMAIL via Resend (same channel as
daily_smoke) and exits non-zero so the GitHub Action also goes red (which
emails the repo owner as a backup channel even if Resend isn't configured).

Env vars:
  PRODUCTION_API_URL   base URL (default https://genly-ai.up.railway.app)
  RESEND_API_KEY       outbound email (optional — falls back to red CI run)
  RESEND_FROM          verified sender (default noreply@genly.pro)
  ALERT_EMAIL          recipient
"""

from __future__ import annotations

import json
import os
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone

_ATTEMPTS = 3
_GAP_S = 10
_TIMEOUT_S = 8
# Tope de lectura del cuerpo de /health. Es una guarda contra una respuesta
# anómala, NO un presupuesto: tiene que quedar MUY por encima del payload real
# (~3,7 KB al 2026-07-25) para que nunca vuelva a truncar el JSON.
_MAX_BODY_BYTES = 256 * 1024


def _probe(url: str) -> tuple[bool, str]:
    """One deploy-gate health probe. Returns (ok, detail).

    Hits /health/deploy, NOT /health. `/health` is strict fleet-coherence: it
    returns 503 `status:down` during ANY rolling deploy (the new API seats
    before every worker advertises the new release SHA). `/health/deploy`
    reports that expected mismatch as `status:degraded` with HTTP 200, and only
    503s on a REAL critical-dependency outage (DB/Redis down). We use the HTTP
    code as the up/down signal: 200 (ok OR degraded) = users are served = UP;
    503 / unreachable = DOWN.

    Why (audit 2026-07-28): pinging strict /health DEADLOCKED prod deploys.
    Railway's "Wait for CI" on api/Worker waits for the whole check suite,
    which includes this ping. A partial deploy → strict /health 503 → ping red
    → Railway skips api/Worker → the fleet never converges → /health stays 503
    → ping stays red. Self-reinforcing. Pinging /health/deploy stays green
    through the rollout, so the fleet converges and the loop can't form. (This
    is the fundamental version of the 2026-07-25 truncated-body false positive
    that first surfaced the same Railway-skip symptom.)
    """
    try:
        req = urllib.request.Request(f"{url}/health/deploy", method="GET")
        with urllib.request.urlopen(req, timeout=_TIMEOUT_S) as resp:
            code = resp.getcode()
            # Leer el cuerpo COMPLETO (tope generoso solo como guarda
            # anti-respuesta-gigante) — evita el falso "prod caído" por JSON
            # truncado del 2026-07-25.
            body = resp.read(_MAX_BODY_BYTES).decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        # 503 = outage real (DB/Redis). Surfaceamos el cuerpo para la alerta.
        detail = ""
        try:
            detail = e.read(512).decode("utf-8", "replace")
        except Exception:
            pass
        return False, f"HTTP {e.code} {detail}".strip()
    except Exception as e:  # urllib timeout, connection refused, DNS, etc.
        return False, f"{type(e).__name__}: {e}"
    if code != 200:
        return False, f"HTTP {code}"
    # Un /health/deploy sano SIEMPRE devuelve JSON; un 200 con cuerpo no-JSON
    # (ej. HTML de un proxy interpuesto) es sospechoso → caída.
    try:
        status = json.loads(body).get("status")
    except ValueError:
        return False, f"200 but non-JSON body: {body[:120]}"
    # 200 + JSON = prod atiende. Aceptamos `ok` Y `degraded` (mismatch de
    # rollout, disco bajo, etc.) como UP — el gate es rollout-safe. Solo
    # 503/unreachable (arriba) pagina.
    return True, f"200 status={status}"


def main() -> int:
    url = (os.environ.get("PRODUCTION_API_URL") or "https://genly-ai.up.railway.app").rstrip("/")
    ts = datetime.now(timezone.utc).isoformat(timespec="seconds")

    last_detail = ""
    for i in range(1, _ATTEMPTS + 1):
        ok, detail = _probe(url)
        last_detail = detail
        print(f"[uptime] {ts} attempt {i}/{_ATTEMPTS}: {'OK' if ok else 'FAIL'} ({detail})")
        if ok:
            return 0  # any single success = up; no alert.
        if i < _ATTEMPTS:
            time.sleep(_GAP_S)

    # All attempts failed → confirmed outage.
    print(f"[uptime] CONFIRMED DOWN after {_ATTEMPTS} attempts: {last_detail}")
    _send_alert(url, last_detail, ts)
    return 1


def _send_alert(url: str, detail: str, ts: str) -> None:
    api_key = os.environ.get("RESEND_API_KEY", "").strip()
    to = os.environ.get("ALERT_EMAIL", "").strip()
    sender = os.environ.get("RESEND_FROM", "noreply@genly.pro").strip()
    if not (api_key and to):
        print("[uptime] alert email NOT sent (missing RESEND_API_KEY/ALERT_EMAIL) — "
              "relying on the red GitHub Action to notify the repo owner.")
        return

    html = (
        f"<div style='font-family:system-ui;color:#1a1a1a;max-width:600px'>"
        f"<h2>\U0001F6A8 GenLy prod no responde</h2>"
        f"<p>El endpoint público de salud falló <strong>{_ATTEMPTS} veces seguidas</strong> "
        f"(~{_ATTEMPTS * _GAP_S}s) a las {ts}.</p>"
        f"<p><strong>URL:</strong> <code>{url}/health</code><br>"
        f"<strong>Detalle:</strong> <code>{detail}</code></p>"
        f"<p>Esto suele ser un evento de infraestructura (Railway). "
        f"Pasos: 1) confirmá en railway.com / status. 2) si es Railway, "
        f"avisá proactivamente al cliente (ver docs/CLIENT_COMMS_OUTAGE.md, "
        f"Plantilla A). 3) los jobs en curso los recupera el reaper.</p>"
        f"<p style='color:#888;font-size:12px;margin-top:24px'>"
        f"Monitor cada 5 min — .github/workflows/uptime.yml</p>"
        f"</div>"
    )

    import urllib.request as _u
    try:
        req = _u.Request(
            "https://api.resend.com/emails",
            data=json.dumps({
                "from": sender,
                "to": [to],
                "subject": "\U0001F6A8 GenLy prod caído — el sitio no responde",
                "html": html,
            }).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        with _u.urlopen(req, timeout=15) as resp:
            rid = json.loads(resp.read().decode("utf-8", "replace")).get("id")
        print(f"[uptime] alert delivered: {rid}")
    except urllib.error.HTTPError as e:
        # Surface Resend's body — a 403 usually means RESEND_FROM's domain
        # isn't verified, or the API key lacks send permission. Without the
        # body it's undebuggable (audit 2026-07-28: silent 403).
        body = ""
        try:
            body = e.read(512).decode("utf-8", "replace")
        except Exception:
            pass
        print(f"[uptime] resend failed: HTTP {e.code} from={sender}: {body}", file=sys.stderr)
    except Exception as e:
        print(f"[uptime] resend failed: {type(e).__name__}: {e}", file=sys.stderr)


if __name__ == "__main__":
    sys.exit(main())
