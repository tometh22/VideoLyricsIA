import { describe, expect, it } from "vitest";
import { reanchorFailureMessage, reanchorHttpFailure, shouldRecoverReanchor } from "./reanchorResult";

const t = () => "";

describe("reanchor failure contract", () => {
  it("preserves nested FastAPI error codes for completed tasks and direct responses", () => {
    for (const terminal of [false, true]) {
      const result = reanchorHttpFailure(409, {
        detail: { code: "editor_state_conflict", detail: "stale editor document" },
      }, { terminal });
      expect(result.code).toBe("editor_state_conflict");
      expect(shouldRecoverReanchor(result)).toBe(false);
      expect(reanchorFailureMessage(result, t)).toContain("La versión guardada cambió");
    }
  });

  it("preserves structure confirmation in both server error formats", () => {
    const error = { code: "reference_structure_unconfirmed", structure: { supported: false } };
    expect(reanchorHttpFailure(409, error).structure).toEqual(error.structure);
    expect(reanchorHttpFailure(409, { detail: error }).structure).toEqual(error.structure);
  });

  it("recovers only uncertain outcomes, not a completed failure or decline", () => {
    expect(shouldRecoverReanchor({ reason: "network" })).toBe(true);
    expect(shouldRecoverReanchor({ reason: "task-lost" })).toBe(true);
    expect(shouldRecoverReanchor(reanchorHttpFailure(502, null))).toBe(true);
    expect(shouldRecoverReanchor(reanchorHttpFailure(500, null, { terminal: true }))).toBe(false);
    expect(shouldRecoverReanchor({ reason: "declined" })).toBe(false);
  });

  it("explains an unavailable original audio without claiming it was a text mismatch", () => {
    const detail = "El audio original ya no está disponible para este job.";
    expect(reanchorFailureMessage(reanchorHttpFailure(409, { detail }), t)).toBe(detail);
  });

  it("does not assert unchanged server timings after losing the response", () => {
    expect(reanchorFailureMessage({ reason: "network" }, t)).toContain("No se pudo confirmar");
    expect(reanchorFailureMessage({ reason: "declined" }, t)).toContain("alineación utilizable");
  });
});
