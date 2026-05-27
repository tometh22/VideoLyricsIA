"""I/O helper for the timeline waveform.

Wraps the cache+download+compute+upload cycle so two callers stay in sync:

  1. The `/jobs/{id}/waveform` endpoint (main.py) — serves the operator
     on demand when they open the editor.
  2. The render pipeline (pipeline.py) — called at the end of a
     successful render so the FIRST operator-facing fetch is always a
     cache hit (~200ms) instead of paying the 5-30s cold-cache cost.

Pure-numpy helpers (peak_envelope) live in `waveform_utils.py` — this
module owns the side-effects (R2, librosa, tempfile) so the math stays
testable in isolation.

Contract: `compute_and_cache_waveform(job_id, input_r2_key)` is best-effort.
Returns the payload dict on success, or None on any failure (R2 down,
librosa OOM, malformed audio, etc). Never raises — the caller decides
whether to surface to the user or skip silently. Re-running on an
already-cached job is cheap: we check the cache first and short-circuit
if the JSON is already in R2.
"""

from __future__ import annotations

import json as _json
import logging
import tempfile

logger = logging.getLogger("genly.waveform")

# Number of peak buckets the timeline canvas draws. Match the value in
# main.get_waveform — both call sites must agree on the shape so the
# cached JSON is interchangeable.
_N_BUCKETS = 1000


def cache_key_for_job(job_id: str) -> str:
    """Canonical R2 key for the cached waveform JSON. Single source of
    truth so endpoint + pipeline both look at the same place."""
    return f"waveform/{job_id}.json"


def compute_and_cache_waveform(
    job_id: str,
    input_r2_key: str,
    *,
    force: bool = False,
) -> dict | None:
    """Compute the peak envelope for `job_id`'s source audio and cache
    it to R2.

    Args:
        job_id: stable id used to build the cache key.
        input_r2_key: R2 key for the source MP3/WAV. Must be present —
            jobs without input_r2_key (very old or test fixtures) can't
            be processed.
        force: if True, skip the cache hit short-circuit and recompute.
            Used by ops scripts that want to rebuild stale envelopes.

    Returns:
        The payload dict `{"peaks": [...], "duration": float}` on success,
        or None on any failure. Never raises.

    Cache behavior:
        - Hit + not forced → returns the cached JSON without recomputing.
        - Miss or forced → downloads, computes, uploads, returns.
        - Upload-to-cache failure is logged but NOT propagated (caller
          got the payload; next call will recompute, which is fine).
    """
    if not job_id or not input_r2_key:
        return None

    # Lazy imports so the module loads cheap (the pipeline imports this
    # at the bottom of `run_pipeline` — paying for librosa at module
    # load would slow every test that touches pipeline.py).
    import storage

    if not storage.is_enabled():
        return None

    cache_key = cache_key_for_job(job_id)

    # Cache hit short-circuit. Force=True skips this branch for the
    # ops-rebuild path.
    if not force:
        try:
            if storage.object_exists(cache_key):
                with tempfile.NamedTemporaryFile(suffix=".json", delete=True) as tf:
                    if storage.download_object(cache_key, tf.name):
                        with open(tf.name, "r", encoding="utf-8") as f:
                            return _json.loads(f.read())
        except Exception as exc:
            logger.warning(
                "[WAVEFORM] cache hit read failed for %s (will recompute): %s",
                job_id, exc,
            )

    # Cache miss. Download the source audio, run librosa, build the envelope.
    with tempfile.NamedTemporaryFile(suffix=".audio", delete=True) as tf:
        if not storage.download_object(input_r2_key, tf.name):
            logger.warning(
                "[WAVEFORM] source audio download failed for %s (key=%s)",
                job_id, input_r2_key,
            )
            return None
        try:
            import librosa
            import numpy as np
            # 8 kHz mono is plenty for an amplitude envelope and keeps
            # the load fast + memory small even for long tracks.
            y, sr = librosa.load(tf.name, sr=8000, mono=True)
        except Exception as exc:
            logger.error(
                "[WAVEFORM] librosa load failed for %s: %s", job_id, exc,
            )
            return None

    duration = float(len(y) / sr) if sr else 0.0
    from waveform_utils import peak_envelope
    peaks = peak_envelope(np.abs(y), _N_BUCKETS)
    payload = {"peaks": peaks, "duration": round(duration, 3)}

    # Cache to R2 (best-effort — a failed write just means the next
    # caller recomputes, which is correct behavior, not data loss).
    try:
        storage.put_object_bytes(
            cache_key, _json.dumps(payload).encode("utf-8"), "application/json"
        )
    except Exception as exc:
        logger.warning("[WAVEFORM] cache write failed for %s: %s", job_id, exc)

    return payload
