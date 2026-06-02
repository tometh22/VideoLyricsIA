"""Email notification system for GenLy AI."""

import html
import logging
import os
import re
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.utils import formatdate, make_msgid, parseaddr
from typing import Optional

logger = logging.getLogger("genly.email")

# --- Configuration ---
SMTP_HOST = os.environ.get("SMTP_HOST", "")
SMTP_PORT = int(os.environ.get("SMTP_PORT", "587"))
SMTP_USER = os.environ.get("SMTP_USER", "")
SMTP_PASSWORD = os.environ.get("SMTP_PASSWORD", "")
# Default From uses genly.pro — the Google Workspace domain. (genly.ai is
# NOT a mailbox domain; a From there fails DKIM/DMARC alignment → spam.)
SMTP_FROM = os.environ.get("SMTP_FROM", "GenLy AI <noreply@genly.pro>")
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


def _html_to_text(html_body: str) -> str:
    """Derive a plain-text version of an HTML email body.

    Spam filters penalize HTML-only messages (no text/plain alternative),
    so every outbound email carries both parts. This doesn't need to be
    pretty — just a readable fallback with the links preserved.
    """
    text = html_body
    # Drop non-content blocks entirely.
    text = re.sub(r"(?is)<(head|style|script)\b.*?</\1>", "", text)
    # Keep link targets: <a href="url">label</a> → "label: url"
    text = re.sub(r'(?is)<a\b[^>]*href="([^"]+)"[^>]*>(.*?)</a>', r"\2: \1", text)
    # Block-level closers and <br> become newlines.
    text = re.sub(r"(?i)<br\s*/?>", "\n", text)
    text = re.sub(r"(?i)</(p|h1|h2|h3|div|tr)>", "\n", text)
    # Strip every remaining tag, then unescape entities.
    text = re.sub(r"(?s)<[^>]+>", "", text)
    text = html.unescape(text)
    # Collapse whitespace: trim lines, drop runs of blank lines.
    lines = [ln.strip() for ln in text.splitlines()]
    text = "\n".join(ln for ln in lines if ln)
    return text


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
    # Date + Message-ID are required by RFC 5322; messages missing them are
    # a classic spam signal. Message-ID uses the From domain so it aligns
    # with the sending identity.
    from_domain = parseaddr(SMTP_FROM)[1].partition("@")[2] or None
    msg["Date"] = formatdate(localtime=True)
    msg["Message-ID"] = make_msgid(domain=from_domain)
    # text/plain first, text/html last: clients render the last part they
    # support, and spam filters expect a plain-text alternative to exist.
    msg.attach(MIMEText(_html_to_text(html_body), "plain"))
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


def send_job_completed(email: str, username: str, artist: str, filename: str, job_id: str):
    """Notify user that a video has been generated."""
    song = filename.replace(".mp3", "")
    url = f"{FRONTEND_URL}/?view=detail&job={job_id}"
    content = f"""
    <h2 style="color:#fff;margin:0 0 16px;">Video ready</h2>
    <p>Hi <strong>{username}</strong>,</p>
    <p>Your lyric video for <strong>{artist} — {song}</strong> is ready to download or publish.</p>
    {_button(url, "View Video")}
    """
    _send_email(email, f"Video ready: {artist} — {song}", _wrap_template(content))


def send_usage_alert(email: str, username: str, percent: int, used: int, limit: int, plan: str):
    """Send usage alert at 80% or 100%."""
    if percent >= 100:
        subject = f"Plan limit reached — {used}/{limit} videos"
        heading = "You've reached your plan limit"
        message = (
            f"You've used <strong>{used}</strong> of your <strong>{limit}</strong> videos "
            f"this month on Plan {plan}. Additional videos will incur overage charges (+30%)."
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


def send_payment_failed(email: str, username: str, amount: float, currency: str):
    """Notify user that their payment failed and action is required."""
    content = f"""
    <h2 style="color:#ef4444;margin:0 0 16px;">Payment failed</h2>
    <p>Hi <strong>{username}</strong>,</p>
    <p>We were unable to process your payment of
    <strong>${amount:.2f} {currency.upper()}</strong>.</p>
    <p>Please update your payment method to keep your subscription active and avoid
    interruptions to your video generation.</p>
    {_button(FRONTEND_URL + "/?view=settings&tab=facturacion", "Update Payment Method")}
    <p style="color:#888;font-size:13px;">If you have questions, reply to this email.</p>
    """
    _send_email(
        email,
        "Action required: Payment failed — GenLy AI",
        _wrap_template(content),
    )
