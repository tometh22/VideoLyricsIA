// fetch() that aborts after `timeoutMs`, so a hung backend never leaves
// the UI stuck on a spinner. Used by dashboard hooks (/usage, /jobs) where
// "cargando..." must be a temporary state, never a permanent one.
//
// Returns the fetch Response on success. Throws on:
//   - network failure (browser CORS reject, DNS, etc.)
//   - timeout exceeded (AbortError, surfaced as Error("timeout"))
// The caller decides whether to retry, surface the error, or fall back.

const DEFAULT_TIMEOUT_MS = 10_000;

export async function fetchWithTimeout(url, opts = {}, timeoutMs = DEFAULT_TIMEOUT_MS) {
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), timeoutMs);
  try {
    return await fetch(url, { ...opts, signal: controller.signal });
  } catch (err) {
    if (err && err.name === "AbortError") {
      const e = new Error("timeout");
      e.name = "TimeoutError";
      throw e;
    }
    throw err;
  } finally {
    clearTimeout(timer);
  }
}
