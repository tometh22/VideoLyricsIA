/* Auto-update on new deploy — 2026-06-26.
 *
 * Problem: this is an SPA. Once a tab is open, client-side routing never
 * re-fetches index.html, so a new Vercel deploy (new hashed bundles) never
 * reaches that tab until a manual reload. The service worker doesn't help —
 * `public/sw.js` is a static file whose bytes don't change between deploys, so
 * the browser never sees a "new SW" to trigger an update/reload. Operators with
 * a long-lived editor tab stayed on the old build indefinitely (incident: we
 * shipped the editor reseed-storm fix but couldn't reach the users to reload).
 *
 * Fix: poll the network-first index.html and compare its hashed /assets/* refs
 * against the ones we booted with. When they differ, a new build is live — and
 * we reload **only while the tab is backgrounded** (visibilitychange → hidden),
 * so the operator is never interrupted mid-edit and comes back already updated.
 * The keepalive flush (#727) persists any unsaved work on the reload's pagehide.
 *
 * Prod-only (dev hashes churn). Best-effort: any fetch error is swallowed.
 */

/** Extract the sorted set of hashed build assets referenced by an index.html. */
export function extractAssetRefs(html) {
  if (typeof html !== "string") return [];
  const refs = new Set();
  const re = /\/assets\/[A-Za-z0-9._-]+\.(?:js|css)/g;
  let m;
  while ((m = re.exec(html))) refs.add(m[0]);
  return [...refs].sort();
}

/**
 * True only when both sides parsed to a non-empty asset set AND they differ —
 * i.e. a genuinely new build. Empty/unparseable input → false (never reload on
 * a transient/offline read).
 */
export function buildChanged(bootRefs, latestRefs) {
  if (!Array.isArray(bootRefs) || !Array.isArray(latestRefs)) return false;
  if (bootRefs.length === 0 || latestRefs.length === 0) return false;
  return bootRefs.join("|") !== latestRefs.join("|");
}

export function initAutoUpdate({ pollMs = 10 * 60 * 1000, env = import.meta.env } = {}) {
  if (!env || !env.PROD) return; // dev: hashes churn, never reload
  if (typeof document === "undefined" || typeof fetch === "undefined") return;

  let bootRefs = null;
  let pending = false;

  const fetchRefs = async () => {
    try {
      const res = await fetch("/", { cache: "no-store" });
      if (!res.ok) return null;
      return extractAssetRefs(await res.text());
    } catch {
      return null; // offline / transient — try again next tick
    }
  };

  const reloadOnce = () => {
    // Circuit breaker: never reload twice in quick succession (guards against a
    // CDN flapping old/new index.html mid-rollout).
    const last = parseInt(sessionStorage.getItem("__autoupdate_reload_at") || "0", 10);
    if (Date.now() - last < 60_000) return;
    sessionStorage.setItem("__autoupdate_reload_at", String(Date.now()));
    // eslint-disable-next-line no-console
    console.warn("[auto-update] new build detected — reloading to update");
    window.location.reload();
  };

  const check = async () => {
    const latest = await fetchRefs();
    if (!latest || !bootRefs) return;
    if (buildChanged(bootRefs, latest)) {
      pending = true;
      // Only apply while hidden so we never interrupt active editing.
      if (document.visibilityState === "hidden") reloadOnce();
    }
  };

  fetchRefs().then((refs) => { bootRefs = refs; });

  document.addEventListener("visibilitychange", () => {
    if (document.visibilityState === "hidden") {
      if (pending) reloadOnce();
      else check();
    } else {
      check(); // returned to the tab — refresh the pending flag
    }
  });

  setInterval(check, pollMs);
}
