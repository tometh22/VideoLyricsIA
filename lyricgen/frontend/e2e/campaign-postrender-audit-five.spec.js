import { test, expect } from "@playwright/test";
import { installEditorHarness, DEFAULT_SEGMENTS } from "./editor-harness";

test("rendered campaign jobs retain the existing edit wizard instead of entering campaign design implicitly", async ({ page }) => {
  const jobId = "rendercamp01";
  await installEditorHarness(page, { jobId, role: "admin", editorV2: true });
  await page.route(`**/status/${jobId}`, route => route.fulfill({ json: {
    job_id: jobId, status: "pending_review", filename: "rendered-campaign.wav",
    artist: "Artista", song_title: "Canción ya renderizada", segments_revision: 0,
    segments_json: DEFAULT_SEGMENTS, render_params: { font: "anton" },
    campaign_id: "campaign001", campaign_item_id: "campaign-item-1",
  } }));
  const welcome = page.getByRole("dialog", { name: "Nuevo editor de letras" });
  await page.addLocatorHandler(welcome, () => welcome.getByRole("button", { name: "Cancelar" }).click());
  await page.goto(`/videos/${jobId}/edit-lyrics`);
  await expect(page.locator('[data-wizard-step="2"]')).toBeVisible();
  // A rendered campaign job has editingJobId, but no transcribeJobId.
  // null===null must not mistake it for an explicitly opened design panel.
  await expect(page.getByRole("button", { name: "Volver a revisar letra", exact: true })).toHaveCount(0);
  await expect(page.locator('[data-wizard-step="2"]')).toHaveClass(/bg-brand/);
  await page.getByRole("button", { name: /4 Lyrics/ }).click();
  await expect(page.getByTestId("lyrics-editor")).toHaveAttribute("aria-busy", "false");
  await expect(page.getByRole("button", { name: "Aprobar cambios y generar video", exact: true })).toBeVisible();
  await expect(page.getByRole("button", { name: "Diseño de esta canción", exact: true })).toHaveCount(0);
});

test("campaign design without a prior assignment bootstraps revision zero and survives reload", async ({ page }) => {
  const jobId = "unassigned01";
  await installEditorHarness(page, { jobId, role: "admin", editorV2: true });
  let settings = {}, revision = 0;
  const saves = [];
  await page.route(`**/status/${jobId}`, route => route.fulfill({ json: {
    job_id: jobId, status: "transcribed_pending", filename: "unassigned.wav",
    artist: "Artista", song_title: "Canción sin asignación", segments_revision: 0,
    segments_json: DEFAULT_SEGMENTS, campaign_id: "campaign001", campaign_item_id: "unassigned-item",
    campaign: { default_render_params: { font: "anton" }, render_overrides: {
      ...settings, ...(revision > 0 ? { creative_assignment: { revision } } : {}),
    } },
  } }));
  await page.route("**/batch/campaigns/campaign001/creative/items/unassigned-item", async route => {
    const body = route.request().postDataJSON();
    saves.push(body);
    expect(body.revision).toBe(revision);
    settings = body.settings;
    revision += 1;
    await route.fulfill({ json: { revision } });
  });
  const welcome = page.getByRole("dialog", { name: "Nuevo editor de letras" });
  await page.addLocatorHandler(welcome, () => welcome.getByRole("button", { name: "Cancelar" }).click());
  await page.goto(`/review/${jobId}`);
  await expect(page.getByTestId("lyrics-editor")).toHaveAttribute("aria-busy", "false");
  await page.getByRole("button", { name: "Diseño de esta canción", exact: true }).click();
  const font = page.getByRole("button", { name: /^Tipografía:?$/ }).first();
  await expect(font).toContainText("Anton");
  // Wait past the 1200ms creative debounce: hydration alone must not pin
  // a new assignment or send a write before any deliberate change.
  await page.waitForTimeout(1500);
  expect(saves).toHaveLength(0);
  await font.click();
  await page.getByRole("option", { name: /Poppins/ }).click();
  await expect.poll(() => saves.length).toBe(1);
  expect(saves[0].revision).toBe(0);
  expect(saves[0].settings.font).toBe("poppins-bold");
  await page.reload();
  await expect(page.getByTestId("lyrics-editor")).toHaveAttribute("aria-busy", "false");
  await page.getByRole("button", { name: "Diseño de esta canción", exact: true }).click();
  await expect(font).toContainText("Poppins");
});
