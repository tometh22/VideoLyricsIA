// Unit tests for computeFieldDiff + buildEditPayloads. These cover the
// edit-wizard's "compute the minimum POST set" logic so a refactor of the
// wizard's field map can't silently change which buckets get fired.
//
// Tests are pure (no DOM, no fetch mocks) — the helpers under test are
// pure too.

import { describe, expect, it } from "vitest";
import {
  computeFieldDiff,
  buildEditPayloads,
  bundleTypographyIntoFirstBucket,
  backgroundRegenExtras,
} from "./editWizardDiff";

const baselineFixture = () => ({
  artist: "Cerati",
  songTitle: "Crimen",
  font: "jost",
  fontScale: "1.0",
  textCase: "upper",
  textContrast: "medium",
  lyricsAnimation: "none",
  lineTransition: "none",
  effect: "",
  backgroundHint: "",
  bgVerbatim: false,
  backgroundMode: "",
  movementStyle: "",
  segments: [
    { start: 0, end: 2, text: "Línea uno" },
    { start: 2, end: 4, text: "Línea dos" },
  ],
});

describe("computeFieldDiff", () => {
  it("returns {} when nothing changed", () => {
    const base = baselineFixture();
    const cur = { ...base, segments: JSON.parse(JSON.stringify(base.segments)) };
    expect(computeFieldDiff(base, cur)).toEqual({});
  });

  it("metadata bucket: artist only", () => {
    const base = baselineFixture();
    const cur = { ...base, artist: "Soda Stereo" };
    const diff = computeFieldDiff(base, cur);
    expect(diff).toEqual({ metadata: { artist: "Soda Stereo" } });
  });

  it("metadata bucket: song_title only (trimmed)", () => {
    const base = baselineFixture();
    const cur = { ...base, songTitle: "  Crímen  " };
    const diff = computeFieldDiff(base, cur);
    // Trim happens — the wire shape never has leading/trailing whitespace.
    expect(diff).toEqual({ metadata: { song_title: "Crímen" } });
  });

  it("metadata bucket: both fields changed", () => {
    const base = baselineFixture();
    const cur = { ...base, artist: "Spinetta", songTitle: "Cementerio Club" };
    expect(computeFieldDiff(base, cur)).toEqual({
      metadata: { artist: "Spinetta", song_title: "Cementerio Club" },
    });
  });

  it("typography bucket: font_scale travels as number, not string", () => {
    const base = baselineFixture();
    const cur = { ...base, fontScale: "1.5" };
    const diff = computeFieldDiff(base, cur);
    expect(diff).toEqual({ typography: { font_scale: 1.5 } });
  });

  it("typography bucket: 1.0 vs 1 does NOT trigger a diff (numeric equality)", () => {
    const base = { ...baselineFixture(), fontScale: 1 };
    const cur = { ...baselineFixture(), fontScale: "1.0" };
    expect(computeFieldDiff(base, cur)).toEqual({});
  });

  it("typography bucket: multiple fields", () => {
    const base = baselineFixture();
    const cur = {
      ...base,
      font: "lobster",
      textCase: "title",
      lyricsAnimation: "karaoke",
    };
    expect(computeFieldDiff(base, cur)).toEqual({
      typography: {
        font: "lobster",
        text_case: "title",
        lyrics_animation: "karaoke",
      },
    });
  });

  it("lyrics bucket: text change", () => {
    const base = baselineFixture();
    const cur = {
      ...base,
      segments: [
        { start: 0, end: 2, text: "Línea uno corregida" },
        { start: 2, end: 4, text: "Línea dos" },
      ],
    };
    const diff = computeFieldDiff(base, cur);
    expect(diff.lyrics).toBeDefined();
    expect(diff.lyrics.segments).toHaveLength(2);
    expect(diff.lyrics.segments[0].text).toBe("Línea uno corregida");
  });

  it("lyrics bucket: timing change (drag-resize)", () => {
    const base = baselineFixture();
    const cur = {
      ...base,
      segments: [
        { start: 0, end: 2.5, text: "Línea uno" }, // end moved 2 → 2.5
        { start: 2.5, end: 4, text: "Línea dos" },
      ],
    };
    const diff = computeFieldDiff(base, cur);
    expect(diff.lyrics).toBeDefined();
    expect(diff.lyrics.segments[0].end).toBe(2.5);
  });

  it("lyrics bucket: ignores unchanged segments", () => {
    const base = baselineFixture();
    const cur = {
      ...base,
      segments: JSON.parse(JSON.stringify(base.segments)),
    };
    expect(computeFieldDiff(base, cur).lyrics).toBeUndefined();
  });

  it("background bucket: hint trimmed", () => {
    const base = baselineFixture();
    const cur = { ...base, backgroundHint: "  paisaje urbano nocturno  " };
    expect(computeFieldDiff(base, cur)).toEqual({
      background: { background_hint: "paisaje urbano nocturno" },
    });
  });

  it("background bucket: movement_style change", () => {
    const base = baselineFixture();
    const cur = { ...base, movementStyle: "static" };
    expect(computeFieldDiff(base, cur)).toEqual({
      background: { movement_style: "static" },
    });
  });

  it("background bucket: bg_verbatim toggle", () => {
    const base = baselineFixture();
    const cur = { ...base, bgVerbatim: true };
    expect(computeFieldDiff(base, cur)).toEqual({
      background: { bg_verbatim: true },
    });
  });

  it("background bucket: only sends background_mode when non-empty", () => {
    const base = baselineFixture();
    const cur1 = { ...base, backgroundMode: "" };
    expect(computeFieldDiff(base, cur1).background).toBeUndefined();
    const cur2 = { ...base, backgroundMode: "imagen" };
    expect(computeFieldDiff(base, cur2)).toEqual({
      background: { background_mode: "imagen" },
    });
  });

  it("multiple buckets: metadata + typography + lyrics + background all changed", () => {
    const base = baselineFixture();
    const cur = {
      ...base,
      artist: "Charly García",
      font: "lobster",
      segments: [
        { start: 0, end: 2, text: "Letra corregida" },
        { start: 2, end: 4, text: "Línea dos" },
      ],
      backgroundHint: "paisaje urbano",
    };
    const diff = computeFieldDiff(base, cur);
    expect(Object.keys(diff).sort()).toEqual([
      "background",
      "lyrics",
      "metadata",
      "typography",
    ]);
  });

  it("returns {} when baseline or current is null/undefined", () => {
    expect(computeFieldDiff(null, baselineFixture())).toEqual({});
    expect(computeFieldDiff(baselineFixture(), null)).toEqual({});
    expect(computeFieldDiff(undefined, undefined)).toEqual({});
  });

  it("empty-string vs undefined: treated as equal (no spurious diff)", () => {
    const base = { ...baselineFixture(), effect: undefined, movementStyle: undefined };
    const cur = { ...baselineFixture(), effect: "", movementStyle: "" };
    expect(computeFieldDiff(base, cur)).toEqual({});
  });
});

describe("buildEditPayloads", () => {
  it("with no typography to bundle, emits stable order: metadata → lyrics → background", () => {
    const diff = {
      background: { background_hint: "bg" },
      lyrics: { segments: [{ start: 0, end: 1, text: "x" }] },
      metadata: { artist: "y" },
    };
    const payloads = buildEditPayloads(diff);
    expect(payloads.map((p) => p.edit_type)).toEqual([
      "metadata",
      "lyrics",
      "background",
    ]);
  });

  it("bundles typography fields into the first non-typography bucket (metadata wins)", () => {
    const diff = {
      metadata: { artist: "Soda" },
      typography: { font: "lobster", font_scale: 1.5 },
      lyrics: { segments: [{ start: 0, end: 1, text: "x" }] },
    };
    const payloads = buildEditPayloads(diff);
    // typography slot disappears, its fields land on metadata payload
    expect(payloads.map((p) => p.edit_type)).toEqual(["metadata", "lyrics"]);
    expect(payloads[0]).toEqual({
      edit_type: "metadata",
      artist: "Soda",
      font: "lobster",
      font_scale: 1.5,
    });
  });

  it("bundles typography into lyrics when metadata is absent", () => {
    const diff = {
      typography: { font: "lobster" },
      lyrics: { segments: [{ start: 0, end: 1, text: "x" }] },
    };
    const payloads = buildEditPayloads(diff);
    expect(payloads.map((p) => p.edit_type)).toEqual(["lyrics"]);
    expect(payloads[0]).toEqual({
      edit_type: "lyrics",
      segments: [{ start: 0, end: 1, text: "x" }],
      font: "lobster",
    });
  });

  it("bundles typography into background when no metadata/lyrics", () => {
    const diff = {
      typography: { font: "lobster" },
      background: { background_hint: "bg" },
    };
    const payloads = buildEditPayloads(diff);
    expect(payloads.map((p) => p.edit_type)).toEqual(["background"]);
    expect(payloads[0]).toEqual({
      edit_type: "background",
      background_hint: "bg",
      font: "lobster",
    });
  });

  it("typography stays standalone when there's no other bucket to bundle into", () => {
    const diff = {
      typography: { font: "lobster", font_scale: 1.2 },
    };
    const payloads = buildEditPayloads(diff);
    expect(payloads.map((p) => p.edit_type)).toEqual(["typography"]);
    expect(payloads[0]).toEqual({
      edit_type: "typography",
      font: "lobster",
      font_scale: 1.2,
    });
  });

  it("each payload has edit_type set + bucket fields flattened", () => {
    const diff = {
      metadata: { artist: "Soda", song_title: "Crimen" },
    };
    expect(buildEditPayloads(diff)).toEqual([
      { edit_type: "metadata", artist: "Soda", song_title: "Crimen" },
    ]);
  });

  it("returns [] for empty diff", () => {
    expect(buildEditPayloads({})).toEqual([]);
  });

  it("omits buckets that aren't present", () => {
    const diff = {
      lyrics: { segments: [{ start: 0, end: 1, text: "x" }] },
    };
    const payloads = buildEditPayloads(diff);
    expect(payloads).toHaveLength(1);
    expect(payloads[0].edit_type).toBe("lyrics");
  });
});

// ── background_library (PR #940 backend) ─────────────────────────────────
describe("computeFieldDiff — background_library", () => {
  it("un pick de biblioteca produce el bucket con background_id", () => {
    const base = baselineFixture();
    const cur = { ...base, editBackgroundId: 42 };
    const diff = computeFieldDiff(base, cur);
    expect(diff.background_library).toEqual({ background_id: 42 });
  });

  it("es mutuamente excluyente con background: el pick supersede el hint", () => {
    const base = baselineFixture();
    const cur = {
      ...base,
      editBackgroundId: 7,
      backgroundHint: "montaña al amanecer",
    };
    const diff = computeFieldDiff(base, cur);
    expect(diff.background_library).toEqual({ background_id: 7 });
    expect(diff.background).toBeUndefined();
  });

  it("sin pick (null/undefined) no hay bucket — mantener fondo actual", () => {
    const base = baselineFixture();
    expect(computeFieldDiff(base, { ...base, editBackgroundId: null }))
      .toEqual({});
    expect(computeFieldDiff(base, { ...base })).toEqual({});
  });

  it("solo hint sin pick sigue produciendo el bucket background normal", () => {
    const base = baselineFixture();
    const cur = { ...base, backgroundHint: "bosque nevado", editBackgroundId: null };
    const diff = computeFieldDiff(base, cur);
    expect(diff.background).toEqual({ background_hint: "bosque nevado" });
    expect(diff.background_library).toBeUndefined();
  });
});

describe("forceBackgroundRegen (re-roll del fondo sin cambiar texto)", () => {
  it("fuerza un bucket background vacío cuando no cambió ningún campo", () => {
    const base = baselineFixture();
    const current = { ...base, forceBackgroundRegen: true };
    const diff = computeFieldDiff(base, current);
    expect(diff.background).toEqual({});
    const payloads = buildEditPayloads(diff);
    expect(payloads).toContainEqual({ edit_type: "background" });
  });

  it("NO aplica si el operador eligió un asset de biblioteca (eso supersede)", () => {
    const base = baselineFixture();
    const current = { ...base, forceBackgroundRegen: true, editBackgroundId: 42 };
    const diff = computeFieldDiff(base, current);
    expect(diff.background).toBeUndefined();
    expect(diff.background_library).toEqual({ background_id: 42 });
  });

  it("sin la intención, un job sin cambios NO produce bucket background", () => {
    const base = baselineFixture();
    const diff = computeFieldDiff(base, { ...base });
    expect(diff.background).toBeUndefined();
  });
});

describe("backgroundRegenExtras — paridad tarjeta 'Regenerar fondo' (#973)", () => {
  it("SIEMPRE manda un flag de validación: default (sin elección) = force", () => {
    // Regresión clave del review adversarial: si no se manda ningún flag, el
    // backend fail-closea a force igual, pero mandarlo explícito hace el
    // contrato inequívoco y matchea la tarjeta removida.
    expect(backgroundRegenExtras({})).toEqual({ force_content_validation: true });
    expect(backgroundRegenExtras(null)).toEqual({ force_content_validation: true });
    expect(backgroundRegenExtras({ bgRegenValidation: true })).toEqual({
      force_content_validation: true,
    });
  });

  it("fondo-libre: bgRegenValidation=false → bypass_content_validation (no force)", () => {
    const out = backgroundRegenExtras({ bgRegenValidation: false });
    expect(out.bypass_content_validation).toBe(true);
    expect(out.force_content_validation).toBeUndefined();
  });

  it("NO decide el motor (Veo/Imagen lo define movement_style, no este helper)", () => {
    // El eje motor se sacó del payload de extras (rediseño 2026-07-24): el
    // estilo de Movimiento ("foto-parallax"→Imagen) ya lo cubre vía el bucket
    // background del diff. Nunca debe aparecer background_mode acá.
    expect(backgroundRegenExtras({ bgRegenEngine: "imagen" }).background_mode).toBeUndefined();
    expect(backgroundRegenExtras({}).background_mode).toBeUndefined();
  });
});
