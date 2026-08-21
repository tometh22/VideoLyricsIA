// The post-render editor must not confuse a temporarily saturated API with an
// audio file that does not exist.  `/source-audio-url` is a small metadata
// request, but it still needs auth + a DB checkout before it can hand the
// browser an R2 URL.  Keep this policy pure so the route and its regression
// tests agree on the wire contract.

const FALLBACK_RETRY_MS = 1_000;
const MAX_RETRY_AFTER_MS = 60_000;

export function isTransientAudioFailure(response) {
  const status = response?.status;
  return status === 408 || status === 429 || status === 503 || status >= 500;
}

export function retryAfterMs(response, attempt) {
  const raw = Number.parseInt(response?.headers?.get?.("Retry-After") || "", 10);
  if (Number.isFinite(raw) && raw > 0) {
    return Math.min(raw * 1_000, MAX_RETRY_AFTER_MS);
  }
  // A bounded exponential fallback prevents a flaky connection from turning
  // into a request storm when the server could not provide a Retry-After.
  return Math.min(FALLBACK_RETRY_MS * (2 ** attempt), MAX_RETRY_AFTER_MS);
}

/**
 * Fetch a signed source-audio URL without declaring the audio missing just
 * because the API was under temporary pressure.  The caller owns UI state and
 * may invoke this again from an explicit "Reintentar audio" action.
 */
export async function loadEditorAudio({
  request,
  wait = (ms) => new Promise((resolve) => setTimeout(resolve, ms)),
  maxRetries = 3,
  onRetry = null,
}) {
  let lastTransient = null;
  for (let attempt = 0; attempt <= maxRetries; attempt += 1) {
    try {
      const response = await request();
      if (response?.ok) {
        const body = await response.json();
        if (body?.url) return { ok: true, url: body.url };
        // The endpoint's successful contract always contains a URL. Treat a
        // malformed success as temporary rather than falsely saying the file
        // was deleted.
        lastTransient = response;
      } else if (response?.status === 404) {
        return { ok: false, reason: "missing" };
      } else if (isTransientAudioFailure(response)) {
        lastTransient = response;
      } else {
        // 401/403 and unexpected 4xx are not proof that R2 lacks the audio.
        // Preserve the editor and offer a manual retry after auth/network
        // recovery instead of presenting a false permanent absence.
        lastTransient = response;
      }
    } catch {
      lastTransient = null;
    }

    if (attempt < maxRetries) {
      const delayMs = retryAfterMs(lastTransient, attempt);
      onRetry?.({ attempt: attempt + 1, delayMs });
      await wait(delayMs);
    }
  }
  return { ok: false, reason: "temporary" };
}
