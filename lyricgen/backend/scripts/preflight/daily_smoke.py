"""Daily smoke check — runs the cheap preflight checks and alerts on failure.

Designed to be invoked by a scheduled GitHub Action once per day. Only runs
zero-cost checks (production_health + volume_caps) so it can run unattended
without ever billing Veo. On failure, sends an email via Resend to
ALERT_EMAIL listing every red check with details.

Exits non-zero on failure so GitHub Actions itself also flags the run red.

Required env vars:
  PRODUCTION_API_URL    base URL of the deployed API (default genly-ai.up.railway.app)
  DATABASE_URL          public Postgres URL for the per-user override audit
  RESEND_API_KEY        for outbound email alert
  RESEND_FROM           verified sender (defaults to noreply@genly.pro)
  ALERT_EMAIL           recipient for failure notifications
"""

from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timezone

from ._base import Status, execute
from .check_production_health import ProductionHealthCheck
from .check_volume_caps import VolumeCapsCheck


ICON = {
    Status.PASS: "✅", Status.FAIL: "❌", Status.WARN: "⚠️",
    Status.ERROR: "💥", Status.SKIPPED: "⏭️",
}


def main() -> int:
    api_url = os.environ.get("PRODUCTION_API_URL") or "https://genly-ai.up.railway.app"
    checks = [ProductionHealthCheck(api_url), VolumeCapsCheck()]
    results = [(execute(c), c) for c in checks]

    print(f"[smoke] {datetime.now(timezone.utc).isoformat(timespec='seconds')}")
    for r, _ in results:
        print(f"  {ICON[r.status]} {r.name} ({r.duration_ms} ms): {r.summary}")

    failed = [r for r, _ in results if r.status in (Status.FAIL, Status.ERROR)]
    if not failed:
        print("\n[smoke] all green")
        return 0

    print(f"\n[smoke] {len(failed)} check(s) red — sending alert")
    sent = _send_alert(results, failed)
    if not sent:
        print("[smoke] alert email NOT delivered (missing RESEND_API_KEY/ALERT_EMAIL)")
    return 1


def _send_alert(all_results: list, failed: list) -> bool:
    api_key = os.environ.get("RESEND_API_KEY", "").strip()
    to = os.environ.get("ALERT_EMAIL", "").strip()
    sender = os.environ.get("RESEND_FROM", "noreply@genly.pro").strip()
    if not (api_key and to):
        return False

    rows = []
    for result, _ in all_results:
        rows.append(
            f"<tr>"
            f"<td style='padding:6px 12px'>{ICON[result.status]} <code>{result.name}</code></td>"
            f"<td style='padding:6px 12px'>{result.status.value}</td>"
            f"<td style='padding:6px 12px'>{result.summary}</td>"
            f"</tr>"
        )

    detail_blocks = []
    for r, _ in failed:
        detail_blocks.append(
            f"<h3 style='margin-top:24px'>❌ {r.name}</h3>"
            f"<p>{r.summary}</p>"
            f"<pre style='background:#1a1a1a;color:#eee;padding:12px;border-radius:6px;"
            f"font-size:12px;overflow:auto'>"
            f"{json.dumps(r.details, indent=2, default=str)[:3000]}"
            f"</pre>"
        )

    html = (
        f"<div style='font-family:system-ui;color:#1a1a1a;max-width:680px'>"
        f"<h2>🚨 GenLy daily smoke check failed</h2>"
        f"<p><strong>{len(failed)}</strong> of {len(all_results)} check(s) "
        f"failed at {datetime.now(timezone.utc).isoformat(timespec='seconds')}.</p>"
        f"<table style='border-collapse:collapse;border:1px solid #ddd'>"
        f"<thead><tr style='background:#f5f5f5'>"
        f"<th style='padding:6px 12px;text-align:left'>Check</th>"
        f"<th style='padding:6px 12px;text-align:left'>Status</th>"
        f"<th style='padding:6px 12px;text-align:left'>Summary</th>"
        f"</tr></thead><tbody>{''.join(rows)}</tbody></table>"
        f"{''.join(detail_blocks)}"
        f"<p style='color:#888;font-size:12px;margin-top:32px'>"
        f"Sent by GenLy preflight smoke runner. To silence: disable "
        f"<code>.github/workflows/daily-smoke.yml</code>."
        f"</p>"
        f"</div>"
    )

    import requests
    try:
        r = requests.post(
            "https://api.resend.com/emails",
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            json={
                "from": sender,
                "to": [to],
                "subject": f"🚨 GenLy smoke check failed — {len(failed)} issue(s)",
                "html": html,
            },
            timeout=15,
        )
        r.raise_for_status()
        print(f"[smoke] alert delivered: {r.json().get('id')}")
        return True
    except Exception as e:
        print(f"[smoke] resend failed: {type(e).__name__}: {e}", file=sys.stderr)
        return False


if __name__ == "__main__":
    sys.exit(main())
