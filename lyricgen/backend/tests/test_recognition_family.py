from pipeline import _tag_recognition_family, transcription_family
import whisperx_transcribe


def test_transport_marker_records_exact_selected_local_model(monkeypatch):
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    rows = _tag_recognition_family(
        [{"start": 0.0, "end": 1.0, "text": "line"}],
        "openai-whisper/large-v3-local",
    )
    assert transcription_family(rows) == "openai-whisper/large-v3-local"


def test_production_family_is_openai_whisper_1(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "configured-for-test")
    assert transcription_family([]) == "openai/whisper-1"


def test_whisperx_family_includes_pinned_provider_model():
    family = whisperx_transcribe.recognition_family()
    assert family.startswith("replicate/")
    assert ":" in family
