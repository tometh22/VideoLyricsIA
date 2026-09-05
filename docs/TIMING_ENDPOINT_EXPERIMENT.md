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
