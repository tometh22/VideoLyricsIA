"""Pure audio-envelope helpers for the timeline waveform.

Kept out of main.py (numpy-only, no FastAPI/DB imports) so the
downsample/normalize math the timeline canvas depends on is unit-testable
without booting the whole app."""


def peak_envelope(amp, n: int) -> list[float]:
    """Downsample an absolute-amplitude array into `n` peak buckets,
    normalized to 0..1. Peak (not mean) per bucket so transients survive
    the downsample. Returns all-zeros for empty input."""
    import numpy as np
    if amp is None or len(amp) == 0:
        return [0.0] * n
    buckets = np.array_split(amp, n)
    raw = np.array([float(b.max()) if b.size else 0.0 for b in buckets])
    peak = float(raw.max()) or 1.0
    return [round(float(v) / peak, 3) for v in raw]
