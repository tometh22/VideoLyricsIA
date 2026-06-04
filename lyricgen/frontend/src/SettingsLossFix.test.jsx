import { describe, it, expect } from "vitest";

// Mirror of the App.jsx handleApproveLyrics fix (2026-06-04). When the operator's
// movement/effect/bg picks didn't sync into currentReview (e.g. chosen after the
// transcribe, or inherited via batchDefaults but never propagated), fall back to
// the matching FILE entry — which ALWAYS reflects the current batch picks
// (updateBatchDefault fans them to files[*]). Reproduces + guards the UMG bug:
// "Foto fija + Bokeh" chosen but the render got movement_style='' + effect=''.
function approvedFromReview(r, files) {
  const _fm = files.find((f) => f?.file?.name === r.file?.name) || {};
  const _rf = (k) => r[k] || _fm[k] || "";
  return {
    movementStyle: _rf("movementStyle"),
    effect: _rf("effect"),
    genre: _rf("genre"),
    backgroundHint: _rf("backgroundHint"),
  };
}

describe("settings-loss fix: approve falls back to the FILE entry (#bug-2026-06-04)", () => {
  it("currentReview vacío + file con picks → usa los del file (el bug exacto)", () => {
    const r = { file: { name: "nada.wav" }, movementStyle: "", effect: "" };
    const files = [{ file: { name: "nada.wav" }, movementStyle: "foto-parallax", effect: "bokeh" }];
    const a = approvedFromReview(r, files);
    expect(a.movementStyle).toBe("foto-parallax");
    expect(a.effect).toBe("bokeh");
  });

  it("currentReview con picks → gana sobre el file (última edición del operador)", () => {
    const r = { file: { name: "x.wav" }, movementStyle: "estatico", effect: "snow" };
    const files = [{ file: { name: "x.wav" }, movementStyle: "foto-parallax", effect: "bokeh" }];
    const a = approvedFromReview(r, files);
    expect(a.movementStyle).toBe("estatico");
    expect(a.effect).toBe("snow");
  });

  it("persiste para TODOS los efectos y TODOS los movement styles", () => {
    const combos = [
      ["estatico", "rain"], ["foto-parallax", "stars"], ["sutil", "light"],
      ["animado", "aurora"], ["foto-parallax", "snow"], ["estandar", "bokeh"],
    ];
    for (const [mv, fx] of combos) {
      const r = { file: { name: "s.wav" }, movementStyle: "", effect: "" };
      const files = [{ file: { name: "s.wav" }, movementStyle: mv, effect: fx }];
      const a = approvedFromReview(r, files);
      expect(a.movementStyle, `movement ${mv}`).toBe(mv);
      expect(a.effect, `effect ${fx}`).toBe(fx);
    }
  });

  it("ningún efecto (Ninguno) se respeta — no inventa uno", () => {
    const r = { file: { name: "n.wav" }, movementStyle: "foto-parallax", effect: "" };
    const files = [{ file: { name: "n.wav" }, movementStyle: "foto-parallax", effect: "" }];
    expect(approvedFromReview(r, files).effect).toBe("");
  });

  it("sin match de file → no rompe (cae a '')", () => {
    const r = { file: { name: "a.wav" }, movementStyle: "", effect: "" };
    const files = [{ file: { name: "OTRO.wav" }, movementStyle: "foto-parallax", effect: "bokeh" }];
    const a = approvedFromReview(r, files);
    expect(a.movementStyle).toBe("");
    expect(a.effect).toBe("");
  });
});
