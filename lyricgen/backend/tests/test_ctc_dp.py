import itertools
import math

import numpy as np
import pytest

import ctc_align
from ctc_dp import blank_logprob, ctc_forward_logprob


def _collapse(path, blank=0):
    collapsed = []
    previous = None
    for token in path:
        if token != previous and token != blank:
            collapsed.append(token)
        previous = token
    return collapsed


def _exhaustive(log_probs, target, blank=0):
    total = 0.0
    for path in itertools.product(range(log_probs.shape[1]), repeat=len(log_probs)):
        if _collapse(path, blank) == list(target):
            total += math.exp(sum(log_probs[t, token] for t, token in enumerate(path)))
    return math.log(total)


@pytest.mark.parametrize("target", [[], [1], [1, 2], [1, 1]])
def test_forward_matches_exhaustive_small_lattice(target):
    probs = np.asarray([
        [.55, .35, .10], [.40, .35, .25], [.45, .15, .40],
    ])
    log_probs = np.log(probs)
    if target == [1, 1]:
        # Three frames are exactly the minimum: 1, blank, 1.
        pass
    assert ctc_forward_logprob(log_probs, target, 0) == pytest.approx(
        _exhaustive(log_probs, target), abs=1e-10,
    )


def test_blank_null_is_exact_all_blank_path():
    log_probs = np.log(np.asarray([[.8, .2], [.7, .3]]))
    assert blank_logprob(log_probs, 0) == pytest.approx(math.log(.8) + math.log(.7))


def test_structural_scorer_uses_forward_probability_and_blank_null():
    dictionary = {"r": 1, "e": 2, "a": 3, "l": 4, "n": 5, "o": 6}
    probs = np.full((20, 7), .01, dtype=float)
    probs[:, 0] = .94
    for frame, token in zip((2, 5, 8, 11), (1, 2, 3, 4)):
        probs[frame, :] = .01
        probs[frame, 0] = .14
        probs[frame, token] = .80
    probs /= probs.sum(axis=1, keepdims=True)
    bundle = {
        "emission": np.log(probs), "dictionary": dictionary,
        "blank_id": 0, "frame_seconds": .1, "window": [0.0, 2.0],
    }
    scored = ctc_align.score_structural_candidates_from_emission(bundle, [
        {"candidate_id": "real", "texts": ["real"], "anchors": [0.0]},
        {"candidate_id": "no", "texts": ["no"], "anchors": [0.0]},
    ])
    assert [candidate["candidate_id"] for candidate in scored] == ["real", "no"]
    assert scored[0]["mean_score"] > scored[1]["mean_score"]


def test_sustained_spelling_collapses_only_for_scoring():
    assert ctc_align.singing_scoring_projection("¡nooooooooo!") == "no"
    assert ctc_align.singing_scoring_projection("Real, uoh uoh") == "real uoh uoh"


def test_sustained_spelling_uses_projected_coverage_not_surface_length():
    dictionary = {"n": 1, "o": 2}
    probs = np.full((20, 3), .01, dtype=float)
    probs[:, 0] = .94
    probs[3] = [.15, .80, .05]
    probs[9] = [.15, .05, .80]
    probs /= probs.sum(axis=1, keepdims=True)
    bundle = {
        "emission": np.log(probs), "dictionary": dictionary,
        "blank_id": 0, "frame_seconds": .1, "window": [0.0, 2.0],
    }
    scored = ctc_align.score_structural_candidates_from_emission(bundle, [{
        "candidate_id": "held", "texts": ["nooooooooo"], "anchors": [0.0],
    }])
    assert len(scored) == 1
    assert scored[0]["events"][0]["scoring_projection"] == "no"
    assert scored[0]["events"][0]["text"] == "nooooooooo"
    assert scored[0]["events"][0]["surface_spelling_verified"] is False
