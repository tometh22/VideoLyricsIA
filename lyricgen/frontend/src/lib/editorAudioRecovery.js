// The post-render editor must not confuse a temporarily saturated API with an
// audio file that does not exist.  `/source-audio-url` is a small metadata
// request, but it still needs auth + a DB checkout before it can hand the
// browser an R2 URL.  Keep this policy pure so the route and its regression
// tests agree on the wire contract.

const FALLBACK_RETRY_MS = 1_000;
const MAX_RETRY_AFTER_MS = 60_000;
const DEFAULT_URL_EXPIRY_SECONDS = 3_600;
const URL_REFRESH_SAFETY_MS = 5 * 60_000;
const MIN_URL_REFRESH_DELAY_MS = 5_000;
export const PROACTIVE_URL_RETRY_MS = 30_000;

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

// R2 URLs used by the editor are intentionally short-lived. Renew them before
// the signature expires so a long timing session does not run out of buffered
// audio halfway through the song. Keep the calculation pure for deterministic
// tests and clamp malformed server values to the endpoint's current contract.
export function audioUrlRefreshDelayMs(expiresInSeconds) {
  const parsed = Number(expiresInSeconds);
  const expiryMs = Number.isFinite(parsed) && parsed > 0
    ? parsed * 1_000
    : DEFAULT_URL_EXPIRY_SECONDS * 1_000;
  return Math.max(MIN_URL_REFRESH_DELAY_MS, expiryMs - URL_REFRESH_SAFETY_MS);
}

// A preventive renewal runs while the current URL still has a five-minute
// safety window. If the API is temporarily unavailable, fail open: keep the
// known-good source and retry shortly. Initial loads and recovery after an
// actual media error still fail closed so the UI can offer its manual retry.
export function editorAudioFailureState(previous, {
  reason,
  failureReason = "temporary",
  now = Date.now,
} = {}) {
  if (
    (reason === "signed_url_expiring" || reason === "preview_pending")
    && failureReason === "temporary"
    && previous?.audioUrl
  ) {
    return {
      ...previous,
      audioLoading: false,
      audioUnavailableReason: null,
      audioRefreshAt: now() + PROACTIVE_URL_RETRY_MS,
    };
  }
  return {
    ...previous,
    audioUrl: null,
    audioLoading: false,
    audioUnavailableReason: failureReason,
    audioRefreshAt: null,
  };
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
        if (body?.url) {
          const result = {
            ok: true,
            url: body.url,
            expiresIn: Number(body.expires_in) || DEFAULT_URL_EXPIRY_SECONDS,
          };
          // Keep the helper's legacy result shape for older servers while
          // accepting the additive preview metadata from the new contract.
          if (Object.prototype.hasOwnProperty.call(body, "source")) {
            result.source = body.source || null;
          }
          if (
            Object.prototype.hasOwnProperty.call(body, "preview_status")
            || Object.prototype.hasOwnProperty.call(body, "preview")
          ) {
            result.previewStatus = body.preview_status || body.preview?.status || null;
            result.previewRetryAfterSeconds = Number(
              body.preview_retry_after_seconds || body.preview?.retry_after_seconds,
            ) || 5;
          }
          return result;
        }
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
