/**
 * Stale-while-revalidate cache layer for JSON GETs (2026-05-30 perf).
 *
 * Why: the SaaS feels "snappy" if the operator's first paint after login
 * shows their data immediately — not after a 600-700 ms backend
 * round-trip. The data we cache here doesn't change minute-to-minute
 * (usage counter, plan info), so showing a 30 s-old value while the
 * background fetch refreshes is invisible to the operator AND removes
 * one synchronous waterfall step from every page that uses this hook.
 *
 * Contract:
 *   - `cached` is the last successful response payload, or null if
 *     nothing fresh enough is in localStorage.
 *   - `refresh()` always fires a network call. If the response shape
 *     matches the cache, calls the consumer's `onFresh` callback so
 *     the UI can re-sync.
 *   - The cache key is scoped by the caller via `cacheKey`. Pass
 *     `usage:<userId>` (NOT just `usage`) so two operators on the same
 *     laptop don't share state.
 *   - TTL is for the cache READ ONLY. The interval at which the
 *     consumer re-fetches is its own decision (UsageBadge polls every
 *     60 s; this layer just makes the FIRST paint instant).
 *
 * What this is NOT:
 *   - It's not a global request deduplicator. Two components asking for
 *     the same URL will each get the cached value (free) but each will
 *     fire its own network refresh. That's fine for now — UsageBadge
 *     is the only consumer.
 *   - It does NOT write through to the cache on every refresh; only
 *     when the new response actually replaces stale data. Otherwise a
 *     500-byte localStorage write happens 60×/minute for no reason.
 */

/** Read a JSON value from localStorage; tolerate quota / parse errors. */
function readCache(key) {
  try {
    const raw = localStorage.getItem(key);
    if (!raw) return null;
    const parsed = JSON.parse(raw);
    if (!parsed || typeof parsed.ts !== "number") return null;
    return parsed;
  } catch {
    return null;
  }
}

/** Write a JSON value to localStorage; swallow quota errors silently. */
function writeCache(key, payload) {
  try {
    localStorage.setItem(
      key,
      JSON.stringify({ ts: Date.now(), payload }),
    );
  } catch {
    /* quota exceeded / private mode — UI still works without cache */
  }
}

/**
 * Read the cached payload if it's younger than `ttlMs`. Returns null if
 * no cache, expired cache, or malformed cache.
 */
export function readCachedJson(cacheKey, ttlMs) {
  const entry = readCache(cacheKey);
  if (!entry) return null;
  if (Date.now() - entry.ts > ttlMs) return null;
  return entry.payload;
}

/**
 * Persist a fresh response. The consumer should call this AFTER a
 * successful network refresh, so the next mount paints instantly.
 */
export function writeCachedJson(cacheKey, payload) {
  writeCache(cacheKey, payload);
}

/**
 * Drop a single cached entry. Use on logout to avoid leaking the
 * previous operator's data to the next one on the same machine.
 */
export function clearCachedJson(cacheKey) {
  try {
    localStorage.removeItem(cacheKey);
  } catch {
    /* noop */
  }
}
