"""Email notification system for GenLy AI."""

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

_enabled = bool(SMTP_HOST and SMTP_USER)


def _send_email(to: str, subject: str, html_body: str):
    """Send an email via SMTP. Silently fails if not configured."""
    if not _enabled:
        logger.debug(f"Email not configured — skipping: {subject} → {to}")
        return

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = SMTP_FROM
    msg["To"] = to
    msg.attach(MIMEText(html_body, "html"))

    try:
        if SMTP_USE_TLS:
            server = smtplib.SMTP(SMTP_HOST, SMTP_PORT)
            server.starttls()
        else:
            server = smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT)

        server.login(SMTP_USER, SMTP_PASSWORD)
        server.sendmail(SMTP_FROM, to, msg.as_string())
        server.quit()
        logger.info(f"Email sent: {subject} → {to}")
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
