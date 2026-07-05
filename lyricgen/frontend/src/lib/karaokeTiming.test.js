import { describe, it, expect } from "vitest";
import { activeWordIndex } from "./karaokeTiming";

// Caso real (Amanda Pujó, 05/07): con lead-in la línea aparece en 109.8
// pero el canto arranca en 110.22 — el reparto uniforme iluminaba la
// primera palabra 0.4s antes de tiempo ("karaoke a destiempo").
const SEG = {
  text: "Frágil espejo de vos",
  start: 109.8,
  end: 113.3,
  words: [
    { word: "frágil", start: 110.22, end: 110.7 },
    { word: "espejo", start: 110.74, end: 111.3 },
    { word: "de", start: 111.34, end: 111.5 },
    { word: "vos", start: 111.54, end: 116.62 }, // nota sostenida
  ],
};

describe("activeWordIndex con word-stamps reales", () => {
  it("durante el lead-in (línea visible, nadie cantó) no ilumina nada", () => {
    expect(activeWordIndex(SEG.text, SEG.words, SEG.start, SEG.end, 109.9)).toBe(-1);
  });

  it("ilumina cada palabra en SU start real, no en el reparto uniforme", () => {
    expect(activeWordIndex(SEG.text, SEG.words, SEG.start, SEG.end, 110.3)).toBe(0);
    expect(activeWordIndex(SEG.text, SEG.words, SEG.start, SEG.end, 111.0)).toBe(1);
    // nota sostenida: "vos" sigue activa mucho después del end de línea
    expect(activeWordIndex(SEG.text, SEG.words, SEG.start, SEG.end, 113.0)).toBe(3);
  });
});

describe("fallback uniforme (sin words o words viejos)", () => {
  it("sin words reparte la ventana en partes iguales (comportamiento histórico)", () => {
    // 4 palabras en 109.8-113.3 → 0.875s c/u
    expect(activeWordIndex(SEG.text, null, SEG.start, SEG.end, 109.9)).toBe(0);
    expect(activeWordIndex(SEG.text, null, SEG.start, SEG.end, 112.0)).toBe(2);
  });

  it("words viejos (cantidad distinta tras editar el texto) → fallback", () => {
    // el operador partió la línea: el texto quedó en 2 palabras pero el
    // array words sigue siendo el de las 4 originales → no usable
    expect(activeWordIndex("de vos", SEG.words, 111.3, 113.3, 111.4)).toBe(0);
    // ventana 2s, 2 palabras → 1s c/u; a los 1.5s va la segunda
    expect(activeWordIndex("de vos", SEG.words, 111.3, 113.3, 112.9)).toBe(1);
  });

  it("texto vacío → -1", () => {
    expect(activeWordIndex("", null, 0, 1, 0.5)).toBe(-1);
  });
});
