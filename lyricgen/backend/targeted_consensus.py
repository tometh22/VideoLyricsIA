"""Bounded second-pass ASR for unsafe lyric windows.

Only windows identified by :mod:`transcription_quality` are sent to the
second ASR.  A change is accepted when the isolated vocal stem agrees with
either the original mix or the primary-ASR word stream.  Doubt leaves the
original untouched and visible to the operator.
"""
from __future__ import annotations

from difflib import SequenceMatcher
import json
import logging
import math
import os
import subprocess
import tempfile
import time
import unicodedata

logger = logging.getLogger("genly.targeted_consensus")
_TRUE = {"1", "true", "yes", "on"}
_GEMINI_EVENT_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["events"],
    "properties": {
        "events": {
            "type": "array",
            "maxItems": 16,
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["start", "end", "text", "kind"],
                "properties": {
                    "start": {"type": "number", "minimum": 0},
                    "end": {"type": "number", "minimum": 0},
                    "text": {"type": "string"},
                    "kind": {
                        "type": "string",
                        "enum": ["sung", "vocalization", "speech"],
                    },
                },
            },
        },
    },
}


def is_enabled() -> bool:
    return os.environ.get("TARGETED_CONSENSUS_ENABLED", "0").strip().lower() in _TRUE


def _f(value, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _norm(text: str) -> str:
    text = unicodedata.normalize("NFD", (text or "").casefold())
    return " ".join(
        "".join(ch for ch in token if ch.isalnum())
        for token in text.split()
        if any(ch.isalnum() for ch in token)
    )


def _text(words: list[dict]) -> str:
    return " ".join(str(w.get("word") or "").strip() for w in words).strip()


def _similarity(left: str, right: str) -> float:
    a, b = _norm(left).split(), _norm(right).split()
    if not a or not b:
        return 0.0
    return SequenceMatcher(None, a, b).ratio()


def _words_in(words: list[dict], start: float, end: float, pad: float = 0.35) -> list[dict]:
    out = []
    for word in words or []:
        if not isinstance(word, dict):
            continue
        a, b = _f(word.get("start")), _f(word.get("end"))
        middle = (a + b) / 2 if b > a else a
        if start - pad <= middle <= end + pad:
            out.append(word)
    return sorted(out, key=lambda item: _f(item.get("start")))


def choose_consensus(stem_words: list[dict], mix_words: list[dict],
                     primary_words: list[dict], *,
                     slowed_words: list[dict] | None = None,
                     witness_words: list[dict] | None = None,
                     threshold: float = 0.72):
    """Return the agreed word stream and evidence, or ``(None, ...)``.

    Isolated-vocal participation (normal or slowed) is mandatory. This
    prevents two noisy full-mix streams from voting backing-instrument
    hallucinations into the lyrics.
    """
    streams = {
        "stem": stem_words, "slowed_stem": slowed_words or [],
        "mix": mix_words, "primary": primary_words,
        "witness": witness_words or [],
    }
    texts = {name: _text(words) for name, words in streams.items()}
    eligible = {
        name: text for name, text in texts.items()
        if len(_norm(text).split()) >= 2
    }
    # An isolated-vocal candidate (normal OR slowed) must agree exactly with
    # an independent representation (mix or primary provider). Agreement
    # between normal+slowed stems alone is useful evidence but never enough to
    # alter content: both came from the same source/model family.
    best = None
    for isolated in ("stem", "slowed_stem"):
        for independent in ("primary", "witness", "mix"):
            if isolated not in eligible or independent not in eligible:
                continue
            similarity = (
                1.0
                if _norm(eligible[isolated]) == _norm(eligible[independent])
                else 0.0
            )
            candidate = (similarity, isolated, independent)
            if best is None or similarity > best[0]:
                best = candidate
    evidence = {"texts": texts, "agreement": round(best[0], 3) if best else 0.0}
    if not best or best[0] < threshold:
        return None, evidence
    evidence["sources"] = [best[1], best[2]]
    return streams[best[1]], evidence


def _transcribe_slowed_window(stem_path: str, start: float, duration: float,
                              speed: float, language: str | None, job_id: str,
                              transcribe_fn) -> list[dict]:
    """ASR a pitch-preserving slowed clip and map words to original time."""
    fd, clip = tempfile.mkstemp(prefix="genly_slow_stem_", suffix=".wav")
    os.close(fd)
    try:
        subprocess.run(
            [
                "ffmpeg", "-y", "-ss", str(start), "-t", str(duration),
                "-i", stem_path, "-filter:a", f"atempo={speed:.4f}",
                "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le",
                "-loglevel", "error", clip,
            ],
            check=True, timeout=90,
        )
        slowed_duration = duration / speed
        words = transcribe_fn(
            clip, 0.0, slowed_duration, language or None, job_id or None,
        )
        mapped = []
        for word in words or []:
            try:
                mapped.append({
                    **dict(word),
                    "start": round(start + _f(word.get("start")) * speed, 3),
                    "end": round(start + _f(word.get("end")) * speed, 3),
                })
            except (TypeError, ValueError):
                continue
        return mapped
    finally:
        try:
            os.unlink(clip)
        except OSError:
            pass


def _transcribe_gemini_events(audio_path: str, start: float, duration: float,
                              language: str | None, job_id: str) -> list[dict]:
    """Blindly transcribe bounded vocal events with a non-Whisper model.

    Gemini receives no artist, title, catalogue, current text, or expected
    repetition count. Its timestamps are used only to match event cardinality;
    accepted lyric timing always comes from the slowed Whisper word stream.
    """
    fd, clip = tempfile.mkstemp(prefix="genly_gemini_vocal_", suffix=".wav")
    os.close(fd)
    recorder = None
    prompt = (
        "Transcribí únicamente los eventos vocales audibles en este fragmento. "
        "Cada evento debe ser una frase o ciclo vocal completo: no dividas una "
        "frase en palabras o sílabas. Conservá cada repetición real como un "
        "evento separado. No describas instrumentos, música ni silencios. No completes "
        "frases conocidas, no inventes repeticiones y no uses conocimiento de "
        "la canción. Clasificá cada evento como sung, vocalization o speech. "
        "Devolvé como máximo 16 eventos. Los tiempos son segundos "
        "relativos al inicio del fragmento. "
        "Si no hay voz, devolvé events vacío. Respondé sólo JSON con la forma "
        '{"events":[{"start":0.0,"end":1.0,"text":"...",'
        '"kind":"sung"}]}.'
    )
    try:
        subprocess.run(
            [
                "ffmpeg", "-y", "-ss", str(start), "-t", str(duration),
                "-i", audio_path, "-ac", "1", "-ar", "16000",
                "-c:a", "pcm_s16le", "-loglevel", "error", clip,
            ],
            check=True, timeout=90,
        )
        if not os.path.exists(clip) or os.path.getsize(clip) == 0:
            return []
        from google import genai
        from pipeline import _call_with_timeout, _get_genai_client
        client = _get_genai_client()
        if client is None:
            return []
        if job_id:
            from provenance import record_ai_call
            recorder = record_ai_call(
                job_id=job_id,
                step="targeted_gemini_verify",
                tool_name="gemini-2.5-flash-audio",
                tool_provider="google_vertex",
                prompt=(
                    f"Blind vocal event transcription start={start:.2f}s "
                    f"duration={duration:.2f}s language={language or 'auto'}; "
                    + prompt
                ),
                input_data_types=["original_mix_audio_clip"],
            )
        with open(clip, "rb") as handle:
            audio_bytes = handle.read()
        response = _call_with_timeout(
            lambda: client.models.generate_content(
                model="gemini-2.5-flash",
                contents=[
                    genai.types.Part.from_bytes(
                        data=audio_bytes, mime_type="audio/wav",
                    ),
                    genai.types.Part.from_text(
                        text="Transcribí este fragmento vocal corto.",
                    ),
                ],
                config=genai.types.GenerateContentConfig(
                    system_instruction=prompt,
                    temperature=0.0,
                    max_output_tokens=4096,
                    response_mime_type="application/json",
                    response_json_schema=_GEMINI_EVENT_SCHEMA,
                    thinking_config=genai.types.ThinkingConfig(
                        thinking_budget=0,
                    ),
                ),
            ),
            timeout_s=60.0,
            label="TARGETED-GEMINI-VERIFY",
        )
        raw = (response.text or "").strip()
        payload = json.loads(raw)
        events = payload.get("events") if isinstance(payload, dict) else None
        if not isinstance(events, list) or len(events) > 16:
            return []
        out = []
        from pipeline import _is_whisper_hallucination
        for event in events:
            if not isinstance(event, dict):
                continue
            text = str(event.get("text") or "").strip()
            kind = str(event.get("kind") or "sung").strip().lower()
            try:
                rel_start = float(event.get("start"))
                rel_end = float(event.get("end", rel_start))
            except (TypeError, ValueError):
                continue
            if not (
                text and len(text) <= 160
                and kind in {"sung", "vocalization", "speech"}
                and math.isfinite(rel_start) and math.isfinite(rel_end)
                and 0.0 <= rel_start <= duration + 0.5
                and rel_start <= rel_end <= duration + 1.0
                and not _is_whisper_hallucination(text)
            ):
                continue
            out.append({
                "start": round(start + rel_start, 3),
                "end": round(start + rel_end, 3),
                "text": text,
                "kind": kind,
            })
        out.sort(key=lambda event: event["start"])
        if recorder:
            recorder.finish(response_summary=f"events={len(out)}")
        return out
    except Exception as exc:
        if recorder:
            recorder.finish(
                response_summary=f"error:{type(exc).__name__}",
            )
        logger.warning(
            "[TARGETED-GEMINI] declined: %r job=%s", exc, job_id,
        )
        return []
    finally:
        try:
            os.unlink(clip)
        except OSError:
            pass


def _word_tokens(text: str) -> list[str]:
    return _norm(text).split()


_VOCALIZATION_TOKENS = {
    "ah", "aha", "eh", "hey", "oh", "ooh", "oooh", "uh", "uoh",
    "uoo", "uou", "woah", "wow", "yeah",
}


def _canonicalize_cycle_vocalizations(cycles: list[dict],
                                      targets: list[tuple[int, dict]],
                                      motif: str) -> list[dict]:
    """Use catalogue text only to normalize equivalent ad-lib spelling.

    Acoustic/Gemini evidence still owns cardinality and timing.  A unanimous
    structural suggestion may replace ``Oh Oh`` with ``uoo uou`` only when the
    lexical opening, number of parts, and every trailing token agree as
    non-lexical vocalizations.  Catalogue text can never add a word or cycle.
    """
    suggestions = []
    for _index, target in targets:
        tokens = _word_tokens(str(target.get("live_structural_suggestion") or ""))
        if (
            len(tokens) >= 2 and tokens[0] == motif
            and all(token in _VOCALIZATION_TOKENS for token in tokens[1:])
        ):
            suggestions.append(tokens)
    if not suggestions:
        return [dict(cycle) for cycle in cycles]
    counts = {}
    for tokens in suggestions:
        key = tuple(tokens)
        counts[key] = counts.get(key, 0) + 1
    canonical, support = max(counts.items(), key=lambda item: (item[1], item[0]))
    if support != len(suggestions):
        return [dict(cycle) for cycle in cycles]
    out = []
    for cycle in cycles:
        tokens = _word_tokens(str(cycle.get("text") or ""))
        if (
            len(tokens) != len(canonical) or not tokens
            or tokens[0] != motif
            or any(token not in _VOCALIZATION_TOKENS for token in tokens[1:])
        ):
            return [dict(original) for original in cycles]
        updated = dict(cycle)
        opening = str(cycle.get("text") or "").strip().split()[0]
        updated["text"] = " ".join([opening, *canonical[1:]])
        updated["vocalization_spelling_source"] = "catalogue_consensus"
        out.append(updated)
    return out


def _event_groups(words: list[dict]) -> list[list[dict]]:
    try:
        from gap_rescue import _agrupar_en_lineas
        return _agrupar_en_lineas(words)
    except Exception:
        return []


def _event_supported(group: list[dict], support_words: list[dict]) -> bool:
    tokens = set(_word_tokens(_text(group)))
    if not tokens:
        return False
    start = _f(group[0].get("start"))
    end = _f(group[-1].get("end"))
    support = _words_in(support_words, start, end, pad=0.9)
    return bool(tokens.intersection(_word_tokens(_text(support))))


def _gemini_cycles(events: list[dict], motif: str) -> list[dict]:
    """Collapse ``motif, ad-lib, ad-lib`` event streams into full cycles.

    Gemini sometimes obeys the complete-cycle prompt and sometimes emits the
    vocalizations as separate events.  The opening token comes from the
    already-flagged structural rows; neither repeat count nor wording comes
    from catalogue metadata.
    """
    vocal = sorted(
        (
            dict(event) for event in (events or [])
            if isinstance(event, dict)
            and event.get("kind") in {"sung", "vocalization"}
            and str(event.get("text") or "").strip()
        ),
        key=lambda event: _f(event.get("start")),
    )
    anchors = [
        index for index, event in enumerate(vocal)
        if (_word_tokens(str(event.get("text") or "")) or [""])[0] == motif
    ]
    if not 2 <= len(anchors) <= 8:
        return []
    complete_lengths = [
        anchors[position + 1] - index
        for position, index in enumerate(anchors[:-1])
    ]
    typical_parts = sorted(complete_lengths)[len(complete_lengths) // 2]
    if not 1 <= typical_parts <= 5:
        return []
    cycles = []
    for position, index in enumerate(anchors):
        next_index = anchors[position + 1] if position + 1 < len(anchors) else len(vocal)
        # The tail may contain a separate "no"/crowd response after the last
        # repeated cycle.  Give the last cycle the cardinality learned from
        # the preceding complete cycles instead of swallowing every trailing
        # vocal event in the bounded clip.
        stop = min(next_index, index + typical_parts)
        parts = vocal[index:stop]
        if len(parts) != typical_parts:
            return []
        text = " ".join(str(part.get("text") or "").strip() for part in parts).strip()
        cycles.append({
            "start": _f(parts[0].get("start")),
            "end": max(_f(part.get("end")) for part in parts),
            "text": text,
            "kind": "sung",
        })
    canonical = str(cycles[0].get("text") or "")
    if any(_similarity(canonical, str(cycle.get("text") or "")) < 0.60
           for cycle in cycles[1:]):
        return []
    return cycles


def _repair_structural_repetition(
    segments: list[dict], slowed_words: list[dict], gemini_events: list[dict],
    support_words: list[dict], *, window_start: float, window_end: float,
    enforce: bool, hybrid_enabled: bool = False,
    hybrid_fn=None, stem_path: str = "", mix_path: str = "", job_id: str = "",
) -> tuple[list[dict], dict]:
    """Replace a malformed repeated motif only with two-model cardinality.

    Structural catalogue metadata chooses the *window and opening token only*;
    neither its wording nor repetition count is used in the candidate. Gemini
    supplies independent content/cardinality and slowed Whisper supplies the
    final timings.
    """
    stats = {
        "attempted": False, "applied": False, "suggested": False,
        "reason": "no_structural_targets", "events": 0,
        "targets_removed": 0, "hybrid_attempted": False,
        "hybrid_accepted": False,
    }
    targets = [
        (index, segment)
        for index, segment in enumerate(segments or [])
        if isinstance(segment, dict)
        and segment.get("live_structural_suggestion")
        and _f(segment.get("start")) <= window_end
        and _f(segment.get("end")) >= window_start
    ]
    if not targets:
        return segments, stats
    stats["attempted"] = True
    openings = [
        (_word_tokens(str(segment.get("text") or "")) or [""])[0]
        for _index, segment in targets
    ]
    motif = max(set(openings), key=openings.count)
    if not motif:
        stats["reason"] = "no_motif"
        return segments, stats

    slow_groups = [
        group for group in _event_groups(slowed_words)
        if group and (_word_tokens(_text(group)) or [""])[0] == motif
        and _safe_line(group)
    ]
    gemini = _gemini_cycles(gemini_events, motif)
    gemini = _canonicalize_cycle_vocalizations(gemini, targets, motif)

    candidate_events = []
    hybrid_verified = False
    if hybrid_enabled:
        stats["hybrid_attempted"] = True
        if not gemini or not stem_path or not mix_path:
            stats["reason"] = "hybrid_missing_evidence"
            return segments, stats
        if hybrid_fn is None:
            from structural_hybrid import verify as hybrid_fn
        verdict = hybrid_fn(
            stem_path, mix_path, gemini,
            window_start=window_start, window_end=window_end, job_id=job_id,
        ) or {}
        stats["hybrid"] = verdict
        if not verdict.get("accepted"):
            stats["reason"] = f"hybrid_{verdict.get('reason') or 'declined'}"
            return segments, stats
        candidate_events = [dict(event) for event in (verdict.get("events") or [])]
        if len(candidate_events) != len(gemini):
            stats["reason"] = "hybrid_cardinality_disagreement"
            return segments, stats
        hybrid_verified = True
        stats["hybrid_accepted"] = True
    elif not (2 <= len(slow_groups) == len(gemini) <= 8):
        stats["reason"] = "cardinality_disagreement"
        return segments, stats

    if not hybrid_verified:
        accepted: list[tuple[list[dict], dict]] = []
        gemini_pos = 0
        for group in slow_groups:
            group_text = _text(group)
            matched = None
            while gemini_pos < len(gemini):
                event = gemini[gemini_pos]
                gemini_pos += 1
                left = _word_tokens(group_text)
                right = _word_tokens(str(event.get("text") or ""))
                similarity = _similarity(group_text, str(event.get("text") or ""))
                threshold = 0.92 if max(len(left), len(right)) <= 3 else 0.82
                onset_ok = abs(
                    _f(group[0].get("start")) - _f(event.get("start"))
                ) <= 0.75
                short_exact = max(len(left), len(right)) <= 3 and left == right
                if onset_ok and (short_exact or similarity >= threshold):
                    matched = event
                    break
            if matched is None or not _event_supported(group, support_words):
                stats["reason"] = "event_disagreement"
                return segments, stats
            accepted.append((group, matched))

        previous_end = None
        for group, event in accepted:
            start = _f(group[0].get("start"))
            end = _f(group[-1].get("end"))
            if not (
                math.isfinite(start) and math.isfinite(end) and end > start
                and window_start - 0.75 <= start <= window_end + 0.75
                and window_start - 0.75 <= end <= window_end + 0.75
                and (previous_end is None or start >= previous_end - 0.20)
            ):
                stats["reason"] = "invalid_event_timing"
                return segments, stats
            previous_end = end
            candidate_events.append({
                "start": round(start, 3), "end": round(end, 3),
                "text": str(event.get("text") or _text(group)).strip(),
                "words": [dict(word) for word in group],
                "review": True,
                "consensus_reprocessed": True,
                "consensus_sources": ["slowed_stem_whisper_1", "gemini_audio"],
                "structural_repair": True,
            })

    # Patch only individually corroborated rows. An unmatched existing event
    # is ambiguous, not disproven; deleting it would turn model absence into
    # evidence. Extra verified events may be inserted only where they do not
    # overlap any retained row.
    if hybrid_verified:
        target_indices = {index for index, _segment in targets}
        candidate = [
            dict(segment) for index, segment in enumerate(segments or [])
            if index not in target_indices
        ]
        if any(
            not _non_overlapping(event["start"], event["end"], candidate)
            for event in candidate_events
        ):
            stats["reason"] = "hybrid_retained_overlap"
            return segments, stats
        candidate.extend(candidate_events)
        candidate.sort(key=lambda segment: _f(segment.get("start")))
        stats.update({
            "suggested": True, "reason": "hybrid_verified",
            "events": len(candidate_events),
            "targets_removed": len(target_indices),
            "events_inserted": len(candidate_events),
        })
        if not enforce:
            return segments, stats
        stats["applied"] = True
        return candidate, stats

    candidate = [dict(segment) for segment in (segments or [])]
    available_targets = {index for index, _segment in targets}
    matched_targets: set[int] = set()
    inserted = 0
    for event in candidate_events:
        best_index = None
        best_overlap = 0.0
        for index in available_targets:
            current = candidate[index]
            overlap = max(
                0.0,
                min(event["end"], _f(current.get("end")))
                - max(event["start"], _f(current.get("start"))),
            )
            if overlap > best_overlap:
                best_overlap = overlap
                best_index = index
        if best_index is not None and best_overlap >= 0.20:
            candidate[best_index] = event
            available_targets.remove(best_index)
            matched_targets.add(best_index)
            continue
        if _non_overlapping(event["start"], event["end"], candidate):
            candidate.append(event)
            inserted += 1
    if not matched_targets and not inserted:
        stats["reason"] = "no_safe_row_operations"
        return segments, stats
    candidate.sort(key=lambda segment: _f(segment.get("start")))
    stats.update({
        "suggested": True, "reason": "verified",
        "events": len(candidate_events),
        "targets_removed": len(matched_targets),
        "events_inserted": inserted,
    })
    if not enforce:
        return segments, stats
    stats["applied"] = True
    return candidate, stats


def _physical_line(words: list[dict]) -> bool:
    if len(words) < 2:
        return False
    duration = _f(words[-1].get("end")) - _f(words[0].get("start"))
    return duration >= 0.45 and len(words) / max(duration, 0.01) <= 6.0


def _safe_line(words: list[dict]) -> bool:
    if not _physical_line(words):
        return False
    try:
        from gap_rescue import _texto_sospechoso
        return not _texto_sospechoso(_text(words))
    except Exception:
        return bool(_norm(_text(words)))


def _non_overlapping(start: float, end: float, segments: list[dict]) -> bool:
    return all(
        min(end, _f(seg.get("end"))) - max(start, _f(seg.get("start"))) <= 0.20
        for seg in segments if isinstance(seg, dict)
    )


def _env_int(name: str, default: int) -> int:
    try:
        return max(0, int(os.environ.get(name, str(default))))
    except (TypeError, ValueError):
        return default


def _env_float(name: str, default: float) -> float:
    try:
        return max(0.0, float(os.environ.get(name, str(default))))
    except (TypeError, ValueError):
        return default


def reprocess(result: dict, audio_path: str, windows: list[dict], *,
              language: str = "", job_id: str = "",
              transcribe_fn=None, gemini_fn=None,
              stem_path: str | None = None,
              hybrid_fn=None) -> tuple[dict, dict]:
    """Apply a cost-capped consensus pass.  Never raises."""
    owned_stem = None
    stats = {
        "attempted": False, "windows_considered": len(windows or []),
        "windows_processed": 0, "asr_calls": 0, "audio_seconds_billed": 0.0,
        "lines_replaced": 0, "lines_inserted": 0, "truncated_windows": 0,
        "lines_suggested": 0, "provider_attempts": 0,
        "submitted_audio_seconds": 0.0, "declined": [],
        "slowed_asr_calls": 0, "slowed_audio_seconds": 0.0,
        "gemini_calls": 0, "gemini_audio_seconds": 0.0,
        "structural_repairs": 0, "structural_events": 0,
        "structural_hybrid_attempts": 0, "structural_hybrid_accepts": 0,
        "structural_hybrid_declined": [],
        "structural_hybrid_diagnostics": [],
    }
    if not isinstance(result, dict) or not windows or not audio_path:
        stats["declined"].append("no_windows")
        return result, stats
    try:
        if transcribe_fn is None:
            from gap_rescue import _transcribe_window
            def transcribe_fn(path, start, duration, lang, current_job_id):
                return _transcribe_window(
                    path, start, duration, lang, current_job_id,
                    provenance_step="targeted_consensus",
                )
        if not stem_path:
            import vocal_sep
            stem_path = vocal_sep.separate_vocals(audio_path, cache_only=True)
            owned_stem = stem_path
        if not stem_path:
            stats["declined"].append("no_stem")
            return result, stats

        # Environment values may tune downward but can never remove the hard
        # operational ceiling.
        max_windows = min(4, _env_int("TARGETED_CONSENSUS_MAX_WINDOWS", 3))
        configured_billed_s = min(
            180.0,
            _env_float("TARGETED_CONSENSUS_MAX_BILLED_SECONDS", 120.0),
        )
        job_asr_budget = min(
            600.0, _env_float("LIVE_ASR_MAX_BILLED_SECONDS", 600.0),
        )
        prior_billed_s = _f(
            ((result.get("postpass_stats") or {}).get("word_vote") or {})
            .get("audio_seconds_billed")
        )
        max_billed_s = min(
            configured_billed_s, max(0.0, job_asr_budget - prior_billed_s),
        )
        stats["job_asr_budget_seconds"] = job_asr_budget
        stats["prior_audio_seconds_billed"] = round(prior_billed_s, 2)
        max_clip_s = min(45.0, _env_float("TARGETED_CONSENSUS_MAX_CLIP_SECONDS", 40.0))
        primary = result.get("_asr_words") or []
        independent_witness = result.get("_independent_asr_words") or []
        segments = [dict(s) for s in (result.get("segments") or [])]
        stats["attempted"] = True
        started_at = time.monotonic()
        hard_deadline_s = min(
            150.0, _env_float("TARGETED_CONSENSUS_DEADLINE_SECONDS", 120.0)
        )
        allow_insertions = (
            os.environ.get("TRANSCRIPTION_QUALITY_MODE", "observe").strip().lower()
            == "enforce"
        )
        slow_enabled = (
            os.environ.get("TARGETED_SLOW_STEM_ENABLED", "0")
            .strip().lower() in _TRUE
        )
        slow_speed = min(
            0.95, max(0.80, _env_float("TARGETED_SLOW_STEM_SPEED", 0.88))
        )
        gemini_enabled = (
            os.environ.get("TARGETED_GEMINI_VERIFY_ENABLED", "0")
            .strip().lower() in _TRUE
        )
        structural_autorepair_enabled = (
            os.environ.get("TARGETED_STRUCTURAL_AUTOREPAIR_ENABLED", "0")
            .strip().lower() in _TRUE
        )
        structural_hybrid_enabled = (
            os.environ.get("TARGETED_ACOUSTIC_CTC_ENABLED", "0")
            .strip().lower() in _TRUE
        )
        if gemini_fn is None:
            gemini_fn = _transcribe_gemini_events

        priority = {
            "voiced_gap": 0, "uncovered_asr": 1,
            "independent_uncovered_asr": 1,
            "live_lexical_unverified": 2,
            "live_structural_disagreement": 2,
            "independent_text_mismatch": 3, "text_mismatch": 4,
        }
        ordered_windows = sorted(
            windows,
            key=lambda item: min(
                (priority.get(reason, 9) for reason in (item.get("reasons") or [])),
                default=9,
            ),
        )
        for window in ordered_windows[:max_windows]:
            if time.monotonic() - started_at >= hard_deadline_s:
                stats["declined"].append("stage_deadline")
                break
            start, end = _f(window.get("start")), _f(window.get("end"))
            reasons = set(window.get("reasons") or [])
            needs_gemini = bool(
                gemini_enabled
                and "live_structural_disagreement" in reasons
            )
            duration = min(max_clip_s, max(0.0, end - start))
            if end - start > max_clip_s:
                stats["truncated_windows"] += 1
                stats["declined"].append("window_truncated")
            slow_duration = duration / slow_speed if slow_enabled else 0.0
            gemini_duration = duration if needs_gemini else 0.0
            window_cost = 2 * duration + slow_duration + gemini_duration
            if duration <= 0 or stats["audio_seconds_billed"] + window_cost > max_billed_s:
                stats["declined"].append("cost_budget")
                break
            stats["asr_calls"] += 1
            stats["provider_attempts"] += 1
            stats["submitted_audio_seconds"] = round(
                stats["submitted_audio_seconds"] + duration, 2
            )
            stem_words = transcribe_fn(stem_path, start, duration, language or None, job_id or None)
            stats["audio_seconds_billed"] = round(
                stats["audio_seconds_billed"] + duration, 2
            )
            slowed_words = []
            if slow_enabled:
                stats["asr_calls"] += 1
                stats["provider_attempts"] += 1
                stats["slowed_asr_calls"] += 1
                stats["submitted_audio_seconds"] = round(
                    stats["submitted_audio_seconds"] + slow_duration, 2
                )
                try:
                    slowed_words = _transcribe_slowed_window(
                        stem_path, start, duration, slow_speed,
                        language, job_id, transcribe_fn,
                    )
                    stats["slowed_audio_seconds"] = round(
                        stats["slowed_audio_seconds"] + slow_duration, 2
                    )
                except Exception as exc:
                    logger.warning(
                        "[TARGETED-CONSENSUS] slowed stem declined: %r job=%s",
                        exc, job_id,
                    )
                    stats["declined"].append(
                        f"slowed_exception:{type(exc).__name__}"
                    )
                finally:
                    # Conservative accounting: once the variant is submitted,
                    # count its full clip even if the provider returns no words.
                    stats["audio_seconds_billed"] = round(
                        stats["audio_seconds_billed"] + slow_duration, 2
                    )
            if time.monotonic() - started_at >= hard_deadline_s:
                stats["declined"].append("stage_deadline_after_stem")
                break
            stats["asr_calls"] += 1
            stats["provider_attempts"] += 1
            stats["submitted_audio_seconds"] = round(
                stats["submitted_audio_seconds"] + duration, 2
            )
            mix_words = transcribe_fn(audio_path, start, duration, language or None, job_id or None)
            gemini_events = []
            if needs_gemini:
                stats["provider_attempts"] += 1
                stats["gemini_calls"] += 1
                stats["submitted_audio_seconds"] = round(
                    stats["submitted_audio_seconds"] + duration, 2
                )
                try:
                    gemini_events = gemini_fn(
                        audio_path, start, duration, language or None, job_id,
                    ) or []
                except Exception as exc:
                    logger.warning(
                        "[TARGETED-GEMINI] wrapper declined: %r job=%s",
                        exc, job_id,
                    )
                    stats["declined"].append(
                        f"gemini_exception:{type(exc).__name__}"
                    )
                finally:
                    stats["audio_seconds_billed"] = round(
                        stats["audio_seconds_billed"] + duration, 2
                    )
                    stats["gemini_audio_seconds"] = round(
                        stats["gemini_audio_seconds"] + duration, 2
                    )
            logger.info(
                "[TARGETED-CONSENSUS] evidence %.1f-%.1f stem=%r slow=%r "
                "mix=%r primary=%r witness=%r gemini=%r job=%s",
                start, end, _text(stem_words)[:240],
                _text(slowed_words)[:240], _text(mix_words)[:240],
                _text(_words_in(primary, start, end, pad=0.6))[:240],
                _text(_words_in(independent_witness, start, end, pad=0.6))[:240],
                [event.get("text") for event in gemini_events[:12]],
                job_id,
            )
            stats["windows_processed"] += 1
            stats["audio_seconds_billed"] = round(
                stats["audio_seconds_billed"] + duration, 2
            )

            if (
                "live_structural_disagreement" in reasons
                and needs_gemini
                and (slow_enabled or structural_hybrid_enabled)
            ):
                repaired, repair_stats = _repair_structural_repetition(
                    segments, slowed_words, gemini_events,
                    list(primary) + list(mix_words),
                    window_start=start, window_end=start + duration,
                    enforce=(
                        allow_insertions and structural_autorepair_enabled
                    ),
                    hybrid_enabled=structural_hybrid_enabled,
                    hybrid_fn=hybrid_fn,
                    stem_path=stem_path,
                    mix_path=audio_path,
                    job_id=job_id,
                )
                if repair_stats.get("hybrid_attempted"):
                    stats["structural_hybrid_attempts"] += 1
                if repair_stats.get("hybrid_accepted"):
                    stats["structural_hybrid_accepts"] += 1
                elif repair_stats.get("hybrid_attempted"):
                    reason = str(repair_stats.get("reason") or "declined")[:120]
                    stats["structural_hybrid_declined"].append(reason)
                if repair_stats.get("hybrid_attempted"):
                    hybrid = repair_stats.get("hybrid") or {}
                    selected = next((
                        candidate for candidate in hybrid.get("scored_hypotheses") or []
                        if candidate.get("viable")
                    ), {})
                    diagnostic = {
                        "window": [round(start, 3), round(start + duration, 3)],
                        "accepted": bool(repair_stats.get("hybrid_accepted")),
                        "reason": str(repair_stats.get("reason") or "declined")[:120],
                        "viable_hypotheses": int(hybrid.get("viable_hypotheses") or 0),
                        "phase_margin": hybrid.get("phase_margin"),
                        "max_phase_delta": selected.get("max_phase_delta"),
                        "median_phase_delta": selected.get("median_phase_delta"),
                        "starts": [
                            round(_f(event.get("start")), 3)
                            for event in hybrid.get("events") or []
                        ],
                    }
                    stats["structural_hybrid_diagnostics"].append(diagnostic)
                    logger.info(
                        "[STRUCTURAL-HYBRID] job=%s window=%.2f-%.2f "
                        "accepted=%s reason=%s events=%s viable=%s "
                        "margin=%s phase_max=%s phase_median=%s starts=%s",
                        job_id, start, start + duration,
                        bool(repair_stats.get("hybrid_accepted")),
                        repair_stats.get("reason"),
                        repair_stats.get("events", 0),
                        diagnostic["viable_hypotheses"],
                        diagnostic["phase_margin"],
                        diagnostic["max_phase_delta"],
                        diagnostic["median_phase_delta"],
                        diagnostic["starts"],
                    )
                if repair_stats.get("suggested"):
                    stats["lines_suggested"] += repair_stats.get("events", 0)
                if repair_stats.get("applied"):
                    removed = int(repair_stats.get("targets_removed") or 0)
                    events = int(repair_stats.get("events") or 0)
                    segments = repaired
                    stats["structural_repairs"] += 1
                    stats["structural_events"] += events
                    stats["lines_replaced"] += removed
                    stats["lines_inserted"] += max(0, events - removed)
                    continue

            target_indices = [
                i for i in (window.get("segment_indices") or [])
                if isinstance(i, int) and 0 <= i < len(segments)
            ]
            for index in target_indices:
                current = segments[index]
                a, b = _f(current.get("start")), _f(current.get("end"))
                sw = _words_in(stem_words, a, b)
                mw = _words_in(mix_words, a, b)
                pw = _words_in(primary, a, b)
                iw = _words_in(independent_witness, a, b)
                sloww = _words_in(slowed_words, a, b)
                agreed, evidence = choose_consensus(
                    sw, mw, pw, slowed_words=sloww, witness_words=iw,
                )
                if not agreed or not _safe_line(agreed):
                    continue
                candidate = _text(agreed)
                # Do not rewrite spelling/punctuation when the current line
                # already substantially agrees with the audio.
                if _similarity(candidate, str(current.get("text") or "")) >= 0.62:
                    continue
                # A mismatch can mean correct lyrics at the wrong timestamp.
                # Replacing the text here would destroy that lyric and create
                # a duplicate of whatever happens to sing at this time. Keep
                # the original immutable and persist a review suggestion.
                current["consensus_suggestion"] = candidate
                current["review"] = True
                current["consensus_sources"] = evidence.get("sources", [])
                stats["lines_suggested"] += 1

            # No target index means a genuine uncovered/voiced gap.  Insert
            # only stem lines independently corroborated by the mix.
            if reasons & {
                "uncovered_asr", "independent_uncovered_asr", "voiced_gap",
            }:
                from gap_rescue import _agrupar_en_lineas
                isolated_groups = [
                    ("stem", group)
                    for group in _agrupar_en_lineas(stem_words)
                ] + [
                    ("slowed_stem", group)
                    for group in _agrupar_en_lineas(slowed_words)
                ]
                seen_groups = set()
                for isolated_source, group in isolated_groups:
                    if not _safe_line(group):
                        continue
                    a, b = _f(group[0].get("start")), _f(group[-1].get("end"))
                    group_key = (round(a, 1), round(b, 1), _norm(_text(group)))
                    if group_key in seen_groups:
                        continue
                    seen_groups.add(group_key)
                    mix_group = _words_in(mix_words, a, b, pad=0.6)
                    primary_group = _words_in(primary, a, b, pad=0.6)
                    witness_group = _words_in(
                        independent_witness, a, b, pad=0.6,
                    )
                    slow_group = _words_in(slowed_words, a, b, pad=0.6)
                    normal_group = _words_in(stem_words, a, b, pad=0.6)
                    is_slow_group = isolated_source == "slowed_stem"
                    agreed, _evidence = choose_consensus(
                        normal_group if is_slow_group else group,
                        mix_group, primary_group,
                        slowed_words=group if is_slow_group else slow_group,
                        witness_words=witness_group,
                    )
                    if not agreed:
                        continue
                    agreed_a = _f(agreed[0].get("start"))
                    agreed_b = _f(agreed[-1].get("end"))
                    if not (
                        math.isfinite(agreed_a) and math.isfinite(agreed_b)
                        and agreed_b > agreed_a
                        and start - 0.75 <= agreed_a <= end + 0.75
                        and start - 0.75 <= agreed_b <= end + 0.75
                    ):
                        stats["declined"].append("invalid_agreed_timing")
                        continue
                    if not _non_overlapping(agreed_a, agreed_b, segments):
                        continue
                    sources = set(_evidence.get("sources") or [])
                    cross_model = (
                        bool(result.get("live_audio_truth"))
                        and "primary" in sources
                    )
                    if not allow_insertions or not cross_model:
                        # Observe mode measures how often consensus could help
                        # but cannot mutate the delivered lyric. Auto-insertions
                        # are enabled only together with a blocking review gate.
                        stats["lines_suggested"] += 1
                        continue
                    segments.append({
                        "start": round(agreed_a, 3),
                        "end": round(agreed_b, 3),
                        "text": _text(agreed), "review": True,
                        "words": [dict(word) for word in agreed],
                        "consensus_reprocessed": True,
                        "consensus_sources": _evidence.get("sources", []),
                    })
                    stats["lines_inserted"] += 1

        output = dict(result)
        output["segments"] = sorted(segments, key=lambda s: _f(s.get("start")))
        output.setdefault("postpass_stats", {})["targeted_consensus"] = stats
        return output, stats
    except Exception as exc:  # never break the transcription cascade
        logger.warning("[TARGETED-CONSENSUS] declined: %r job=%s", exc, job_id)
        stats["declined"].append(f"exception:{type(exc).__name__}")
        return result, stats
    finally:
        if owned_stem:
            try:
                os.unlink(owned_stem)
            except OSError:
                pass
