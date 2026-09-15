"""Versioned gold contract and metrics for sung-line display endpoints.

This module is deliberately offline and side-effect free.  It does not read the
application database, call a provider, render media, or mutate lyric timings.
The unit under evaluation is an interval judged by a human, not a single
timestamp and not a model-authored confidence score.
"""
from __future__ import annotations

import math
from collections import Counter, defaultdict
from typing import Any, Iterable, Mapping


SCHEMA = "timing-endpoint-gold-v1"
PREDICTION_SCHEMA = "timing-endpoint-prediction-v1"
LABEL_CLASSES = {"short", "correct", "ambiguous"}
SAMPLE_ROLES = {"random", "difficult", "control"}
ANNOTATION_MODES = {"blind", "anchored"}
SPLITS = {"train", "calibration", "test"}
DECISIONS = {"apply", "abstain"}
VOCAL_ATTRIBUTIONS = {"target_singer", "relevant_ensemble", "ambiguous"}
TASK_TYPES = {"text", "timing", "reference", "other"}
_TEXT_FIELDS = {"text", "lyric_text", "lyrics", "reference_text"}
_DEPENDENCY_FIELDS = (
    "song_group_id", "artist_group_id", "recording_group_id", "job_id",
    "audio_sha256", "evaluation_unit_id",
)


class GoldValidationError(ValueError):
    """Raised when the gold can leak, alias, or encode an invalid interval."""


def _finite_number(value: Any, field: str) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise GoldValidationError(f"{field} must be a finite number") from exc
    if not math.isfinite(number):
        raise GoldValidationError(f"{field} must be a finite number")
    return number


def _required_string(row: Mapping[str, Any], field: str) -> str:
    value = str(row.get(field) or "").strip()
    if not value:
        raise GoldValidationError(f"{field} is required")
    return value


def validate_gold(rows: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Validate and normalize endpoint labels.

    Artist, song and related-recording groups are forbidden from crossing
    train/calibration/test.  This is stricter than a random line split and is
    intentional: lines from the same artist are not independent evidence.
    """
    normalized: list[dict[str, Any]] = []
    label_ids: set[str] = set()
    group_splits: dict[tuple[str, str], str] = {}
    for raw in rows:
        present_text = sorted(_TEXT_FIELDS.intersection(raw))
        if present_text:
            raise GoldValidationError(
                f"gold must not carry lyric text fields: {', '.join(present_text)}"
            )
        row = dict(raw)
        if row.get("schema") != SCHEMA:
            raise GoldValidationError(f"schema must be {SCHEMA}")
        label_id = _required_string(row, "label_id")
        if label_id in label_ids:
            raise GoldValidationError(f"duplicate label_id: {label_id}")
        label_ids.add(label_id)

        for field in (
            "job_id", "line_id", "audio_sha256", "song_group_id",
            "artist_group_id", "recording_group_id", "evaluation_unit_id",
            "reviewer_id", "annotation_version",
        ):
            row[field] = _required_string(row, field)
        label_class = str(row.get("label_class") or "")
        sample_role = str(row.get("sample_role") or "")
        annotation_mode = str(row.get("annotation_mode") or "")
        split = str(row.get("split") or "")
        vocal_attribution = str(row.get("vocal_attribution") or "")
        if label_class not in LABEL_CLASSES:
            raise GoldValidationError(f"invalid label_class for {label_id}")
        if sample_role not in SAMPLE_ROLES:
            raise GoldValidationError(f"invalid sample_role for {label_id}")
        if annotation_mode not in ANNOTATION_MODES:
            raise GoldValidationError(f"invalid annotation_mode for {label_id}")
        if split not in SPLITS:
            raise GoldValidationError(f"invalid split for {label_id}")
        if vocal_attribution not in VOCAL_ATTRIBUTIONS:
            raise GoldValidationError(f"invalid vocal_attribution for {label_id}")
        if row.get("measurement_source") != "rendered_video":
            raise GoldValidationError(
                f"final endpoint measurement must come from rendered_video: {label_id}"
            )
        if annotation_mode == "blind" and row.get("engine_end_visible") is not False:
            raise GoldValidationError(
                f"blind annotation must attest engine_end_visible=false: {label_id}"
            )

        current = _finite_number(row.get("current_end_s"), "current_end_s")
        sung = _finite_number(row.get("sung_end_s"), "sung_end_s")
        lower = _finite_number(row.get("acceptable_end_min_s"), "acceptable_end_min_s")
        upper = _finite_number(row.get("acceptable_end_max_s"), "acceptable_end_max_s")
        if min(current, sung, lower, upper) < 0 or lower > upper:
            raise GoldValidationError(f"invalid endpoint interval for {label_id}")
        tolerance = 1e-6
        if label_class == "short" and current >= lower - tolerance:
            raise GoldValidationError(f"short current_end is not before interval: {label_id}")
        if label_class == "correct" and not lower - tolerance <= current <= upper + tolerance:
            raise GoldValidationError(f"correct current_end is outside interval: {label_id}")
        row.update({
            "current_end_s": current,
            "sung_end_s": sung,
            "acceptable_end_min_s": lower,
            "acceptable_end_max_s": upper,
            "label_class": label_class,
            "sample_role": sample_role,
            "annotation_mode": annotation_mode,
            "split": split,
            "vocal_attribution": vocal_attribution,
        })

        for group_field in _DEPENDENCY_FIELDS:
            key = (group_field, row[group_field])
            previous = group_splits.setdefault(key, split)
            if previous != split:
                raise GoldValidationError(
                    f"{group_field} crosses splits ({previous}/{split}): {row[group_field]}"
                )
        normalized.append(row)
    if not normalized:
        raise GoldValidationError("gold is empty")
    return normalized


def validate_predictions(
    rows: Iterable[Mapping[str, Any]], gold_ids: set[str],
) -> dict[str, dict[str, Any]]:
    normalized: dict[str, dict[str, Any]] = {}
    for raw in rows:
        row = dict(raw)
        if row.get("schema") != PREDICTION_SCHEMA:
            raise GoldValidationError(f"prediction schema must be {PREDICTION_SCHEMA}")
        label_id = _required_string(row, "label_id")
        if label_id not in gold_ids:
            raise GoldValidationError(f"prediction has unknown label_id: {label_id}")
        if label_id in normalized:
            raise GoldValidationError(f"duplicate prediction: {label_id}")
        decision = str(row.get("decision") or "")
        if decision not in DECISIONS:
            raise GoldValidationError(f"invalid decision for {label_id}")
        row["decision"] = decision
        row["proposer_family"] = _required_string(row, "proposer_family")
        row["selector_family"] = _required_string(row, "selector_family")
        if decision == "apply":
            row["proposed_end_s"] = _finite_number(
                row.get("proposed_end_s"), "proposed_end_s",
            )
            if row["proposed_end_s"] < 0:
                raise GoldValidationError(f"negative proposed_end_s: {label_id}")
        else:
            row["proposed_end_s"] = None
            row["abstention_reason"] = _required_string(row, "abstention_reason")
        normalized[label_id] = row
    missing = gold_ids.difference(normalized)
    if missing:
        raise GoldValidationError(f"missing predictions for {len(missing)} gold labels")
    return normalized


def clopper_pearson_lower(successes: int, total: int, alpha: float = 0.05) -> float | None:
    """Exact one-sided binomial lower bound, dependency free."""
    if total <= 0:
        return None
    if not 0 <= successes <= total or not 0 < alpha < 1:
        raise ValueError("invalid binomial arguments")
    if successes == 0:
        return 0.0

    def upper_tail(probability: float) -> float:
        return sum(
            math.comb(total, k) * probability ** k * (1 - probability) ** (total - k)
            for k in range(successes, total + 1)
        )

    low, high = 0.0, 1.0
    for _ in range(80):
        middle = (low + high) / 2
        if upper_tail(middle) < alpha:
            low = middle
        else:
            high = middle
    return (low + high) / 2


def clopper_pearson_upper(events: int, total: int, alpha: float = 0.05) -> float | None:
    """Exact one-sided upper bound, used for damage/no-harm risk."""
    if total <= 0:
        return None
    if not 0 <= events <= total or not 0 < alpha < 1:
        raise ValueError("invalid binomial arguments")
    if events == total:
        return 1.0

    def lower_tail(probability: float) -> float:
        return sum(
            math.comb(total, k) * probability ** k * (1 - probability) ** (total - k)
            for k in range(0, events + 1)
        )

    low, high = 0.0, 1.0
    for _ in range(80):
        middle = (low + high) / 2
        if lower_tail(middle) > alpha:
            low = middle
        else:
            high = middle
    return (low + high) / 2


def zero_error_sample_size(target_precision: float, confidence: float = 0.95) -> int:
    """Independent zero-error observations needed for a target lower bound."""
    if not 0 < target_precision < 1 or not 0 < confidence < 1:
        raise ValueError("precision and confidence must be between zero and one")
    return math.ceil(math.log(1 - confidence) / math.log(target_precision))


def _dependency_units(gold: list[dict[str, Any]]) -> dict[str, str]:
    """Connected components: a supplied unit ID may merge, never split evidence.

    Include abstentions and ambiguous labels when finding dependencies, because
    excluding them could break a bridge between related songs/recordings.
    """
    parent = {row["label_id"]: row["label_id"] for row in gold}

    def root(label_id: str) -> str:
        while parent[label_id] != label_id:
            parent[label_id] = parent[parent[label_id]]
            label_id = parent[label_id]
        return label_id

    seen: dict[tuple[str, str], str] = {}
    for row in gold:
        label_id = row["label_id"]
        for field in _DEPENDENCY_FIELDS:
            key = (field, row[field])
            if key in seen:
                left, right = sorted((root(label_id), root(seen[key])))
                parent[right] = left
            else:
                seen[key] = label_id
    return {label_id: root(label_id) for label_id in parent}


def _evaluate_subset(
    gold: list[dict[str, Any]], predictions: Mapping[str, Mapping[str, Any]],
) -> dict[str, Any]:
    judged = [row for row in gold if row["label_class"] != "ambiguous"
              and row["vocal_attribution"] != "ambiguous"]
    requested = [row for row in gold if predictions[row["label_id"]]["decision"] == "apply"]
    changed = [row for row in requested if not math.isclose(
        predictions[row["label_id"]]["proposed_end_s"], row["current_end_s"],
        rel_tol=0, abs_tol=1e-6,
    )]
    applied = [row for row in changed if row in judged]
    correct_applied = [
        row for row in applied
        if row["acceptable_end_min_s"]
        <= predictions[row["label_id"]]["proposed_end_s"]
        <= row["acceptable_end_max_s"]
    ]
    short = [row for row in judged if row["label_class"] == "short"]
    recovered_short = [row for row in correct_applied if row["label_class"] == "short"]
    controls = [row for row in judged if row["label_class"] == "correct"]
    applied_controls = [row for row in applied if row["label_class"] == "correct"]
    harmed_controls = [row for row in applied if row["label_class"] == "correct" and row not in correct_applied]

    units: dict[str, list[bool]] = defaultdict(list)
    control_units: dict[str, list[bool]] = defaultdict(list)
    dependency_units = _dependency_units(gold)
    for row in applied:
        unit = dependency_units[row["label_id"]]
        units[unit].append(row in correct_applied)
        if row["label_class"] == "correct":
            control_units[unit].append(row in harmed_controls)
    successful_units = sum(all(results) for results in units.values())
    harmed_control_units = sum(any(results) for results in control_units.values())
    return {
        "labels": len(gold),
        "judged_labels": len(judged),
        "ambiguous_labels": len(gold) - len(judged),
        "applied_changes": len(applied),
        "requested_applications": len(requested),
        "no_op_applications": len(requested) - len(changed),
        "unjudgable_applications": sum(row not in judged for row in changed),
        "abstentions": sum(predictions[row["label_id"]]["decision"] == "abstain" for row in gold),
        "coverage": round(len(applied) / len(judged), 6) if judged else None,
        "successful_changes": len(correct_applied),
        "applied_change_precision": round(len(correct_applied) / len(applied), 6) if applied else None,
        # Line-level proportions are descriptive only, never binomial evidence.
        "applied_change_precision_lower_95": None,
        "short_labels": len(short),
        "short_recovered": len(recovered_short),
        "short_recall": round(len(recovered_short) / len(short), 6) if short else None,
        "correct_controls": len(controls),
        "applied_controls": len(applied_controls),
        "harmed_controls": len(harmed_controls),
        "control_harm_rate": round(len(harmed_controls) / len(applied_controls), 6) if applied_controls else None,
        "control_units_with_changes": len(control_units),
        "harmed_control_units": harmed_control_units,
        "control_harm_upper_95": (
            clopper_pearson_upper(harmed_control_units, len(control_units))
            if control_units else None
        ),
        "evaluation_units_with_changes": len(units),
        "successful_evaluation_units": successful_units,
        "unit_precision_lower_95": (
            clopper_pearson_lower(successful_units, len(units))
            if units else None
        ),
    }


def evaluate_experiment(
    gold_rows: Iterable[Mapping[str, Any]],
    prediction_rows: Iterable[Mapping[str, Any]],
    *,
    threshold_frozen_before_test: bool,
    review_rows: Iterable[Mapping[str, Any]] = (),
) -> dict[str, Any]:
    gold = validate_gold(gold_rows)
    predictions = validate_predictions(prediction_rows, {row["label_id"] for row in gold})
    by_split = {
        split: _evaluate_subset(
            [row for row in gold if row["split"] == split], predictions,
        )
        for split in sorted(SPLITS)
    }
    test = by_split["test"]
    exploratory_blockers = []
    if not threshold_frozen_before_test:
        exploratory_blockers.append("threshold_not_frozen_before_test")
    if not any(row["split"] == "test" and row["annotation_mode"] == "blind" for row in gold):
        exploratory_blockers.append("no_blind_test_labels")
    if (test["unit_precision_lower_95"] or 0.0) < 0.95:
        exploratory_blockers.append("unit_precision_lower_95_below_0.95")
    if test["unjudgable_applications"]:
        exploratory_blockers.append("changes_applied_to_ambiguous_labels")
    if (test["control_harm_upper_95"] or 1.0) > 0.05:
        exploratory_blockers.append("control_harm_upper_95_above_0.05")
    for required_class in sorted(LABEL_CLASSES):
        if not any(row["split"] == "test" and row["label_class"] == required_class for row in gold):
            exploratory_blockers.append(f"missing_test_{required_class}_labels")
    for required_role in ("random", "control"):
        if not any(row["split"] == "test" and row["sample_role"] == required_role for row in gold):
            exploratory_blockers.append(f"missing_test_{required_role}_sample")

    certification_blockers = list(exploratory_blockers)
    if test["evaluation_units_with_changes"] < zero_error_sample_size(0.99):
        certification_blockers.append("fewer_than_299_independent_evaluation_units")
    if (test["unit_precision_lower_95"] or 0.0) < 0.99:
        certification_blockers.append("unit_precision_lower_95_below_0.99")
    if (test["control_harm_upper_95"] or 1.0) > 0.01:
        certification_blockers.append("conditional_control_harm_upper_95_above_0.01")

    family_pairs = Counter(
        (row["proposer_family"], row["selector_family"])
        for row in predictions.values()
    )
    gold_jobs = {row["job_id"] for row in gold}
    minutes_by_task: Counter[str] = Counter()
    minutes_by_job: Counter[str] = Counter()
    for raw in review_rows:
        job_id = _required_string(raw, "job_id")
        task_type = str(raw.get("task_type") or "")
        if job_id not in gold_jobs:
            raise GoldValidationError(f"review metric has unknown job_id: {job_id}")
        if task_type not in TASK_TYPES:
            raise GoldValidationError(f"invalid task_type for review metric: {task_type}")
        active_minutes = _finite_number(raw.get("active_minutes"), "active_minutes")
        if active_minutes < 0:
            raise GoldValidationError("active_minutes cannot be negative")
        minutes_by_task[task_type] += active_minutes
        minutes_by_job[job_id] += active_minutes

    return {
        "schema": "timing-endpoint-experiment-report-v2",
        "uncertainty_unit": "connected_song_artist_recording_job_audio_unit_groups",
        "uncertainty_estimand": "probability_all_applied_changes_in_group_are_safe",
        "harm_estimand": "probability_of_any_harm_in_group_given_control_change_applied",
        "independence_requires_external_sampling_audit": True,
        "gold_schema": SCHEMA,
        "prediction_schema": PREDICTION_SCHEMA,
        "label_distribution": dict(sorted(Counter(row["label_class"] for row in gold).items())),
        "sample_distribution": dict(sorted(Counter(row["sample_role"] for row in gold).items())),
        "annotation_distribution": dict(sorted(Counter(row["annotation_mode"] for row in gold).items())),
        "by_split": by_split,
        "model_components": [
            {"proposer_family": pair[0], "selector_family": pair[1], "labels": count,
             "distinct_family_labels": pair[0] != pair[1],
             "independent_families": False, "independence_status": "not_attested"}
            for pair, count in sorted(family_pairs.items())
        ],
        "human_review": {
            "songs_with_measurement": len(minutes_by_job),
            "minutes_by_task": {
                key: round(value, 4) for key, value in sorted(minutes_by_task.items())
            },
            "minutes_per_song": {
                key: round(value, 4) for key, value in sorted(minutes_by_job.items())
            },
        },
        "exploratory_gate": {
            "passed": not exploratory_blockers,
            "blockers": exploratory_blockers,
            "target_lower_95": 0.95,
        },
        "production_99_gate": {
            "passed": not certification_blockers,
            "blockers": certification_blockers,
            "minimum_independent_zero_error_units": zero_error_sample_size(0.99),
        },
        # This report never grants runtime authority. Promotion remains an
        # explicit, separately reviewed release decision after clean evidence.
        "automatic_apply_allowed": False,
        "database_mutations": 0,
        "paid_provider_calls": 0,
    }
