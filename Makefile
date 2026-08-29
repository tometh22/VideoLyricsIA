PYTHON ?= python3
VARIANT ?= prod_raw
GOLDEN ?= eval/golden
HYPOTHESIS_ROOT ?= eval/hypotheses/$(VARIANT)
BACKEND ?= lyricgen/backend

.PHONY: eval eval-test eval-autopsy eval-extract eval-verify-portal eval-finalize eval-language-id eval-freeze eval-t4-learned eval-error-predictor eval-lora-prep eval-nonhistorical eval-from-snapshot eval-taxonomy-ensemble eval-runtime-replay eval-stems-local eval-lora-research-prep eval-t7-prep eval-phase2-status eval-publish-diagnostic

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

eval-t7-prep:
	PYTHONPATH=. $(PYTHON) -m eval.prepare_t7_corruptions

eval-phase2-status:
	PYTHONPATH=. $(PYTHON) -m eval.phase2_status

eval-publish-diagnostic:
	PYTHONPATH=. $(PYTHON) -m eval.publish_diagnostic_report

eval-nonhistorical:
	PYTHONPATH=. $(PYTHON) -m eval.generate_nonhistorical --golden "$(GOLDEN)" --backend "$(BACKEND)" --output eval/hypotheses/local_baseline_8
	PYTHONPATH=. $(PYTHON) -m eval.score --golden "$(GOLDEN)" --variant local_baseline_8 --hypothesis-root eval/hypotheses/local_baseline_8 --output eval/runs/local_baseline_8

eval-from-snapshot:
	PYTHONPATH=. $(PYTHON) -m eval.build_from_snapshot --snapshot "$(SNAPSHOT)" --output "$(GOLDEN)"
