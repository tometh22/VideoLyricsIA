"""Bounded second-pass ASR for unsafe lyric windows.

Only windows identified by :mod:`transcription_quality` are sent to the
second ASR.  A change is accepted when the isolated vocal stem agrees with
either the original mix or the primary-ASR word stream.  Doubt leaves the
original untouched and visible to the operator.
"""
from __future__ import annotations

from difflib import SequenceMatcher
import logging
import os
import time
import unicodedata

logger = logging.getLogger("genly.targeted_consensus")
_TRUE = {"1", "true", "yes", "on"}


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
                     primary_words: list[dict], *, threshold: float = 0.72):
    """Return the agreed word stream and evidence, or ``(None, ...)``.

    Stem participation is mandatory.  This prevents two noisy full-mix
    streams from voting a backing-instrument hallucination into the lyrics.
    """
    streams = {"stem": stem_words, "mix": mix_words, "primary": primary_words}
    texts = {name: _text(words) for name, words in streams.items()}
    eligible = {
        name: text for name, text in texts.items()
        if len(_norm(text).split()) >= 2
    }
    best = None
    for other in ("mix", "primary"):
        if "stem" not in eligible or other not in eligible:
            continue
        # Global fuzzy similarity is unsafe for lyrics: "hoy"/"muy" and
        # "lejos"/"cerca" can score >0.8 in a long line while changing the
        # meaning. Every token must agree before content can be accepted.
        similarity = 1.0 if _norm(eligible["stem"]) == _norm(eligible[other]) else 0.0
        if best is None or similarity > best[0]:
            best = (similarity, other)
    evidence = {"texts": texts, "agreement": round(best[0], 3) if best else 0.0}
    if not best or best[0] < threshold:
        return None, evidence
    # Stem timestamps are preferred because instrumental energy is absent.
    evidence["sources"] = ["stem", best[1]]
    return stem_words, evidence


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
              transcribe_fn=None, stem_path: str | None = None) -> tuple[dict, dict]:
    """Apply a cost-capped consensus pass.  Never raises."""
    stats = {
        "attempted": False, "windows_considered": len(windows or []),
        "windows_processed": 0, "asr_calls": 0, "audio_seconds_billed": 0.0,
        "lines_replaced": 0, "lines_inserted": 0, "truncated_windows": 0,
        "lines_suggested": 0, "provider_attempts": 0,
        "submitted_audio_seconds": 0.0, "declined": [],
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
        if not stem_path:
            stats["declined"].append("no_stem")
            return result, stats

        # Environment values may tune downward but can never remove the hard
        # operational ceiling.
        max_windows = min(4, _env_int("TARGETED_CONSENSUS_MAX_WINDOWS", 3))
        max_billed_s = min(180.0, _env_float("TARGETED_CONSENSUS_MAX_BILLED_SECONDS", 120.0))
        max_clip_s = min(45.0, _env_float("TARGETED_CONSENSUS_MAX_CLIP_SECONDS", 40.0))
        primary = result.get("_asr_words") or []
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

        priority = {"voiced_gap": 0, "uncovered_asr": 1, "text_mismatch": 2}
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
            duration = min(max_clip_s, max(0.0, end - start))
            if end - start > max_clip_s:
                stats["truncated_windows"] += 1
                stats["declined"].append("window_truncated")
            if duration <= 0 or stats["audio_seconds_billed"] + 2 * duration > max_billed_s:
                stats["declined"].append("cost_budget")
                break
            stats["asr_calls"] += 1
            stats["provider_attempts"] += 1
            stats["submitted_audio_seconds"] = round(
                stats["submitted_audio_seconds"] + duration, 2
            )
            stem_words = transcribe_fn(stem_path, start, duration, language or None, job_id or None)
            if time.monotonic() - started_at >= hard_deadline_s:
                stats["declined"].append("stage_deadline_after_stem")
                break
            stats["asr_calls"] += 1
            stats["provider_attempts"] += 1
            stats["submitted_audio_seconds"] = round(
                stats["submitted_audio_seconds"] + duration, 2
            )
            mix_words = transcribe_fn(audio_path, start, duration, language or None, job_id or None)
            stats["windows_processed"] += 1
            stats["audio_seconds_billed"] = round(
                stats["audio_seconds_billed"] + 2 * duration, 2
            )

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
                agreed, evidence = choose_consensus(sw, mw, pw)
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
            reasons = set(window.get("reasons") or [])
            if reasons & {"uncovered_asr", "voiced_gap"}:
                from gap_rescue import _agrupar_en_lineas
                for group in _agrupar_en_lineas(stem_words):
                    if not _safe_line(group):
                        continue
                    a, b = _f(group[0].get("start")), _f(group[-1].get("end"))
                    mix_group = _words_in(mix_words, a, b, pad=0.6)
                    agreed, _evidence = choose_consensus(group, mix_group, [])
                    if not agreed or not _non_overlapping(a, b, segments):
                        continue
                    if not allow_insertions:
                        # Observe mode measures how often consensus could help
                        # but cannot mutate the delivered lyric. Auto-insertions
                        # are enabled only together with a blocking review gate.
                        stats["lines_suggested"] += 1
                        continue
                    segments.append({
                        "start": round(a, 3), "end": round(b, 3),
                        "text": _text(group), "review": True,
                        "words": [dict(word) for word in group],
                        "consensus_reprocessed": True,
                        "consensus_sources": ["stem", "mix"],
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
