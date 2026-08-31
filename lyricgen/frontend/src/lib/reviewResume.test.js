import { describe, expect, it } from "vitest";

import { creativeFieldsForReviewResume } from "./reviewResume";


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
    });
  });
});
