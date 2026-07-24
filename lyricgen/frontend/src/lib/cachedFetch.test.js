/**
 * Tests for the stale-while-revalidate cache helper (2026-05-30 perf).
 *
 * The helper is small but lives on the critical path of the operator's
 * first paint after login — a bug here would either:
 *   - leak the previous operator's data (security), or
 *   - degrade silently to "no cache" without surfacing (perf rot).
 *
 * Pin both: cross-user isolation via the cacheKey contract, and the
 * TTL boundary at which a stale entry stops being served.
 */
import { describe, it, expect, beforeEach, vi } from "vitest";
import {
  readCachedJson,
  writeCachedJson,
  clearCachedJson,
} from "./cachedFetch";

describe("cachedFetch helper", () => {
  beforeEach(() => {
    localStorage.clear();
    vi.useRealTimers();
  });

  it("returns null when no cache exists", () => {
    expect(readCachedJson("nope", 60_000)).toBe(null);
  });

  it("writes then reads back the payload exactly", () => {
    const payload = { used: 25, limit: 250, plan: "250" };
    writeCachedJson("cache:usage:7", payload);
    expect(readCachedJson("cache:usage:7", 60_000)).toEqual(payload);
  });

  it("returns null when the cache is older than TTL", () => {
    vi.useFakeTimers();
    vi.setSystemTime(new Date("2026-05-30T12:00:00Z"));
    writeCachedJson("cache:usage:7", { used: 25 });

    // Advance 6 minutes — past the 5-minute TTL we use in UsageBadge.
    vi.setSystemTime(new Date("2026-05-30T12:06:00Z"));
    expect(readCachedJson("cache:usage:7", 5 * 60_000)).toBe(null);
  });

  it("returns the payload exactly at the TTL boundary (inclusive read)", () => {
    vi.useFakeTimers();
    vi.setSystemTime(new Date("2026-05-30T12:00:00Z"));
    writeCachedJson("cache:usage:7", { used: 25 });

    // Same millisecond — well within TTL.
    vi.setSystemTime(new Date("2026-05-30T12:00:30Z"));
    expect(readCachedJson("cache:usage:7", 60_000)).toEqual({ used: 25 });
  });

  it("isolates entries by cacheKey (cross-user safety)", () => {
    writeCachedJson("cache:usage:7", { used: 25, tenant: "umusic" });
    writeCachedJson("cache:usage:42", { used: 0, tenant: "tomas" });
    expect(readCachedJson("cache:usage:7", 60_000)).toEqual({ used: 25, tenant: "umusic" });
    expect(readCachedJson("cache:usage:42", 60_000)).toEqual({ used: 0, tenant: "tomas" });
  });

  it("clearCachedJson removes a single key without touching others", () => {
    writeCachedJson("cache:usage:7", { used: 25 });
    writeCachedJson("cache:other:7", { foo: "bar" });
    clearCachedJson("cache:usage:7");
    expect(readCachedJson("cache:usage:7", 60_000)).toBe(null);
    expect(readCachedJson("cache:other:7", 60_000)).toEqual({ foo: "bar" });
  });

  it("tolerates malformed cache entries (corrupt JSON returns null, no throw)", () => {
    localStorage.setItem("cache:usage:7", "{not json");
    expect(readCachedJson("cache:usage:7", 60_000)).toBe(null);
  });

  it("tolerates cache entries missing the timestamp shape", () => {
    localStorage.setItem("cache:usage:7", JSON.stringify({ payload: { used: 25 } }));
    expect(readCachedJson("cache:usage:7", 60_000)).toBe(null);
  });
});
