"""Deterministic song-block bootstrap confidence intervals."""

from __future__ import annotations

import math
import random
from typing import Callable, Sequence, TypeVar

T = TypeVar("T")


def percentile(values: Sequence[float], fraction: float) -> float:
    if not values:
        raise ValueError("percentile requires values")
    ordered = sorted(float(value) for value in values)
    index = max(0, min(len(ordered) - 1, math.ceil(fraction * len(ordered)) - 1))
    return ordered[index]


def song_bootstrap_ci(
    songs: Sequence[T], statistic: Callable[[Sequence[T]], float],
    *, iterations: int = 2000, seed: int = 20260829, confidence: float = 0.95,
) -> dict[str, float | int]:
    if not songs:
        raise ValueError("song bootstrap requires at least one song")
    generator = random.Random(seed)
    estimates = []
    for _ in range(iterations):
        sample = [songs[generator.randrange(len(songs))] for _ in songs]
        estimates.append(float(statistic(sample)))
    tail = (1.0 - confidence) / 2.0
    return {
        "estimate": float(statistic(songs)),
        "low": percentile(estimates, tail),
        "high": percentile(estimates, 1.0 - tail),
        "confidence": confidence,
        "iterations": iterations,
        "seed": seed,
        "unit": "song",
    }
