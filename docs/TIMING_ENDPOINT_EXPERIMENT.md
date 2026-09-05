# Timing endpoint experiment

This protocol measures whether an acoustic proposal improves a lyric-line end.
It never changes production timings and never treats two heads of one model as
independent witnesses.

## Gold contract

Each JSONL row uses `timing-endpoint-gold-v1` and records both `sung_end_s` and
the human-acceptable interval `acceptable_end_min_s`–`acceptable_end_max_s`.
The cohort must contain `short`, `correct`, and `ambiguous` labels; `random`,
`difficult`, and `control` samples; and a blind subset with
`engine_end_visible=false`. Lyric/reference text is forbidden in the metric
artifact. Final endpoints must declare `measurement_source=rendered_video`;
database-only timestamps are rejected. Song, artist, and related-recording groups cannot cross train,
calibration, and test.

The annotation guide must explicitly resolve whether a continuing chorus is
the relevant voice for a line. If target-voice attribution or reverberation is
editorially ambiguous, label the endpoint `ambiguous`; do not manufacture a
point target.

## Model contract

The proposer emits plausible acoustic ends using the verified last word plus
post-context from synchronized vocal stem and original mix. Candidate features
may include voicing probability, f0 and uncertainty, a roughly 300 ms
fluctuation/vibrato view, spectral/timbre continuity, band energy, phonetic
evidence, and other-voice activity. The selector receives both the current end
and candidate and decides `apply` or `abstain`. Frozen audio representations
plus a small regularized selector are the first experiment; ROSVOT and
target-singer VAD are candidate representations, not certified endpoints.

`hold=0.5` remains the fallback. Thresholds are selected only on calibration,
then frozen before test. Shadow output uses
`timing-endpoint-prediction-v1`; no proposal receives runtime authority.

## Gates and metrics

Run `scripts/evaluate_timing_endpoint_experiment.py` with gold and prediction
JSONL and, when available, `--review-metrics` rows from the editor timer
(`job_id`, `task_type`, `active_minutes`). The report separates precision conditioned on applied changes, coverage,
short-end recovery, damage to correct controls, abstentions, and evaluation
units grouped by song/recording. It uses exact one-sided binomial bounds, so 50
independent zero-error units only support a 94.2% lower bound; 299 support 99%.
The first gate is exploratory (lower 95% bound at least 95%). Production 99%
also requires at least 299 independent evaluation units and a clean frozen
test. Live recordings remain mandatory human review regardless of the result.

Review minutes per song and task type are joined from the editor timer in the
analysis report; they are not inferred from model latency. Rendered-video
timestamps, rather than database timestamps, are the final endpoint source for
all delivery measurements.

### Evaluator v2: evidence units and conditional risk

Line proportions are descriptive, not independent-binomial confidence bounds.
The evaluator builds connected components across song, artist, recording,
job, audio hash and supplied evaluation-unit IDs. A caller's ID can merge
components, never split related lines into independent evidence. Abstained
and ambiguous labels also participate in grouping. No such group may cross
train/calibration/test. Correct grouping still requires an external audit of
sampling independence; identifiers alone cannot prove it.

Both exploratory and 99% gates use groups with actual changes. A group is a
success only if every judged applied change is safe. The exact lower bound
therefore estimates group-wide safety, not line precision. No-op applications
do not count as evidence. Applying an ambiguous endpoint or ambiguous vocal
attribution blocks the gate; those cases remain separately reported.

Control harm is conditioned on actually changing a correct control. The
descriptive denominator is changed controls; the conservative bound uses
groups with changed controls, counting any harm in a group as a failure.
Untouched controls cannot dilute risk. Exploratory harm must have a one-sided
95% upper bound at most 5%; the 99% gate requires at most 1%. Zero observed
damage is never reported as zero upper-bound risk. Test itself must contain
all label classes and random/control samples; training rows cannot satisfy
test coverage requirements.

These are separate marginal 95% bounds, not a joint simultaneous guarantee.
Threshold freezing is an input attestation, not cryptographic proof: the
release reviewer must verify a single preregistered calibration choice, a
previously untouched test, rendered candidate endpoints after clipping, and
sampling/group independence before interpreting a passed offline gate. No
model has been trained or certified by adding this evaluator.
