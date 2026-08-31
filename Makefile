PYTHON ?= python3
VARIANT ?= prod_raw
GOLDEN ?= eval/golden
HYPOTHESIS_ROOT ?= eval/hypotheses/$(VARIANT)
BACKEND ?= lyricgen/backend

.PHONY: eval eval-test eval-autopsy eval-extract eval-verify-portal eval-finalize eval-language-id eval-freeze eval-t4-learned eval-error-predictor eval-lora-prep eval-nonhistorical eval-from-snapshot eval-taxonomy-ensemble eval-runtime-replay eval-stems-local eval-stem-cohorts eval-stem-audit eval-lora-research-prep eval-t7-prep eval-phase2-status eval-publish-diagnostic eval-ztlr eval-final-text-realign eval-hierarchical-realign eval-report-hierarchical eval-post-realign-review eval-flag-union eval-mss-alt eval-publish-zero-touch eval-agent-prepare eval-agent-run eval-agent-score eval-agent-policy eval-difficult-cohort eval-code-switch-lid eval-difficulty-router eval-gemini-heavy eval-difficult-heavy eval-difficult-score eval-difficult-report

eval:
	PYTHONPATH=. $(PYTHON) -m eval.score --golden "$(GOLDEN)" --variant "$(VARIANT)" $(if $(filter prod_raw,$(VARIANT)),,--hypothesis-root "$(HYPOTHESIS_ROOT)")

eval-test:
	PYTHONPATH=. $(PYTHON) -m pytest -q eval/tests

eval-autopsy:
	PYTHONPATH=. $(PYTHON) -m eval.autopsy --golden "$(GOLDEN)" $(if $(INCLUDE_ESTIMATED),--include-estimated,)

eval-extract:
	PYTHONPATH=. $(PYTHON) -m eval.extract --output "$(GOLDEN)" --expected-count 65

eval-verify-portal:
	PYTHONPATH=. $(PYTHON) -m eval.verify_portal --golden "$(GOLDEN).partial" --portal-payload "$(PORTAL_PAYLOAD)" --output "$(VERIFICATION)"

eval-finalize:
	PYTHONPATH=. $(PYTHON) -m eval.extract --output "$(GOLDEN)" --finalize-verification "$(VERIFICATION)"

eval-language-id:
	PYTHONPATH=. $(PYTHON) -m eval.language_id --golden "$(GOLDEN)"

eval-freeze:
	PYTHONPATH=. $(PYTHON) -m eval.freeze_baseline --golden "$(GOLDEN)" --autopsy-41 "$(AUTOPSY_41)" --autopsy-57 "$(AUTOPSY_57)"

eval-t4-learned:
	PYTHONPATH=. $(PYTHON) -m eval.train_timing --golden "$(GOLDEN)"

eval-error-predictor:
	PYTHONPATH=. $(PYTHON) -m eval.train_error_predictor --golden "$(GOLDEN)"

eval-lora-prep:
	PYTHONPATH=. $(PYTHON) -m eval.prepare_lora --golden "$(GOLDEN)"

eval-lora-research-prep:
	PYTHONPATH=. $(PYTHON) -m eval.prepare_jamendolyrics

eval-taxonomy-ensemble:
	PYTHONPATH=. $(PYTHON) -m eval.taxonomy_ensemble --word-errors eval/runs/prod_raw/autopsy/word_errors.csv --batch-input eval/runs/error_taxonomy/batch_input.jsonl --qwen-results eval/runs/error_taxonomy/local_results.jsonl

eval-runtime-replay:
	PYTHONPATH=. $(PYTHON) -m eval.replay_runtime_suggestions --runtime-ref c9bdc358

eval-stems-local:
	PYTHONPATH=. $(PYTHON) -m eval.generate_missing_stems

eval-stem-cohorts:
	PYTHONPATH=. $(PYTHON) -m eval.stem_cohort_report

eval-stem-audit:
	PYTHONPATH=. $(PYTHON) -m eval.stem_cohort_audit --device $${DEVICE:-mps}

eval-t7-prep:
	PYTHONPATH=. $(PYTHON) -m eval.prepare_t7_corruptions

eval-phase2-status:
	PYTHONPATH=. $(PYTHON) -m eval.phase2_status

eval-publish-diagnostic:
	PYTHONPATH=. $(PYTHON) -m eval.publish_diagnostic_report

eval-ztlr:
	PYTHONPATH=. $(PYTHON) -m eval.ztlr

eval-final-text-realign:
	PYTHONPATH=. $(PYTHON) -m eval.realign_final_text

eval-hierarchical-realign:
	PYTHONPATH=. $(PYTHON) -m eval.realign_final_text --aligners current_xlsr_hierarchical --audio-source stem --output eval/runs/final_text_realign_hierarchical_26

eval-report-hierarchical:
	PYTHONPATH=. $(PYTHON) -m eval.report_hierarchical_realign

eval-post-realign-review:
	PYTHONPATH=. $(PYTHON) -m eval.post_realign_review

eval-flag-union:
	PYTHONPATH=. $(PYTHON) -m eval.flag_union

eval-mss-alt:
	PYTHONPATH=. $(PYTHON) -m eval.mss_alt

eval-publish-zero-touch:
	PYTHONPATH=. $(PYTHON) -m eval.publish_zero_touch

eval-agent-prepare:
	PYTHONPATH=. $(PYTHON) -m eval.agent_corrector prepare $(if $(CANDIDATES),--candidates "$(CANDIDATES)",) $(if $(EXTRACT_CLIPS),--extract-clips,)

eval-agent-run:
	PYTHONPATH=. $(PYTHON) -m eval.agent_corrector run $(if $(AGENT_LIMIT),--limit "$(AGENT_LIMIT)",)

eval-agent-score:
	PYTHONPATH=. $(PYTHON) -m eval.agent_corrector score $(if $(ADJUDICATIONS),--adjudications "$(ADJUDICATIONS)",)

eval-agent-policy:
	@test -n "$(ACTIVATED_AT)" || (echo "ACTIVATED_AT is required" >&2; exit 2)
	PYTHONPATH=. $(PYTHON) -m eval.agent_tiers policy --activated-at "$(ACTIVATED_AT)"

eval-difficult-cohort:
	PYTHONPATH=. $(PYTHON) -m eval.difficult_cohort

eval-code-switch-lid:
	PYTHONPATH=. $(PYTHON) -m eval.code_switching lid --stems eval/cache/full_stems --output eval/runs/code_switch_lid_full --quality exact --quality reconstructed --fallback-to-mix

eval-difficulty-router:
	PYTHONPATH=. $(PYTHON) -m eval.difficulty_router --lid eval/runs/code_switch_lid_full/report.json

eval-gemini-heavy:
	PYTHONPATH=. $(PYTHON) -m eval.gemini_heavy_candidates $(if $(GEMINI_LIMIT),--limit "$(GEMINI_LIMIT)",)

eval-difficult-heavy:
	PYTHONPATH=. $(PYTHON) -m eval.difficult_pipeline run $(if $(INDEPENDENT_ROOT),--independent-root "$(INDEPENDENT_ROOT)",)

eval-difficult-score:
	PYTHONPATH=. $(PYTHON) -m eval.difficult_pipeline score

eval-difficult-report:
	PYTHONPATH=. $(PYTHON) -m eval.difficult_block_report

eval-nonhistorical:
	PYTHONPATH=. $(PYTHON) -m eval.generate_nonhistorical --golden "$(GOLDEN)" --backend "$(BACKEND)" --output eval/hypotheses/local_baseline_8
	PYTHONPATH=. $(PYTHON) -m eval.score --golden "$(GOLDEN)" --variant local_baseline_8 --hypothesis-root eval/hypotheses/local_baseline_8 --output eval/runs/local_baseline_8

eval-from-snapshot:
	PYTHONPATH=. $(PYTHON) -m eval.build_from_snapshot --snapshot "$(SNAPSHOT)" --output "$(GOLDEN)"

# Chequeo semanal: produccion sigue guardando el crudo exacto de cada job?
# Es la unica via para que la cohorte limpia (hoy 23 de 65) crezca. Si esto
# se rompe el sintoma es invisible: los jobs se aprueban igual y meses despues
# descubris que la cohorte no crecio.
check-raw-coverage:
	@python3 -m eval.check_raw_coverage --days 7 --min-pct 100
