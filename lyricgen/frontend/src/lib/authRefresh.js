// Keep session refreshes bounded and coordinated across tabs. Staging uses a
// 24-hour JWT, so a "refresh when less than 24 hours remain" policy refreshes
// the freshly-issued token immediately and creates a request storm.

export const AUTH_REFRESH_THRESHOLD_SECONDS = 6 * 60 * 60;
export const AUTH_REFRESH_LEASE_MS = 15_000;
export const AUTH_REFRESH_MIN_INTERVAL_MS = 60_000;
export const AUTH_REFRESH_LOCK_KEY = "genly_auth_refresh_lock";

export function shouldRefreshToken(secondsLeft) {
  return Number.isFinite(secondsLeft) && secondsLeft <= AUTH_REFRESH_THRESHOLD_SECONDS;
}

function ownerId() {
  try {
    if (typeof crypto !== "undefined" && crypto.randomUUID) return crypto.randomUUID();
  } catch { /* browser crypto may be unavailable in older clients */ }
  return `${Date.now()}-${Math.random().toString(36).slice(2)}`;
}

export function acquireAuthRefreshLease(storage, now = Date.now()) {
  if (!storage) return null;
  let existing = null;
  try {
    existing = JSON.parse(storage.getItem(AUTH_REFRESH_LOCK_KEY) || "null");
  } catch { /* malformed lock is safe to replace */ }
  if (existing?.expiresAt > now) return null;
  const lease = { owner: ownerId(), expiresAt: now + AUTH_REFRESH_LEASE_MS };
  try {
    storage.setItem(AUTH_REFRESH_LOCK_KEY, JSON.stringify(lease));
    const confirmed = JSON.parse(storage.getItem(AUTH_REFRESH_LOCK_KEY) || "null");
    return confirmed?.owner === lease.owner ? lease : null;
  } catch {
    // If storage is disabled, keep the per-tab single-flight protection in
    // App.jsx; failure to coordinate must not log a user out.
    return lease;
  }
}

export function releaseAuthRefreshLease(storage, lease) {
  if (!storage || !lease) return;
  try {
    const current = JSON.parse(storage.getItem(AUTH_REFRESH_LOCK_KEY) || "null");
    if (current?.owner === lease.owner) storage.removeItem(AUTH_REFRESH_LOCK_KEY);
  } catch { /* best effort */ }
}
