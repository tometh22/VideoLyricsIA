#!/usr/bin/env bash
# Run the Tier 2 event-loop load test.
#
#   STAGING_USER=tomas@epical.digital STAGING_PASS='***' ./run.sh
#
# Steps: provision tokens -> sample no-load baseline of /health/live ->
# run k6 (Docker) -> the k6 summary reports loop_health_ms (the loop signal).
# Compare loop_health_ms p95 against the baseline printed below: a flat
# loop_health under load = the event loop stayed free (Tier 2 working).
#
# For a REAL saturation run, first disable the limiter on the staging API
# service: RATE_LIMIT_ENABLED=false (Railway → service → Variables), wait for
# redeploy, run, then set it back to true. With the limiter on you'll see lots
# of rate_limited_429 and the load never saturates.
set -euo pipefail
cd "$(dirname "$0")"

BASE="${STAGING_BASE:-https://api-staging-9b82.up.railway.app}"
export STAGING_BASE="$BASE"

echo "=== 1. provision tokens ==="
./provision_tokens.sh

echo ""
echo "=== 2. baseline /health/live (no load, 8 samples) ==="
python3 - "$BASE" <<'PY'
import sys, urllib.request, ssl, time, statistics
base=sys.argv[1]; ctx=ssl.create_default_context(); xs=[]
for _ in range(8):
    t=time.time()
    try:
        urllib.request.urlopen(base+"/health/live", timeout=10, context=ctx).read()
        xs.append((time.time()-t)*1000)
    except Exception: pass
print(f"  baseline loop_health p50={statistics.median(xs):.0f}ms p95={sorted(xs)[int(len(xs)*0.95)-1]:.0f}ms")
print("  (compare k6's loop_health_ms p95 below against this — flat = loop free)")
PY

echo ""
echo "=== 3. k6 load test ==="
if command -v k6 >/dev/null 2>&1; then
  BASE_URL="$BASE" k6 run tier2_eventloop.js
else
  # Fallback: k6 via Docker (needs the daemon running).
  docker run --rm -i -e BASE_URL="$BASE" -v "$(pwd)":/ls -w /ls grafana/k6 run tier2_eventloop.js
fi
