/**
 * Frontend observability — Sentry init.
 *
 * Mirror of `lyricgen/backend/observability.py::init_sentry`. Same
 * principle: no-op when DSN is missing so dev sessions never break
 * because of telemetry config.
 *
 * Why this matters: PR #474 fixed a P0 ("Aprobar y generar" silently
 * failed in prod for Agus). The bug existed for hours before the
 * operator told us. With Sentry wired, the first crash inside
 * GlobalErrorBoundary would have surfaced as a Slack/email alert in
 * seconds — no waiting for a user to report.
 *
 * DSN setup (Vercel):
 *   1. Create a Sentry project of type "React" at sentry.io
 *   2. Copy the DSN (looks like https://abc@oXYZ.ingest.sentry.io/123)
 *   3. Vercel dashboard → Genly project → Settings → Environment Variables
 *      → add VITE_SENTRY_DSN for Production AND Preview
 *   4. Redeploy. The bundle picks up the DSN at build time (Vite inlines
 *      VITE_* vars; runtime env vars are NOT supported in the browser).
 *
 * Without DSN, this module logs a one-line warning and stays inert.
 */
import * as Sentry from "@sentry/react";

const DSN = import.meta.env?.VITE_SENTRY_DSN || "";
const ENV = import.meta.env?.VITE_SENTRY_ENV
  || import.meta.env?.MODE
  || "dev";

let _initialized = false;

export function initSentry() {
  if (_initialized) return;
  _initialized = true;
  if (!DSN) {
    console.warn("[OBS] VITE_SENTRY_DSN not set — Sentry disabled (dev or unconfigured deploy)");
    return;
  }
  try {
    Sentry.init({
      dsn: DSN,
      environment: ENV,
      // 10% trace sampling — mirrors backend traces_sample_rate. Enough
      // to spot regressions, low enough to stay in Sentry free tier.
      tracesSampleRate: 0.1,
      // Replay disabled by default (extra cost + privacy review). Turn
      // on per-session via Sentry.replayIntegration() if we ever want
      // session-replay debugging for a specific incident.
      replaysSessionSampleRate: 0,
      replaysOnErrorSampleRate: 0,
      // Don't send PII in default events. We send tenant_id and role
      // via setUser below (non-PII identifiers), enough to scope
      // incidents to the affected operator without leaking emails.
      sendDefaultPii: false,
      // Tag every event with the current release so we can correlate
      // a spike to a specific deploy. Vercel exposes the commit SHA as
      // VERCEL_GIT_COMMIT_SHA at build time, but Vite only inlines
      // VITE_*-prefixed vars — the `build` script in package.json maps
      // it into VITE_RELEASE (UMG-launch hardening 2026-06-01; before
      // that, prod events were never release-tagged).
      release: import.meta.env?.VITE_RELEASE
        || import.meta.env?.VITE_COMMIT_SHA
        || undefined,
      // Drop noisy errors we already know about and can't fix from
      // the browser side (extension injections, third-party SDK
      // network blips). Add to this list AFTER confirming the alert
      // is genuinely useless — don't pre-filter on speculation.
      ignoreErrors: [
        // Vite preload errors are handled by the reload-on-stale-bundle
        // hook in main.jsx; don't double-alert.
        /Failed to fetch dynamically imported module/i,
        // Browser extensions throw these on innocuous DOM mutations.
        /ResizeObserver loop limit exceeded/i,
        /ResizeObserver loop completed with undelivered notifications/i,
      ],
    });
    console.info("[OBS] Sentry initialized", { env: ENV });
  } catch (e) {
    // Never let Sentry init throw out of the app boot.
    console.warn("[OBS] Sentry init failed:", e?.message || e);
  }
}

/**
 * Attach the logged-in user's identity (tenant + role only — no PII)
 * to subsequent events. Call once after login resolves. Pass null on
 * logout to clear.
 */
export function setSentryUser(user) {
  if (!_initialized || !DSN) return;
  try {
    if (!user) {
      Sentry.setUser(null);
      return;
    }
    Sentry.setUser({
      // username, not email — non-PII identifier for cross-event scoping.
      username: user.username || undefined,
      // Tenant + role are operationally critical for triaging — "is
      // this affecting UMG?" is the first question on any incident.
      tenant_id: user.tenant_id || undefined,
      role: user.role || undefined,
    });
  } catch {
    /* noop */
  }
}

/**
 * Capture a handled exception with optional context. Use for caught
 * errors that we want surfaced to the dashboard but don't want to
 * crash the UI (network glitches, edge cases). Unhandled errors are
 * captured automatically via GlobalErrorBoundary + window listeners.
 */
export function captureHandledError(error, context = {}) {
  if (!_initialized || !DSN) return;
  try {
    Sentry.captureException(error, { extra: context });
  } catch {
    /* noop */
  }
}
