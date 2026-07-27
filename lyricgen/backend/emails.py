"""Email notification system for GenLy AI."""

import html
import logging
import os
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from typing import Optional

logger = logging.getLogger("genly.email")

# --- Configuration ---
SMTP_HOST = os.environ.get("SMTP_HOST", "")
SMTP_PORT = int(os.environ.get("SMTP_PORT", "587"))
SMTP_USER = os.environ.get("SMTP_USER", "")
SMTP_PASSWORD = os.environ.get("SMTP_PASSWORD", "")
SMTP_FROM = os.environ.get("SMTP_FROM", "GenLy AI <noreply@genly.ai>")
SMTP_USE_TLS = os.environ.get("SMTP_USE_TLS", "true").lower() == "true"
FRONTEND_URL = os.environ.get("FRONTEND_URL", "http://localhost:5173")

# Staging guard. On non-prod, redirect every outbound email to a single
# allow-listed inbox so we never accidentally email a real customer from
# tests/dev. Set EMAIL_STAGING_ALLOWLIST to a comma-separated list of
# addresses to also allow (rare). Setting EMAIL_STAGING_REDIRECT empty
# silently drops all mail on non-prod (safest default if no test inbox
# is wired up).
ENVIRONMENT = (os.environ.get("ENVIRONMENT")
               or os.environ.get("ENV")
               or "production").lower().strip()
EMAIL_STAGING_REDIRECT = os.environ.get("EMAIL_STAGING_REDIRECT", "").strip()
EMAIL_STAGING_ALLOWLIST = {
    s.strip().lower() for s in os.environ.get("EMAIL_STAGING_ALLOWLIST", "").split(",")
    if s.strip()
}

_enabled = bool(SMTP_HOST and SMTP_USER)


def _staging_gate(to: str, subject: str) -> Optional[str]:
    """Return the address to actually send to, or None to drop the message.
    Production passes through unchanged.
    """
    if ENVIRONMENT == "production":
        return to
    if to.strip().lower() in EMAIL_STAGING_ALLOWLIST:
        return to
    if EMAIL_STAGING_REDIRECT:
        logger.info(f"[STAGING] Redirecting email to {EMAIL_STAGING_REDIRECT} "
                    f"(originally {to}, subject={subject!r})")
        return EMAIL_STAGING_REDIRECT
    logger.info(f"[STAGING] Dropping email (no redirect configured): "
                f"to={to}, subject={subject!r}")
    return None


def _send_email(to: str, subject: str, html_body: str):
    """Send an email via SMTP. Silently fails if not configured."""
    if not _enabled:
        logger.debug(f"Email not configured — skipping: {subject} → {to}")
        return

    target = _staging_gate(to, subject)
    if target is None:
        return  # dropped by staging guard

    msg = MIMEMultipart("alternative")
    # Stamp the subject so it's obvious in the inbox we're not in prod.
    if ENVIRONMENT != "production":
        subject = f"[{ENVIRONMENT.upper()}] {subject}"
    msg["Subject"] = subject
    msg["From"] = SMTP_FROM
    msg["To"] = target
    msg.attach(MIMEText(html_body, "html"))

    try:
        if SMTP_USE_TLS:
            server = smtplib.SMTP(SMTP_HOST, SMTP_PORT)
            server.starttls()
        else:
            server = smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT)

        server.login(SMTP_USER, SMTP_PASSWORD)
        server.sendmail(SMTP_FROM, target, msg.as_string())
        server.quit()
        logger.info(f"Email sent: {subject} → {target}")
    except Exception as e:
        logger.error(f"Failed to send email: {e}")


# ---------------------------------------------------------------------------
# Base template
# ---------------------------------------------------------------------------

def _wrap_template(content: str) -> str:
    return f"""
    <!DOCTYPE html>
    <html>
    <head>
      <meta charset="utf-8">
      <meta name="viewport" content="width=device-width, initial-scale=1.0">
    </head>
    <body style="margin:0;padding:0;background:#09090f;font-family:'Inter',system-ui,sans-serif;">
      <div style="max-width:560px;margin:0 auto;padding:40px 24px;">
        <!-- Logo -->
        <div style="text-align:center;margin-bottom:32px;">
          <span style="display:inline-block;background:linear-gradient(135deg,#7c5cfc,#a78bfa);
            border-radius:12px;padding:10px 16px;color:#fff;font-weight:800;font-size:18px;
            letter-spacing:-0.5px;">GenLy AI</span>
        </div>

        <!-- Content card -->
        <div style="background:#1a1a24;border:1px solid rgba(255,255,255,0.06);
          border-radius:16px;padding:32px;color:#e5e5e5;line-height:1.6;">
          {content}
        </div>

        <!-- Footer -->
        <div style="text-align:center;margin-top:24px;">
          <p style="color:#666;font-size:11px;margin:0;">
            GenLy AI Pro — Plataforma de lyric videos con IA
          </p>
        </div>
      </div>
    </body>
    </html>
    """


def _button(url: str, text: str) -> str:
    return f"""
    <div style="text-align:center;margin:24px 0;">
      <a href="{url}" style="display:inline-block;background:linear-gradient(135deg,#7c5cfc,#a78bfa);
        color:#fff;text-decoration:none;padding:14px 32px;border-radius:12px;font-weight:600;
        font-size:14px;">{text}</a>
    </div>
    """


# ---------------------------------------------------------------------------
# Email types
# ---------------------------------------------------------------------------

def send_lead_notification(name: str, company: str, email: str, volume: str, message: str):
    """Notify the sales inbox of a new lead from the public landing form.

    On non-production the staging gate redirects/drops this like any other
    mail; the lead is still persisted in the DB regardless.
    """
    to = (os.environ.get("SALES_EMAIL")
          or os.environ.get("OWNER_EMAIL")
          or os.environ.get("ALERT_EMAIL")
          or "tomas@epical.digital")
    esc = html.escape
    msg_html = esc(message or "—").replace(chr(10), "<br>")
    content = f"""
    <h2 style="color:#fff;margin:0 0 16px;">Nuevo lead de ventas</h2>
    <p><strong>Nombre:</strong> {esc(name)}</p>
    <p><strong>Sello / empresa:</strong> {esc(company or "—")}</p>
    <p><strong>Email:</strong> {esc(email)}</p>
    <p><strong>Volumen estimado:</strong> {esc(volume or "—")}</p>
    <p style="margin-top:16px;"><strong>Mensaje:</strong><br>{msg_html}</p>
    """
    _send_email(to, "Nuevo lead de ventas — GenLy AI", _wrap_template(content))


def send_umg_change_request_notification(
    artist: str, song: str, comment: str, delivery_id: int, job_id: str,
):
    """Notify ops the instant UMG submits a change request on the deliveries
    portal. Real-time counterpart to the "Cambios de UMG" admin panel — the
    panel requires opening the admin, this lands in the inbox so a pending
    request never sits unseen (incident 2026-07-24: the panel had been
    removed from the admin and requests piled up silently).
    """
    to = (os.environ.get("ALERT_EMAIL")
          or os.environ.get("OWNER_EMAIL")
          or "tomas@epical.digital")
    esc = html.escape
    comment_html = esc(comment or "—").replace(chr(10), "<br>")
    content = f"""
    <h2 style="color:#fff;margin:0 0 16px;">UMG pidió un cambio</h2>
    <p><strong>Artista:</strong> {esc(artist or "—")}</p>
    <p><strong>Canción:</strong> {esc(song or "—")}</p>
    <p><strong>Job:</strong> {esc(job_id or "—")} · <strong>Delivery:</strong> #{delivery_id}</p>
    <p style="margin-top:16px;"><strong>Pedido:</strong><br>{comment_html}</p>
    <p style="margin-top:16px;color:#888;font-size:13px;">
      Resolvelo desde Admin → Operación → Cambios de UMG.
    </p>
    """
    _send_email(to, f"UMG pidió un cambio — {artist or 'sin artista'}", _wrap_template(content))


def send_welcome(email: str, username: str):
    """Send welcome email after registration."""
    content = f"""
    <h2 style="color:#fff;margin:0 0 16px;">Welcome to GenLy AI</h2>
    <p>Hi <strong>{username}</strong>,</p>
    <p>Your account is ready. Start creating lyric videos in minutes — upload MP3s,
    review AI-transcribed lyrics, and publish directly to YouTube.</p>
    {_button(FRONTEND_URL, "Go to Dashboard")}
    <p style="color:#888;font-size:13px;">If you have any questions, reply to this email.</p>
    """
    _send_email(email, "Welcome to GenLy AI", _wrap_template(content))


def send_email_verification(email: str, username: str, token: str):
    """Send email verification link."""
    url = f"{FRONTEND_URL}/?verify_email={token}"
    content = f"""
    <h2 style="color:#fff;margin:0 0 16px;">Verify your email</h2>
    <p>Hi <strong>{username}</strong>,</p>
    <p>Click the button below to verify your email address:</p>
    {_button(url, "Verify Email")}
    <p style="color:#888;font-size:13px;">This link expires in 48 hours. If you didn't create
    an account, you can safely ignore this email.</p>
    """
    _send_email(email, "Verify your email — GenLy AI", _wrap_template(content))


def send_password_reset(email: str, username: str, token: str):
    """Send password reset link."""
    url = f"{FRONTEND_URL}/?reset_password={token}"
    content = f"""
    <h2 style="color:#fff;margin:0 0 16px;">Reset your password</h2>
    <p>Hi <strong>{username}</strong>,</p>
    <p>We received a request to reset your password. Click the button below:</p>
    {_button(url, "Reset Password")}
    <p style="color:#888;font-size:13px;">This link expires in 2 hours. If you didn't request
    a password reset, you can safely ignore this email.</p>
    """
    _send_email(email, "Password reset — GenLy AI", _wrap_template(content))


def send_job_completed(email: str, username: str, artist: str, filename: str, job_id: str,
                       needs_review: bool = False):
    """Notify user that a video has been generated.

    Con REQUIRE_REVIEW el render termina en pending_review y el video es
    invisible hasta que el operador lo apruebe — el asunto/CTA tienen que
    decir "revisar", no "listo". En español porque los operadores (UMG
    AR/CL) trabajan la app en español; el resto de los emails migra después.
    """
    song = filename.replace(".mp3", "")
    url = f"{FRONTEND_URL}/?view=detail&job={job_id}"
    if needs_review:
        content = f"""
        <h2 style="color:#fff;margin:0 0 16px;">Tu video está listo para revisar</h2>
        <p>Hola <strong>{username}</strong>,</p>
        <p>El video de <strong>{artist} — {song}</strong> ya se generó y está esperando
        tu revisión. Entrá para verlo, ajustar lo que haga falta y aprobarlo.</p>
        {_button(url, "Revisar y aprobar")}
        """
        _send_email(email, f"Listo para revisar: {artist} — {song}", _wrap_template(content))
        return
    content = f"""
    <h2 style="color:#fff;margin:0 0 16px;">Video ready</h2>
    <p>Hi <strong>{username}</strong>,</p>
    <p>Your lyric video for <strong>{artist} — {song}</strong> is ready to download or publish.</p>
    {_button(url, "View Video")}
    """
    _send_email(email, f"Video ready: {artist} — {song}", _wrap_template(content))


def send_review_reminder(email: str, username: str, artist: str, song: str,
                         job_id: str, days_waiting: int):
    """Remind the owner that a finished video is still sitting in review.

    Caso real (2026-06-25): el primer video de una operadora de UMG Chile
    quedó 8 días en pending_review sin que nadie lo viera — para ella la
    app "no funcionó". Se envía una sola vez por job (dedupe por AuditLog
    en reaper.remind_stale_pending_review)."""
    url = f"{FRONTEND_URL}/?view=detail&job={job_id}"
    dias = f"{days_waiting} día" + ("s" if days_waiting != 1 else "")
    content = f"""
    <h2 style="color:#fff;margin:0 0 16px;">Tenés un video esperando revisión</h2>
    <p>Hola <strong>{username}</strong>,</p>
    <p>El video de <strong>{artist} — {song}</strong> está terminado y espera tu
    aprobación hace <strong>{dias}</strong>. Hasta que lo revises no se puede
    descargar ni publicar.</p>
    {_button(url, "Revisar ahora")}
    """
    _send_email(email, f"Video esperando revisión: {artist} — {song}", _wrap_template(content))


def send_usage_alert(email: str, username: str, percent: int, used: int, limit: int, plan: str):
    """Send usage alert at 80% or 100%."""
    if percent >= 100:
        # Overage price is per-plan (e.g. Plan 250 = $15/video), not a flat
        # +30%. Render the real per-video cost from the PLANS source of truth.
        from auth import PLANS  # local import avoids a circular dependency
        _plan = PLANS.get(plan, {})
        _overage = _plan.get("price_per_video", 0) * _plan.get("overage_rate", 1)
        _overage_note = (
            f"Additional videos are billed at <strong>${_overage:g}/video</strong>."
            if _overage else "Additional videos will incur overage charges."
        )
        subject = f"Plan limit reached — {used}/{limit} videos"
        heading = "You've reached your plan limit"
        message = (
            f"You've used <strong>{used}</strong> of your <strong>{limit}</strong> videos "
            f"this month on Plan {plan}. {_overage_note}"
        )
    else:
        subject = f"Usage alert — {percent}% of plan used"
        heading = f"{percent}% of your plan used"
        message = (
            f"You've used <strong>{used}</strong> of your <strong>{limit}</strong> videos "
            f"this month on Plan {plan}. Consider upgrading to avoid overage charges."
        )

    content = f"""
    <h2 style="color:#fff;margin:0 0 16px;">{heading}</h2>
    <p>Hi <strong>{username}</strong>,</p>
    <p>{message}</p>
    {_button(FRONTEND_URL + "/?view=settings", "Manage Plan")}
    """
    _send_email(email, subject, _wrap_template(content))


def send_invoice_paid(email: str, username: str, amount: float, currency: str, invoice_url: str):
    """Notify user of successful payment."""
    content = f"""
    <h2 style="color:#fff;margin:0 0 16px;">Payment received</h2>
    <p>Hi <strong>{username}</strong>,</p>
    <p>We've received your payment of <strong>${amount:.2f} {currency.upper()}</strong>.</p>
    {_button(invoice_url, "View Invoice") if invoice_url else ""}
    <p style="color:#888;font-size:13px;">Thank you for using GenLy AI.</p>
    """
    _send_email(email, f"Payment received — ${amount:.2f}", _wrap_template(content))


def send_subscription_welcome(email: str, username: str, plan: str, limit: int,
                              monthly_price_usd: float = 0, manage_url: str = ""):
    """One-time welcome after the FIRST paid invoice of a new subscription.

    Triggered from _handle_invoice_paid only when
    billing_reason=='subscription_create', so recurring monthly invoices keep
    producing the receipt (send_invoice_paid), never a second welcome."""
    manage_url = manage_url or (FRONTEND_URL + "/?view=settings&tab=facturacion")
    price_line = f"<strong>${monthly_price_usd:,.0f}/mo</strong> " if monthly_price_usd else ""
    content = f"""
    <h2 style="color:#fff;margin:0 0 16px;">Welcome to Plan {plan}</h2>
    <p>Hi <strong>{username}</strong>,</p>
    <p>Your subscription is active. You're on <strong>Plan {plan}</strong> {price_line}with
    <strong>{limit}</strong> videos included per month.</p>
    <ul style="color:#cfcfcf;font-size:14px;padding-left:18px;margin:8px 0 4px;">
      <li>AI transcription + karaoke timing</li>
      <li>HD lyric video render &amp; YouTube publish</li>
      <li>Background library + AI backgrounds</li>
    </ul>
    {_button(FRONTEND_URL, "Go to Dashboard")}
    <p style="color:#888;font-size:13px;">Manage your plan, payment method, and invoices anytime in
    <a href="{manage_url}" style="color:#a78bfa;">Settings &rarr; Billing</a>.</p>
    """
    _send_email(email, f"Welcome to Plan {plan} — GenLy AI", _wrap_template(content))


def send_plan_changed(email: str, username: str, old_plan: str, new_plan: str,
                      new_limit: int, direction: str = "changed"):
    """Confirm an upgrade/downgrade. `direction` in {'upgrade','downgrade',
    'changed'} decides the heading/copy; the caller computes it from the plan
    limits."""
    if direction == "upgrade":
        heading = "Your plan has been upgraded"
        lead = (f"You're now on <strong>Plan {new_plan}</strong> with "
                f"<strong>{new_limit}</strong> videos per month "
                f"(up from Plan {old_plan}). The change is effective immediately and "
                f"prorated on your next invoice.")
    elif direction == "downgrade":
        heading = "Your plan has been updated"
        lead = (f"You've moved to <strong>Plan {new_plan}</strong> "
                f"(<strong>{new_limit}</strong> videos/mo) from Plan {old_plan}. "
                f"Any unused balance is credited on your next invoice.")
    else:
        heading = "Your plan has changed"
        lead = (f"Your subscription is now on <strong>Plan {new_plan}</strong> "
                f"(<strong>{new_limit}</strong> videos/mo).")
    content = f"""
    <h2 style="color:#fff;margin:0 0 16px;">{heading}</h2>
    <p>Hi <strong>{username}</strong>,</p>
    <p>{lead}</p>
    {_button(FRONTEND_URL + "/?view=settings&tab=facturacion", "View Billing")}
    <p style="color:#888;font-size:13px;">Didn't make this change? Reply to this email right away.</p>
    """
    _send_email(email, f"Plan updated: now on Plan {new_plan} — GenLy AI", _wrap_template(content))


def send_subscription_cancelled(email: str, username: str,
                                access_until: str = "", resubscribe_url: str = ""):
    """Confirm cancellation (fired at subscription.deleted). `access_until`
    (human-readable, optional) is the end of the paid period if Stripe
    provides one; otherwise we omit the date line."""
    resubscribe_url = resubscribe_url or (FRONTEND_URL + "/?view=settings")
    access_line = (
        f"<p>You'll keep access until <strong>{access_until}</strong>, after which your "
        f"account moves to the free plan.</p>"
        if access_until else
        "<p>Your account has moved to the free plan.</p>"
    )
    content = f"""
    <h2 style="color:#fff;margin:0 0 16px;">Your subscription was cancelled</h2>
    <p>Hi <strong>{username}</strong>,</p>
    {access_line}
    <p>Changed your mind? You can re-subscribe to any plan at any time.</p>
    {_button(resubscribe_url, "Reactivate Subscription")}
    <p style="color:#888;font-size:13px;">Need a copy of past invoices? Find them in
    <a href="{FRONTEND_URL}/?view=settings&tab=facturacion" style="color:#a78bfa;">Settings &rarr; Billing</a>.</p>
    """
    _send_email(email, "Subscription cancelled — GenLy AI", _wrap_template(content))


def send_cancellation_scheduled(email: str, username: str, access_until: str = ""):
    """Fired when a user schedules a cancellation (cancel_at_period_end) from
    the in-app cancel CTA. Distinct from send_subscription_cancelled, which
    fires later when the subscription actually ends."""
    until = (f"<strong>{access_until}</strong>" if access_until else "the end of your billing period")
    content = f"""
    <h2 style="color:#fff;margin:0 0 16px;">Your subscription is scheduled to cancel</h2>
    <p>Hi <strong>{username}</strong>,</p>
    <p>Your subscription will end on {until}. You keep full access until then —
    nothing changes before that date.</p>
    <p>Changed your mind? You can reactivate any time before it ends and keep
    your current plan with no interruption.</p>
    {_button(FRONTEND_URL + "/?view=settings&tab=facturacion", "Reactivate Subscription")}
    <p style="color:#888;font-size:13px;">If you didn't request this, reply to this email right away.</p>
    """
    _send_email(email, "Subscription scheduled to cancel — GenLy AI", _wrap_template(content))


def send_payment_failed(email: str, username: str, amount: float, currency: str,
                        retry_date: str = ""):
    """Notify user that their payment failed and action is required.

    `retry_date` (optional, human-readable) is Stripe's next automatic retry —
    showing it turns a scary "payment failed" into a calm dunning message so the
    user knows they have time to fix their card before access is affected."""
    retry_line = (
        f'<p>We\'ll automatically retry the charge on <strong>{retry_date}</strong>. '
        f'Update your payment method before then to keep your subscription active.</p>'
        if retry_date else
        '<p>Please update your payment method to keep your subscription active and avoid '
        'interruptions to your video generation.</p>'
    )
    content = f"""
    <h2 style="color:#ef4444;margin:0 0 16px;">Payment failed</h2>
    <p>Hi <strong>{username}</strong>,</p>
    <p>We were unable to process your payment of
    <strong>${amount:.2f} {currency.upper()}</strong>.</p>
    {retry_line}
    {_button(FRONTEND_URL + "/?view=settings&tab=facturacion", "Update Payment Method")}
    <p style="color:#888;font-size:13px;">If you have questions, reply to this email.</p>
    """
    _send_email(
        email,
        "Action required: Payment failed — GenLy AI",
        _wrap_template(content),
    )
