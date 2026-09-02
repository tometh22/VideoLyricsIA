import storage


def test_generate_signed_url_can_override_browser_media_type(monkeypatch):
    captured = {}

    class FakeClient:
        def generate_presigned_url(self, operation, Params, ExpiresIn):
            captured.update({
                "operation": operation,
                "params": Params,
                "expires_in": ExpiresIn,
            })
            return "https://r2.example.test/signed"

    monkeypatch.setattr(storage, "_get_client", lambda: FakeClient())
    monkeypatch.setattr(storage, "R2_BUCKET", "audio")

    result = storage.generate_signed_url(
        "inputs/song.wav",
        expiry_seconds=900,
        response_content_type="audio/wav",
    )

    assert result == "https://r2.example.test/signed"
    assert captured["params"] == {
        "Bucket": "audio",
        "Key": "inputs/song.wav",
        "ResponseContentType": "audio/wav",
    }
