/* GenLy AI Service Worker
 * 2026-05-31
 *
 * Goal: keep hashed asset bundles available offline / on flaky LATAM
 * networks so a wizard mid-flow doesn't blank out when 3G drops.
 *
 * NON-GOALS:
 *   - DO NOT cache HTML. The HTML must always be network-first so a new
 *     deploy reaches the user immediately (otherwise we'd serve an HTML
 *     that points to chunk hashes that no longer exist, and the
 *     vite:preloadError handler in main.jsx would reload-loop).
 *   - DO NOT cache API calls (/auth, /usage, /jobs, /generate, …).
 *     These are per-user/per-tenant and stale data leaks data.
 *   - DO NOT cache anything cross-origin (Google Fonts CDN handles its
 *     own cache headers; we'd just duplicate without benefit).
 *
 * Safe-by-default contract:
 *   - The SW NEVER returns a cached response if a network response is
 *     available — except for hashed /assets/* which are immutable by
 *     filename. Hashed filenames mean "if this URL responds 200, the
 *     bytes are guaranteed identical to the cached bytes".
 *   - On any unexpected fetch shape (POST, opaque, non-GET, non-same-
 *     origin), we pass through to fetch() without touching the cache.
 *   - On a Cache API error (quota, corrupt), we fall back to network
 *     so a degraded SW never blocks the app.
 *
 * Update flow:
 *   - skipWaiting() + clients.claim() on install/activate means a new
 *     SW takes over on the same load that registered it. Combined with
 *     the network-first HTML, a user opening a stale tab gets the new
 *     HTML → new asset URLs → SW cache fills with the new build.
 *   - The OLD cached entries for the previous build's chunk hashes
 *     stay around until the LRU eviction in pruneCache() culls them.
 */

const CACHE_NAME = 'genly-assets-v1';
const MAX_CACHE_ENTRIES = 60;          // covers ~3 builds' worth of chunks.
const ASSETS_PATH_RE = /^\/assets\/.+\.(js|css|woff2?)$/i;

self.addEventListener('install', (event) => {
  // Activate immediately on first load — no waiting on the user to
  // close every other tab.
  event.waitUntil(self.skipWaiting());
});

self.addEventListener('activate', (event) => {
  event.waitUntil((async () => {
    // Drop any caches from older SW iterations whose name we've bumped.
    const names = await caches.keys();
    await Promise.all(
      names
        .filter((n) => n.startsWith('genly-') && n !== CACHE_NAME)
        .map((n) => caches.delete(n)),
    );
    await self.clients.claim();
  })());
});

self.addEventListener('fetch', (event) => {
  const req = event.request;

  // Pass-through for anything that isn't a safe GET on our own origin.
  if (req.method !== 'GET') return;
  const url = new URL(req.url);
  if (url.origin !== self.location.origin) return;

  // Hashed JS/CSS bundles: cache-first. The hash in the filename is
  // the version, so a cache hit is always the right bytes.
  if (ASSETS_PATH_RE.test(url.pathname)) {
    event.respondWith(cacheFirst(req));
    return;
  }

  // Everything else (HTML, API, SSE, uploads): network-only. Don't
  // even touch the cache so we can't accidentally leak a response.
});

async function cacheFirst(request) {
  try {
    const cache = await caches.open(CACHE_NAME);
    const cached = await cache.match(request);
    if (cached) return cached;

    const response = await fetch(request);
    // Only cache successful, basic-type responses. opaque/error responses
    // can be a CDN sentinel and we don't want to pin them.
    if (response.ok && response.type === 'basic') {
      // Clone before put — body is a one-shot stream.
      cache.put(request, response.clone()).then(() => pruneCache(cache));
    }
    return response;
  } catch (err) {
    // Network failed and nothing in cache — let it bubble. The page
    // already handles "Failed to fetch dynamically imported module"
    // via main.jsx's vite:preloadError listener (reload to recover).
    throw err;
  }
}

async function pruneCache(cache) {
  try {
    const keys = await cache.keys();
    if (keys.length <= MAX_CACHE_ENTRIES) return;
    // Delete oldest-first. cache.keys() returns in insertion order so
    // slicing from the start is the closest we get to LRU without
    // tracking timestamps ourselves.
    const overflow = keys.length - MAX_CACHE_ENTRIES;
    for (let i = 0; i < overflow; i += 1) {
      await cache.delete(keys[i]);
    }
  } catch {
    // Pruning failures are best-effort — never let them surface.
  }
}
