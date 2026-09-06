"""Bounded audio-only adapters. Writes PRIVATE experiment cache, never product DB.

No caller-supplied text enters either blind ASR request. Provider timestamps
remain hypotheses. Acoustic tools have their own clock/provenance.
"""
from __future__ import annotations

from dataclasses import asdict
import hashlib
import json
import math
import os
from pathlib import Path
import subprocess
import time

from reviewer_shadow import ShadowPolicy, local_to_global, tokens
from shadow_reference_import import digest

LEGACY_PROMPT_VERSION = "blind-vocal-events-shadow-v1"
BLIND_PROMPT = (
    "Escuchá únicamente este audio. Transcribí lo efectivamente cantado, sin completar "
    "desde memoria ni inferir una letra conocida. Conservá idioma y repeticiones. "
    "No recibiste artista, título ni letra candidata. Seguí el esquema de respuesta. "
    "Los tiempos son hipótesis locales del fragmento. No confundas ausencia de pitch "
    "con silencio. Cada frase tiene como máximo 240 caracteres; dividí frases largas "
    "en eventos contiguos sin perder palabras. Una vocal sostenida no son repeticiones "
    "nuevas: no expandas la duración de una nota escribiendo sílabas infinitas. "
    "Conservá las repeticiones que realmente oís, sin inventar más allá del fragmento."
)
PROMPT_VERSION = "blind-vocal-events-shadow-v2-bounded-schema"
MAX_EVENT_CHARACTERS = 240
AMBIGUITY_REASONS = ["none", "overlapping_voices", "sustained_chorus",
                     "overlapping_adlibs", "uncertain_boundary", "multiple_conditions", "uncertain"]
# Vertex documents maxItems, enums and numeric ranges, but NOT maxLength.
# Character limits below are descriptions + local validation, not a claimed
# provider-enforced maxLength guarantee. Never recover truncated JSON as valid.
BLIND_RESPONSE_SCHEMA = {
    "type": "OBJECT", "required": ["events", "editorial_ambiguity", "ambiguity_reason", "reverb"],
    "propertyOrdering": ["events", "editorial_ambiguity", "ambiguity_reason", "reverb"],
    "properties": {
        "events": {"type": "ARRAY", "maxItems": 16, "items": {
            "type": "OBJECT", "required": ["text", "start", "end", "kind"],
            "propertyOrdering": ["text", "start", "end", "kind"],
            "properties": {
                "text": {"type": "STRING", "description": "Frase realmente oída, máximo 240 caracteres; no expandir vocales sostenidas como repeticiones."},
                "start": {"type": "NUMBER", "minimum": 0, "maximum": 24},
                "end": {"type": "NUMBER", "minimum": 0, "maximum": 24},
                "kind": {"type": "STRING", "enum": ["sung", "speech", "vocalization"]}}}},
        "editorial_ambiguity": {"type": "BOOLEAN", "description": "Hay superposición de voces, coro sostenido, adlibs superpuestos o corte incierto."},
        "ambiguity_reason": {"type": "STRING", "enum": AMBIGUITY_REASONS},
        "reverb": {"type": "STRING", "enum": ["audible", "uncertain", "absent"]}}}


def valid_blind_response(payload, *, legacy=False):
    if not isinstance(payload, dict) or not isinstance(payload.get("events"), list) or len(payload["events"]) > 16:
        return False
    for event in payload["events"]:
        if not isinstance(event, dict) or not isinstance(event.get("text"), str) or not event["text"].strip():
            return False
        if not legacy and len(event["text"]) > MAX_EVENT_CHARACTERS:
            return False
        start, end = event.get("start"), event.get("end")
        if not all(isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v) for v in (start, end)):
            return False
        if not 0 <= start < end or (not legacy and end > 24):
            return False
        if event.get("kind") not in {"sung", "speech", "vocalization"}:
            return False
    return legacy or (isinstance(payload.get("editorial_ambiguity"), bool)
        and payload.get("ambiguity_reason") in AMBIGUITY_REASONS
        and payload.get("reverb") in {"audible", "uncertain", "absent"})


def private_write(path, value):
    with Path(path).open("x") as handle:
        os.chmod(path, 0o600)
        json.dump(value, handle, ensure_ascii=False)


def file_sha(path):
    h = hashlib.sha256()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def extract_clip(path, window, destination):
    duration = window["end"] - window["start"]
    if not (0 < duration <= ShadowPolicy().max_clip_seconds):
        raise ValueError("clip_budget_exceeded")
    subprocess.run(["ffmpeg", "-nostdin", "-v", "error", "-ss", str(window["start"]),
                    "-t", str(duration), "-i", str(path), "-ac", "1", "-ar", "16000",
                    "-c:a", "pcm_s16le", "-threads", "1", "-n", str(destination)],
                   check=True, capture_output=True, timeout=45)
    os.chmod(destination, 0o600)
    return file_sha(destination)


def probe(path):
    result = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
                             "-of", "json", str(path)], capture_output=True, check=True, timeout=20)
    return float(json.loads(result.stdout)["format"]["duration"])


def pcm(path, *, rate=8000):
    import numpy as np
    result = subprocess.run(["ffmpeg", "-nostdin", "-v", "error", "-i", str(path),
                             "-ac", "1", "-ar", str(rate), "-f", "f32le", "-threads", "1", "pipe:1"],
                            check=True, capture_output=True, timeout=90)
    return np.frombuffer(result.stdout, dtype="<f4")


def envelope(signal, frame=160):
    import numpy as np
    trimmed = signal[:len(signal) // frame * frame]
    return np.sqrt(np.mean(trimmed.reshape(-1, frame) ** 2, axis=1))


def check_sync(mix, stem):
    """Three separated envelope windows; uncertainty is NOT silent zero offset."""
    import numpy as np
    duration_mix, duration_stem = probe(mix), probe(stem)
    m, s = envelope(pcm(mix)), envelope(pcm(stem))
    n = min(len(m), len(s))
    observations = []
    for fraction in (.2, .5, .8):
        center = int(n * fraction)
        start, end = max(25, center - 250), min(n - 25, center + 250)
        a = m[start:end]
        scores = []
        for lag in range(-25, 26):
            b = s[start + lag:end + lag]
            score = float(np.corrcoef(a, b)[0, 1]) if len(a) > 1 and np.std(a) > 1e-8 and np.std(b) > 1e-8 else -1.0
            scores.append((score, lag))
        score, lag = max(scores)
        observations.append({"lag_seconds": lag * .02, "correlation": score})
    verified = (abs(duration_mix - duration_stem) <= .05 and
                all(o["correlation"] >= .35 and abs(o["lag_seconds"]) <= .04 for o in observations))
    return {"tool": "ffmpeg-rms-envelope-sync-v1", "duration_mix": duration_mix,
            "duration_stem": duration_stem, "observations": observations,
            "mix_stem_sync_verified": verified, "automatic_offset_applied": False,
            "status": "consistent" if verified else "uncertain", "frame_seconds": .02}


class BlindAudioTools:
    def __init__(self, cache_dir, *, policy=ShadowPolicy()):
        self.cache = Path(cache_dir)
        self.cache.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.policy = policy
        self.calls = 0

    def listen(self, clip, *, provider, view, source, window):
        if provider not in {"openai", "google"}:
            raise ValueError("unsupported_audio_provider")
        model = "whisper-1" if provider == "openai" else "gemini-2.5-flash"
        identity = {"clip_sha256": file_sha(clip), "provider": provider, "model": model,
                    "prompt_version": PROMPT_VERSION if provider == "google" else "no-prompt-v1",
                    "source": source, "window": window, "view": view, "policy": asdict(self.policy)}
        task_id = digest(identity)
        target, attempt = self.cache / (task_id + ".json"), self.cache / (task_id + ".attempt.json")
        if target.exists():
            result = json.loads(target.read_text())
            return {**result, "cache_hit": True, "calls_this_run": 0}
        if provider == "google":
            old_identity = {**identity, "prompt_version": LEGACY_PROMPT_VERSION}
            old_id = digest(old_identity)
            old_target = self.cache / (old_id + ".json")
            if old_target.exists():
                old = json.loads(old_target.read_text())
                if (all(old.get(k) == v for k, v in old_identity.items())
                        and old.get("tool_status") == "ok" and old.get("received_audio") is True
                        and old.get("conditioning_texts") == []
                        and valid_blind_response(old.get("response"), legacy=True)):
                    return {**old, "cache_hit": True, "calls_this_run": 0,
                        "cache_compatibility": "valid_legacy_blind_v1", "requested_prompt_version": PROMPT_VERSION}
            elif (self.cache / (old_id + ".attempt.json")).exists():
                return {**old_identity, "tool_status": "unknown_completion", "calls_this_run": 0,
                    "reason": "prior_attempt_has_no_result_do_not_repurchase", "received_audio": False}
        base = {**identity, "tool_status": "not_run", "received_audio": False,
                "conditioning_texts": [], "family": "openai/whisper-1" if provider == "openai" else "google/gemini-2.5-flash-audio",
                "calls": 0, "calls_this_run": 0, "usage": None, "observed_cost_usd": None,
                "clock_status": "provider_timestamp_hypothesis", "automatic_apply_allowed": False}
        if attempt.exists():
            return {**base, "tool_status": "unknown_completion", "reason": "prior_attempt_has_no_result_do_not_repurchase"}
        if self.calls >= self.policy.max_calls_per_song:
            return {**base, "tool_status": "budget_exhausted"}
        private_write(attempt, {"identity": identity, "started_at": time.time()})
        self.calls += 1
        begin = time.monotonic()
        base.update(calls=1, calls_this_run=1)
        try:
            if provider == "openai":
                payload = self._whisper(clip)
            else:
                payload = self._gemini(clip)
            base.update(payload, tool_status=payload.get("tool_status", "ok"), received_audio=True)
        except Exception as exc:
            base.update(tool_status="tool_error", error_type=type(exc).__name__,
                        http_status=getattr(getattr(exc, "response", None), "status_code", None))
        base["latency_seconds"] = round(time.monotonic() - begin, 4)
        private_write(target, base)
        return base

    @staticmethod
    def _whisper(clip):
        import requests
        with open(clip, "rb") as handle:
            response = requests.post("https://api.openai.com/v1/audio/transcriptions",
                headers={"Authorization": "Bearer " + os.environ["OPENAI_API_KEY"]},
                files={"file": ("blind.wav", handle, "audio/wav")},
                data={"model": "whisper-1", "response_format": "verbose_json",
                      "timestamp_granularities[]": "word", "temperature": "0"}, timeout=(15, 75))
        response.raise_for_status()
        payload = response.json()
        return {"response": payload, "usage": payload.get("usage"),
                "request_id": response.headers.get("x-request-id"), "model_version": "whisper-1"}

    @staticmethod
    def _gemini(clip):
        from google import genai
        from google.oauth2 import service_account
        credentials = service_account.Credentials.from_service_account_file(
            os.environ["GOOGLE_APPLICATION_CREDENTIALS"], scopes=["https://www.googleapis.com/auth/cloud-platform"])
        project = os.environ.get("VERTEX_PROJECT") or credentials.project_id
        with genai.Client(vertexai=True, project=project,
                          location=os.environ.get("VERTEX_LOCATION", "us-central1"),
                          credentials=credentials.with_quota_project(project),
                          http_options=genai.types.HttpOptions(timeout=75000,
                              retry_options=genai.types.HttpRetryOptions(attempts=1))) as client:
            response = client.models.generate_content(model="gemini-2.5-flash",
                contents=[genai.types.Part.from_bytes(data=Path(clip).read_bytes(), mime_type="audio/wav")],
                config=genai.types.GenerateContentConfig(system_instruction=BLIND_PROMPT,
                    temperature=0, response_mime_type="application/json", max_output_tokens=4096,
                    response_schema=BLIND_RESPONSE_SCHEMA,
                    thinking_config=genai.types.ThinkingConfig(thinking_budget=0)))
        finish = getattr(response.candidates[0], "finish_reason", None) if response.candidates else None
        finish = getattr(finish, "value", finish)
        trace = {"raw_response_text": response.text, "response": {}, "finish_reason": finish,
                "response_schema_sha256": digest(BLIND_RESPONSE_SCHEMA),
                "character_limit_provider_enforced": False,
                "usage": response.usage_metadata.model_dump(mode="json") if response.usage_metadata else None,
                "model_version": response.model_version, "request_id": response.response_id}
        if finish != "STOP":
            return {**trace, "tool_status": "invalid_response", "error_type": "incomplete_or_blocked_generation"}
        try:
            payload = json.loads(response.text)
        except (json.JSONDecodeError, TypeError):
            return {**trace, "tool_status": "invalid_response", "error_type": "invalid_json_response"}
        if not valid_blind_response(payload):
            return {**trace, "tool_status": "invalid_response", "error_type": "invalid_blind_events_schema"}
        return {**trace, "response": payload}


def localized_witnesses(results, segment, window, duration):
    """Localize unique Gemini phrases against independently recognized ASR words.

    This checks occurrence identity only. It does NOT turn ASR/Gemini clocks
    into verified acoustic endpoints or certify reference correctness.
    """
    whisper = next((r for r in results if r["provider"] == "openai" and r["tool_status"] == "ok"), None)
    gemini = next((r for r in results if r["provider"] == "google" and r["tool_status"] == "ok"), None)
    if not whisper or not gemini:
        return []
    raw_words = whisper["response"].get("words") or []
    flat, word_owners = [], []
    for i, w in enumerate(raw_words):
        normalized = tokens(w.get("word", ""))
        flat.extend(normalized)
        word_owners.extend([i] * len(normalized))
    output = []
    for event in gemini["response"]["events"]:
        phrase = tokens(event.get("text", ""))
        if not phrase or event.get("kind") not in {"sung", "vocalization"}:
            continue
        occurrences = [i for i in range(len(flat) - len(phrase) + 1) if flat[i:i + len(phrase)] == phrase]
        if len(occurrences) != 1:
            continue
        pos = occurrences[0]
        first, last = raw_words[word_owners[pos]], raw_words[word_owners[pos + len(phrase) - 1]]
        try:
            start, end = local_to_global(first["start"], last["end"], offset=window["offset_seconds"],
                                        clip_duration=window["end"] - window["start"], song_duration=duration)
        except (ValueError, KeyError):
            continue
        overlap = max(0, min(end, segment["end"]) - max(start, segment["start"]))
        if overlap / max(.001, end - start) < .75:
            continue
        # A whole phrase must cover the current line rather than a subset of
        # it; otherwise a short matching suffix could erase the opening words.
        if start > segment["start"] + .75 or end < segment["end"] - 1.0:
            continue
        for result in (whisper, gemini):
            output.append({"kind": "content", "text": event["text"], "family": result["family"],
                           "tool_status": "ok", "received_audio": True, "conditioning_texts": [],
                           "occurrence_verified": True, "occurrence_method": "unique_phrase_cross_ASR_window_v1",
                           "occurrence_clock": [start, end], "timing_verified": False,
                           "editorial_ambiguity": gemini["response"].get("editorial_ambiguity", True),
                           "source_request_id": result.get("request_id")})
    return output


def acoustic_endpoint_candidates(stem_clip, segment, window, sync):
    """Measured pause candidates, NOT accepted phrase ends. No pitch-constant rule.

    Their target voice / phonetic attribution remains unverified, so the
    selector abstains until those independent checks or human gold exist.
    """
    import numpy as np
    rms = envelope(pcm(stem_clip))
    threshold = max(1e-5, float(np.percentile(rms, 95)) * .08)
    active = rms > threshold
    runs, beginning = [], None
    for i, value in enumerate(active):
        if not value and beginning is None:
            beginning = i
        if (value or i == len(active) - 1) and beginning is not None:
            finish = i if value else i + 1
            if finish - beginning >= 10:
                runs.append((beginning, finish))
            beginning = None
    current_local = float(segment["end"]) - window["offset_seconds"]
    selected = sorted(runs, key=lambda r: abs(r[0] * .02 - current_local))[:2]
    return [{"kind": "endpoint", "clock_source": "acoustic_tool", "tool_status": "ok",
             "tool": "rms-pause-candidate-v1", "end_seconds": round(a * .02 + window["offset_seconds"], 6),
             "pause_duration_seconds": (b - a) * .02, "energy_threshold": threshold,
             "frame_seconds": .02, "target_voice_verified": False, "phonetic_end_supported": False,
             "mix_stem_sync_verified": sync["mix_stem_sync_verified"],
             "reason": "pause_is_candidate_not_word_end_proof", "automatic_apply_allowed": False}
            for a, b in selected]
