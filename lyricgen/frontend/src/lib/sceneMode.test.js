import { describe, expect, it } from "vitest";
import { inspiredByLyricsForSceneMode } from "./sceneMode";

function transitionToMode(previousInspiredByLyrics, mode) {
  // The previous value is intentionally not an input to the production
  // mapping. Keeping it here makes the regression scenario explicit.
  void previousInspiredByLyrics;
  return inspiredByLyricsForSceneMode(mode);
}

function legacyPayloadForTransition(previousInspiredByLyrics, mode) {
  const body = new FormData();
  body.append(
    "match_lyrics",
    String(transitionToMode(previousInspiredByLyrics, mode)),
  );
  return body;
}

describe("scene mode -> legacy match_lyrics", () => {
  it("Lyrics -> Prompt clears the lyrics flag", () => {
    expect(inspiredByLyricsForSceneMode("lyrics")).toBe(true);
    expect(transitionToMode(true, "prompt")).toBe(false);
    expect(legacyPayloadForTransition(true, "prompt").get("match_lyrics")).toBe("false");
  });

  it("Auto -> Prompt keeps the same deterministic prompt payload", () => {
    const fromAuto = legacyPayloadForTransition(false, "prompt").get("match_lyrics");
    const fromLyrics = legacyPayloadForTransition(true, "prompt").get("match_lyrics");

    expect(inspiredByLyricsForSceneMode("auto")).toBe(false);
    expect(fromAuto).toBe("false");
    expect(fromLyrics).toBe(fromAuto);
  });
});
