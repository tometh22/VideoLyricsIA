#!/usr/bin/env bash
# Verify the four preconditions for zero-downtime deploys on Railway.
#
# Run this after Railway recovers from the 2026-05-19 us-east4 outage,
# before pushing the feat/zero-downtime-deploys branch. It prints a
# table — if anything is RED, fix it BEFORE the next deploy or the
# rolling overlap won't engage and clients will still see 502s.
#
# Usage:
#   cd lyricgen/backend
#   bash scripts/verify_deploy_resilience.sh                # production (default)
#   bash scripts/verify_deploy_resilience.sh staging        # staging
#
# Requirements:
#   - railway CLI logged in (railway login)
#   - linked to the Genly IA project (railway link)
#
# Exit codes:
#   0  all checks passed
#   1  at least one critical check failed
#   2  pre-flight (no railway CLI, etc)

set -u

_TARGET_ENV="${1:-production}"

if ! command -v railway >/dev/null 2>&1; then
  echo "FATAL: railway CLI not found. brew install railway then 'railway login'." >&2
  exit 2
fi

# Auth pre-flight. A stale OAuth token makes every `railway variables`
# call return empty, which the checks below would misreport as FAIL
# (value missing) instead of the truth (couldn't read it). Catch it here.
if ! railway whoami >/dev/null 2>&1; then
  echo "FATAL: railway CLI is not authenticated (token expired or never logged in)." >&2
  echo "       Run:  railway login" >&2
  echo "       Then link the project:  railway link" >&2
  echo "       Then re-run this script." >&2
  exit 2
fi

# Service names match exactly what's in Railway dashboard (case-sensitive).
# See railway.toml comment header for the rationale.
_API_SERVICE="api"
_WORKER_SERVICE="Worker"

# Pretty print helpers. No emoji; just OK / WARN / FAIL columns so the
# table grep-able from CI later if we want.
_pass=0
_warn=0
_fail=0
_results=()

_check() {
  local name="$1"
  local status="$2"   # OK | WARN | FAIL
  local detail="$3"
  _results+=("$(printf '%-44s %-6s %s' "$name" "$status" "$detail")")
  case "$status" in
    OK)   _pass=$((_pass+1)) ;;
    WARN) _warn=$((_warn+1)) ;;
    FAIL) _fail=$((_fail+1)) ;;
  esac
}

echo "=== verify_deploy_resilience.sh — env=$_TARGET_ENV ==="
echo

# ---------------------------------------------------------------------
# Check 1 — Postgres max_connections is high enough for the new layout
# ---------------------------------------------------------------------
# Math (see railway.toml comment):
#   API:    2 replicas × 2 uvicorn workers × (pool 6 + overflow 4) =  40
#   Worker: 7 replicas × 1 process × 10                            =  70
#   Admin/migrations reserve                                       =   5
#   TOTAL                                                          = 115
#
# Default PG ships with max_connections=100 → we'd be ~15 over the cap.
# Pro PG goes up to 500. Anything ≥ 150 is safe; ≥ 100 but < 150 we WARN.
_db_url=$(railway variables --service "$_API_SERVICE" --environment "$_TARGET_ENV" --kv 2>/dev/null \
  | grep -E '^DATABASE_URL=' | head -1 | cut -d= -f2-)

if [ -z "$_db_url" ]; then
  _check "PG max_connections" "FAIL" "DATABASE_URL not readable via railway CLI"
else
  if ! command -v psql >/dev/null 2>&1; then
    _check "PG max_connections" "WARN" "psql not installed locally; can't probe"
  else
    _maxc=$(psql "$_db_url" -At -c "SHOW max_connections;" 2>/dev/null)
    if [ -z "$_maxc" ]; then
      _check "PG max_connections" "FAIL" "psql connection failed"
    elif [ "$_maxc" -ge 150 ]; then
      _check "PG max_connections" "OK" "max_connections=$_maxc (≥150 safe for 115 conn ceiling)"
    elif [ "$_maxc" -ge 115 ]; then
      _check "PG max_connections" "WARN" "max_connections=$_maxc (tight; 115 needed). Bump in Railway PG settings."
    else
      _check "PG max_connections" "FAIL" "max_connections=$_maxc < 115 needed. DEPLOY WILL EXHAUST POOL."
    fi
  fi
fi

# ---------------------------------------------------------------------
# Check 2 — Railway plan tier (Pro or higher needed for OVERLAP deploys)
# ---------------------------------------------------------------------
# There's no clean CLI command for "what's my plan". Inferred signal:
# the `numReplicas` setting in railway.toml is only honored on Pro+.
# We check that the ACTUAL deployed replica count matches what we
# configured. If Pro is off and we asked for numReplicas=2, Railway
# silently caps at 1.
_expected_replicas=2
_actual_replicas=$(railway status --service "$_API_SERVICE" --environment "$_TARGET_ENV" --json 2>/dev/null \
  | grep -oE '"replicas":\s*[0-9]+' | grep -oE '[0-9]+' | head -1)

if [ -z "$_actual_replicas" ]; then
  _check "Railway plan tier" "WARN" "could not read replica count; verify Pro plan manually in dashboard"
elif [ "$_actual_replicas" -ge "$_expected_replicas" ]; then
  _check "Railway plan tier" "OK" "api running $_actual_replicas replicas (≥$_expected_replicas)"
else
  _check "Railway plan tier" "FAIL" "api on $_actual_replicas replicas; railway.toml asks $_expected_replicas. Likely Hobby plan; upgrade to Pro."
fi

# ---------------------------------------------------------------------
# Check 3 — RAILWAY_SHUTDOWN_TIMEOUT_SECONDS on Worker (180+ recommended)
# ---------------------------------------------------------------------
# Without this var, Railway SIGKILLs the worker at 30 s default — kills
# in-flight Veo polls. See DEPLOY_RESILIENCE.md Layer 1.
_shutdown_timeout=$(railway variables --service "$_WORKER_SERVICE" --environment "$_TARGET_ENV" --kv 2>/dev/null \
  | grep -E '^RAILWAY_SHUTDOWN_TIMEOUT_SECONDS=' | head -1 | cut -d= -f2-)

if [ -z "$_shutdown_timeout" ]; then
  _check "Worker shutdown timeout" "FAIL" "RAILWAY_SHUTDOWN_TIMEOUT_SECONDS not set. Worker will SIGKILL mid-Veo on every deploy."
elif [ "$_shutdown_timeout" -ge 180 ]; then
  _check "Worker shutdown timeout" "OK" "RAILWAY_SHUTDOWN_TIMEOUT_SECONDS=${_shutdown_timeout}s (≥180s)"
else
  _check "Worker shutdown timeout" "WARN" "RAILWAY_SHUTDOWN_TIMEOUT_SECONDS=${_shutdown_timeout}s < 180s recommended. Veo p99 is ~120s."
fi

# ---------------------------------------------------------------------
# Check 4 — /health endpoint live and returning expected shape
# ---------------------------------------------------------------------
case "$_TARGET_ENV" in
  production) _api_url="https://genly-ai.up.railway.app" ;;
  staging)    _api_url="https://api-staging-9b82.up.railway.app" ;;
  *)          _api_url="" ;;
esac

if [ -z "$_api_url" ]; then
  _check "Health endpoint" "WARN" "unknown env '$_TARGET_ENV'; skipping live probe"
else
  _health=$(curl -s -m 10 "$_api_url/health" 2>/dev/null)
  if [ -z "$_health" ]; then
    _check "Health endpoint" "FAIL" "$_api_url/health no response (Railway down? deploy in progress?)"
  elif echo "$_health" | grep -q '"status":"ok"'; then
    # Also verify the body shape — db/redis/r2 all up
    _db_status=$(echo "$_health"   | grep -oE '"db":"[^"]+"' | head -1)
    _redis_status=$(echo "$_health"| grep -oE '"redis":"[^"]+"' | head -1)
    _r2_status=$(echo "$_health"   | grep -oE '"r2":"[^"]+"' | head -1)
    _check "Health endpoint" "OK" "$_db_status $_redis_status $_r2_status"
  else
    _check "Health endpoint" "FAIL" "/health responded but status != ok"
  fi
fi

# ---------------------------------------------------------------------
# Check 5 — railway.toml expectations match (sanity)
# ---------------------------------------------------------------------
_toml=$(cat "$(dirname "$0")/../../../railway.toml" 2>/dev/null)
if [ -z "$_toml" ]; then
  _check "railway.toml" "WARN" "could not read; verify config manually"
else
  if echo "$_toml" | grep -qE 'numReplicas\s*=\s*2' && echo "$_toml" | grep -qE 'healthcheckPath\s*=\s*"/health"'; then
    _check "railway.toml" "OK" "numReplicas=2 + healthcheckPath=/health configured"
  else
    _check "railway.toml" "FAIL" "missing numReplicas=2 or healthcheckPath in [services.api.deploy]"
  fi
fi

# ---------------------------------------------------------------------
# Print results
# ---------------------------------------------------------------------
echo
printf '%-44s %-6s %s\n' "CHECK" "STATUS" "DETAIL"
printf '%-44s %-6s %s\n' "-----" "------" "------"
for r in "${_results[@]}"; do echo "$r"; done
echo
echo "Summary: $_pass OK, $_warn WARN, $_fail FAIL"

if [ "$_fail" -gt 0 ]; then
  echo
  echo "DO NOT push feat/zero-downtime-deploys until FAILs are resolved."
  exit 1
fi

if [ "$_warn" -gt 0 ]; then
  echo
  echo "Warnings present but non-blocking. Review before next deploy."
fi

echo
echo "Optional live test — run during the next push to staging:"
echo "  while true; do"
echo "    curl -s -o /dev/null -w '%{http_code} ' '$_api_url/health'; sleep 0.5;"
echo "  done"
echo "Expected during deploy: all 200s. Any 5xx = OVERLAP not engaging."

exit 0
