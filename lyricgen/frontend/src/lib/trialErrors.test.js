import { describe, expect, it } from "vitest";
import { translateBackendError } from "./lyricsEditSubmit";

describe("structured trial errors", () => {
  it.each([
    ["trial_not_started", "El trial todavía no fue activado."],
    ["trial_expired", "Finalizaron las 24 horas del trial."],
    ["trial_feature_unavailable", "Esta función no está disponible en el trial."],
    ["trial_credits_exhausted", "No quedan créditos para nuevos videos. Los reservados pueden terminarse."],
    ["trial_retry_limit", "Este video alcanzó el límite de reintentos del trial."],
    ["scenes_incomplete", "Regenerá las escenas fallidas antes de aprobar."],
  ])("displays %s as the server's human message without dumping policy data", (code, message) => {
    expect(translateBackendError({ code, message, trial: { state: "pending", grant_id: 2 } })).toBe(message);
  });

  it("preserves unknown errors and nested structured details", () => {
    expect(translateBackendError({ detail: { code: "unknown", message: "Proveedor no disponible" } })).toBe("Proveedor no disponible");
    expect(translateBackendError("Storage unavailable")).toBe("Storage unavailable");
  });
});
