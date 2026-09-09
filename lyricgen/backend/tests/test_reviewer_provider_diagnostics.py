from types import SimpleNamespace

from reviewer_shadow_audio import BlindAudioTools


def test_quota_diagnostics_keep_codes_but_not_provider_message(monkeypatch, tmp_path):
    clip = tmp_path / 'audio.wav'
    clip.write_bytes(b'synthetic-hash-only-no-network')
    class Failure(Exception):
        response = SimpleNamespace(status_code=429, headers={'retry-after':'90'},
            json=lambda: {'error':{'code':'credit_balance_exhausted','type':'insufficient_quota',
                                  'message':'PRIVATE_API_KEY_AND_ACCOUNT_DETAILS'}})
    def fail(*args): raise Failure('PRIVATE_PROVIDER_MESSAGE')
    monkeypatch.setattr(BlindAudioTools, '_whisper', fail)
    result = BlindAudioTools(tmp_path/'requests').listen(clip, provider='openai', view='mix',
        source={'job_id':'test'},window={'start':0.,'end':1.,'offset_seconds':0.})
    assert result['provider_error_code'] == 'credit_balance_exhausted'
    assert result['provider_error_type'] == 'insufficient_quota'
    assert result['retry_after_seconds'] == 90
    assert result['received_audio'] is False
    assert 'PRIVATE' not in str(result)
    assert 'PRIVATE' not in next((tmp_path/'requests').glob('*.json')).read_text()


def test_malformed_error_body_does_not_hide_original_http_status(monkeypatch, tmp_path):
    clip = tmp_path/'audio.wav'; clip.write_bytes(b'synthetic')
    class Failure(Exception):
        response = SimpleNamespace(status_code=429, headers={}, json=lambda: ['invalid'])
    def fail(*args): raise Failure()
    monkeypatch.setattr(BlindAudioTools, '_whisper', fail)
    result = BlindAudioTools(tmp_path/'requests').listen(clip,provider='openai',view='mix',
        source={'job_id':'test'},window={'start':0.,'end':1.,'offset_seconds':0.})
    assert result['http_status'] == 429 and result['tool_status'] == 'tool_error'
    assert 'provider_error_code' not in result


def test_vertex_sdk_code_without_response_reaches_circuit_breaker(monkeypatch, tmp_path):
    clip = tmp_path/'audio.wav'; clip.write_bytes(b'synthetic')
    class Failure(Exception):
        code = 429
        status = 'RESOURCE_EXHAUSTED'
        response = None
    def fail(*args): raise Failure('PRIVATE_PROVIDER_MESSAGE')
    monkeypatch.setattr(BlindAudioTools, '_gemini', fail)
    result = BlindAudioTools(tmp_path/'requests').listen(clip,provider='google',view='mix',
        source={'job_id':'test'},window={'start':0.,'end':1.,'offset_seconds':0.})
    assert result['http_status'] == 429
    assert result['provider_error_type'] == 'RESOURCE_EXHAUSTED'
    assert 'PRIVATE' not in str(result)
