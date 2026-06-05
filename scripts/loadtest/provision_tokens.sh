#!/usr/bin/env bash
# Provision JWT tokens for the k6 load test and write them to tokens.json.
#
# The API rate-limiter keys by user_id (120/min each), so multiple users give
# multiple buckets. /auth/register is rate-limited to 5/min per IP, so we keep
# the new-user count small. NO secrets in this file — pass via env:
#
#   STAGING_BASE=https://api-staging-9b82.up.railway.app \
#   STAGING_USER=tomas@epical.digital STAGING_PASS='***' \
#   N_EXTRA=4 ./provision_tokens.sh
#
# Writes ./tokens.json (array of JWT strings). Throwaway loadtest_* users are
# created on staging only — clean them up later if you care (no delete endpoint;
# harmless on staging).
set -euo pipefail
cd "$(dirname "$0")"

BASE="${STAGING_BASE:-https://api-staging-9b82.up.railway.app}"
USER="${STAGING_USER:?set STAGING_USER}"
PASS="${STAGING_PASS:?set STAGING_PASS}"
N_EXTRA="${N_EXTRA:-4}"   # extra throwaway users (keep < 5 to respect register 5/min)

tokens=()

extract() { python3 -c "import sys,json; print(json.load(sys.stdin).get('token',''))"; }

echo "[provision] login $USER"
t=$(curl -sS -m 20 -X POST "$BASE/auth/login" -H "Content-Type: application/json" \
    -d "{\"username\":\"$USER\",\"password\":\"$PASS\"}" | extract)
[ -n "$t" ] && tokens+=("$t") || { echo "  login FAILED"; exit 1; }

ts=$(date +%s)
for i in $(seq 1 "$N_EXTRA"); do
  u="loadtest_${ts}_${i}"
  echo "[provision] register $u"
  t=$(curl -sS -m 20 -X POST "$BASE/auth/register" -H "Content-Type: application/json" \
      -d "{\"username\":\"$u\",\"password\":\"LoadTest_x9!\",\"email\":\"\"}" | extract || true)
  [ -n "$t" ] && tokens+=("$t") || echo "  register $u skipped (429/limit?)"
done

# Write tokens.json
python3 - "${tokens[@]}" <<'PY'
import sys, json
toks = [t for t in sys.argv[1:] if t]
json.dump(toks, open("tokens.json", "w"))
print(f"[provision] wrote {len(toks)} tokens -> tokens.json")
PY
