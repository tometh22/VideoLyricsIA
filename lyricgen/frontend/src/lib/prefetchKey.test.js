/**
 * Regression test for the background-transcription cache aliasing bug
 * (incident 2026-06-01): the cache was keyed by the array INDEX into
 * `files`, so removing the previous song's audio and uploading a new one
 * into the freed index made the new audio inherit the previous song's
 * cached transcription — the operator saw the PREVIOUS song's lyrics over
 * the NEW audio.
 *
 * The fix keys the cache by FILE IDENTITY (prefetchKey). These tests pin
 * the invariant that makes aliasing impossible: two different files MUST
 * get different keys, and the same file MUST get the same key.
 */
import { describe, expect, it } from "vitest";
import { prefetchKey } from "./prefetchKey";

const fileA = { name: "Luz de Dia.mp3", size: 4_200_000, lastModified: 111 };
const fileB = { name: "Donde Estan Corazon.mp3", size: 6_800_000, lastModified: 222 };

describe("prefetchKey — cache aliasing prevention", () => {
  it("two different audios get DIFFERENT keys (the core invariant)", () => {
    expect(prefetchKey(fileA)).not.toBe(prefetchKey(fileB));
  });

  it("the same audio re-picked gets the SAME key (cache hit stays valid)", () => {
    const sameAgain = { name: "Luz de Dia.mp3", size: 4_200_000, lastModified: 111 };
    expect(prefetchKey(fileA)).toBe(prefetchKey(sameAgain));
  });

  it("differs when ANY of name / size / lastModified differs", () => {
    expect(prefetchKey(fileA)).not.toBe(prefetchKey({ ...fileA, name: "x.mp3" }));
    expect(prefetchKey(fileA)).not.toBe(prefetchKey({ ...fileA, size: 1 }));
    expect(prefetchKey(fileA)).not.toBe(prefetchKey({ ...fileA, lastModified: 999 }));
  });

  it("a missing file yields a stable empty key (degenerate, nothing to transcribe)", () => {
    expect(prefetchKey(null)).toBe("");
    expect(prefetchKey(undefined)).toBe("");
  });

  it("REPRO: remove A, upload B at the same index — B must NOT hit A's cache slot", () => {
    // Model the prefetchCache as the keyed object App.jsx uses.
    const cache = {};
    // Audio A finishes its background transcription.
    cache[prefetchKey(fileA)] = { status: "ready", data: { segments: ["A-1", "A-2"] }, jobId: "job-A" };

    // Operator clicks X (removes A) and uploads B. With index keying, B
    // would land on index 0 and read A's entry. With file-identity keying:
    const bEntry = cache[prefetchKey(fileB)];
    expect(bEntry).toBeUndefined(); // cache MISS → B gets transcribed fresh

    // And A's slot is still A's (no clobber), so A stays correct.
    expect(cache[prefetchKey(fileA)].jobId).toBe("job-A");
  });
});
