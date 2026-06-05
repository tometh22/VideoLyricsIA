# Tier 2 event-loop load test

Validates the Tier 2 change (blocking DB/auth work moved off the uvicorn event
loop into the threadpool). Proves the loop stays responsive under concurrent
authenticated load instead of serializing all traffic.

## What it measures

- **`loop_health_ms`** — latency of `GET /health/live` (no-dependency liveness,
  served on the event loop) sampled at a steady rate *while* the `dashboards`
  scenario ramps to 80 VUs hammering the converted DB handlers (`/jobs`,
  `/usage`, `/auth/me`). **Flat `loop_health_ms` under load = the loop stayed
  free** (Tier 2 working). A spike would mean the loop is blocked.
- `handler_ms` — p50/p95/p99 of the converted handlers (200s only).
- `rate_limited_429` — count of throttled requests (see below).

## Run

```bash
cd scripts/loadtest
STAGING_USER=you@example.com STAGING_PASS='***' ./run.sh
```

Needs: Docker (runs `grafana/k6`), `python3`, `curl`. No secrets are stored —
credentials come from env. Throwaway `loadtest_*` users are registered on
staging (harmless; no delete endpoint).

## ⚠️ Disable the rate limiter for a REAL saturation run

The API rate-limiter (`120/min`, keyed by user_id) throttles the load — with it
on you get a wall of `rate_limited_429` and never saturate the threadpool/DB
pool, so you're testing the limiter, not the loop.

For a true saturation test, on the **staging API service** (Railway → Variables):

```
RATE_LIMIT_ENABLED=false
```

Wait for the redeploy, run `./run.sh`, then set it back to `true`. (The flag is
read at import, so it needs a restart/redeploy to take effect.)

## Caveats (read before trusting absolute numbers)

- **Run from in-region.** From a laptop over the internet, every request pays a
  fixed ~0.7s of TLS+RTT to Railway, so absolute latencies are inflated. The
  **delta** of `loop_health_ms` vs the no-load baseline (printed by `run.sh`) is
  the trustworthy signal. For clean absolute numbers, run k6 from a box in the
  app's region (a Railway service, or a us-west VM).
- **No A/B vs old code** once merged. A rigorous before/after runs this against
  the OLD-code preview deploy first, then the new — i.e. load-test *before*
  merging next time.
- This is the right rigor for the current scale. Continuous load-testing in CI,
  canary comparison, and instrumented event-loop-lag metrics are the next level
  (worth it around ~50+ concurrent users).
