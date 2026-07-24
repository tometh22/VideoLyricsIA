/**
 * Contrato del mirror editor→currentReview (auditoría 2026-06-10, bug
 * "se mueve y no me deja hacer cambios").
 *
 * La invariante que rompe el loop de re-renders App↔LyricsEditor:
 * cuando los valores no cambiaron, mergeEditedSegments devuelve EL MISMO
 * objeto (identidad estable) — setCurrentReview no re-renderiza. El loop
 * original (arrow inline + effect con el callback en deps) llegó a
 * ~5000 ciclos/100ms en el harness de la auditoría.
 */
import { describe, expect, it } from "vitest";
import { mergeEditedSegments } from "./reviewSegments";

const SEGS = [
  { start: 1.0, end: 2.0, text: "alpha" },
  { start: 2.5, end: 4.0, text: "beta" },
];

describe("mergeEditedSegments", () => {
  it("valores iguales → devuelve el MISMO objeto (corta el loop)", () => {
    const review = { transcribeJobId: "j1", segments: SEGS };
    const sameValues = SEGS.map((s) => ({ ...s }));   // refs nuevas, valores idénticos
    expect(mergeEditedSegments(review, sameValues)).toBe(review);
  });

  it("edición real → objeto nuevo con los segments nuevos", () => {
    const review = { transcribeJobId: "j1", segments: SEGS };
    const moved = [{ ...SEGS[0], start: 1.2 }, SEGS[1]];
    const out = mergeEditedSegments(review, moved);
    expect(out).not.toBe(review);
    expect(out.segments).toBe(moved);
    expect(out.transcribeJobId).toBe("j1");
  });

  it("review null → passthrough (no crashea fuera de sesión de review)", () => {
    expect(mergeEditedSegments(null, SEGS)).toBe(null);
  });

  it("texto cambiado también cuenta como edición real", () => {
    const review = { segments: SEGS };
    const retyped = [{ ...SEGS[0], text: "alpha corregida" }, SEGS[1]];
    expect(mergeEditedSegments(review, retyped)).not.toBe(review);
  });
});
