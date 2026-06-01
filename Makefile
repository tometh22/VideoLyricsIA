# Quality gates — mirror .github/workflows/ci.yml so regressions are caught
# locally BEFORE they reach CI (or prod). Two layers:
#
#   make check   → fast, no-DB subset. Run by the pre-push hook. Catches the
#                  NameError-class breaks (ruff F821), syntax errors, missing
#                  i18n keys, frontend test failures and build breaks.
#   make test    → full backend pytest suite. Needs Postgres + DATABASE_URL,
#                  same as CI's `backend` job. Run it before big backend PRs.
#
# One-time setup on a fresh clone:  make install-hooks
# Emergency bypass of the hook:     git push --no-verify

BACKEND  := lyricgen/backend
FRONTEND := lyricgen/frontend
PY       := python3

.PHONY: check check-backend check-frontend test test-backend install-hooks

check: check-backend check-frontend
	@echo "✓ fast checks passed — safe to push"

# Fast backend gates, no DB required.
# - F821 (undefined names) is the lint gate added after the 2026-05-26
#   incident where 3 chained NameErrors blocked /generate in prod for ~17h.
# - ast.parse mirrors CI's "Syntax check all modules" (parse-only, so it
#   passes on any Python version regardless of `X | Y` runtime support).
check-backend:
	@echo "→ backend: ruff F821 + syntax"
	@$(PY) -m ruff --version >/dev/null 2>&1 || { echo "✗ ruff not found — install with: pip install ruff"; exit 1; }
	cd $(BACKEND) && $(PY) -m ruff check --select F821 .
	cd $(BACKEND) && $(PY) -c "import ast, glob; [ast.parse(open(f).read()) for f in glob.glob('*.py')]"

# Frontend: i18n key coverage + unit tests + production build (same as CI).
check-frontend:
	@echo "→ frontend: i18n + tests + build"
	cd $(FRONTEND) && npm run check:i18n
	cd $(FRONTEND) && npm test
	cd $(FRONTEND) && npm run build

test: test-backend

# Full backend suite — needs Postgres + DATABASE_URL (see ci.yml `backend`).
test-backend:
	cd $(BACKEND) && $(PY) -m pytest tests/ --tb=short

# Route git hooks at the committed .githooks dir so the pre-push gate is
# shared across the team (default .git/hooks is not version-controlled).
install-hooks:
	git config core.hooksPath .githooks
	@echo "✓ hooks installed — 'make check' now runs on every push"

# Black-box cross-tenant isolation pen-test (staging only). Needs two
# accounts in DIFFERENT tenants (PENTEST_A_*/PENTEST_B_*) + optional A2 in
# A's tenant. Refuses to run against prod hosts unless --force is passed.
#   PENTEST_BASE_URL  staging API base URL (or STAGING_API_URL)
# See docs/RUNBOOK_LAUNCH.md §1.
.PHONY: pentest-isolation
pentest-isolation:
	cd $(BACKEND) && $(PY) scripts/pentest_tenant_isolation.py
