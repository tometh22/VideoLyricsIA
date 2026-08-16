"""Small, dependency-light CTC dynamic programs used by quality v5."""
from __future__ import annotations

import math
from collections.abc import Sequence

import numpy as np


def _logadd(values: Sequence[float]) -> float:
    finite = [float(value) for value in values if math.isfinite(float(value))]
    if not finite:
        return float("-inf")
    maximum = max(finite)
    return maximum + math.log(sum(math.exp(value - maximum) for value in finite))


def ctc_forward_logprob(
    log_probs, target_ids: Sequence[int], blank_id: int,
) -> float:
    """Log P(target|frames), summing every legal CTC path via log-sum-exp."""
    emission = np.asarray(log_probs, dtype=np.float64)
    if emission.ndim != 2 or not len(emission):
        return float("-inf")
    blank_id = int(blank_id)
    targets = [int(value) for value in target_ids]
    if not 0 <= blank_id < emission.shape[1]:
        raise ValueError("blank_id outside emission vocabulary")
    if any(value == blank_id or not 0 <= value < emission.shape[1]
           for value in targets):
        raise ValueError("target ids must be non-blank vocabulary ids")
    if not targets:
        return float(np.sum(emission[:, blank_id]))

    states = [blank_id]
    for target in targets:
        states.extend((target, blank_id))
    previous = np.full(len(states), float("-inf"), dtype=np.float64)
    previous[0] = emission[0, blank_id]
    previous[1] = emission[0, states[1]]
    for frame in range(1, len(emission)):
        current = np.full(len(states), float("-inf"), dtype=np.float64)
        for state, token in enumerate(states):
            incoming = [previous[state]]
            if state >= 1:
                incoming.append(previous[state - 1])
            if (
                state >= 2 and token != blank_id
                and token != states[state - 2]
            ):
                incoming.append(previous[state - 2])
            current[state] = _logadd(incoming) + emission[frame, token]
        previous = current
    return _logadd((previous[-1], previous[-2]))


def blank_logprob(log_probs, blank_id: int) -> float:
    emission = np.asarray(log_probs, dtype=np.float64)
    if emission.ndim != 2 or not len(emission):
        return float("-inf")
    return float(np.sum(emission[:, int(blank_id)]))
