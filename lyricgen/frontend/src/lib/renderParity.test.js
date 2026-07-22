/**
 * JS↔Python render-parity gate.
 *
 * shared/renderParity.json is GENERATED from ass_render.py (the render is
 * the source of truth) by backend/scripts/gen_render_parity_fixture.py.
 * This suite evaluates the REAL frontend functions over that matrix — not
 * a hand-copied formula like the retired FontScalePicker mirror, which
 * passed while the actual preview drifted 1.7× from the render.
 *
 * If this fails: either the JS mirror diverged (fix lyricTiers.js) or the
 * backend changed and the fixture is stale (backend test says so too —
 * regenerate with `python3 scripts/gen_render_parity_fixture.py`).
 */
import { describe, expect, it } from "vitest";
import fixture from "../shared/renderParity.json";
import {
  FADE_SECONDS,
  FONT_SCALE_MAX,
  FONT_SCALE_MIN,
  FONT_SIZE_NORM,
  LYRIC_TIERS,
  fadeSeconds,
  lyricFontPx,
} from "./lyricTiers";

describe("render parity: font sizes", () => {
  it("lyricFontPx matches ass_render.lyric_fontsize across the whole matrix", () => {
    const failures = [];
    for (const c of fixture.font_sizes) {
      const family = c.family === "unknown" ? "" : c.family;
      const got = lyricFontPx(c.text_len, c.font_scale, family);
      if (got !== c.px) {
        failures.push(`len=${c.text_len} fs=${c.font_scale} ${c.family}: js=${got} py=${c.px}`);
      }
    }
    expect(failures, failures.join("\n")).toEqual([]);
  });

  it("clamp bounds match", () => {
    expect(FONT_SCALE_MIN).toBe(fixture.font_scale_clamp.min);
    expect(FONT_SCALE_MAX).toBe(fixture.font_scale_clamp.max);
  });

  it("tier table matches", () => {
    const jsTiers = LYRIC_TIERS.map((t) => ({
      max_chars: Number.isFinite(t.maxChars) ? t.maxChars : null,
      base_px: t.fontPx,
    }));
    expect(jsTiers).toEqual(fixture.tiers);
  });

  it("per-font normalization table matches", () => {
    expect(FONT_SIZE_NORM).toEqual(fixture.font_size_norm);
  });
});

describe("render parity: fades", () => {
  it("FADE_SECONDS matches _FADE_DURATIONS_S (cut → 0)", () => {
    for (const [name, secs] of Object.entries(fixture.fade_durations_s)) {
      expect(FADE_SECONDS[name], name).toBe(secs);
    }
    expect(FADE_SECONDS.cut).toBe(0);
  });

  it("fadeSeconds matches ass_render.fade_seconds incl. the dur/3 cap", () => {
    for (const c of fixture.fades) {
      expect(fadeSeconds(c.transition, c.seg_duration), `${c.transition}@${c.seg_duration}`)
        .toBeCloseTo(c.seconds, 9);
    }
  });
});
