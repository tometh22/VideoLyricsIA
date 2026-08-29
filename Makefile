PYTHON ?= python3
VARIANT ?= prod_raw
GOLDEN ?= eval/golden
HYPOTHESIS_ROOT ?= eval/hypotheses/$(VARIANT)

.PHONY: eval eval-test eval-extract eval-finalize eval-from-snapshot

eval:
	PYTHONPATH=. $(PYTHON) -m eval.score --golden "$(GOLDEN)" --variant "$(VARIANT)" $(if $(filter prod_raw,$(VARIANT)),,--hypothesis-root "$(HYPOTHESIS_ROOT)")

eval-test:
	PYTHONPATH=. $(PYTHON) -m pytest -q eval/tests

eval-extract:
	PYTHONPATH=. $(PYTHON) -m eval.extract --output "$(GOLDEN)" --expected-count 65

eval-finalize:
	PYTHONPATH=. $(PYTHON) -m eval.extract --output "$(GOLDEN)" --finalize-verification "$(VERIFICATION)"

eval-from-snapshot:
	PYTHONPATH=. $(PYTHON) -m eval.build_from_snapshot --snapshot "$(SNAPSHOT)" --output "$(GOLDEN)"
