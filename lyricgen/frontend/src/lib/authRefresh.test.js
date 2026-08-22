import {
  AUTH_REFRESH_THRESHOLD_SECONDS,
  acquireAuthRefreshLease,
  releaseAuthRefreshLease,
  shouldRefreshToken,
} from "./authRefresh";

function storage() {
  const values = new Map();
  return {
    getItem: (key) => values.get(key) || null,
    setItem: (key, value) => values.set(key, String(value)),
    removeItem: (key) => values.delete(key),
  };
}

test("refresh threshold leaves a freshly-issued 24h token alone", () => {
  expect(shouldRefreshToken(24 * 60 * 60 - 1)).toBe(false);
  expect(shouldRefreshToken(AUTH_REFRESH_THRESHOLD_SECONDS)).toBe(true);
});

test("only one tab owns the short refresh lease", () => {
  const store = storage();
  const first = acquireAuthRefreshLease(store, 1000);
  expect(first).toBeTruthy();
  expect(acquireAuthRefreshLease(store, 1001)).toBeNull();
  releaseAuthRefreshLease(store, first);
  expect(acquireAuthRefreshLease(store, 1002)).toBeTruthy();
});

test("expired lease can be recovered", () => {
  const store = storage();
  const first = acquireAuthRefreshLease(store, 1000);
  expect(acquireAuthRefreshLease(store, first.expiresAt + 1)).toBeTruthy();
});
