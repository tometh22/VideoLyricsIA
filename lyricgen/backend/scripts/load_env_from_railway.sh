#!/usr/bin/env bash
# Load just the env vars the benchmark harness needs, live from Railway.
#
# WHY this script vs writing a .env.local with secrets baked in:
#   - Nothing is persisted to disk — secrets stay in your shell session
#   - You can rotate keys in Railway and the next `source` picks them up
#   - No risk of accidentally git-committing a .env.local
#
# Usage:
#   cd lyricgen/backend
#   source scripts/load_env_from_railway.sh                  # prod (default)
#   source scripts/load_env_from_railway.sh staging          # staging
#
# Requirements:
#   - railway CLI logged in (railway login)
#   - linked to the Genly IA project (railway link)
#   - jq installed (brew install jq)
#
# What it does:
#   1. Pulls all api-service vars from the chosen environment
#   2. Exports the subset the benchmark needs:
#      DATABASE_URL, R2_*, OPENAI_API_KEY, VERTEX_PROJECT, VERTEX_LOCATION
#   3. Decodes GOOGLE_APPLICATION_CREDENTIALS_JSON_B64 into a tmpfile
#      and exports GOOGLE_APPLICATION_CREDENTIALS pointing at it
#   4. Prints a summary (without echoing secret values)

set -e

_TARGET_ENV="${1:-production}"

if ! command -v railway >/dev/null 2>&1; then
  echo "[ERR] railway CLI not found. Install: brew install railway"
  return 1 2>/dev/null || exit 1
fi
if ! command -v jq >/dev/null 2>&1; then
  echo "[ERR] jq not found. Install: brew install jq"
  return 1 2>/dev/null || exit 1
fi

echo "[+] Pulling api-service env vars from '$_TARGET_ENV'..."
railway environment "$_TARGET_ENV" >/dev/null 2>&1 || {
  echo "[ERR] Could not switch to environment '$_TARGET_ENV'."
  echo "      Run 'railway environment' to list available envs."
  return 1 2>/dev/null || exit 1
}

# Pull as JSON so we can read individual keys without parsing key=value strings
_VARS_JSON="$(railway variables --service api --json 2>/dev/null)"
if [ -z "$_VARS_JSON" ] || [ "$_VARS_JSON" = "null" ]; then
  echo "[ERR] No vars returned. Are you logged in (railway whoami)?"
  return 1 2>/dev/null || exit 1
fi

# Extract one key at a time. jq's `-r` strips the JSON quotes.
_get_var() {
  echo "$_VARS_JSON" | jq -r --arg k "$1" '.[$k] // empty'
}

export DATABASE_URL="$(_get_var DATABASE_URL)"
export R2_ACCESS_KEY_ID="$(_get_var R2_ACCESS_KEY_ID)"
export R2_SECRET_ACCESS_KEY="$(_get_var R2_SECRET_ACCESS_KEY)"
export R2_ENDPOINT_URL="$(_get_var R2_ENDPOINT_URL)"
export R2_BUCKET="$(_get_var R2_BUCKET)"
export OPENAI_API_KEY="$(_get_var OPENAI_API_KEY)"
export VERTEX_PROJECT="$(_get_var VERTEX_PROJECT)"
export VERTEX_LOCATION="$(_get_var VERTEX_LOCATION)"

# Vertex creds live in env as base64-encoded JSON. The SDK wants a file
# path via GOOGLE_APPLICATION_CREDENTIALS, so decode to a tmpfile.
_VERTEX_B64="$(_get_var GOOGLE_APPLICATION_CREDENTIALS_JSON_B64)"
if [ -n "$_VERTEX_B64" ]; then
  _VERTEX_TMP="${TMPDIR:-/tmp}/vertex_creds_${_TARGET_ENV}.json"
  echo "$_VERTEX_B64" | base64 -d > "$_VERTEX_TMP"
  chmod 600 "$_VERTEX_TMP"
  export GOOGLE_APPLICATION_CREDENTIALS="$_VERTEX_TMP"
fi

# Sanity: count what we got
echo ""
echo "[+] Loaded env vars (values redacted):"
for var in DATABASE_URL R2_ACCESS_KEY_ID R2_SECRET_ACCESS_KEY R2_ENDPOINT_URL R2_BUCKET OPENAI_API_KEY VERTEX_PROJECT VERTEX_LOCATION GOOGLE_APPLICATION_CREDENTIALS; do
  val_len=$(eval "echo -n \"\${$var:-}\"" | wc -c | tr -d ' ')
  if [ "$val_len" -gt 0 ]; then
    echo "    ✓ $var  (${val_len} chars)"
  else
    echo "    ✗ $var  (MISSING)"
  fi
done
echo ""
echo "[+] Ready. Next:"
echo "    edit scripts/benchmark_jobs.txt with your ground-truth job_ids"
echo "    python scripts/build_benchmark_dataset.py"
