"""Optional mail to UMG when a correction they asked for is published."""
import pytest

import emails
import umg_publication_notice as notice

ENV = ("UMG_PUBLISH_NOTIFY_ENABLED", "UMG_PUBLISH_NOTIFY_RECIPIENTS", "UMG_PUBLISH_NOTIFY_RECIPIENTS_ARGENTINA",
       "UMG_PUBLISH_NOTIFY_RECIPIENTS_CHILE", "UMG_PORTAL_URL_ARGENTINA")


@pytest.fixture(autouse=True)
def clean(monkeypatch):
    for name in ENV:
        monkeypatch.delenv(name, raising=False)


def test_off_by_default_and_without_recipients_nothing_is_ever_sent(monkeypatch):
    sent = []
    monkeypatch.setattr(emails, "_send_email", lambda *a, **k: sent.append(a))
    assert notice.enabled() is False
    assert notice.should_notify(content_changed=True, resolved_requests=[1], hidden_from_client=False) is False
    monkeypatch.setenv("UMG_PUBLISH_NOTIFY_ENABLED", "1")
    assert notice.should_notify(content_changed=True, resolved_requests=[1], hidden_from_client=False) is True
    assert notice.notify(artist="A", song="S", portal_id="argentina", revision=2, comments=["x"]) == 0   # no recipients
    assert sent == []


def test_only_a_visible_publication_that_answered_requests_notifies(monkeypatch):
    monkeypatch.setenv("UMG_PUBLISH_NOTIFY_ENABLED", "1")
    ok = dict(content_changed=True, resolved_requests=[7], hidden_from_client=False)
    assert notice.should_notify(**ok) is True
    assert notice.should_notify(**{**ok, "content_changed": False}) is False        # plain re-send
    assert notice.should_notify(**{**ok, "resolved_requests": []}) is False         # answered nothing
    assert notice.should_notify(**{**ok, "hidden_from_client": True}) is False      # the client cannot see it


def test_recipients_are_validated_deduplicated_capped_and_per_portal(monkeypatch):
    monkeypatch.setenv("UMG_PUBLISH_NOTIFY_RECIPIENTS", "a@x.com, b@x.com; A@x.com\nnot-an-email, <c@x.com>")
    assert notice.recipients("argentina") == ["a@x.com", "b@x.com"]
    monkeypatch.setenv("UMG_PUBLISH_NOTIFY_RECIPIENTS_CHILE", "chile@x.com")
    assert notice.recipients("chile") == ["chile@x.com"] and notice.recipients("argentina") == ["a@x.com", "b@x.com"]
    monkeypatch.setenv("UMG_PUBLISH_NOTIFY_RECIPIENTS", ",".join(f"u{i}@x.com" for i in range(30)))
    assert len(notice.recipients("argentina")) == notice.MAX_RECIPIENTS


def test_notify_sends_one_mail_per_recipient_and_survives_a_failure(monkeypatch):
    monkeypatch.setenv("UMG_PUBLISH_NOTIFY_RECIPIENTS_ARGENTINA", "a@x.com,b@x.com,c@x.com")
    calls = []

    def fake(to, *, artist, song, portal_id, revision, comments, portal_url):
        calls.append((to, portal_url))
        if to == "b@x.com":
            raise RuntimeError("smtp down")
    monkeypatch.setattr(emails, "send_umg_publication_notice", fake)
    assert notice.notify(artist="A", song="S", portal_id="argentina", revision=2, comments=[]) == 2
    assert [c[0] for c in calls] == ["a@x.com", "b@x.com", "c@x.com"]
    assert calls[0][1] == "https://umg.genly.pro"
    monkeypatch.setenv("UMG_PORTAL_URL_ARGENTINA", "https://custom.example/")
    assert notice.portal_url("argentina") == "https://custom.example"
    assert notice.portal_url("chile") == "https://umgchile.genly.pro"


def test_the_mail_is_escaped_names_the_version_and_the_portal(monkeypatch):
    captured = {}
    monkeypatch.setattr(emails, "_send_email", lambda to, subject, body: captured.update(to=to, subject=subject, body=body))
    emails.send_umg_publication_notice(
        "a@x.com", artist="Los <Artistas>", song="Tema\nUno", portal_id="chile", revision=3,
        comments=["sacar el <b>punto</b>", "  ", "x" * 500], portal_url="https://umgchile.genly.pro")
    assert "\n" not in captured["subject"] and "Versión 3" in captured["subject"] and "(Chile)" in captured["subject"]
    body = captured["body"]
    assert "Ya está la versión 3" in body and "Los &lt;Artistas&gt;" in body and "&lt;b&gt;punto" in body
    assert "<b>punto</b>" not in body and body.count("<li>") == 2                      # blank comment dropped
    assert "x" * 241 not in body and "https://umgchile.genly.pro" in body              # comment capped at 240


def test_outside_production_the_staging_gate_still_applies(monkeypatch):
    """Real recipients only receive from staging if their address was allow-listed on purpose."""
    monkeypatch.setattr(emails, "ENVIRONMENT", "staging")
    monkeypatch.setattr(emails, "EMAIL_STAGING_ALLOWLIST", set())
    monkeypatch.setattr(emails, "EMAIL_STAGING_REDIRECT", "")
    assert emails._staging_gate("umg@real.com", "s") is None
    monkeypatch.setattr(emails, "EMAIL_STAGING_ALLOWLIST", {"umg@real.com"})
    assert emails._staging_gate("umg@real.com", "s") == "umg@real.com"
