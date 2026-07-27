from scripts.preflight import daily_smoke
from scripts.preflight._base import CheckResult, Status


class _Response:
    def raise_for_status(self):
        return None

    def json(self):
        return {"id": "email-test-id"}


def test_failed_results_are_rendered_without_tuple_unpacking(monkeypatch):
    monkeypatch.setenv("RESEND_API_KEY", "test-key")
    monkeypatch.setenv("ALERT_EMAIL", "ops@example.com")

    sent = {}

    def fake_post(url, **kwargs):
        sent["url"] = url
        sent.update(kwargs)
        return _Response()

    monkeypatch.setattr("requests.post", fake_post)
    failed = CheckResult(
        name="production_health",
        status=Status.FAIL,
        summary="API unavailable",
        details={"status": 503},
    )

    assert daily_smoke._send_alert([(failed, object())], [failed]) is True
    assert "production_health" in sent["json"]["html"]
    assert "API unavailable" in sent["json"]["html"]
