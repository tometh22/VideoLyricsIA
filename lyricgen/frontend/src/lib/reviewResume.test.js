import { describe, expect, it } from "vitest";

import { readFileSync } from "node:fs";
import { resolve } from "node:path";

import {
  creativeFieldsForReviewResume, mergeWizardPicksIntoResumedReview, WIZARD_PICK_FIELDS,
} from "./reviewResume";
import { LOOK_VARIANT_FIELDS } from "./lyricLookVariants";


describe("creativeFieldsForReviewResume", () => {
  it("restores a persisted operator prompt longer than the old 2000-char cap", () => {
    const prompt = "movimiento de bandera y nubes ".repeat(80);
    expect(prompt.length).toBeGreaterThan(2000);

    const restored = creativeFieldsForReviewResume({
      render_params: {
        background_hint: prompt,
        bg_verbatim: true,
        movement_style: "estatico",
        genre: "rock",
      },
    });

    expect(restored.backgroundHint).toBe(prompt);
    expect(restored.bgVerbatim).toBe(true);
    expect(restored.movementStyle).toBe("estatico");
    expect(restored.genre).toBe("rock");
    expect(restored.animateImage).toBe(false);
  });

  it("restores the image-to-video choice for a backend-prepared job", () => {
    expect(creativeFieldsForReviewResume({
      render_params: { animate_image: true },
    }).animateImage).toBe(true);
  });

  it("keeps legacy top-level creative fields when render_params is absent", () => {
    expect(creativeFieldsForReviewResume({
      genre: "rock",
      concept: "plaza vacía",
      movement_style: "sutil",
      effect: "grain",
      background_hint: "bandera suave",
      bg_verbatim: true,
    })).toEqual({
      genre: "rock",
      concept: "plaza vacía",
      movementStyle: "sutil",
      effect: "grain",
      backgroundHint: "bandera suave",
      bgVerbatim: true,
      animateImage: false,
    });
  });
});

// Incidente 11-nov-2026: "Revisar letra" → la review recibe /review/<id> →
// el resume re-leía /status y pisaba el look elegido con el render_params
// vacío del job (todavía sin /generate) → la letra salía blanca y plana.
describe("mergeWizardPicksIntoResumedReview", () => {
  const audio = new Blob(["a"], { type: "audio/wav" });
  const wizardReview = {
    file: audio,
    transcribeJobId: "job123",
    lyricLook: "perspectiva", lyricGenre: "", lyricVariant: 0,
    lookResolved: { look: "perspectiva", variant: 0 },
    font: "anton", textCase: "title", fontScale: "1.1", textContrast: "strong",
    lyricColor: "#FFFFFF", movementStyle: "estatico", effect: "bokeh",
    backgroundHint: "rosa de fuego", bgVerbatim: true,
    titleTemplate: "centered", titleSize: "1.2",
    queueIdx: 1, queue: [{ file: audio }, { file: audio }],
    segments: [{ start: 0, end: 1, text: "local" }],
  };
  const serverReview = {
    file: null,
    transcribeJobId: "job123",
    lyricLook: "", lyricVariant: "", font: "", textCase: "upper", fontScale: "1.0",
    movementStyle: "", effect: "", backgroundHint: "", titleTemplate: "auto",
    segments: [{ start: 0, end: 1, text: "server" }],
    needsLanguageReview: true,
    outputReferenceDivergence: true,
    queueIdx: 0, queue: [{ filename: "a.wav" }],
  };

  it("keeps the operator's look, typography, background and title picks", () => {
    const merged = mergeWizardPicksIntoResumedReview(serverReview, wizardReview);
    expect(merged).toMatchObject({
      lyricLook: "perspectiva", lyricVariant: 0,
      lookResolved: { look: "perspectiva", variant: 0 },
      font: "anton", textCase: "title", fontScale: "1.1", textContrast: "strong",
      movementStyle: "estatico", effect: "bokeh",
      backgroundHint: "rosa de fuego", bgVerbatim: true,
      titleTemplate: "centered", titleSize: "1.2",
    });
    // The operator's audio + the batch position survive too.
    expect(merged.file).toBe(audio);
    expect(merged.queueIdx).toBe(1);
    expect(merged.queue).toBe(wizardReview.queue);
  });

  it("still lets the server own the transcription and the review gates", () => {
    const merged = mergeWizardPicksIntoResumedReview(serverReview, wizardReview);
    expect(merged.segments).toEqual(serverReview.segments);
    expect(merged.needsLanguageReview).toBe(true);
    expect(merged.outputReferenceDivergence).toBe(true);
  });

  it("is a no-op for a plain resume (deep link, history, campaign)", () => {
    expect(mergeWizardPicksIntoResumedReview(serverReview, null)).toBe(serverReview);
  });

  it("covers every look-variant field the submit sends", () => {
    for (const [camel] of LOOK_VARIANT_FIELDS) expect(WIZARD_PICK_FIELDS).toContain(camel);
    expect(WIZARD_PICK_FIELDS).toContain("lyricLook");
  });

  it("is wired into App.jsx's /review/<id> resume", () => {
    const src = readFileSync(resolve(process.cwd(), "src/App.jsx"), "utf8");
    expect(src).toMatch(/reviewAlreadyOpen\(creativeReviewRef\.current, resumeJobId\)/);
    expect(src).toMatch(/setCurrentReview\(\(cur\) => mergeWizardPicksIntoResumedReview\(/);
    expect(src).toMatch(/wizardOwnReview && reviewAlreadyOpen\(cur, resumeJobId\) \? cur : null/);
  });
});
