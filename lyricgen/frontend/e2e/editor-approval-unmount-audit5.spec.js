import { test, expect } from "@playwright/test";
import { installEditorHarness } from "./editor-harness";

test("leaving during the approval flush cannot start approval from the abandoned editor", async ({ page }) => {
  const jobId = "approvewait5";
  const segments = [{ segment_id: "a", start: 0, end: 2, text: "Letra revisada" }];
  await installEditorHarness(page, { jobId, role: "admin", editorV2: true, segments });
  let releaseSave;
  const saveGate = new Promise(resolve => { releaseSave = resolve; });
  let heldPatches = 0;
  const approvals = [];
  await page.route("**/*", async route => {
    const request = route.request(), path = new URL(request.url()).pathname;
    if (path === `/status/${jobId}`) return route.fulfill({ json: {
      job_id: jobId, filename: "approval.wav", status: "transcribed_pending", campaign_id: "audit5", campaign_item_id: "item-a",
      segments_revision: 0, segments_json: segments,
    } });
    if (path === `/editor/${jobId}` && request.method() === "PATCH") {
      heldPatches += 1;
      await saveGate;
      return route.fallback();
    }
    if (path.endsWith(`/jobs/${jobId}/approve-lyrics`)) {
      approvals.push(request.postDataJSON());
      return route.fulfill({ json: { status: "lyrics_approved", approved_version_id: "audit5-approved" } });
    }
    return route.fallback();
  });
  const announcement = page.getByRole("dialog", { name: "Nuevo editor de letras" });
  await page.addLocatorHandler(announcement, async () => announcement.getByRole("button", { name: "Cancelar" }).click());
  await page.goto(`/review/${jobId}?return_to=%2Fcampaigns%2Faudit5`);
  await expect(page.getByTestId("lyrics-editor")).toHaveAttribute("aria-busy", "false");
  await page.getByRole("button", { name: "Aprobar letra y timing", exact: true }).click();
  await expect.poll(() => heldPatches).toBe(1);
  await page.getByRole("link", { name: "Inicio", exact: true }).click();
  await expect(page).toHaveURL(/\/dashboard$/);
  const saved = page.waitForResponse(response => new URL(response.url()).pathname === `/editor/${jobId}`
    && response.request().method() === "PATCH");
  releaseSave();
  await saved;
  await page.waitForTimeout(300);
  expect(approvals).toHaveLength(0);
  await expect(page).toHaveURL(/\/dashboard$/);
});
