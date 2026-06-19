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

// ── Console-tag forwarding (observability world-class, 2026-06-10) ──
//
// Incidente del editor de sync: los console.warn de diagnóstico
// ("[drag-persist] prop-sync RESEEDING") dispararon en el browser de la
// operadora y murieron ahí — el bug no dejó NINGÚN rastro server-side
// porque nunca lanzó una excepción. Regla nueva: todo console.warn que
// empiece con un tag "[algo]" se forwardea a Sentry como evento warning
// agrupado por tag (un issue por tag, contador de eventos). Lo no
// tagueado se descarta (ruido de libs/React).

/** Extrae el tag "[xxx]" del inicio de un mensaje de consola, o null. */
export function consoleTagOf(message) {
  if (typeof message !== "string") return null;
  const m = message.match(/^\[([\w-]{2,40})\]/);
  return m ? m[1].toLowerCase() : null;
}

// Throttle client-side por tag: protege la cuota de Sentry si un warn
// taggeado entra en loop (la lección del loop de re-renders: 5000
// ciclos/100ms habrían sido 5000 eventos/seg). 1 evento por tag por
// minuto; la frecuencia real igual se ve en el issue agrupado.
const _TAG_THROTTLE_MS = 60_000;
const _lastSentByTag = new Map();

// Tags that signal a UI freeze / render-storm (P0 UMG Chile 2026-06-16). These
// escalate to `error` level in beforeSend so Sentry's replaysOnErrorSampleRate
// (1.0) records a Session Replay of the moment — a watchable clip of the freeze
// instead of just a counter. Everything else stays `warning` (no replay).
const _FREEZE_TAGS = new Set([
  "ui-freeze",
  "reseed-storm",
  "render-storm",
  "editor-reload-loop",
  "ui-longtask-burst",
]);

/** Decide si un mensaje de consola se forwardea. Exportado para tests. */
export function shouldForwardConsoleEvent(message, now = Date.now()) {
  const tag = consoleTagOf(message);
  if (!tag) return { forward: false, tag: null };
  const last = _lastSentByTag.get(tag) || 0;
  if (now - last < _TAG_THROTTLE_MS) return { forward: false, tag };
  _lastSentByTag.set(tag, now);
  return { forward: true, tag };
}

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
      // Observability world-class (2026-06-10, post incidente del editor):
      //
      // browserTracingIntegration — SIN esta integración explícita,
      // tracesSampleRate solo era decorativo (v8+ no la incluye por
      // default): hoy descubrimos que NUNCA capturamos traces ni Web
      // Vitals. Con ella: pageload/navigation + INP/LCP/CLS. El loop de
      // re-renders del editor (5000 ciclos/100ms) habría pintado un INP
      // catastrófico ANTES del reporte de la operadora.
      //
      // replayIntegration — graba la sesión (DOM + consola + red) con
      // masking TOTAL de texto e inputs (las lyrics de los tenants son
      // contenido bajo contrato — jamás salen legibles). 100% de las
      // sesiones con error + muestra del resto. El bug del editor no
      // dejó rastro server-side; con replay, el reporte de la operadora
      // se convierte en "mirar el minuto exacto".
      //
      // captureConsoleIntegration — los console.warn taggeados
      // ("[drag-persist] …") suben como eventos agrupados por tag (ver
      // beforeSend). Lo no-tagueado se descarta.
      integrations: [
        Sentry.browserTracingIntegration(),
        Sentry.replayIntegration({
          maskAllText: true,
          maskAllInputs: true,
          blockAllMedia: true,
        }),
        Sentry.captureConsoleIntegration({ levels: ["warn"] }),
      ],
      tracesSampleRate: parseFloat(import.meta.env?.VITE_SENTRY_TRACES_RATE || "0.1"),
      replaysSessionSampleRate: parseFloat(import.meta.env?.VITE_SENTRY_REPLAY_SESSION_RATE || "0.1"),
      replaysOnErrorSampleRate: 1.0,
      // Filtro de eventos de consola: solo pasan los warn taggeados, con
      // fingerprint por tag (un issue agrupado por familia de diagnóstico)
      // y throttle client-side de 1/min por tag para cuidar la cuota.
      beforeSend(event) {
        if (event.logger !== "console") return event;
        const msg = event.message
          || event.logentry?.message
          || (event.logentry?.formatted ?? "");
        const { forward, tag } = shouldForwardConsoleEvent(msg);
        if (!forward) return null;
        event.fingerprint = ["console-tag", tag];
        // Freeze/storm diagnostics escalate to `error` so replaysOnErrorSampleRate
        // (1.0) records a Session Replay of the ~60s leading up to the freeze —
        // turning the operator's "se traba / titila" into a watchable clip with
        // the exact action sequence (P0 UMG Chile 2026-06-16). Other tagged
        // diagnostics stay `warning` (no replay, saves quota).
        event.level = _FREEZE_TAGS.has(tag) ? "error" : "warning";
        return event;
      },
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
