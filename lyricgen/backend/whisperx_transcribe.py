"""WhisperX transcription — word-level timestamps for the NO-LYRICS path.

WHY
---
When we have no known lyrics (lrclib 404, Gemini empty) the audio is the only
source of truth. OpenAI's whisper-1 gives segment-level timing (±500ms) and
interpolates word times. WhisperX (Whisper large-v2 + wav2vec2 phoneme forced
alignment + VAD) pins each word to <100ms and its VAD makes it far less prone to
the single-mega-segment hallucination. This is the engine behind Rotor-grade
"transcribe a hard song with no lyrics and it still lands". Run on Replicate so
there's no torch/GPU on the workers (same plumbing as `forced_align.py`).

CONTRACT
--------
- Behind `WHISPERX_ENABLED` (default off) + `REPLICATE_API_TOKEN`.
- `transcribe_whisperx(audio_path, language=None) -> list[dict] | None` returns
  `[{"start","end","text","words":[{"word","start","end"}]}]` aligned to the
  audio, or **None** on any failure / when disabled / when the result looks
  empty. NEVER raises — the caller falls back to whisper-1.
- `_map_segments` is pure (no network) and unit-testable.
"""

from __future__ import annotations

import logging
import os

logger = logging.getLogger("genly.whisperx")

_TRUE = ("1", "true", "yes", "on", "y", "t")

# ── VAD-first ad-lib re-transcription (PR #708) ───────────────────────────────
_ADLIB_VAD_TOP_DB: float = 25.0        # more sensitive than pipeline's 30-35
_ADLIB_VAD_MERGE_GAP_S: float = 0.3   # preserve ~0.43s gaps between uh clusters
_ADLIB_VAD_MIN_REGION_S: float = 2.0  # discard sub-2s noise blips
_ADLIB_MIN_DUR: float = 6.0           # keep in sync with post_reconcile.ADLIB_MIN_DUR
# Fixed lookahead past the adlib block end. whisperX often places displaced
# lyric segments (e.g. "Tomas del miedo" at 95.8s when it actually starts at
# 102.02s) immediately after the adlib. Extending the VAD window by this
# amount lets us detect AND retranscribe those segments at correct timestamps.
# n_consumed is then set dynamically based on the last VAD region found
# (old approach: fixed ns_end which was only ~3s wide, missing the real onset).
_ADLIB_LOOKAHEAD_S: float = 20.0

# Replicate whisperX model. Override via env once a version is verified for
# prod. NOTE: pin to a known-good version hash before enabling in production.
_MODEL = os.environ.get(
    "WHISPERX_MODEL",
    # Verified live 2026-05-22 via Replicate API (account tometh22).
    "victor-upmeet/whisperx:655845d6190ef70573c669245f245892cd039df4b880a1e3a65852c09252f5cc",
)


def is_enabled() -> bool:
    """On only when the flag is set AND a Replicate token is present."""
    flag = os.environ.get("WHISPERX_ENABLED", "0").strip().lower() in _TRUE
    return flag and bool(os.environ.get("REPLICATE_API_TOKEN", "").strip())


def _find_voice_onset_after_adlib(
    audio_path: str,
    search_from: float,
    search_to: float,
) -> float | None:
    """Return the voice onset after the largest silence gap in [search_from, search_to].

    After a whisperX adlib block, uh sounds often continue for several seconds
    past the segment boundary. This finds the first voice region that starts
    after the largest silence gap — which is the true onset of the next lyric
    line. Returns None if no clear gap (≥0.4s) is found or on any error.
    """
    try:
        import librosa
        duration = search_to - search_from
        if duration <= 0:
            return None
        y, sr = librosa.load(
            audio_path, sr=16000, mono=True,
            offset=search_from, duration=duration,
        )
        intervals = librosa.effects.split(y, top_db=_ADLIB_VAD_TOP_DB)
        if len(intervals) < 2:
            return None

        max_gap = 0.0
        best_onset: float | None = None
        for i in range(1, len(intervals)):
            gap = (intervals[i][0] - intervals[i - 1][1]) / sr
            if gap > max_gap:
                max_gap = gap
                best_onset = search_from + intervals[i][0] / sr

        if max_gap < 0.4 or best_onset is None:
            return None
        return best_onset
    except Exception as exc:
        logger.debug(
            "[ADLIB-VAD] _find_voice_onset_after_adlib %.1f-%.1fs: %s",
            search_from, search_to, exc,
        )
        return None


def _correct_post_adlib_timing(segs: list[dict], audio_path: str) -> list[dict]:
    """Shift the timing of the lyric segment immediately following an adlib block.

    whisperX sometimes places the adlib tail (uh/uoh that continue past the
    block boundary) as the first lyric line — e.g. "Tomas del miedo tu don"
    appears 7s too early. We run VAD from the adlib block's end, find the
    real voice onset after the last uh cluster (identified by the largest
    silence gap), and correct the start/end timestamps. Text is never changed.

    Only fires when the next segment starts ≤2s after the adlib block ends
    and a clear gap (≥0.4s) is detected in the audio.
    """
    _enabled = os.environ.get(
        "ADLIB_TAIL_TIMING_FIX_ENABLED", "1"
    ).strip().lower() in _TRUE
    if not _enabled or not audio_path or not os.path.exists(audio_path):
        return segs

    try:
        from post_reconcile import _is_adlib_loop
    except ImportError:
        return segs

    out = list(segs)
    for i, seg in enumerate(out):
        if i + 1 >= len(out):
            break
        text = (seg.get("text") or "").strip()
        if not _is_adlib_loop(text):
            continue
        try:
            seg_end = float(seg["end"])
        except (KeyError, TypeError, ValueError):
            continue

        next_seg = out[i + 1]
        try:
            ns_start = float(next_seg["start"])
            ns_end = float(next_seg["end"])
            ns_text = (next_seg.get("text") or "").strip()
        except (KeyError, TypeError, ValueError):
            continue

        if ns_start - seg_end > 2.0 or _is_adlib_loop(ns_text):
            continue

        true_start = _find_voice_onset_after_adlib(audio_path, seg_end, seg_end + 20.0)
        if true_start is None or true_start <= ns_start + 0.5:
            continue

        orig_dur = max(ns_end - ns_start, 2.0)
        out[i + 1] = {
            **next_seg,
            "start": round(true_start, 3),
            "end": round(true_start + orig_dur, 3),
        }
        logger.info(
            "[ADLIB-VAD] tail timing corrected: %r %.1fs→%.1fs (gap=%.2fs)",
            ns_text[:30], ns_start, true_start, true_start - ns_start,
        )

    return out


def _detect_adlib_voice_regions(
    audio_path: str,
    seg_start: float,
    seg_end: float,
) -> list[tuple[float, float]]:
    """Return VAD-detected non-silent regions within [seg_start, seg_end].

    Loads only the audio slice (offset + duration) via librosa to avoid
    decoding a full 5-min WAV for a 40s ad-lib block. sr=16000 matches the
    ffmpeg extraction in _retranscribe_slice. Returns [(seg_start, seg_end)]
    as a no-split sentinel when ≤1 region is found or on any failure.
    Never raises.
    """
    try:
        import librosa
        duration = seg_end - seg_start
        if duration <= 0:
            return [(seg_start, seg_end)]
        y, sr = librosa.load(
            audio_path, sr=16000, mono=True,
            offset=seg_start, duration=duration,
        )
        intervals = librosa.effects.split(y, top_db=_ADLIB_VAD_TOP_DB)
        regions: list[tuple[float, float]] = []
        for s_samp, e_samp in intervals:
            t_s = seg_start + float(s_samp) / sr
            t_e = seg_start + float(e_samp) / sr
            if t_e - t_s >= _ADLIB_VAD_MIN_REGION_S:
                regions.append((t_s, t_e))
        if not regions:
            return [(seg_start, seg_end)]
        # Merge regions separated by < _ADLIB_VAD_MERGE_GAP_S.
        # Less aggressive than pipeline._detect_speech_regions (0.5s) to
        # preserve the ~0.43s natural silences between uh clusters.
        merged: list[tuple[float, float]] = [regions[0]]
        for r_s, r_e in regions[1:]:
            if r_s - merged[-1][1] <= _ADLIB_VAD_MERGE_GAP_S:
                merged[-1] = (merged[-1][0], r_e)
            else:
                merged.append((r_s, r_e))
        return merged if len(merged) >= 2 else [(seg_start, seg_end)]
    except Exception as exc:
        logger.warning(
            "[ADLIB-VAD] detect failed (%.1f-%.1fs): %s", seg_start, seg_end, exc,
        )
        return [(seg_start, seg_end)]


def _retranscribe_slice(
    audio_path: str,
    start_s: float,
    end_s: float,
    language: str | None = None,
) -> list[dict] | None:
    """Extract [start_s, end_s] from audio_path as a temp MP3 and transcribe
    via Whisper-1 API. Returns segments with timestamps offset to absolute
    time. Returns None on any failure. Never raises.

    Uses lazy import of pipeline._transcribe_via_openai_api to avoid circular
    imports (pipeline.py imports whisperx_transcribe at module level).

    No lyrics_hint: short context lets Whisper's LM break the uh-attractor
    and correctly transcribe the singing passage that follows.
    """
    import subprocess
    import tempfile
    import os as _os

    chunk_path: str | None = None
    try:
        from pipeline import _transcribe_via_openai_api  # lazy: safe after both modules load
        with tempfile.NamedTemporaryFile(suffix=".mp3", delete=False) as tf:
            chunk_path = tf.name
        rc = subprocess.run(
            [
                "ffmpeg", "-y",
                "-ss", str(start_s), "-to", str(end_s),
                "-i", audio_path,
                "-ar", "16000", "-ac", "1",
                "-codec:a", "libmp3lame", "-q:a", "5",
                chunk_path,
            ],
            capture_output=True,
            timeout=60,
        )
        if rc.returncode != 0:
            logger.warning(
                "[ADLIB-VAD] ffmpeg extract %.1f-%.1fs failed (rc=%d)",
                start_s, end_s, rc.returncode,
            )
            return None
        raw = _transcribe_via_openai_api(chunk_path, language=language)
        if not raw:
            return None
        result: list[dict] = []
        for seg in raw:
            try:
                out_seg = {
                    **seg,
                    "start": round(float(seg["start"]) + start_s, 3),
                    "end": round(float(seg["end"]) + start_s, 3),
                }
                if out_seg.get("words"):
                    out_seg["words"] = [
                        {**w,
                         "start": round(float(w["start"]) + start_s, 3),
                         "end": round(float(w["end"]) + start_s, 3)}
                        for w in out_seg["words"]
                    ]
                result.append(out_seg)
            except (KeyError, TypeError, ValueError):
                continue
        return result or None
    except Exception as exc:
        logger.warning(
            "[ADLIB-VAD] _retranscribe_slice %.1f-%.1fs failed: %s", start_s, end_s, exc,
        )
        return None
    finally:
        if chunk_path:
            try:
                _os.unlink(chunk_path)
            except OSError:
                pass


def _vad_split_adlib_segments(
    segs: list[dict],
    audio_path: str,
    language: str | None = None,
) -> list[dict]:
    """For ad-lib mega-blocks with no word stamps, run VAD-based split and
    per-region re-transcription so each cluster becomes its own segment.

    This replicates Rotor's VAD-first approach: instead of transcribing a
    40s mega-block of uh sounds (which causes Whisper's LM to stay stuck in
    the uh-attractor), we detect natural silence boundaries, then transcribe
    each 5-15s region independently. Short context allows Whisper to correctly
    identify singing passages (e.g., "Tomas del miedo tu don") within what
    was previously a single undifferentiated uh block.

    Entry conditions for processing a segment:
      - _is_adlib_loop(text) is True (lazy import from post_reconcile)
      - no "words" key (already-aligned segments skip this step)
      - duration >= _ADLIB_MIN_DUR
      - ADLIB_VAD_RETRANSCRIBE_ENABLED env var is not "0" / falsy

    Fallback chain (defense in depth):
      1. VAD returns ≥2 regions AND ALL retranscriptions succeed → new segs
      2. Any failure → keep original segment; post_reconcile._split_adlib_by_time
         (PR #707 equal-time split) still handles it downstream.
    """
    _enabled = os.environ.get(
        "ADLIB_VAD_RETRANSCRIBE_ENABLED", "1"
    ).strip().lower() in _TRUE

    if not _enabled or not audio_path or not os.path.exists(audio_path):
        return segs

    try:
        from post_reconcile import _is_adlib_loop  # safe: post_reconcile doesn't import us
    except ImportError:
        logger.warning("[ADLIB-VAD] could not import _is_adlib_loop; skipping")
        return segs

    out: list[dict] = []
    idx = 0
    while idx < len(segs):
        seg = segs[idx]
        text = (seg.get("text") or "").strip()
        try:
            seg_start = float(seg["start"])
            seg_end = float(seg["end"])
        except (KeyError, TypeError, ValueError):
            out.append(seg)
            idx += 1
            continue
        dur = seg_end - seg_start

        # Don't skip segments that have word stamps — forced alignment succeeds
        # on "uh" tokens but still produces one mega-block that needs VAD split.
        if not (dur >= _ADLIB_MIN_DUR and _is_adlib_loop(text)):
            out.append(seg)
            idx += 1
            continue

        # Fixed lookahead: extend VAD window past seg_end so we can detect
        # displaced singing segments. n_consumed is determined AFTER VAD runs,
        # based on the last voice region found — segments after that point keep
        # their whisperX timestamps and are left untouched.
        n_consumed = 1
        process_end = seg_end + _ADLIB_LOOKAHEAD_S

        logger.info(
            "[ADLIB-VAD] candidate: %.1f-%.1fs (%.1fs, lookahead→%.1fs) %r…",
            seg_start, seg_end, seg_end - seg_start, process_end, text[:40],
        )
        regions = _detect_adlib_voice_regions(audio_path, seg_start, process_end)
        if len(regions) < 2:
            logger.info(
                "[ADLIB-VAD] VAD found %d region(s) → fallback to equal-time split",
                len(regions),
            )
            out.append(seg)
            idx += 1
            continue

        # Dynamic n_consumed: absorb whisperX segments whose start times fall
        # within the last detected VAD region. Those segments have displaced
        # timing due to the adlib block and will be replaced by retranscription.
        # Segments starting at or after last_region_end keep correct timing.
        last_region_end = regions[-1][1]
        _ci = idx + 1
        while _ci < len(segs):
            ns = segs[_ci]
            try:
                ns_start = float(ns["start"])
                ns_text_raw = (ns.get("text") or "").strip()
            except (KeyError, TypeError, ValueError):
                break
            if ns_start >= last_region_end or _is_adlib_loop(ns_text_raw):
                break
            n_consumed += 1
            logger.info(
                "[ADLIB-VAD] consuming displaced segment %r at %.1fs (< last region %.1fs)",
                ns_text_raw[:40], ns_start, last_region_end,
            )
            _ci += 1

        logger.info("[ADLIB-VAD] %d regions detected; re-transcribing each", len(regions))
        sub_segs: list[list[dict]] = []
        all_ok = True
        for r_start, r_end in regions:
            result = _retranscribe_slice(audio_path, r_start, r_end, language)
            if result is None:
                logger.warning(
                    "[ADLIB-VAD] retranscribe failed for %.1f-%.1fs — keeping original",
                    r_start, r_end,
                )
                all_ok = False
                break
            sub_segs.append(result)

        if not all_ok or not sub_segs:
            for k in range(n_consumed):
                out.append(segs[idx + k])
            idx += n_consumed
            continue

        flat = sorted(
            [s for region_segs in sub_segs for s in region_segs],
            key=lambda s: s.get("start", 0),
        )
        if not flat:
            for k in range(n_consumed):
                out.append(segs[idx + k])
            idx += n_consumed
            continue

        logger.info(
            "[ADLIB-VAD] replaced %.1f-%.1fs (%d orig seg(s)) with %d sub-segment(s): %s",
            seg_start, process_end, n_consumed, len(flat),
            ", ".join(f"{s['start']:.1f}-{s['end']:.1f}s" for s in flat[:6]),
        )
        out.extend(flat)
        idx += n_consumed

    return out


# ── Post-reconcile large-gap cluster correction ───────────────────────
# Fixes displaced timing in the reconciled output BEFORE post_reconcile's
# stretch-trim fills the gaps. Root cause: whisperX doesn't produce adlib
# segments for some songs ("No Hay Santos"), so _vad_split_adlib_segments
# never fires. The reconciler maps canonical text to whisperX's (wrong)
# timestamps. We detect the large gap in the reconciled output, find the
# true voice onset via VAD + retranscription, and shift the cluster.
_LARGE_GAP_CLUSTER_GAP_THRESH: float = 12.0  # gap (prev.end → seg.start) to trigger
_LARGE_GAP_CLUSTER_SPAN_S: float = 8.0       # max start-to-start span to extend cluster
_LARGE_GAP_CLUSTER_LOOKAHEAD_S: float = 20.0  # window past cluster_last_start for VAD
_LARGE_GAP_CLUSTER_MIN_SHIFT: float = 1.5    # don't correct unless onset is >1.5s later


def _correct_large_gap_cluster(
    segs: list[dict],
    audio_path: str,
    language: str | None = None,
) -> list[dict]:
    """Correct timing of lyric segments displaced after an invisible adlib block.

    Runs on the RECONCILED output (before post_reconcile stretch-trim).
    When the reconciled list has a gap > _LARGE_GAP_CLUSTER_GAP_THRESH from
    prev.end to seg.start, uses VAD + per-region retranscription to find the
    real voice onsets. Adlib regions (uh/uoh) are filtered; remaining lyric
    regions are assigned to the cluster segments in order.

    Safety checks:
      - Only fires if the first lyric region onset is >_LARGE_GAP_CLUSTER_MIN_SHIFT
        later than the cluster's current start (already-correct clusters skip).
      - If retranscription fails for any region the cluster is left untouched.
      - Gated by LARGE_GAP_CLUSTER_FIX_ENABLED (default "1").

    Example — "No Hay Santos" (Amanda Pujó):
      Reconciler gives "Tomas del miedo" at 95.8s (true onset: 102.02s) and
      "Frágil espejo" at 98.8s (true onset: 110.17s) because whisperX jumps
      from "para qué" (68s) to the post-adlib lyric without seeing the uh
      blocks. This function shifts both segments to the VAD-detected onsets.
    """
    _enabled = os.environ.get(
        "LARGE_GAP_CLUSTER_FIX_ENABLED", "1"
    ).strip().lower() in _TRUE
    if not _enabled or not audio_path or not os.path.exists(audio_path):
        return segs

    try:
        from post_reconcile import _is_adlib_loop
    except ImportError:
        logger.warning("[GAP-CLUSTER] could not import _is_adlib_loop; skipping")
        return segs

    out = list(segs)
    i = 1
    while i < len(out):
        try:
            prev_end = float(out[i - 1].get("end", 0))
            seg_start = float(out[i].get("start", 0))
        except (TypeError, ValueError):
            i += 1
            continue

        gap = seg_start - prev_end
        if gap <= _LARGE_GAP_CLUSTER_GAP_THRESH:
            i += 1
            continue

        # Build the cluster: consecutive segments with small start-to-start span.
        cluster: list[int] = [i]
        j = i + 1
        while j < len(out):
            try:
                prev_s = float(out[j - 1].get("start", 0))
                curr_s = float(out[j].get("start", 0))
            except (TypeError, ValueError):
                break
            if curr_s - prev_s > _LARGE_GAP_CLUSTER_SPAN_S:
                break
            cluster.append(j)
            j += 1

        try:
            c_last_start = float(out[cluster[-1]].get("start", 0))
        except (TypeError, ValueError):
            i = j
            continue

        # VAD window: prev_end → min(c_last_start + lookahead, next_seg.start - 2s)
        if j < len(out):
            try:
                next_start = float(out[j].get("start", 0))
                vad_end = min(c_last_start + _LARGE_GAP_CLUSTER_LOOKAHEAD_S,
                              next_start - 2.0)
            except (TypeError, ValueError):
                vad_end = c_last_start + _LARGE_GAP_CLUSTER_LOOKAHEAD_S
        else:
            vad_end = c_last_start + _LARGE_GAP_CLUSTER_LOOKAHEAD_S

        vad_start = prev_end
        if vad_end <= vad_start + 3.0:
            i = j
            continue

        logger.info(
            "[GAP-CLUSTER] gap %.1fs before %r at %.1fs — VAD %.1f-%.1fs (cluster=%d segs)",
            gap, (out[i].get("text") or "")[:25], seg_start,
            vad_start, vad_end, len(cluster),
        )

        regions = _detect_adlib_voice_regions(audio_path, vad_start, vad_end)
        if len(regions) <= 1 and (not regions or regions[0] == (vad_start, vad_end)):
            logger.info("[GAP-CLUSTER] VAD found no sub-regions; skipping cluster")
            i = j
            continue

        # Retranscribe each region; filter adlib loops.
        lyric_regions: list[tuple[float, float]] = []
        retrans_ok = True
        for r_start, r_end in regions:
            sub = _retranscribe_slice(audio_path, r_start, r_end, language)
            if sub is None:
                logger.info(
                    "[GAP-CLUSTER] retranscribe %.1f-%.1fs failed; aborting cluster",
                    r_start, r_end,
                )
                retrans_ok = False
                break
            combined = " ".join((s.get("text") or "").strip() for s in sub).strip()
            if _is_adlib_loop(combined):
                logger.info(
                    "[GAP-CLUSTER] skip adlib region %.1f-%.1fs: %r",
                    r_start, r_end, combined[:30],
                )
                continue
            lyric_regions.append((r_start, r_end))

        if not retrans_ok:
            i = j
            continue

        if len(lyric_regions) < len(cluster):
            logger.info(
                "[GAP-CLUSTER] only %d lyric region(s) for %d cluster seg(s); skipping",
                len(lyric_regions), len(cluster),
            )
            i = j
            continue

        # Safety: only correct if the first lyric onset is meaningfully later
        # than the cluster's current start. Prevents double-correction when the
        # cluster is already in the right position (e.g. the second couplet).
        try:
            c_start = float(out[cluster[0]].get("start", 0))
        except (TypeError, ValueError):
            i = j
            continue

        if lyric_regions[0][0] <= c_start + _LARGE_GAP_CLUSTER_MIN_SHIFT:
            logger.info(
                "[GAP-CLUSTER] cluster at %.1fs already correct (first lyric onset %.1fs); skipping",
                c_start, lyric_regions[0][0],
            )
            i = j
            continue

        # Apply: assign each lyric region to the corresponding cluster segment.
        for k, ci in enumerate(cluster):
            r_start, r_end = lyric_regions[k]
            old_start = float(out[ci].get("start", 0))
            old_end = float(out[ci].get("end", 0))
            old_dur = max(old_end - old_start, 1.5)
            out[ci] = {
                **out[ci],
                "start": round(r_start, 3),
                "end": round(r_end, 3),
            }
            logger.info(
                "[GAP-CLUSTER] corrected %r %.1fs→%.1fs (+%.1fs)",
                (out[ci].get("text") or "")[:30],
                old_start, r_start, r_start - old_start,
            )

        i = j

    return out


# ──────────────────────────────────────────────────────────────────────
# Cache helpers (2026-05-25). Content-addressable, on-disk DB. Same
# audio + same language + same lyrics_hint → same whisperX output, so
# cache hits return instantly instead of paying ~$0.005 + 75-180 s per
# Replicate inference.
# ──────────────────────────────────────────────────────────────────────

def _compute_cache_key(audio_path: str, language: str | None, lyrics_hint: str | None):
    """Return (cache_key, audio_hash, lyrics_hint_hash) — or (None, None, None)
    on read error (cache effectively disabled for this call).

    audio_hash is sha256 of the file bytes (first 16 hex chars; chunked
    1 MB reads to avoid loading a 60 MB WAV into RAM). lyrics_hint_hash
    is sha1 of the (trimmed) hint or "" if no hint. Both contribute to
    cache_key because each changes the output deterministically.
    """
    import hashlib
    try:
        h = hashlib.sha256()
        with open(audio_path, "rb") as f:
            for chunk in iter(lambda: f.read(1024 * 1024), b""):
                h.update(chunk)
        audio_hash = h.hexdigest()[:16]
    except Exception:
        return (None, None, None)
    hint = (lyrics_hint or "").strip()
    hint_hash = hashlib.sha1(hint.encode("utf-8")).hexdigest()[:16] if hint else ""
    lang = (language or "").lower()
    # WHISPERX_CACHE_VERSION lets ops invalidate all cached results by bumping
    # this env var (e.g. "2") without touching the DB. Default "1" preserves
    # all pre-existing rows so changing it is a deliberate opt-in bust.
    ver = os.environ.get("WHISPERX_CACHE_VERSION", "1").strip() or "1"
    key = f"wx:{audio_hash}:{lang}:{hint_hash}:{ver}"
    return (key, audio_hash, hint_hash)


def _cache_lookup(cache_key: str, expected_audio_hash: str | None = None) -> list[dict] | None:
    """Return cached segments for `cache_key`, or None on miss / error.
    Errors are swallowed — cache is best-effort, the path falls through
    to the live Replicate call.

    `expected_audio_hash` (optional): defense-in-depth audio integrity
    check. The cache_key already starts with the audio hash, but if a
    bug ever lets a row's `audio_hash` column drift from the key (manual
    DB edit, schema migration mishap, future bug), the cross-check below
    catches it and forces a live recompute. Cost is one column compare;
    upside is "lyrics of a different song appear" never happens silently.
    """
    try:
        from database import TranscriptionCache, SessionLocal
        import json
        db = SessionLocal()
        try:
            row = db.query(TranscriptionCache).filter(
                TranscriptionCache.cache_key == cache_key
            ).first()
            if not row:
                return None
            # 2026-05-31 defense-in-depth (Agus batch upload report):
            # if expected_audio_hash is provided, the row must match.
            # On mismatch we treat as a miss + log so the next run still
            # works correctly (live call repopulates the cache).
            if (expected_audio_hash is not None
                    and row.audio_hash is not None
                    and row.audio_hash != expected_audio_hash):
                logger.warning(
                    "[WHISPERX] cache row audio_hash mismatch for key=%s "
                    "(stored=%s expected=%s); forcing live recompute",
                    cache_key, row.audio_hash, expected_audio_hash,
                )
                return None
            return json.loads(row.segments)
        finally:
            db.close()
    except Exception as e:
        logger.warning("[WHISPERX] cache lookup failed (%s); will run live", e)
        return None


def _cache_write(cache_key: str, audio_hash: str, lyrics_hint_hash: str,
                 language: str | None, segments: list[dict]) -> None:
    """Persist `segments` under `cache_key`. Swallows all errors — a
    failed write must not break the request that just succeeded."""
    try:
        from database import TranscriptionCache, SessionLocal
        from sqlalchemy.dialects.postgresql import insert as pg_insert
        import json
        db = SessionLocal()
        try:
            row = TranscriptionCache(
                cache_key=cache_key,
                audio_hash=audio_hash,
                engine="whisperx",
                language=(language or "")[:8] or None,
                lyrics_hint_hash=lyrics_hint_hash or None,
                segments=json.dumps(segments),
            )
            db.merge(row)   # idempotent upsert (cache_key is PK)
            db.commit()
        finally:
            db.close()
    except Exception as e:
        logger.warning("[WHISPERX] cache write failed (%s); ignoring", e)


def _map_segments(output) -> list[dict]:
    """Map whisperX output to our segment shape. Pure + testable.

    whisperX returns {"segments": [{"start","end","text",
    "words":[{"word","start","end","score"}]}], ...}. We keep line-level
    start/end/text and carry per-word stamps (for a future word-level editor).
    Drops segments with no usable text or non-numeric bounds.
    """
    if isinstance(output, dict):
        raw = output.get("segments") or []
    elif isinstance(output, list):
        raw = output
    else:
        return []

    segs: list[dict] = []
    for s in raw:
        if not isinstance(s, dict):
            continue
        text = (s.get("text") or "").strip()
        if not text:
            continue
        try:
            start = float(s.get("start"))
            end = float(s.get("end"))
        except (TypeError, ValueError):
            continue
        if end < start:
            end = start
        words = []
        for w in (s.get("words") or []):
            if not isinstance(w, dict):
                continue
            wt = (w.get("word") or w.get("text") or "").strip()
            try:
                ws = float(w.get("start"))
                we = float(w.get("end"))
            except (TypeError, ValueError):
                continue  # whisperX omits stamps for non-alignable tokens
            if wt:
                # Preserve `score` (whisperX confidence per word, 0-1) so
                # the editor can render low-confidence lines tinted red.
                w_out = {"word": wt, "start": ws, "end": we}
                try:
                    score = w.get("score")
                    if score is not None:
                        w_out["score"] = float(score)
                except (TypeError, ValueError):
                    pass
                words.append(w_out)
        seg = {"start": start, "end": end, "text": text}
        if words:
            seg["words"] = words
        segs.append(seg)
    return segs


def _filter_ghosts(segs: list[dict]) -> list[dict]:
    """Drop suspicious tiny segments: whisperX occasionally tags an
    instrumental flourish or breath as a 1-word segment (real example on
    El Arbol intro: `'Amén'` at 5.15s for 0.18s). Anything <0.5s AND
    <2 words is treated as a ghost. Single-word holds longer than 0.5s
    (e.g., a chanted name) are kept. Pure + testable."""
    out: list[dict] = []
    for s in segs:
        try:
            dur = float(s.get("end", 0)) - float(s.get("start", 0))
        except (TypeError, ValueError):
            dur = 0.0
        words = len((s.get("text") or "").split())
        if dur < 0.5 and words < 2:
            continue
        out.append(s)
    return out


def _split_long_segments(segs: list[dict], *, max_dur: float | None = None,
                          min_split_gap: float = 0.0) -> list[dict]:
    """Split segments longer than `max_dur` at the largest internal
    word-to-word gap, recursively, so subtitles aren't long walls of text.
    Requires per-word stamps (whisperX provides them with `align_output=True`);
    segments without words are left untouched.

    The split point is the BIGGEST gap > `min_split_gap`. When a segment is
    longer than max_dur we split at whatever pause exists, even tiny ones —
    Rotor lines run ~3-5s and whisperX's native segments are 8-25s, so we
    have to be aggressive (incident "El Arbol": an 8.7s line that wasn't
    splitting at the natural phrase boundary between "calles" and "La"
    because min_split_gap=0.3 was too strict; the actual pause whisperX
    measured there was ~0.15s).

    Defaults are env-tunable via `WHISPERX_MAX_LINE_S` (default 6.0) and
    `WHISPERX_MIN_SPLIT_GAP_S` (default 0.0). Pure + testable.
    """
    if max_dur is None:
        try:
            max_dur = float(os.environ.get("WHISPERX_MAX_LINE_S", "6.0"))
        except (TypeError, ValueError):
            max_dur = 6.0
    try:
        min_split_gap = float(os.environ.get("WHISPERX_MIN_SPLIT_GAP_S", min_split_gap))
    except (TypeError, ValueError):
        pass
    if max_dur <= 0:
        return segs

    def _split_once(seg: dict) -> list[dict]:
        words = seg.get("words") or []
        if len(words) < 4:
            return [seg]
        try:
            start = float(seg["start"]); end = float(seg["end"])
        except (TypeError, ValueError, KeyError):
            return [seg]
        if (end - start) <= max_dur:
            return [seg]
        # Long enough to require a split: find biggest gap > min_split_gap.
        best_i, best_gap = -1, -1.0
        for i in range(len(words) - 1):
            try:
                gap = float(words[i + 1]["start"]) - float(words[i]["end"])
            except (TypeError, ValueError, KeyError):
                continue
            if gap > best_gap and gap > min_split_gap:
                best_gap, best_i = gap, i
        if best_i < 0:
            return [seg]   # no usable gap structure (back-to-back words)
        # Split AT the gap: left = words[:best_i+1], right = words[best_i+1:]
        left_words = words[: best_i + 1]
        right_words = words[best_i + 1:]
        left = {
            "start": float(left_words[0]["start"]),
            "end": float(left_words[-1]["end"]),
            "text": " ".join(w.get("word", "").strip() for w in left_words if w.get("word")),
            "words": left_words,
        }
        right = {
            "start": float(right_words[0]["start"]),
            "end": float(right_words[-1]["end"]),
            "text": " ".join(w.get("word", "").strip() for w in right_words if w.get("word")),
            "words": right_words,
        }
        # Recurse on each half (depth-bounded by shrinking duration).
        return _split_once(left) + _split_once(right)

    out: list[dict] = []
    for s in segs:
        out.extend(_split_once(s))
    return out


# 2026-05-31 — default dropped from 120 → 80 ms after Agus.Cafisi
# reported "estribillo a destiempo" on "Nada fue un error" and "Luz
# de día (versión cumbia)". Investigation against the live segments_json:
#   - segment.start of all 50 segments in job 9df1132f6169 was exactly
#     -120 ms vs words[0].start — the signature of _apply_lead_in.
#   - 120 ms sits AT the karaoke "feel" threshold (~100 ms). Trained
#     ears (UMG ops reviewers) perceive it as the line landing before
#     the vocal — i.e. "destiempo".
#   - 80 ms keeps a perceptible anticipation effect (helps the eye
#     find the line before the singer enters) while sitting clearly
#     below the threshold where most listeners read the offset as
#     wrong.
# Anyone wanting the previous behavior can set LYRIC_LEAD_IN_MS=120
# in the Railway env without a code change.
_DEFAULT_LEAD_MS = 80


def _apply_lead_in(segs: list[dict], *, lead_ms: int | None = None) -> list[dict]:
    """Pull each segment's start time earlier by `lead_ms` so the subtitle
    appears slightly before the singer enters that line — the karaoke
    "anticipation" effect. WhisperX timestamps are ±100ms accurate and
    musicians lean a touch behind/ahead of the beat, so without lead-in the
    line often lands a few ms after the voice (which the user perceives as
    "delay").

    Clamps so we never go below 0 or overlap the previous segment's end.
    Per-word stamps inside `segs[i]["words"]` are NOT shifted (they stay
    truthful to the audio; only the line's display window moves).

    Default 80ms (was 120ms before 2026-05-31), env-tunable via
    `LYRIC_LEAD_IN_MS`. Pure + testable.
    """
    if lead_ms is None:
        try:
            lead_ms = int(os.environ.get("LYRIC_LEAD_IN_MS", str(_DEFAULT_LEAD_MS)))
        except (TypeError, ValueError):
            lead_ms = _DEFAULT_LEAD_MS
    if lead_ms <= 0 or not segs:
        return segs
    lead_s = lead_ms / 1000.0
    out: list[dict] = []
    prev_end: float | None = None       # None until we've seen one segment
    for s in segs:
        try:
            start = float(s.get("start", 0))
        except (TypeError, ValueError):
            out.append(s); continue
        # Floor at prev_end+5ms (no overlap with prior) and at 0 (no negative).
        floor = 0.0 if prev_end is None else max(0.0, prev_end + 0.005)
        new_start = max(start - lead_s, floor)
        new_start = min(new_start, start)   # never push later
        new_seg = dict(s)
        new_seg["start"] = new_start
        out.append(new_seg)
        try:
            prev_end = float(s.get("end", new_start))
        except (TypeError, ValueError):
            prev_end = new_start
    return out


def transcribe_whisperx(audio_path: str, language: str | None = None,
                        lyrics_hint: str | None = None) -> list[dict] | None:
    """Transcribe `audio_path` with whisperX. Returns segments with word
    stamps, or None (disabled / failure / empty). Never raises.

    `lyrics_hint`: optional reference text (lrclib / Genius / Gemini plain
    lyrics). When provided, the first ~800 chars are passed as Whisper's
    `initial_prompt` — biases the model's vocabulary towards the actual
    lyrics so it stops mishearing (e.g. "Legalícenla" instead of
    "Le realizan la"). Costs nothing extra — Whisper-large-v3 already
    accepts the prompt parameter; we just weren't using it.

    INCIDENT 2026-05-25: PROD without this hint produced ["Le realizan
    la"] × 4 in the intro of "Legalícenla" because the audio is
    phonemically ambiguous on a heavy electric guitar mix. With the
    lrclib plain text as prompt, the model expects "Legalícenla" and
    locks onto the right interpretation.
    """
    if not is_enabled():
        return None
    if not audio_path or not os.path.exists(audio_path):
        return None

    try:
        import replicate  # noqa: F401 — used inside `call_with_budget`
    except ImportError:
        logger.warning("[WHISPERX] replicate SDK not installed — falling back")
        return None

    # 2026-05-25 — Content-addressable cache. Same audio + same language
    # + same lyrics_hint = same whisperX output deterministically. Ahorra
    # ~$0.005 + 75-180 s en cualquier re-corrida (operador re-subiendo,
    # job retry, regenera tras edit). Cache miss corre normal + write.
    # Errores en cache son swallowed — el path original sigue intacto.
    cache_key, audio_hash, hint_hash = _compute_cache_key(audio_path, language, lyrics_hint)
    if cache_key:
        cached_segs = _cache_lookup(cache_key, expected_audio_hash=audio_hash)
        if cached_segs is not None:
            logger.info(
                "[WHISPERX] cache hit audio_hash=%s (%d segs, $0 + 0 s vs ~75-180 s + $0.005)",
                audio_hash, len(cached_segs),
            )
            return cached_segs

    payload: dict = {"align_output": True}
    if language:
        payload["language"] = language

    # Lyrics-aware prompting. The Replicate whisperX model
    # (victor-upmeet/whisperx) accepts `initial_prompt`, forwarded to the
    # underlying faster-whisper.
    #
    # EMPIRICAL FINDING 2026-05-25 (live A/B against Legalícenla on
    # Replicate — see scripts/scratch/test_prompt_variants.py):
    #   - LONG prompt (full lyrics, 800 chars): HURTS. whisperX collapses
    #     to 2 distorted segments because the model parrots prompt
    #     vocabulary across all audio. First seg displaced by 30+ seconds.
    #   - SHORT prompt (~22-120 chars, 2-4 seed lines): WORKS. "Legalícenla.
    #     Oh-oh-oh." (22 chars) produced 8 well-aligned segments with
    #     "Legalícenla" correctly recognised — vs 9 segments with the
    #     wrong "Legalízala" without any hint.
    #
    # Strategy: take the first 2-4 non-empty lines of the reference
    # (typically the chorus / opening — densest in distinctive words),
    # joined with ". " and capped at 120 chars total. Enough vocab to
    # bias without saturating attention.
    if lyrics_hint and lyrics_hint.strip():
        lines = [ln.strip() for ln in lyrics_hint.splitlines() if ln.strip()]
        seed_lines: list[str] = []
        running_chars = 0
        for ln in lines:
            if running_chars + len(ln) > 120 and seed_lines:
                break
            seed_lines.append(ln)
            running_chars += len(ln) + 2          # +2 for ". " joiner
            if len(seed_lines) >= 4:
                break
        prompt_text = ". ".join(seed_lines)[:120]
        payload["initial_prompt"] = prompt_text
        logger.info("[WHISPERX] initial_prompt primed: %s chars (%s seed lines) — %r",
                    len(prompt_text), len(seed_lines), prompt_text[:60])

    # OBSERVABILITY (audit 2026-05-24): whisperX USED to be a single
    # `replicate.run(...)` with no budget cap. When Replicate degraded,
    # the request could hang ~90 min before timing out — same shape as
    # the forced_align bug fixed in PR #281 but on a different code
    # path. Extracted to `replicate_budget.call_with_budget` so all
    # three Replicate consumers (FA, demucs, whisperX) share the same
    # bounded retry + typed-error short-circuit.
    from replicate_budget import call_with_budget, _budget_for

    # FD leak fix: cada retry del budget reabre el archivo; sin tracking
    # explícito, los handles se acumulan si Replicate falla mid-upload
    # (timeout/cancel). Mismo patrón que forced_align.py / vocal_sep.py.
    _open_handles: list = []
    def _input_factory():
        f = open(audio_path, "rb")
        _open_handles.append(f)
        return {"audio_file": f, **payload}

    try:
        output = call_with_budget(
            _MODEL, _input_factory,
            total_budget_s=_budget_for("whisperx", default_s=480.0),
            backoff=[0, 8, 24],
            call_label="WHISPERX",
        )
    finally:
        for _h in _open_handles:
            try:
                _h.close()
            except Exception:
                pass
    if output is None:
        return None

    segs = _map_segments(output)
    raw_n = len(segs)
    segs = _vad_split_adlib_segments(segs, audio_path, language)
    segs = _correct_post_adlib_timing(segs, audio_path)
    segs = _filter_ghosts(segs)
    segs = _split_long_segments(segs)
    # Acoustic similarity correction: replace low-confidence ASR hallucinations
    # with text from acoustically similar high-confidence segments (chorus repeats,
    # bridge variations). Runs AFTER _split_long_segments so long merged segments
    # (e.g. "Tomas del miedo tu don, frágil espejo de voz.") are already split
    # into individual lines before we compare — otherwise the anchor text would
    # be the combined phrase, producing wrong replacements.
    try:
        from acoustic_match import correct_by_acoustic_similarity
        segs = correct_by_acoustic_similarity(segs, audio_path)
    except Exception as _am_err:
        logger.warning("[ACOUSTIC] correction skipped: %s", _am_err)
    segs = _apply_lead_in(segs)
    if len(segs) < 2:
        logger.warning("[WHISPERX] thin/empty result (%s raw -> %s usable) — falling back",
                       raw_n, len(segs))
        return None
    logger.info("[WHISPERX] transcribed %s segments (%s raw, after ghost-filter + split)", len(segs), raw_n)

    # Persist result for future identical calls. Errors silenced inside.
    if cache_key:
        _cache_write(cache_key, audio_hash, hint_hash, language, segs)

    return segs
