import { test, expect } from "@playwright/test";
import { installEditorHarness } from "./editor-harness";

test("a delayed next-song response cannot pull the operator back after leaving the review", async ({ page }) => {
  const jobId = "racenext001";
  await installEditorHarness(page, { jobId, role: "admin", editorV2: true });
  let release;
  const gate = new Promise(resolve => { release = resolve; });
  await page.route(`**/status/${jobId}`, route => route.fulfill({ json: {
    job_id: jobId, status: "transcribed_pending", campaign_id: "racecamp01", campaign_item_id: "raceitem01",
    filename: "race.wav", segments_revision: 0, segments_json: [{ start: 0, end: 2, text: "Letra" }],
  } }));
  await page.route("**/batch/campaigns/racecamp01/review-queue/next**", async route => {
    await gate;
    await route.fulfill({ json: { job_id: "racenext002", open_path: "/review/racenext002" } });
  });
  await page.goto(`/review/${jobId}`);
  await expect(page.getByTestId("lyrics-editor")).toHaveAttribute("aria-busy", "false");
  const requested = page.waitForRequest(request => request.url().includes("/review-queue/next"));
  await page.getByRole("button", { name: "Siguiente", exact: true }).click();
  await requested;
  await page.getByRole("link", { name: "Inicio", exact: true }).click();
  await expect(page).toHaveURL(/\/dashboard$/);
  const response = page.waitForResponse(response => response.url().includes("/review-queue/next"));
  release();
  await (await response).finished();
  // Allow the released fetch/body and React navigation callbacks to settle.
  await page.waitForTimeout(300);
  await expect(page).toHaveURL(/\/dashboard$/);
});
