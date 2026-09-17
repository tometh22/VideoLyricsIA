import { expect, test } from "@playwright/test";
import { installEditorHarness } from "./editor-harness.js";

for (const editorV2 of [false, true]) {
test(`approves a saved UMG correction from a legacy request-only link after reload (v2=${editorV2})`, async ({ page }) => {
  const jobId = "umg-legacy-approval";
  const corrected = [{ _id: "line-1", start: 0.4, end: 3.9, text: "Respirarse, emborrachar, morir y seguir viviendo" }];
  const harness = await installEditorHarness(page, {
    jobId, segments: corrected, role: "admin", preserveStorage: true, editorV2,
    initialRevision: 2,
    latestApprovedVersion: { id: "old-approved", revision: 1,
      segments: [{ ...corrected[0], text: "Respirarse emborrachar" }] },
  });
  await page.route("**/admin/change-requests/85/proposals/current", route => route.fulfill({
    json: { proposal: { id: "proposal-85", job_id: jobId, change_request_id: 85,
      status: "applied", applied_revision: 2 } },
  }));
  await page.route("**/admin/change-requests**", route => {
    if (route.request().url().endsWith("/proposals/current")) return route.fallback();
    return route.fulfill({ json: { requests: [], pending_count: 0, resolved_count: 0 } });
  });
  await page.goto(`/videos/${jobId}/edit-lyrics?change_request_id=85`);
  await expect(page.getByRole("button", { name: /4 Lyrics/ })).toBeVisible();
  await page.reload();
  await expect(page.getByRole("button", { name: /4 Lyrics/ })).toBeVisible();
  const announcement = page.getByRole("button", { name: /Entendido|Entendí|Cancelar|Cerrar novedades/ }).first();
  if (await announcement.isVisible()) await announcement.click();
  await page.getByRole("button", { name: /4 Lyrics/ }).click();
  await expect(page.getByLabel("Letra de la línea 1")).toHaveValue(corrected[0].text);
  await page.getByRole("button", { name: /Aprobar y generar/i }).click();
  const override = page.getByRole("button", { name: "Aprobar igualmente" });
  if (await override.isVisible()) await override.click();
  await expect.poll(() => harness.approvals.length).toBe(1);
  expect(harness.approvals[0]).toMatchObject({
    edit_type: "lyrics", change_request_id: 85, change_request_proposal_id: "proposal-85",
    segments: [{ text: corrected[0].text }],
  });
  await expect(page).toHaveURL(/admin\?section=cambios&change_request_id=85&render_submitted=1/);
});
}
