"""El fallback de _get_unique_prompt debe honrar el prompt del operador en
CUALQUIER policy mode, y el call de Gemini debe reintentar errores de quota.

Bug real (job staging 3b28837a1784, 2026-07): el operador editó el fondo con
un prompt custom, Gemini devolvió 429 RESOURCE_EXHAUSTED (llamada única, sin
retry) y el fallback combinatorio eligió una escena random de _BG_SCENES
("northern lights aurora...") ignorando el prompt — porque la rama que
respeta el hint solo corría con policy=enforce y staging corre shadow.

Dos fixes pineados acá:
  1. WS-A: el check del hint corre ANTES del branch policy_enforces, así
     shadow/off también lo honran. Enforce queda bit-idéntico (mismo
     sanitize_generated_text, que es no-op fuera de enforce).
  2. WS-B: _generate_content_with_quota_retry reintenta SOLO 429/
     RESOURCE_EXHAUSTED con backoff; TimeoutError (watchdog anti-deadlock)
     y cualquier otro error propagan sin retry.
"""

import pytest

import pipeline
from background_policy import resolve_atmospherics_policy


HINT = "carnaval en la playa con serpentinas de colores"


def _fail_analysis(monkeypatch, tmp_path):
    """Simula la falla del provider: Gemini no devolvió prompt utilizable."""
    monkeypatch.setattr(
        pipeline, "_analyze_lyrics_for_background",
        lambda *a, **kw: {"style": "video", "prompt": None},
    )
    monkeypatch.setattr(pipeline, "_USED_PROMPTS_FILE", str(tmp_path / "used.json"))


# ── WS-A: fallback honra el hint ─────────────────────────────────────────


def test_shadow_fallback_honors_operator_hint(monkeypatch, tmp_path):
    """Policy shadow (la de staging) + hint + provider caído → el prompt es
    el del operador, NUNCA una escena random del pool."""
    _fail_analysis(monkeypatch, tmp_path)
    result = pipeline._get_unique_prompt(
        lyrics_text="letra de la canción",
        artist="Artista", song_title="Tema",
        background_hint=HINT,
        atmospherics_policy=resolve_atmospherics_policy(HINT, mode="shadow"),
    )
    assert "carnaval" in result["prompt"]
    for scene in pipeline._BG_SCENES:
        assert scene not in result["prompt"], (
            f"provider failure must not replace the operator prompt with the "
            f"stock scene {scene!r}"
        )


def test_enforce_fallback_with_hint_keeps_parity(monkeypatch, tmp_path):
    """Enforce + hint: mismo resultado que antes del refactor (el hint,
    pasado por sanitize_generated_text — no-op para este texto)."""
    _fail_analysis(monkeypatch, tmp_path)
    result = pipeline._get_unique_prompt(
        lyrics_text="letra de la canción",
        artist="Artista", song_title="Tema",
        background_hint=HINT,
        atmospherics_policy=resolve_atmospherics_policy(HINT, mode="enforce"),
    )
    assert result["prompt"] == HINT


def test_enforce_fallback_without_hint_stays_neutral(monkeypatch, tmp_path):
    """Enforce sin hint: el fallback neutral identity-based queda intacto."""
    _fail_analysis(monkeypatch, tmp_path)
    result = pipeline._get_unique_prompt(
        lyrics_text="letra de la canción",
        artist="Artista", song_title="Tema",
        background_hint=None,
        atmospherics_policy=resolve_atmospherics_policy(None, mode="enforce"),
    )
    assert "Original non-figurative" in result["prompt"]


def test_shadow_fallback_without_hint_stays_combinatorial(monkeypatch, tmp_path):
    """Shadow sin hint: sigue el fallback combinatorio de siempre."""
    _fail_analysis(monkeypatch, tmp_path)
    result = pipeline._get_unique_prompt(
        lyrics_text="letra de la canción",
        artist="Artista", song_title="Tema",
        background_hint=None,
        atmospherics_policy=resolve_atmospherics_policy(None, mode="shadow"),
    )
    assert any(scene in result["prompt"] for scene in pipeline._BG_SCENES)
    assert "4k, photorealistic" in result["prompt"]


def test_imagen_provider_hint_fallback_returns_image_style(monkeypatch, tmp_path):
    """for_provider=imagen → style=image también en el fallback del hint."""
    _fail_analysis(monkeypatch, tmp_path)
    result = pipeline._get_unique_prompt(
        lyrics_text="letra", artist="A", song_title="T",
        background_hint=HINT, for_provider="imagen",
        atmospherics_policy=resolve_atmospherics_policy(HINT, mode="shadow"),
    )
    assert result["style"] == "image"
    assert "carnaval" in result["prompt"]


# ── WS-B: retry de quota ─────────────────────────────────────────────────


def _no_thread_call(monkeypatch):
    """Ejecuta fn inline (sin executor) para testear SOLO la lógica de retry."""
    monkeypatch.setattr(
        pipeline, "_call_with_timeout",
        lambda fn, timeout_s, label="": fn(),
    )


def test_quota_retry_recovers_after_two_429s(monkeypatch):
    _no_thread_call(monkeypatch)
    sleeps: list[float] = []
    monkeypatch.setattr(pipeline.time, "sleep", sleeps.append)
    calls = {"n": 0}

    def flaky():
        calls["n"] += 1
        if calls["n"] <= 2:
            raise RuntimeError("429 RESOURCE_EXHAUSTED: quota exceeded")
        return "ok"

    assert pipeline._generate_content_with_quota_retry(
        flaky, timeout_s=60.0, label="BG-ANALYZE") == "ok"
    assert calls["n"] == 3
    assert sleeps == list(pipeline._BG_429_BACKOFF_S)


def test_quota_retry_exhausted_reraises(monkeypatch):
    _no_thread_call(monkeypatch)
    sleeps: list[float] = []
    monkeypatch.setattr(pipeline.time, "sleep", sleeps.append)

    def always_429():
        raise RuntimeError("429 RESOURCE_EXHAUSTED")

    with pytest.raises(RuntimeError, match="RESOURCE_EXHAUSTED"):
        pipeline._generate_content_with_quota_retry(
            always_429, timeout_s=60.0, label="BG-ANALYZE")
    assert sleeps == list(pipeline._BG_429_BACKOFF_S)


def test_non_quota_error_is_not_retried(monkeypatch):
    _no_thread_call(monkeypatch)
    monkeypatch.setattr(
        pipeline.time, "sleep",
        lambda s: pytest.fail("must not sleep/retry on a non-quota error"),
    )
    calls = {"n": 0}

    def boom():
        calls["n"] += 1
        raise ValueError("malformed response")

    with pytest.raises(ValueError):
        pipeline._generate_content_with_quota_retry(
            boom, timeout_s=60.0, label="BG-ANALYZE")
    assert calls["n"] == 1


def test_timeout_error_is_never_retried(monkeypatch):
    """TimeoutError viene del watchdog anti-deadlock de _call_with_timeout:
    reintentarlo re-bloquearía al worker en el hang del que el watchdog
    justamente escapa."""
    _no_thread_call(monkeypatch)
    monkeypatch.setattr(
        pipeline.time, "sleep",
        lambda s: pytest.fail("must not sleep/retry on TimeoutError"),
    )
    calls = {"n": 0}

    def hang():
        calls["n"] += 1
        # El mensaje del watchdog contiene "exceeded 60s timeout" — sin "429",
        # pero probamos también que ni un TimeoutError con "429" se reintente.
        raise TimeoutError("BG-ANALYZE call exceeded 60s timeout (429 upstream)")

    with pytest.raises(TimeoutError):
        pipeline._generate_content_with_quota_retry(
            hang, timeout_s=60.0, label="BG-ANALYZE")
    assert calls["n"] == 1
