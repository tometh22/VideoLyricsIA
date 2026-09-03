"""Unit tests for `_gemini_cleanup_lyrics` and its content-addressable
cache helpers. Pure data-in/data-out where possible; the Gemini call is
mocked because the live SDK requires Vertex credentials.

Spec recap (see pipeline.py docstring for full context):
- Takes audio_path + lrclib_plain, returns cleaned text or None.
- Gated behind GEMINI_LYRICS_CLEANUP_ENABLED env flag (default off).
- Returns None on missing audio, empty plain, disabled flag, Gemini
  error, suspicious line-count ratio (<50% or >250% of input).
- On success: writes content-addressable cache row (engine="gemini_cleanup").
- INCIDENT 2026-05-26 (Arbol de la vida / Sin Gamulán family): lrclib
  has community-grade defects (missing accents, miscounted chorus
  repeats). This helper closes the gap vs Rotor's licensed LyricFind
  catalog at ~$0.01/song.
"""
import os
import tempfile
import pytest
from unittest.mock import patch, MagicMock

import pipeline
from recognition_provenance import begin_collection, end_collection


@pytest.fixture
def tiny_audio(tmp_path):
    """A 1KB pseudo-audio file. Content doesn't matter; the file just
    needs to be readable so the hash + Gemini upload paths run."""
    p = tmp_path / "test.wav"
    p.write_bytes(b"RIFF\x00\x00\x00\x00WAVE" + b"\x00" * 1000)
    return str(p)


# ─── Feature-flag gate ──────────────────────────────────────────────

def test_returns_none_when_flag_off(tiny_audio, monkeypatch):
    """Default behaviour: no Gemini call, no cache touch, returns None."""
    monkeypatch.delenv("GEMINI_LYRICS_CLEANUP_ENABLED", raising=False)
    out = pipeline._gemini_cleanup_lyrics(tiny_audio, "Algun texto")
    assert out is None


def test_returns_none_for_empty_plain(tiny_audio, monkeypatch):
    monkeypatch.setenv("GEMINI_LYRICS_CLEANUP_ENABLED", "1")
    assert pipeline._gemini_cleanup_lyrics(tiny_audio, "") is None
    assert pipeline._gemini_cleanup_lyrics(tiny_audio, "   ") is None
    assert pipeline._gemini_cleanup_lyrics(tiny_audio, None) is None


def test_returns_none_for_missing_audio(monkeypatch):
    monkeypatch.setenv("GEMINI_LYRICS_CLEANUP_ENABLED", "1")
    assert pipeline._gemini_cleanup_lyrics("/no/such/file.wav", "x\ny\nz") is None
    assert pipeline._gemini_cleanup_lyrics("", "x") is None


# ─── Cache key determinism ──────────────────────────────────────────

def test_cache_key_deterministic(tiny_audio):
    """Same audio + same hint → same key. Different hint → different key."""
    k1, ah1, hh1 = pipeline._gemini_cleanup_cache_key(tiny_audio, "hola")
    k2, ah2, hh2 = pipeline._gemini_cleanup_cache_key(tiny_audio, "hola")
    assert k1 == k2
    assert ah1 == ah2
    assert hh1 == hh2

    k3, _, hh3 = pipeline._gemini_cleanup_cache_key(tiny_audio, "adios")
    assert k3 != k1
    assert hh3 != hh1


def test_cache_key_namespace_prefix(tiny_audio):
    """Cache key must be namespaced so it can't collide with whisperX
    cache rows (same DB table, different engine)."""
    key, _, _ = pipeline._gemini_cleanup_cache_key(tiny_audio, "lyrics")
    assert key.startswith("gem-clean:")


def test_strict_audio_reference_never_reuses_regular_cleanup_cache(tiny_audio):
    regular, _, _ = pipeline._gemini_cleanup_cache_key(tiny_audio, "hola")
    strict, _, _ = pipeline._gemini_cleanup_cache_key(
        tiny_audio, "hola", policy="strict-audio-v1",
    )
    assert regular != strict


def test_cache_key_unreadable_audio_returns_none():
    """Hash failure shouldn't crash — cache is best-effort. Caller still
    proceeds to a live Gemini call without a cache key."""
    k, ah, hh = pipeline._gemini_cleanup_cache_key("/does/not/exist.wav", "x")
    assert k is None
    assert ah is None
    assert hh is None


def test_cache_hit_returns_cleaned_text_and_freezes_cached_raw(
        tiny_audio, monkeypatch):
    monkeypatch.setenv("GEMINI_LYRICS_CLEANUP_ENABLED", "1")
    raw = "Claro, acá está:\nLínea corregida"
    cleaned = "Línea corregida"
    monkeypatch.setattr(
        pipeline, "_gemini_cleanup_cache_lookup",
        lambda _key: {
            "schema": "gemini-cleanup-cache-v2",
            "raw_text": raw,
            "cleaned": cleaned,
        },
    )
    monkeypatch.setattr(
        pipeline, "_get_genai_client",
        lambda: (_ for _ in ()).throw(
            AssertionError("v2 cache hit must not call Gemini")
        ),
    )
    collector, token = begin_collection()
    try:
        out = pipeline._gemini_cleanup_lyrics(tiny_audio, "Línea original")
        snapshot = collector.snapshot()
    finally:
        end_collection(token)

    assert out == cleaned
    assert snapshot["completed_attempt_count"] == 1
    assert snapshot["hypotheses"][0]["events"] == [{"text": raw}]
    assert snapshot["hypotheses"][0]["transformation"] == (
        "gemini_cleanup_cache_hit_raw"
    )


def test_legacy_cache_without_raw_forces_live_recompute(tiny_audio, monkeypatch):
    monkeypatch.setenv("GEMINI_LYRICS_CLEANUP_ENABLED", "1")
    plain = "Línea uno\nLínea dos\nLínea tres\nLínea cuatro"
    fake_client = _patch_genai_with(monkeypatch, _FakeResponse(plain))
    monkeypatch.setattr(
        pipeline, "_gemini_cleanup_cache_lookup",
        lambda _key: {"cleaned": "legacy processed only"},
    )
    monkeypatch.setattr(
        pipeline, "_gemini_cleanup_cache_write", lambda *a, **k: None,
    )

    assert pipeline._gemini_cleanup_lyrics(tiny_audio, plain) == plain
    assert fake_client.models.generate_content.call_count == 1


# ─── Sanity gate on line-count ratio ────────────────────────────────

class _FakeResponse:
    """Mimic google-genai Response with just the attrs we read."""

    def __init__(self, text, finish_reason="STOP"):
        self.text = text
        cand = MagicMock()
        cand.finish_reason = finish_reason
        self.candidates = [cand]


def _patch_genai_with(monkeypatch, fake_response):
    """Helper: replace _get_genai_client + the genai SDK so we don't hit
    Vertex but still exercise the full Gemini-call code path."""
    fake_client = MagicMock()
    fake_client.models.generate_content.return_value = fake_response
    monkeypatch.setattr(pipeline, "_get_genai_client", lambda: fake_client)

    # The function does `from google import genai` at runtime — we need
    # genai.types.Part.from_bytes and genai.types.GenerateContentConfig
    # to exist. The real ones work fine even without credentials (they
    # don't talk to the network), so we don't need to patch them.
    return fake_client


def test_rejects_when_output_too_short(tiny_audio, monkeypatch):
    """If Gemini returns <50% of the input line count, prefer raw text
    over a likely-truncated cleanup. Real incident: Pro hit MAX_TOKENS
    and returned only the first 20 of 58 lines (PR #X exploration)."""
    monkeypatch.setenv("GEMINI_LYRICS_CLEANUP_ENABLED", "1")
    plain = "\n".join(f"line {i}" for i in range(20))   # 20 lines in
    raw = "line 1\nline 2\nline 3"
    _patch_genai_with(monkeypatch, _FakeResponse(raw))  # 3 out

    collector, token = begin_collection()
    try:
        out = pipeline._gemini_cleanup_lyrics(tiny_audio, plain)
        snapshot = collector.snapshot()
    finally:
        end_collection(token)
    assert out is None
    assert snapshot["completed_attempt_count"] == 1
    assert snapshot["hypotheses"][0]["events"] == [{"text": raw}]
    assert snapshot["hypotheses"][0]["transformation"] == (
        "gemini_cleanup_raw"
    )


def test_rejects_when_output_too_long(tiny_audio, monkeypatch):
    """If Gemini explodes the input to 10x lines, something is wrong
    (probable confusion of song with infinite loop / chorus expansion
    gone wrong). Bail to raw text."""
    monkeypatch.setenv("GEMINI_LYRICS_CLEANUP_ENABLED", "1")
    plain = "\n".join(f"line {i}" for i in range(10))   # 10 lines in
    huge = "\n".join(f"line {i}" for i in range(50))    # 50 lines out (5x)
    _patch_genai_with(monkeypatch, _FakeResponse(huge))

    out = pipeline._gemini_cleanup_lyrics(tiny_audio, plain)
    assert out is None


def test_accepts_modest_expansion(tiny_audio, monkeypatch):
    """The Arbol case: lrclib has the chorus 8× but the song actually
    has 12× — a legitimate 1.5x expansion. Must pass every defense."""
    monkeypatch.setenv("GEMINI_LYRICS_CLEANUP_ENABLED", "1")
    plain = (
        "Después de tanto vagar por las calles\n"
        "La ciudad te parece tan gris\n"
        "Árbol de la vida dame hoy\n"
        "Dame hoy tu fruta divina\n"
        "Árbol de la vida dame hoy\n"
        "Dame hoy tu fruta divina\n"
        "Árbol de la vida dame hoy\n"
        "Dame hoy tu fruta divina"
    )  # 8 lines, real Spanish lyrics with accents
    # Cleaned: same content + 4 more chorus repeats (1.5x). Same
    # vocabulary, just more repetition — overlap stays ~1.0.
    cleaned = plain + "\n".join([
        "",
        "Árbol de la vida dame hoy",
        "Dame hoy tu fruta divina",
        "Árbol de la vida dame hoy",
        "Dame hoy tu fruta divina",
    ])
    _patch_genai_with(monkeypatch, _FakeResponse(cleaned))

    # Disable cache write to avoid touching the DB in tests
    monkeypatch.setattr(pipeline, "_gemini_cleanup_cache_write",
                        lambda *a, **k: None)
    monkeypatch.setattr(pipeline, "_gemini_cleanup_cache_lookup",
                        lambda *a, **k: None)

    out = pipeline._gemini_cleanup_lyrics(tiny_audio, plain)
    assert out == cleaned


# ─── Gemini failure modes ───────────────────────────────────────────

def test_gemini_exception_returns_none(tiny_audio, monkeypatch):
    """Any exception in the Gemini call → return None so caller falls
    back to lrclib raw. Never raises to the caller."""
    monkeypatch.setenv("GEMINI_LYRICS_CLEANUP_ENABLED", "1")
    fake_client = MagicMock()
    fake_client.models.generate_content.side_effect = RuntimeError("vertex 500")
    monkeypatch.setattr(pipeline, "_get_genai_client", lambda: fake_client)

    out = pipeline._gemini_cleanup_lyrics(tiny_audio, "a\nb\nc\nd")
    assert out is None


def test_gemini_empty_response_returns_none(tiny_audio, monkeypatch):
    """Safety-filter rejections come back as empty .text — fall back."""
    monkeypatch.setenv("GEMINI_LYRICS_CLEANUP_ENABLED", "1")
    _patch_genai_with(monkeypatch, _FakeResponse("", finish_reason="SAFETY"))

    out = pipeline._gemini_cleanup_lyrics(tiny_audio, "a\nb\nc\nd")
    assert out is None


# ─── Defense 1: refusal detection ───────────────────────────────────

def test_is_refusal_english():
    """Common Gemini refusal openers in English."""
    assert pipeline._gemini_cleanup_is_refusal(
        "I cannot provide the lyrics due to copyright concerns."
    )
    assert pipeline._gemini_cleanup_is_refusal(
        "As an AI language model, I am unable to transcribe..."
    )
    assert pipeline._gemini_cleanup_is_refusal("I'm sorry, I can't help with that.")


def test_is_refusal_spanish():
    """Spanish-localised refusals — Gemini sometimes responds in the
    requested language even when refusing."""
    assert pipeline._gemini_cleanup_is_refusal(
        "Lo siento, no puedo proporcionar las letras de esta canción."
    )
    assert pipeline._gemini_cleanup_is_refusal("No puedo ayudar con esa solicitud.")


def test_not_a_refusal_when_lyrics_use_similar_words():
    """A song lyric that happens to contain 'no puedo' early on is NOT
    a refusal — the marker check requires opener context, not just the
    phrase anywhere. Our markers like 'no puedo proporcionar' include
    'proporcionar' to avoid this false positive."""
    not_refusals = [
        "Después de tanto vagar por las calles",
        "No puedo dormir, no puedo comer",        # real lyric
        "Yo no puedo más, te dejo",                # another real lyric
        "Para mí, para mí, para mí",
    ]
    for text in not_refusals:
        assert not pipeline._gemini_cleanup_is_refusal(text), \
            f"False positive on real lyric: {text!r}"


def test_refusal_in_function_returns_none(tiny_audio, monkeypatch):
    """End-to-end: Gemini returns a refusal that passes line-count gate
    (it's short but >50% of a short input). Defense 1 must catch it."""
    monkeypatch.setenv("GEMINI_LYRICS_CLEANUP_ENABLED", "1")
    plain = "a\nb\nc"   # 3 lines
    refusal = "I cannot provide lyrics for copyrighted material.\nPlease consult..."
    _patch_genai_with(monkeypatch, _FakeResponse(refusal))
    monkeypatch.setattr(pipeline, "_gemini_cleanup_cache_write", lambda *a, **k: None)
    monkeypatch.setattr(pipeline, "_gemini_cleanup_cache_lookup", lambda *a, **k: None)
    assert pipeline._gemini_cleanup_lyrics(tiny_audio, plain) is None


# ─── Defense 2: preamble strip ──────────────────────────────────────

def test_strip_preamble_sure_here():
    """The most common Gemini preamble — strip the first line."""
    inp = "Sure, here are the corrected lyrics:\nDespués de tanto\nLa ciudad gris"
    out = pipeline._gemini_cleanup_strip_preamble(inp)
    assert out.startswith("Después")
    assert "Sure" not in out


def test_strip_preamble_spanish_variant():
    inp = "Aquí están las letras corregidas:\nDespués de tanto\nLa ciudad gris"
    out = pipeline._gemini_cleanup_strip_preamble(inp)
    assert out.startswith("Después")


def test_strip_preamble_preserves_lyrics_starting_with_words():
    """A real first line that happens to start with a common word
    must NOT be stripped. Only recognised preamble openers go."""
    inp = "Hace tiempo que no te veo\nDespués de tanto vagar"
    out = pipeline._gemini_cleanup_strip_preamble(inp)
    assert out == inp                                  # unchanged
    assert out.startswith("Hace tiempo")


def test_strip_preamble_handles_leading_blanks():
    """Preamble can come after blank lines (Gemini formatting quirk)."""
    inp = "\n\nSure, here are the lyrics:\nLetra primera línea\nSegunda línea"
    out = pipeline._gemini_cleanup_strip_preamble(inp)
    assert out.startswith("Letra")


def test_strip_preamble_idempotent():
    """Calling twice produces the same result as calling once."""
    inp = "Sure, here are the lyrics:\nLetra A\nLetra B"
    once = pipeline._gemini_cleanup_strip_preamble(inp)
    twice = pipeline._gemini_cleanup_strip_preamble(once)
    assert once == twice


# ─── Defense 3: language drift detection ────────────────────────────

def test_language_intact_when_both_spanish():
    plain = "Después de tanto vagar por las calles\nLa ciudad te parece tan gris"
    cleaned = "Después de tanto vagar por las calles\nLa ciudad te parece tan gris"
    assert pipeline._gemini_cleanup_language_intact(cleaned, plain)


def test_language_drift_when_translated_to_english():
    """The killer case: lrclib is Spanish, Gemini translated to English."""
    plain = "Después de tanto vagar por las calles\nLa ciudad te parece tan gris\nMejor hacerse un viaje al campo"
    translated = "After so much wandering through the streets\nThe city looks so grey to you\nBetter to take a trip to the country"
    assert not pipeline._gemini_cleanup_language_intact(translated, plain)


def test_language_intact_when_input_has_no_markers():
    """If input had no ñ/á/é/... (rare for Spanish but valid for some
    languages), we can't measure drift — return True and trust the
    other defenses."""
    plain = "no markers here\njust ascii"
    cleaned = "totally different\nbut also ascii"
    assert pipeline._gemini_cleanup_language_intact(cleaned, plain)


# ─── Defense 4: word overlap (hallucination floor) ──────────────────

def test_word_overlap_high_when_only_accents_fixed():
    """The happy case: Gemini added accents but kept all words. Overlap
    should be ~1.0 because normalisation strips accents anyway."""
    plain = "Para mi, para mi, para mi\nNo me gusta verte"
    cleaned = "Para mí, para mí, para mí\nNo me gusta verte"
    assert pipeline._gemini_cleanup_word_overlap(cleaned, plain) >= 0.9


def test_word_overlap_low_when_hallucinated():
    """Gemini invented totally different words — must score very low."""
    plain = "Después de tanto vagar por las calles\nLa ciudad gris"
    fake = "Mañana cantaré una nueva canción\nSobre el desierto rojo"
    overlap = pipeline._gemini_cleanup_word_overlap(fake, plain)
    assert overlap < 0.5


def test_line_grounding_rejects_one_translated_phrase_hidden_in_mixed_song():
    source = (
        "Después de tanto vagar por las calles\n"
        "Are you ready?\n"
        "La ciudad te parece tan gris"
    )
    translated = (
        "Después de tanto vagar por las calles\n"
        "Estoy listo\n"
        "La ciudad te parece tan gris"
    )

    # Song-level overlap remains high; the line-level provenance gate catches
    # the translated minority-language phrase.
    assert pipeline._gemini_cleanup_word_overlap(translated, source) >= 0.5
    assert not pipeline._gemini_cleanup_lines_grounded(translated, source)


def test_line_grounding_allows_local_correction_and_code_switch():
    source = "Are you ready?\nfragil corazón"
    corrected = "Are you ready?\nFrágil corazón"

    assert pipeline._gemini_cleanup_lines_grounded(corrected, source)


def test_lexical_anchor_cannot_be_faked_by_one_preserved_name_or_number():
    assert not pipeline._has_lexical_anchor(
        "¿Estás listo, Tom?", "Are you ready, Tom?",
    )
    assert not pipeline._has_lexical_anchor(
        "Estoy listo, 638", "Are you ready, 638",
    )
    assert not pipeline._has_lexical_anchor("No hay", "No way")
    assert pipeline._has_lexical_anchor(
        "Frágil corazón", "fragil corazon",
    )


def test_hallucination_in_function_returns_none(tiny_audio, monkeypatch):
    """End-to-end: Gemini returns lyrics-shaped but-unrelated text.
    Line count passes, language intact, but word overlap is too low."""
    monkeypatch.setenv("GEMINI_LYRICS_CLEANUP_ENABLED", "1")
    plain = ("Después de tanto vagar por las calles\n"
             "La ciudad te parece tan gris\n"
             "Mejor hacerse un viaje al campo\n"
             "Y sentirse libre para poder sentir")
    fake = ("Mañana cantaré una nueva canción\n"
            "Sobre el desierto rojo del olvido\n"
            "Donde la luna pinta sus colores\n"
            "Y yo recuerdo los días felices")
    _patch_genai_with(monkeypatch, _FakeResponse(fake))
    monkeypatch.setattr(pipeline, "_gemini_cleanup_cache_write", lambda *a, **k: None)
    monkeypatch.setattr(pipeline, "_gemini_cleanup_cache_lookup", lambda *a, **k: None)
    assert pipeline._gemini_cleanup_lyrics(tiny_audio, plain) is None


def test_translation_in_function_returns_none(tiny_audio, monkeypatch):
    """End-to-end: Gemini silently translated lrclib to English. Output
    passes line count + overlap-low-ish but language drift defense
    catches it (no Spanish markers)."""
    monkeypatch.setenv("GEMINI_LYRICS_CLEANUP_ENABLED", "1")
    plain = ("Después de tanto vagar por las calles\n"
             "La ciudad te parece tan gris")
    translated = ("After so much wandering through the streets\n"
                  "The city looks so grey to you")
    _patch_genai_with(monkeypatch, _FakeResponse(translated))
    monkeypatch.setattr(pipeline, "_gemini_cleanup_cache_write", lambda *a, **k: None)
    monkeypatch.setattr(pipeline, "_gemini_cleanup_cache_lookup", lambda *a, **k: None)
    assert pipeline._gemini_cleanup_lyrics(tiny_audio, plain) is None


def test_client_unavailable_returns_none(tiny_audio, monkeypatch):
    """If the Vertex client can't be built (creds missing in some envs),
    return None and let the caller use lrclib raw."""
    monkeypatch.setenv("GEMINI_LYRICS_CLEANUP_ENABLED", "1")

    def _raise():
        raise RuntimeError("creds missing")
    monkeypatch.setattr(pipeline, "_get_genai_client", _raise)

    out = pipeline._gemini_cleanup_lyrics(tiny_audio, "a\nb\nc\nd")
    assert out is None
