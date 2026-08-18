"""Hierarchical, text-independent acoustic Performance Graph for lyrics v6.

The module deliberately starts *after* feature extraction.  It consumes a
chronological sequence of primitive vocal regions plus optional per-region and
per-boundary acoustic features.  It does not inspect lyrics, artist names, song
IDs, ASR output, or reference text.

Two levels are inferred:

* :class:`AcousticSubevent` merges primitive cuts separated by ``CONTINUE``.
* :class:`PerformancePhrase` groups subevents separated by ``SUBEVENT`` and
  starts a new editable phrase at ``PHRASE``.

The phrase-count distribution is computed by exact forward dynamic
programming over the complete semi-Markov DAG.  Boundary marginals use the
matching backward pass.  ``n_best`` only controls the number of diagnostic
paths returned and therefore cannot change either posterior or best path.

All model values in this module are uncalibrated raw scores.  Mathematical
posteriors under this local model are exposed as such, while
``calibrated_confidence`` remains ``None`` until a separately validated
calibration layer attaches one.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, fields, is_dataclass
from enum import Enum
import hashlib
import json
import math
from typing import Any, Mapping, Sequence


POLICY_VERSION = "performance-graph-v6"
_NEG_INF = float("-inf")
_EPS = 1e-12
# Weak phrase-creation prior.  It prevents singleton rewards without erasing
# real short pauses/pitch resets; cardinality remains explicitly uncalibrated.
_PHRASE_CREATION_PRIOR = -.28


class BoundaryState(str, Enum):
    CONTINUE = "CONTINUE"
    SUBEVENT = "SUBEVENT"
    PHRASE = "PHRASE"


class VocalTaxonomy(str, Enum):
    SUNG_LEAD = "SUNG_LEAD"
    SUNG_CROWD = "SUNG_CROWD"
    SPEECH = "SPEECH"
    NONLEXICAL = "NONLEXICAL"
    METADATA = "METADATA"
    CROWD_NOISE = "CROWD_NOISE"
    UNKNOWN = "UNKNOWN"


class PhraseComposition(str, Enum):
    LEXICAL = "lexical"
    VOCALIZATION = "vocalization"
    SUSTAINED = "sustained"
    LEXICAL_PLUS_VOCALIZATION = "lexical_plus_vocalization"
    UNKNOWN = "unknown"


@dataclass(frozen=True)
class PrimitiveAcousticRegion:
    """A high-recall acoustic atom produced by an upstream front-end."""

    id: str
    start: float
    end: float
    features: Mapping[str, Any]


@dataclass(frozen=True)
class PerformanceBoundary:
    id: str
    left_primitive_id: str
    right_primitive_id: str
    time: float
    state: BoundaryState
    raw_scores: Mapping[str, float]
    raw_score: float
    state_posterior: Mapping[str, float]
    calibrated_confidence: float | None = None


@dataclass(frozen=True)
class AcousticSubevent:
    id: str
    acoustic_start: float
    acoustic_end: float
    display_end: float
    primitive_ids: tuple[str, ...]
    taxonomy: VocalTaxonomy
    raw_type_scores: Mapping[str, float]
    raw_composition_scores: Mapping[str, float]
    raw_score: float
    calibrated_confidence: float | None = None


@dataclass(frozen=True)
class PerformancePhrase:
    id: str
    acoustic_start: float
    acoustic_end: float
    display_end: float
    subevent_ids: tuple[str, ...]
    primitive_ids: tuple[str, ...]
    taxonomy: VocalTaxonomy
    composition: PhraseComposition
    raw_type_scores: Mapping[str, float]
    raw_composition_scores: Mapping[str, float]
    raw_score: float
    calibrated_confidence: float | None = None


@dataclass(frozen=True)
class PerformanceAlternative:
    id: str
    rank: int
    raw_score: float
    phrase_spans: tuple[tuple[int, int], ...]

    @property
    def phrase_count(self) -> int:
        return len(self.phrase_spans)


@dataclass(frozen=True)
class PerformanceGraph:
    id: str
    policy_version: str
    primitives: tuple[PrimitiveAcousticRegion, ...]
    boundaries: tuple[PerformanceBoundary, ...]
    subevents: tuple[AcousticSubevent, ...]
    phrases: tuple[PerformancePhrase, ...]
    cardinality_posterior: Mapping[int, float]
    alternatives: tuple[PerformanceAlternative, ...]
    raw_score: float
    calibrated_confidence: float | None
    calibration_status: str

    def to_dict(self) -> dict[str, Any]:
        return _jsonable(self)


def _finite(value: Any, default: float = 0.0) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    return number if math.isfinite(number) else default


def _clip(value: Any, low: float = 0.0, high: float = 1.0) -> float:
    return min(high, max(low, _finite(value)))


def _round_time(value: float) -> float:
    return round(float(value), 6)


def _logsumexp(values: Sequence[float]) -> float:
    finite = [value for value in values if value != _NEG_INF]
    if not finite:
        return _NEG_INF
    largest = max(finite)
    return largest + math.log(sum(math.exp(value - largest) for value in finite))


def _stable_id(prefix: str, payload: Any) -> str:
    encoded = json.dumps(
        _jsonable(payload), sort_keys=True, separators=(",", ":"),
        ensure_ascii=True,
    ).encode("utf-8")
    return f"{prefix}_{hashlib.sha256(encoded).hexdigest()[:20]}"


def _jsonable(value: Any) -> Any:
    if isinstance(value, Enum):
        return value.value
    if is_dataclass(value):
        return {
            item.name: _jsonable(getattr(value, item.name))
            for item in fields(value)
        }
    if isinstance(value, Mapping):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_jsonable(item) for item in value]
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    return value


def _feature_map(item: Mapping[str, Any]) -> dict[str, Any]:
    nested = item.get("features")
    output = dict(nested) if isinstance(nested, Mapping) else {}
    for key, value in item.items():
        if key not in {"id", "start", "end", "acoustic_start", "acoustic_end", "features"}:
            output.setdefault(str(key), value)
    return output


def _normalize_primitives(
    primitive_regions: Sequence[PrimitiveAcousticRegion | Mapping[str, Any] | Sequence[Any]],
) -> tuple[PrimitiveAcousticRegion, ...]:
    normalized: list[tuple[float, float, dict[str, Any]]] = []
    for index, item in enumerate(primitive_regions):
        if isinstance(item, PrimitiveAcousticRegion):
            start, end, feature_map = item.start, item.end, dict(item.features)
        elif isinstance(item, Mapping):
            start = item.get("start", item.get("acoustic_start"))
            end = item.get("end", item.get("acoustic_end"))
            feature_map = _feature_map(item)
        elif isinstance(item, Sequence) and not isinstance(item, (str, bytes)):
            if len(item) not in {2, 3}:
                raise ValueError(f"primitive {index} must contain start, end[, features]")
            start, end = item[0], item[1]
            feature_map = dict(item[2]) if len(item) == 3 and isinstance(item[2], Mapping) else {}
        else:
            raise TypeError(f"unsupported primitive type at index {index}")
        start_value = _finite(start, float("nan"))
        end_value = _finite(end, float("nan"))
        if not math.isfinite(start_value) or not math.isfinite(end_value) or end_value <= start_value:
            raise ValueError(f"primitive {index} has an invalid acoustic range")
        normalized.append((_round_time(start_value), _round_time(end_value), feature_map))

    normalized.sort(key=lambda row: (row[0], row[1]))
    if len(normalized) > 64:
        raise ValueError("performance graph supports at most 64 primitives per window")
    for index in range(1, len(normalized)):
        if normalized[index][0] < normalized[index - 1][1] - 1e-6:
            raise ValueError("primitive acoustic regions must not overlap")

    result = []
    for start, end, feature_map in normalized:
        primitive_id = _stable_id(
            "apr", {"policy": POLICY_VERSION, "start": start, "end": end},
        )
        result.append(PrimitiveAcousticRegion(
            id=primitive_id, start=start, end=end, features=feature_map,
        ))
    return tuple(result)


def _lookup(features: Mapping[str, Any], *keys: str, default: float = 0.0) -> float:
    for key in keys:
        if key in features:
            return _finite(features[key], default)
    return default


def _boundary_feature_at(
    boundary_features: Sequence[Mapping[str, Any]] | Mapping[Any, Mapping[str, Any]] | None,
    index: int,
    left: PrimitiveAcousticRegion,
    right: PrimitiveAcousticRegion,
) -> dict[str, Any]:
    if boundary_features is None:
        return {}
    if isinstance(boundary_features, Mapping):
        keys = (
            index, str(index), f"{left.id}:{right.id}",
            (left.id, right.id),
        )
        for key in keys:
            value = boundary_features.get(key)
            if isinstance(value, Mapping):
                return dict(value)
        return {}
    if index >= len(boundary_features):
        raise ValueError("boundary_features must contain one entry per primitive gap")
    value = boundary_features[index]
    if not isinstance(value, Mapping):
        raise TypeError(f"boundary feature {index} must be a mapping")
    return dict(value)


def _boundary_raw_scores(
    left: PrimitiveAcousticRegion,
    right: PrimitiveAcousticRegion,
    features: Mapping[str, Any],
) -> dict[str, float]:
    supplied = features.get("raw_scores")
    if isinstance(supplied, Mapping) and all(
        state.value in supplied or state in supplied for state in BoundaryState
    ):
        return {
            state.value: _finite(supplied.get(state.value, supplied.get(state)))
            for state in BoundaryState
        }
    explicit_keys = {
        BoundaryState.CONTINUE: "continue_score",
        BoundaryState.SUBEVENT: "subevent_score",
        BoundaryState.PHRASE: "phrase_score",
    }
    if all(key in features for key in explicit_keys.values()):
        return {
            state.value: _finite(features[key])
            for state, key in explicit_keys.items()
        }

    gap = max(0.0, right.start - left.end)
    silence = _clip(features.get("silence_probability", gap / .80))
    onset = _clip(features.get(
        "onset_strength",
        _lookup(right.features, "onset_strength", "onset", default=0.0),
    ))
    reset = _clip(features.get(
        "reset_probability",
        max(
            _lookup(features, "pitch_reset", "timbre_reset", default=0.0),
            _lookup(right.features, "pitch_reset", "timbre_reset", default=0.0),
        ),
    ))
    breath = _clip(features.get("breath_probability", 0.0))
    internal_change = _clip(features.get(
        "internal_change",
        max(reset, _lookup(features, "timbre_change", default=0.0)),
    ))
    default_continuity = math.exp(-gap / .35)
    continuity = _clip(features.get(
        "continuity",
        _lookup(features, "pitch_continuity", "embedding_similarity",
                default=default_continuity),
    ))

    # These are transparent log-potentials, not calibrated probabilities.
    continue_score = .25 + 1.50 * continuity - 1.60 * silence - .80 * reset
    subevent_score = (
        .10 + .80 * (1.0 - continuity) + .65 * onset
        + .70 * internal_change + .45 * breath - 1.10 * silence
    )
    phrase_score = (
        -.15 + 1.80 * silence + 1.00 * reset + .65 * onset
        - .60 * continuity
    )
    return {
        BoundaryState.CONTINUE.value: round(continue_score, 10),
        BoundaryState.SUBEVENT.value: round(subevent_score, 10),
        BoundaryState.PHRASE.value: round(phrase_score, 10),
    }


def _taxonomy_scores(features: Mapping[str, Any]) -> dict[str, float]:
    supplied = features.get("taxonomy_scores")
    if isinstance(supplied, Mapping):
        return {
            taxonomy.value: _finite(supplied.get(taxonomy.value, supplied.get(taxonomy)), 0.0)
            for taxonomy in VocalTaxonomy
        }

    voicing = _clip(_lookup(features, "voicing", "voiced_probability", default=0.0))
    harmonicity = _clip(_lookup(features, "harmonicity", default=0.0))
    lexical = _clip(_lookup(features, "lexical_probability", "articulated_probability", default=0.0))
    lead = _clip(_lookup(features, "sung_lead_probability", "lead_presence", default=0.0))
    crowd = _clip(_lookup(features, "sung_crowd_probability", "crowd_presence", default=0.0))
    speech = _clip(_lookup(features, "speech_probability", default=0.0))
    nonlexical = _clip(_lookup(features, "nonlexical_probability", "vocalization_probability", default=0.0))
    metadata = _clip(_lookup(features, "metadata_probability", default=0.0))
    crowd_noise = _clip(_lookup(features, "crowd_noise_probability", default=0.0))
    acoustic_voice = .58 * voicing + .42 * harmonicity
    return {
        VocalTaxonomy.SUNG_LEAD.value: max(lead, acoustic_voice * (.55 + .45 * lexical)),
        VocalTaxonomy.SUNG_CROWD.value: crowd,
        VocalTaxonomy.SPEECH.value: speech,
        VocalTaxonomy.NONLEXICAL.value: nonlexical,
        VocalTaxonomy.METADATA.value: metadata,
        VocalTaxonomy.CROWD_NOISE.value: crowd_noise,
        VocalTaxonomy.UNKNOWN.value: max(.05, .45 * (1.0 - max(acoustic_voice, speech, crowd_noise))),
    }


def _composition_scores(features: Mapping[str, Any]) -> dict[str, float]:
    supplied = features.get("composition_scores")
    if isinstance(supplied, Mapping):
        return {
            composition.value: _finite(
                supplied.get(composition.value, supplied.get(composition)), 0.0,
            )
            for composition in PhraseComposition
        }
    lexical = _clip(_lookup(features, "lexical_probability", "articulated_probability", default=0.0))
    vocalization = _clip(_lookup(features, "nonlexical_probability", "vocalization_probability", default=0.0))
    sustained = _clip(_lookup(features, "sustained_probability", default=0.0))
    mixed = 1.35 * min(lexical, max(vocalization, sustained))
    return {
        PhraseComposition.LEXICAL.value: lexical * (1.0 - .40 * max(vocalization, sustained)),
        PhraseComposition.VOCALIZATION.value: vocalization * (1.0 - .40 * lexical),
        PhraseComposition.SUSTAINED.value: sustained * (1.05 - .25 * lexical),
        PhraseComposition.LEXICAL_PLUS_VOCALIZATION.value: mixed,
        PhraseComposition.UNKNOWN.value: max(.05, .50 * (1.0 - max(lexical, vocalization, sustained))),
    }


def _weighted_feature_scores(
    primitives: Sequence[PrimitiveAcousticRegion],
    scorer,
) -> dict[str, float]:
    totals: dict[str, float] = {}
    total_weight = 0.0
    for primitive in primitives:
        weight = max(.001, primitive.end - primitive.start)
        total_weight += weight
        for key, value in scorer(primitive.features).items():
            totals[key] = totals.get(key, 0.0) + weight * _finite(value)
    return {
        key: round(value / max(_EPS, total_weight), 10)
        for key, value in totals.items()
    }


def _group_composition_scores(
    primitives: Sequence[PrimitiveAcousticRegion],
) -> dict[str, float]:
    """Aggregate composition while preserving heterogeneous child roles.

    Averaging already-classified primitive compositions loses a phrase made of
    one lexical child followed by one vocalization child: no individual child
    is mixed, but their parent is.  The parent score therefore combines the
    duration-weighted evidence with the strongest lexical and vocalization
    evidence found anywhere among its children.
    """
    averaged = _weighted_feature_scores(primitives, _composition_scores)
    lexical_peak = max(
        _clip(_lookup(item.features, "lexical_probability", "articulated_probability", default=0.0))
        for item in primitives
    )
    vocal_peak = max(
        max(
            _clip(_lookup(item.features, "nonlexical_probability", "vocalization_probability", default=0.0)),
            _clip(_lookup(item.features, "sustained_probability", default=0.0)),
        )
        for item in primitives
    )
    averaged[PhraseComposition.LEXICAL_PLUS_VOCALIZATION.value] = round(max(
        averaged.get(PhraseComposition.LEXICAL_PLUS_VOCALIZATION.value, 0.0),
        1.35 * min(lexical_peak, vocal_peak),
    ), 10)
    return averaged


def _selected_taxonomy(scores: Mapping[str, float]) -> VocalTaxonomy:
    return max(VocalTaxonomy, key=lambda item: (_finite(scores.get(item.value)), -list(VocalTaxonomy).index(item)))


def _selected_composition(scores: Mapping[str, float]) -> PhraseComposition:
    return max(PhraseComposition, key=lambda item: (_finite(scores.get(item.value)), -list(PhraseComposition).index(item)))


def _span_raw_score(primitives: Sequence[PrimitiveAcousticRegion], start: int, end: int) -> float:
    """Acoustic phrase-span potential, exclusive of boundary state scores."""
    duration = primitives[end].end - primitives[start].start
    short_penalty = 2.40 * max(0.0, .18 - duration)
    # Duration is weak evidence only.  An unbounded long-span penalty
    # mathematically forced neutral sustained phrases to split even when no
    # acoustic boundary supported that decision.  Cap it below the cost of
    # creating an extra phrase; genuine PHRASE boundaries can still dominate.
    long_penalty = min(.18, .18 * max(0.0, duration - 10.0) ** 1.35)
    # Cohesion is evidence for joining *adjacent* primitives.  The old score
    # added a positive average once per phrase; splitting a cohesive motif into
    # four singleton phrases therefore collected the reward four times.  Here
    # the reward exists only for internal joins, while every phrase pays one
    # explicit prior.  Neutral/tied boundaries consequently prefer the least
    # fragmented path without preventing a strong PHRASE boundary from winning.
    internal_cohesion = 0.0
    for index in range(start, end):
        left = _clip(_lookup(
            primitives[index].features, "phrase_cohesion", default=.5,
        ))
        right = _clip(_lookup(
            primitives[index + 1].features, "phrase_cohesion", default=.5,
        ))
        internal_cohesion += min(left, right)

    # Recurrence is a soft structural cue, not a per-phrase bounty.  It can
    # support a multi-primitive phrase only when an internal join is actually
    # proposed; singleton spans receive no recurrence bonus.
    recurrence = 0.0
    if end > start:
        recurrence = max(
            _clip(_lookup(
                primitives[index].features,
                "motif_recurrence", "recurrence", default=0.0,
            ))
            for index in range(start, end + 1)
        )
    return round(
        _PHRASE_CREATION_PRIOR - short_penalty - long_penalty
        + .18 * internal_cohesion
        + .06 * recurrence * math.log1p(end - start),
        10,
    )


def _lattice(
    primitives: Sequence[PrimitiveAcousticRegion],
    boundary_scores: Sequence[Mapping[str, float]],
) -> tuple[
    dict[tuple[int, int], float],
    dict[tuple[int, int], float],
    dict[tuple[int, int], float],
]:
    span_raw: dict[tuple[int, int], float] = {}
    span_weight: dict[tuple[int, int], float] = {}
    span_viterbi_weight: dict[tuple[int, int], float] = {}
    count = len(primitives)
    for start in range(count):
        internal = 0.0
        internal_viterbi = 0.0
        for end in range(start, count):
            if end > start:
                raw = boundary_scores[end - 1]
                internal += _logsumexp([
                    raw[BoundaryState.CONTINUE.value],
                    raw[BoundaryState.SUBEVENT.value],
                ])
                internal_viterbi += max(
                    raw[BoundaryState.CONTINUE.value],
                    raw[BoundaryState.SUBEVENT.value],
                )
            acoustic = _span_raw_score(primitives, start, end)
            terminal = (
                boundary_scores[end][BoundaryState.PHRASE.value]
                if end < count - 1 else 0.0
            )
            span_raw[(start, end)] = acoustic
            span_weight[(start, end)] = acoustic + internal + terminal
            span_viterbi_weight[(start, end)] = (
                acoustic + internal_viterbi + terminal
            )
    return span_raw, span_weight, span_viterbi_weight


def _forward_backward(
    count: int,
    span_weight: Mapping[tuple[int, int], float],
    boundary_scores: Sequence[Mapping[str, float]],
) -> tuple[dict[int, float], list[dict[str, float]], float]:
    if count == 0:
        return {0: 1.0}, [], 0.0

    cardinality = [[_NEG_INF] * (count + 1) for _ in range(count + 1)]
    cardinality[0][0] = 0.0
    for end_position in range(1, count + 1):
        for start in range(end_position):
            weight = span_weight[(start, end_position - 1)]
            for phrase_count in range(1, end_position + 1):
                previous = cardinality[start][phrase_count - 1]
                if previous == _NEG_INF:
                    continue
                cardinality[end_position][phrase_count] = _logsumexp([
                    cardinality[end_position][phrase_count], previous + weight,
                ])
    log_partition = _logsumexp(cardinality[count][1:])
    posterior = {
        phrase_count: math.exp(value - log_partition)
        for phrase_count, value in enumerate(cardinality[count])
        if phrase_count > 0 and value != _NEG_INF
    }

    forward = [_NEG_INF] * (count + 1)
    backward = [_NEG_INF] * (count + 1)
    forward[0] = 0.0
    backward[count] = 0.0
    for end_position in range(1, count + 1):
        forward[end_position] = _logsumexp([
            forward[start] + span_weight[(start, end_position - 1)]
            for start in range(end_position)
        ])
    for start in range(count - 1, -1, -1):
        backward[start] = _logsumexp([
            span_weight[(start, end_position - 1)] + backward[end_position]
            for end_position in range(start + 1, count + 1)
        ])

    marginals: list[dict[str, float]] = []
    for boundary_index in range(count - 1):
        phrase_terms = [
            forward[start]
            + span_weight[(start, boundary_index)]
            + backward[boundary_index + 1]
            for start in range(boundary_index + 1)
        ]
        internal_base_terms = []
        nonphrase_norm = _logsumexp([
            boundary_scores[boundary_index][BoundaryState.CONTINUE.value],
            boundary_scores[boundary_index][BoundaryState.SUBEVENT.value],
        ])
        for start in range(boundary_index + 1):
            for end in range(boundary_index + 1, count):
                internal_base_terms.append(
                    forward[start] + span_weight[(start, end)]
                    + backward[end + 1] - nonphrase_norm
                )
        continue_terms = [
            term + boundary_scores[boundary_index][BoundaryState.CONTINUE.value]
            for term in internal_base_terms
        ]
        subevent_terms = [
            term + boundary_scores[boundary_index][BoundaryState.SUBEVENT.value]
            for term in internal_base_terms
        ]
        logs = {
            BoundaryState.CONTINUE.value: _logsumexp(continue_terms),
            BoundaryState.SUBEVENT.value: _logsumexp(subevent_terms),
            BoundaryState.PHRASE.value: _logsumexp(phrase_terms),
        }
        values = {
            key: math.exp(value - log_partition)
            for key, value in logs.items()
        }
        total = sum(values.values())
        marginals.append({key: value / max(_EPS, total) for key, value in values.items()})
    return posterior, marginals, log_partition


def _k_best_paths(
    count: int,
    span_weight: Mapping[tuple[int, int], float],
    n_best: int,
) -> list[tuple[float, tuple[tuple[int, int], ...]]]:
    keep = max(1, int(n_best))
    paths: list[list[tuple[float, tuple[tuple[int, int], ...]]]] = [[] for _ in range(count + 1)]
    paths[0] = [(0.0, ())]
    for end_position in range(1, count + 1):
        candidates = []
        for start in range(end_position):
            for prefix_score, prefix in paths[start]:
                candidates.append((
                    prefix_score + span_weight[(start, end_position - 1)],
                    prefix + ((start, end_position - 1),),
                ))
        # Fewer spans win exact score ties.  Acoustic PHRASE evidence still
        # changes the score; this tie-break only removes the former accidental
        # lexicographic preference for ((0, 0), (1, 1), ...).
        candidates.sort(key=lambda item: (-item[0], len(item[1]), item[1]))
        seen = set()
        for candidate in candidates:
            if candidate[1] in seen:
                continue
            seen.add(candidate[1])
            paths[end_position].append(candidate)
            if len(paths[end_position]) >= keep:
                break
    return paths[count]


def _best_boundary_states(
    phrase_spans: Sequence[tuple[int, int]],
    boundary_scores: Sequence[Mapping[str, float]],
) -> list[BoundaryState]:
    states = [BoundaryState.CONTINUE] * len(boundary_scores)
    phrase_ends = {end for _, end in phrase_spans[:-1]}
    for index, scores in enumerate(boundary_scores):
        if index in phrase_ends:
            states[index] = BoundaryState.PHRASE
        else:
            states[index] = max(
                (BoundaryState.CONTINUE, BoundaryState.SUBEVENT),
                key=lambda state: (scores[state.value], state.value),
            )
    return states


def _build_subevents(
    primitives: Sequence[PrimitiveAcousticRegion],
    states: Sequence[BoundaryState],
) -> tuple[AcousticSubevent, ...]:
    if not primitives:
        return ()
    groups: list[tuple[int, int]] = []
    start = 0
    for boundary_index, state in enumerate(states):
        if state is not BoundaryState.CONTINUE:
            groups.append((start, boundary_index))
            start = boundary_index + 1
    groups.append((start, len(primitives) - 1))

    output = []
    for left, right in groups:
        members = primitives[left:right + 1]
        type_scores = _weighted_feature_scores(members, _taxonomy_scores)
        composition_scores = _group_composition_scores(members)
        taxonomy = _selected_taxonomy(type_scores)
        primitive_ids = tuple(item.id for item in members)
        event_id = _stable_id("ase", {
            "policy": POLICY_VERSION, "primitive_ids": primitive_ids,
        })
        output.append(AcousticSubevent(
            id=event_id,
            acoustic_start=members[0].start,
            acoustic_end=members[-1].end,
            display_end=members[-1].end,
            primitive_ids=primitive_ids,
            taxonomy=taxonomy,
            raw_type_scores=type_scores,
            raw_composition_scores=composition_scores,
            raw_score=max(type_scores.values(), default=0.0),
            calibrated_confidence=None,
        ))
    return tuple(output)


def _build_phrases(
    primitives: Sequence[PrimitiveAcousticRegion],
    subevents: Sequence[AcousticSubevent],
    states: Sequence[BoundaryState],
    span_raw: Mapping[tuple[int, int], float],
    *,
    max_display_hold_gap: float,
) -> tuple[PerformancePhrase, ...]:
    if not primitives:
        return ()
    primitive_index = {primitive.id: index for index, primitive in enumerate(primitives)}
    groups: list[list[AcousticSubevent]] = [[]]
    for subevent in subevents:
        if groups[-1]:
            previous_end = primitive_index[groups[-1][-1].primitive_ids[-1]]
            if previous_end < len(states) and states[previous_end] is BoundaryState.PHRASE:
                groups.append([])
        groups[-1].append(subevent)

    phrases = []
    for group in groups:
        left = primitive_index[group[0].primitive_ids[0]]
        right = primitive_index[group[-1].primitive_ids[-1]]
        members = primitives[left:right + 1]
        type_scores = _weighted_feature_scores(members, _taxonomy_scores)
        composition_scores = _group_composition_scores(members)
        primitive_ids = tuple(item.id for item in members)
        subevent_ids = tuple(item.id for item in group)
        phrase_id = _stable_id("aph", {
            "policy": POLICY_VERSION,
            "primitive_ids": primitive_ids,
            "subevent_ids": subevent_ids,
        })
        phrases.append(PerformancePhrase(
            id=phrase_id,
            acoustic_start=members[0].start,
            acoustic_end=members[-1].end,
            display_end=members[-1].end,
            subevent_ids=subevent_ids,
            primitive_ids=primitive_ids,
            taxonomy=_selected_taxonomy(type_scores),
            composition=_selected_composition(composition_scores),
            raw_type_scores=type_scores,
            raw_composition_scores=composition_scores,
            raw_score=span_raw[(left, right)],
            calibrated_confidence=None,
        ))

    # A mixed lexical/vocalization phrase may visually hold to the next
    # acoustic phrase onset. Its measured acoustic end remains immutable.
    held = []
    for index, phrase in enumerate(phrases):
        display_end = phrase.acoustic_end
        if (
            phrase.composition is PhraseComposition.LEXICAL_PLUS_VOCALIZATION
            and index + 1 < len(phrases)
        ):
            gap = phrases[index + 1].acoustic_start - phrase.acoustic_end
            if 0.0 <= gap <= max_display_hold_gap:
                display_end = phrases[index + 1].acoustic_start
        held.append(PerformancePhrase(
            **{**asdict(phrase), "display_end": _round_time(display_end)},
        ))
    return tuple(held)


def build_performance_graph(
    primitive_regions: Sequence[PrimitiveAcousticRegion | Mapping[str, Any] | Sequence[Any]],
    boundary_features: Sequence[Mapping[str, Any]] | Mapping[Any, Mapping[str, Any]] | None = None,
    *,
    n_best: int = 8,
    max_display_hold_gap: float = 2.5,
) -> PerformanceGraph:
    """Build a deterministic hierarchical Performance Graph.

    ``n_best`` affects only :attr:`PerformanceGraph.alternatives`. Exact
    inference always evaluates the complete semi-Markov DAG.
    """
    if n_best < 0:
        raise ValueError("n_best must be non-negative")
    if max_display_hold_gap < 0:
        raise ValueError("max_display_hold_gap must be non-negative")
    primitives = _normalize_primitives(primitive_regions)
    if not primitives:
        graph_id = _stable_id("apg", {"policy": POLICY_VERSION, "primitives": []})
        return PerformanceGraph(
            id=graph_id, policy_version=POLICY_VERSION, primitives=(),
            boundaries=(), subevents=(), phrases=(),
            cardinality_posterior={0: 1.0}, alternatives=(), raw_score=0.0,
            calibrated_confidence=None, calibration_status="uncalibrated",
        )

    score_rows = []
    feature_rows = []
    for index in range(len(primitives) - 1):
        row = _boundary_feature_at(
            boundary_features, index, primitives[index], primitives[index + 1],
        )
        feature_rows.append(row)
        score_rows.append(_boundary_raw_scores(primitives[index], primitives[index + 1], row))

    span_raw, span_weight, span_viterbi_weight = _lattice(primitives, score_rows)
    cardinality, boundary_marginals, _ = _forward_backward(
        len(primitives), span_weight, score_rows,
    )
    paths = _k_best_paths(
        len(primitives), span_viterbi_weight, max(1, n_best),
    )
    best_score, best_spans = paths[0]
    states = _best_boundary_states(best_spans, score_rows)

    boundaries = []
    for index, (scores, marginal, state) in enumerate(zip(
        score_rows, boundary_marginals, states,
    )):
        left, right = primitives[index], primitives[index + 1]
        boundary_id = _stable_id("apb", {
            "policy": POLICY_VERSION,
            "left": left.id,
            "right": right.id,
        })
        boundaries.append(PerformanceBoundary(
            id=boundary_id,
            left_primitive_id=left.id,
            right_primitive_id=right.id,
            time=_round_time((left.end + right.start) / 2.0),
            state=state,
            raw_scores=dict(scores),
            raw_score=scores[state.value],
            state_posterior=dict(marginal),
            calibrated_confidence=None,
        ))

    subevents = _build_subevents(primitives, states)
    phrases = _build_phrases(
        primitives, subevents, states, span_raw,
        max_display_hold_gap=max_display_hold_gap,
    )
    alternatives = []
    if n_best:
        for rank, (score, spans) in enumerate(paths[:n_best], 1):
            alternatives.append(PerformanceAlternative(
                id=_stable_id("apa", {
                    "policy": POLICY_VERSION,
                    "primitive_ids": [item.id for item in primitives],
                    "spans": spans,
                }),
                rank=rank,
                raw_score=round(score, 10),
                phrase_spans=spans,
            ))
    graph_id = _stable_id("apg", {
        "policy": POLICY_VERSION,
        "primitive_ids": [item.id for item in primitives],
        "boundary_states": [item.state.value for item in boundaries],
        "phrases": [
            {
                "id": item.id,
                "taxonomy": item.taxonomy.value,
                "composition": item.composition.value,
            }
            for item in phrases
        ],
    })
    return PerformanceGraph(
        id=graph_id,
        policy_version=POLICY_VERSION,
        primitives=primitives,
        boundaries=tuple(boundaries),
        subevents=subevents,
        phrases=phrases,
        cardinality_posterior=cardinality,
        alternatives=tuple(alternatives),
        raw_score=round(best_score, 10),
        calibrated_confidence=None,
        calibration_status="uncalibrated",
    )


def _positive_distribution(values: Mapping[str, float]) -> dict[str, float]:
    shifted = {key: max(0.0, _finite(value)) for key, value in values.items()}
    total = sum(shifted.values())
    if total <= _EPS:
        return {key: 0.0 for key in shifted}
    return {key: value / total for key, value in shifted.items()}


def _legacy_type_posterior(
    raw_composition_scores: Mapping[str, float],
    raw_type_scores: Mapping[str, float],
) -> dict[str, float]:
    composition = _positive_distribution(raw_composition_scores)
    taxonomy = _positive_distribution(raw_type_scores)
    legacy_type = {
        "silence": 0.0,
        "short_vocalization": composition.get(PhraseComposition.VOCALIZATION.value, 0.0),
        "sustained_vocalization": composition.get(PhraseComposition.SUSTAINED.value, 0.0),
        "lexical_phrase": (
            composition.get(PhraseComposition.LEXICAL.value, 0.0)
            + .65 * composition.get(PhraseComposition.LEXICAL_PLUS_VOCALIZATION.value, 0.0)
        ),
        "crowd_or_overlap": (
            taxonomy.get(VocalTaxonomy.SUNG_CROWD.value, 0.0)
            + taxonomy.get(VocalTaxonomy.CROWD_NOISE.value, 0.0)
        ),
    }
    return _positive_distribution(legacy_type)


def _legacy_event(phrase: PerformancePhrase) -> dict[str, Any]:
    legacy_type = _legacy_type_posterior(
        phrase.raw_composition_scores, phrase.raw_type_scores,
    )
    return {
        "id": phrase.id,
        "start": phrase.acoustic_start,
        "end": phrase.display_end,
        "acoustic_start": phrase.acoustic_start,
        "acoustic_end": phrase.acoustic_end,
        "display_end": phrase.display_end,
        "acoustic_event_ids": list(phrase.subevent_ids),
        "primitive_ids": list(phrase.primitive_ids),
        "taxonomy": phrase.taxonomy.value,
        "composition": phrase.composition.value,
        "type_posterior": legacy_type,
        "raw_type_scores": dict(phrase.raw_type_scores),
        "raw_composition_scores": dict(phrase.raw_composition_scores),
        "raw_score": phrase.raw_score,
        "confidence": phrase.calibrated_confidence,
        "calibrated_confidence": phrase.calibrated_confidence,
        "confidence_kind": "calibrated" if phrase.calibrated_confidence is not None else "uncalibrated",
    }


def to_legacy_acoustic_structure(graph: PerformanceGraph) -> dict[str, Any]:
    """Adapt a v6 graph to the read shape produced by ``acoustic_structure``.

    The adapter never promotes raw scores into legacy ``confidence``.
    """
    best_events = [_legacy_event(phrase) for phrase in graph.phrases]
    alternative_rows = []
    for alternative in graph.alternatives:
        events = []
        for left, right in alternative.phrase_spans:
            members = graph.primitives[left:right + 1]
            raw_type_scores = _weighted_feature_scores(members, _taxonomy_scores)
            raw_composition_scores = _group_composition_scores(members)
            taxonomy = _selected_taxonomy(raw_type_scores)
            composition = _selected_composition(raw_composition_scores)
            event_id = _stable_id("aph", {
                "policy": POLICY_VERSION,
                "primitive_ids": [item.id for item in members],
                "alternative": alternative.id,
            })
            events.append({
                "id": event_id,
                "start": members[0].start,
                "end": members[-1].end,
                "acoustic_start": members[0].start,
                "acoustic_end": members[-1].end,
                "display_end": members[-1].end,
                "primitive_ids": [item.id for item in members],
                "taxonomy": taxonomy.value,
                "composition": composition.value,
                "type_posterior": _legacy_type_posterior(
                    raw_composition_scores, raw_type_scores,
                ),
                "raw_type_scores": raw_type_scores,
                "raw_composition_scores": raw_composition_scores,
                "raw_score": _span_raw_score(graph.primitives, left, right),
                "confidence": None,
                "calibrated_confidence": None,
                "confidence_kind": "uncalibrated",
            })
        alternative_rows.append({
            "rank": alternative.rank,
            "score": round(-alternative.raw_score, 10),
            "raw_score": alternative.raw_score,
            "event_count": alternative.phrase_count,
            "events": events,
        })

    best_partition = {
        "rank": 1,
        "score": round(-graph.raw_score, 10),
        "raw_score": graph.raw_score,
        "event_count": len(best_events),
        "events": best_events,
    } if best_events else None
    return {
        "policy_version": graph.policy_version,
        "accepted": bool(best_events),
        "reason": "analyzed" if best_events else "no_vocal_events",
        "performance_graph_id": graph.id,
        "primitive_regions": [
            [primitive.start, primitive.end] for primitive in graph.primitives
        ],
        "boundaries": [{
            "id": boundary.id,
            "time": boundary.time,
            "state": boundary.state.value,
            "raw_scores": dict(boundary.raw_scores),
            "raw_score": boundary.raw_score,
            "state_posterior": dict(boundary.state_posterior),
            "calibrated_confidence": boundary.calibrated_confidence,
        } for boundary in graph.boundaries],
        "subevents": [_jsonable(subevent) for subevent in graph.subevents],
        "best_partition": best_partition,
        "n_best": alternative_rows,
        "motif_groups": [],
        "cardinality_posterior": {
            str(key): round(value, 12)
            for key, value in sorted(graph.cardinality_posterior.items())
        },
        "automatic_apply_allowed": False,
        "diagnostics": {
            "text_independent": True,
            "hierarchical": True,
            "cardinality_inference": "exact_forward_backward",
            "n_best_affects_inference": False,
            "confidence_calibrated": graph.calibrated_confidence is not None,
            "calibration_status": graph.calibration_status,
        },
    }


__all__ = [
    "POLICY_VERSION",
    "AcousticSubevent",
    "BoundaryState",
    "PerformanceAlternative",
    "PerformanceBoundary",
    "PerformanceGraph",
    "PerformancePhrase",
    "PhraseComposition",
    "PrimitiveAcousticRegion",
    "VocalTaxonomy",
    "build_performance_graph",
    "to_legacy_acoustic_structure",
]
