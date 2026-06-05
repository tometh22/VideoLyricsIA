// k6 load test — Tier 2 event-loop unblocking validation.
//
// Hypothesis under test: after Tier 2 (blocking DB/auth work moved off the
// event loop into the threadpool), the loop stays responsive even while many
// concurrent authenticated requests hammer the converted DB-touching handlers.
//
// The signal: `loop_health_ms` — latency of GET /health/live (a no-dependency
// liveness endpoint served directly on the event loop). If the loop were
// blocked by sync DB work, this would spike under load. If Tier 2 works, it
// stays flat while the `dashboards` scenario ramps.
//
// Run via run.sh (provisions tokens, runs k6 in Docker). Reads:
//   - BASE_URL env  (staging API base)
//   - ./tokens.json (array of JWT strings, written by provision_tokens.sh)
//
// IMPORTANT: for a real saturation test, disable the rate limiter on the
// staging API service first (RATE_LIMIT_ENABLED=false) — otherwise most
// requests return 429 and you're testing the limiter, not the event loop.
// See README.md.

import http from 'k6/http';
import { Trend, Rate, Counter } from 'k6/metrics';
import { sleep } from 'k6';

const BASE = __ENV.BASE_URL || 'https://api-staging-9b82.up.railway.app';
const TOKENS = JSON.parse(open('./tokens.json')); // [jwt, jwt, ...]

const loopLatency = new Trend('loop_health_ms', true);  // event-loop responsiveness
const handlerLatency = new Trend('handler_ms', true);   // converted DB handlers (200s only)
const rl429 = new Counter('rate_limited_429');
const errors = new Rate('errors');

export const options = {
  scenarios: {
    // Realistic operator dashboards polling the converted handlers concurrently.
    dashboards: {
      executor: 'ramping-vus',
      startVUs: 0,
      stages: [
        { duration: '20s', target: 40 },
        { duration: '40s', target: 80 },
        { duration: '15s', target: 0 },
      ],
      exec: 'dashboard',
      gracefulStop: '5s',
    },
    // The loop-health probe: steady low rate, measures whether the loop stays
    // responsive WHILE the dashboards scenario saturates the threadpool/DB pool.
    loop_probe: {
      executor: 'constant-arrival-rate',
      rate: 5, timeUnit: '1s', duration: '75s',
      preAllocatedVUs: 10, maxVUs: 20,
      exec: 'loopProbe',
    },
  },
  thresholds: {
    // The whole point: the loop stays responsive under load. Bound is
    // network-inclusive (laptop→Railway adds a fixed ~0.7s); run from
    // in-region to tighten this. The DELTA vs the no-load baseline is what
    // matters — see run.sh output.
    'loop_health_ms': ['p(95)<2000'],
    'handler_ms': ['p(99)<8000'],
  },
};

function tok() { return TOKENS[Math.floor(Math.random() * TOKENS.length)]; }

export function dashboard() {
  const params = { headers: { Authorization: `Bearer ${tok()}` } };
  // The hot, converted, DB/auth-touching handlers a dashboard hits each poll.
  for (const path of ['/jobs', '/usage', '/auth/me']) {
    const r = http.get(`${BASE}${path}`, params);
    if (r.status === 200) handlerLatency.add(r.timings.duration);
    else if (r.status === 429) rl429.add(1);
    else errors.add(1);
  }
  sleep(1); // think time between polls
}

export function loopProbe() {
  const r = http.get(`${BASE}/health/live`);
  loopLatency.add(r.timings.duration);
}
