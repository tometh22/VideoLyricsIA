"""Tests for the SMTP email module (deliverability hardening, 2026-06-02).

Customer report: GenLy transactional mail landing in Gmail spam. Code-side
causes were HTML-only bodies (no text/plain alternative) and missing
RFC 5322 Date / Message-ID headers. Pin:
  - every outbound message is multipart/alternative with BOTH a text/plain
    and a text/html part, plain first / html last
  - Date and Message-ID headers are present; Message-ID uses the From domain
  - _html_to_text produces readable text (tags stripped, links preserved,
    entities unescaped)
  - the staging gate still drops / redirects on non-prod

The DNS half of the fix (DKIM + DMARC for genly.ai) lives in
docs/RUNBOOK_EMAIL_DELIVERABILITY.md — not testable from here.
"""
from __future__ import annotations

import email as email_lib
import os
import sys

import pytest

# Bring backend modules into path before any backend import.
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

import emails


class FakeSMTP:
    """Minimal smtplib.SMTP stand-in that records the sent message."""

    sent: list[dict] = []  # class-level so the test can read after send

    def __init__(self, host, port):
        self.host = host
        self.port = port

    def starttls(self):
        pass

    def login(self, user, password):
        pass

    def sendmail(self, from_addr, to_addr, msg_string):
        FakeSMTP.sent.append({
            "from": from_addr,
            "to": to_addr,
            "message": email_lib.message_from_string(msg_string),
        })

    def quit(self):
        pass


@pytest.fixture
def smtp_prod(monkeypatch):
    """Enable email sending against a FakeSMTP, in production mode."""
    FakeSMTP.sent = []
    monkeypatch.setattr(emails.smtplib, "SMTP", FakeSMTP)
    monkeypatch.setattr(emails, "_enabled", True)
    monkeypatch.setattr(emails, "ENVIRONMENT", "production")
    monkeypatch.setattr(emails, "SMTP_HOST", "smtp.example.com")
    monkeypatch.setattr(emails, "SMTP_USER", "noreply@genly.ai")
    monkeypatch.setattr(emails, "SMTP_FROM", "GenLy AI <noreply@genly.ai>")
    monkeypatch.setattr(emails, "SMTP_USE_TLS", True)
    return FakeSMTP


# ---------------------------------------------------------------------------
# _html_to_text
# ---------------------------------------------------------------------------

def test_html_to_text_strips_tags_and_keeps_links():
    html_body = (
        '<html><head><style>p {color: red}</style></head><body>'
        '<h2>Verify your email</h2>'
        '<p>Hi <strong>tom&aacute;s</strong>,</p>'
        '<a href="https://app.genly.ai/?verify=abc">Verify Email</a>'
        '</body></html>'
    )
    text = emails._html_to_text(html_body)

    assert "<" not in text and ">" not in text
    assert "color: red" not in text  # style block dropped
    assert "Verify your email" in text
    assert "tomás" in text  # entity unescaped
    assert "Verify Email: https://app.genly.ai/?verify=abc" in text


def test_html_to_text_collapses_blank_lines():
    text = emails._html_to_text("<p>one</p>\n\n\n   \n<p>two</p>")
    assert text == "one\ntwo"


# ---------------------------------------------------------------------------
# _send_email — MIME structure & headers
# ---------------------------------------------------------------------------

def test_send_email_has_plain_and_html_parts(smtp_prod):
    emails._send_email("user@example.com", "Test subject", "<p>Hello <b>world</b></p>")

    assert len(smtp_prod.sent) == 1
    msg = smtp_prod.sent[0]["message"]
    assert msg.get_content_type() == "multipart/alternative"

    parts = msg.get_payload()
    content_types = [p.get_content_type() for p in parts]
    # plain first, html last (clients prefer the last part they support)
    assert content_types == ["text/plain", "text/html"]
    assert "Hello world" in parts[0].get_payload(decode=True).decode()
    assert "<b>world</b>" in parts[1].get_payload(decode=True).decode()


def test_send_email_has_date_and_aligned_message_id(smtp_prod):
    emails._send_email("user@example.com", "Test subject", "<p>hi</p>")

    msg = smtp_prod.sent[0]["message"]
    assert msg["Date"] is not None
    assert msg["Message-ID"] is not None
    # Message-ID domain aligns with the From domain (genly.ai)
    assert msg["Message-ID"].rstrip(">").endswith("genly.ai")
    assert msg["From"] == "GenLy AI <noreply@genly.ai>"
    assert msg["To"] == "user@example.com"


def test_send_email_noop_when_not_configured(monkeypatch):
    FakeSMTP.sent = []
    monkeypatch.setattr(emails.smtplib, "SMTP", FakeSMTP)
    monkeypatch.setattr(emails, "_enabled", False)
    emails._send_email("user@example.com", "Test", "<p>hi</p>")
    assert FakeSMTP.sent == []


# ---------------------------------------------------------------------------
# Staging gate (unchanged behaviour — pin it so the refactor didn't break it)
# ---------------------------------------------------------------------------

def test_staging_drops_mail_without_redirect(smtp_prod, monkeypatch):
    monkeypatch.setattr(emails, "ENVIRONMENT", "staging")
    monkeypatch.setattr(emails, "EMAIL_STAGING_REDIRECT", "")
    monkeypatch.setattr(emails, "EMAIL_STAGING_ALLOWLIST", set())

    emails._send_email("customer@example.com", "Test", "<p>hi</p>")
    assert smtp_prod.sent == []


def test_staging_redirects_mail(smtp_prod, monkeypatch):
    monkeypatch.setattr(emails, "ENVIRONMENT", "staging")
    monkeypatch.setattr(emails, "EMAIL_STAGING_REDIRECT", "qa@genly.ai")
    monkeypatch.setattr(emails, "EMAIL_STAGING_ALLOWLIST", set())

    emails._send_email("customer@example.com", "Test", "<p>hi</p>")
    assert len(smtp_prod.sent) == 1
    assert smtp_prod.sent[0]["to"] == "qa@genly.ai"
    # Subject stamped with the environment
    assert smtp_prod.sent[0]["message"]["Subject"].startswith("[STAGING]")


# ---------------------------------------------------------------------------
# Public senders produce both parts end-to-end
# ---------------------------------------------------------------------------

def test_send_welcome_is_multipart(smtp_prod):
    emails.send_welcome("user@example.com", "tomas")

    msg = smtp_prod.sent[0]["message"]
    parts = msg.get_payload()
    assert [p.get_content_type() for p in parts] == ["text/plain", "text/html"]
    plain = parts[0].get_payload(decode=True).decode()
    assert "Go to Dashboard" in plain  # button link survives in text form


def test_send_password_reset_keeps_token_link_in_plain_text(smtp_prod):
    emails.send_password_reset("user@example.com", "tomas", "tok123")

    plain = smtp_prod.sent[0]["message"].get_payload()[0].get_payload(decode=True).decode()
    assert "reset_password=tok123" in plain
