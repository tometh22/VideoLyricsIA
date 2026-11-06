"""The client-change notification must land on the right person, with a link."""
from unittest.mock import patch

import emails


def _send(**kwargs):
    with patch("emails._send_email") as sent, patch.object(emails, "FRONTEND_URL", "https://staging.example"), \
            patch.dict("os.environ", {"ALERT_EMAIL": "ops@example.com"}):
        emails.send_umg_change_request_notification("Artista", "Canción", "Cambiar la palabra", 7, "job1", **kwargs)
    return sent.call_args_list


def test_subject_names_the_portal_and_body_links_the_request():
    (call,) = _send(request_id=42, portal_id="chile", campaign_name="UMG Agosto")
    to, subject, body = call.args
    assert to == "ops@example.com"
    assert subject == "UMG Chile pidió un cambio — Artista · Canción"
    assert "https://staging.example/admin?section=cambios&change_request_id=42" in body
    assert "UMG Agosto" in body and "UMG Chile pidió un cambio" in body


def test_without_context_it_still_sends_the_generic_mail_with_the_section_link():
    (call,) = _send()
    _, subject, body = call.args
    assert subject == "UMG pidió un cambio — Artista · Canción"
    assert "https://staging.example/admin?section=cambios" in body
    assert "change_request_id" not in body


def test_campaign_owner_gets_the_same_mail_once_and_ops_is_never_duplicated():
    calls = _send(request_id=1, portal_id="argentina", owner_email="dueño@example.com")
    assert [c.args[0] for c in calls] == ["ops@example.com", "dueño@example.com"]
    assert calls[0].args[1] == calls[1].args[1] and "Argentina" in calls[0].args[1]
    assert len(_send(request_id=1, owner_email="OPS@example.com")) == 1


def test_html_in_the_client_comment_is_escaped():
    with patch("emails._send_email") as sent:
        emails.send_umg_change_request_notification("A", "S", "<script>x</script>", 1, "j", request_id=3)
    assert "<script>" not in sent.call_args.args[2]


def test_a_newline_in_the_song_metadata_cannot_break_the_subject():
    (call,) = _send(request_id=5, portal_id="chile")
    with patch("emails._send_email") as sent:
        emails.send_umg_change_request_notification("Art\r\nista", "Can\nción", "x", 1, "j", request_id=5, portal_id="chile")
    subject = sent.call_args.args[1]
    assert "\n" not in subject and "\r" not in subject
    assert subject == "UMG Chile pidió un cambio — Art ista · Can ción"
