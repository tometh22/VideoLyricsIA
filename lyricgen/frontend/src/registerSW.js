/* Service Worker registration — 2026-05-31.
 *
 * Registration is deliberately:
 *   - PROD-only (dev's HMR keeps changing chunk hashes — caching
 *     would just churn the SW install lifecycle in the operator's
 *     browser, no benefit while developing).
 *   - Post-load (waits for `load` event) so the SW download doesn't
 *     compete with the first paint's critical-path requests.
 *   - Crash-isolated (any error here is logged, not thrown — a broken
 *     SW must never break the app).
 *
 * Emergency rollback: setting the response of /sw.js to 404 makes the
 * browser unregister the SW on the next navigation. Or push an empty
 * `self.addEventListener('install', () => self.skipWaiting())` to drop
 * the cache without losing the SW machinery.
 */
export function registerServiceWorker() {
  // Skip in dev. import.meta.env.PROD is true only on `vite build`.
  if (!import.meta.env.PROD) return;

  if (typeof navigator === 'undefined' || !('serviceWorker' in navigator)) {
    return;
  }

  // Wait for `load` so the SW install doesn't steal bandwidth from
  // the first paint. The user is already looking at the rendered
  // app by the time we register.
  window.addEventListener('load', () => {
    navigator.serviceWorker
      .register('/sw.js')
      .then((reg) => {
        // Listen for updates so we can log them — useful when triaging
        // "is the user on the old build?" with Sentry breadcrumbs.
        reg.addEventListener('updatefound', () => {
          const installing = reg.installing;
          if (!installing) return;
          installing.addEventListener('statechange', () => {
            if (installing.state === 'activated') {
              console.info('[sw] new version activated');
            }
          });
        });
      })
      .catch((err) => {
        // Don't surface — a failed SW registration must never block
        // the app. Just log so it's debuggable in DevTools.
        console.warn('[sw] registration failed', err);
      });
  });
}
