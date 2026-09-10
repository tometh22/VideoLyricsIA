import { describe, expect, it } from "vitest";
import { reviewCounts, reviewStateLabel, reviewActionLabel, reviewDestination, safeReviewReturnPath, reviewStateFilter } from "./reviewerNavigation";

describe("reviewer navigation contract", () => {
  it("keeps campaign counts independent of the active scope", () => {
    expect(reviewCounts({ scope: { total: 40 }, campaign_totals: { songs: 300, approved: 40 } }))
      .toEqual({ all: 300, approved: 40, pending: 260, discarded: 0 });
    expect(reviewCounts(null).approved).toBeUndefined();
  });
  it.each([
    ["pending", "Pendiente de procesamiento", null], ["processing", "Procesando", null],
    ["ready", "Sin revisar", "Revisar"], ["reviewing", "En revisión", "Revisar"],
    ["approved", "Aprobada", "Ver canción"], ["exported", "Exportada", "Ver canción"],
    ["failed", "Fallida", null], ["unknown", "Estado no disponible", null],
  ])("renders state %s honestly", (state, label, action) => {
    const row = { job_id: "one", state };
    expect(reviewStateLabel(row)).toBe(label);
    expect(reviewActionLabel(row)).toBe(action);
  });
  it("respects foreign locks but never labels approvals as in-progress", () => {
    const row = { job_id: "one", state: "reviewing", reviewer_lock_active: true, reviewer_name: "Agus" };
    expect(reviewStateLabel(row)).toBe("En revisión por Agus");
    expect(reviewActionLabel(row)).toBeNull();
    expect(reviewActionLabel({ ...row, reviewer_is_current_user: true })).toBe("Continuar");
    expect(reviewStateLabel({ ...row, state: "approved" })).toBe("Aprobada");
    expect(reviewActionLabel({ ...row, state: "approved" })).toBe("Ver canción");
  });
  it("does not assign the previous reviewer to an unreviewed row", () => {
    expect(reviewStateLabel({ state: "ready", reviewer_name: "Agus" })).toBe("Sin revisar");
  });
  it("opens approvals as details and preserves the return filter", () => {
    expect(reviewDestination({ job_id: "a/b", state: "approved" }, "/admin/cola?scope=approved"))
      .toBe("/videos/a%2Fb?return_to=%2Fadmin%2Fcola%3Fscope%3Dapproved");
  });
  it("rejects status filters that would silently override an approvals tab", () => {
    expect(reviewStateFilter("ready", "approved")).toBe("");
    expect(reviewStateFilter("approved", "pending")).toBe("");
    expect(reviewStateFilter("failed", "all")).toBe("failed");
  });
  it.each(["/admin/cola?scope=approved", "/campaigns/test?tab=approved&q=hola"])("allows reviewer return %s", (path) => {
    expect(safeReviewReturnPath(path)).toBe(path);
  });
  it.each(["https://evil.test", "//evil.test", "/\\evil.test", "/admin/cola-evil", "/admin/settings", "javascript:alert(1)"])("rejects unrelated return %s", (path) => {
    expect(safeReviewReturnPath(path)).toBeNull();
  });
});
