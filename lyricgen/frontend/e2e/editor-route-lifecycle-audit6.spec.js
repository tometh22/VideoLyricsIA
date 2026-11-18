import { test, expect } from "@playwright/test";
import { installEditorHarness, DEFAULT_SEGMENTS } from "./editor-harness";

for (const scenario of ["edit", "variant", "variant-overage", "edit-current", "variant-current"]) {
  const stay = scenario.endsWith("-current");
  const mode = stay ? scenario.replace("-current", "") : scenario;
  test(`${scenario}: submission response respects its originating route`, async ({ page }) => {
    const jobId = "routeaudit06";
    await installEditorHarness(page, { jobId, role: "admin", editorV2: true });
    await page.route(`**/status/${jobId}`, route => route.fulfill({ json: {
      job_id: jobId, status: mode === "edit" ? "pending_review" : "done",
      filename: "rendered.wav", artist: "Artista", song_title: "Canción renderizada",
      segments_revision: 0, segments_json: DEFAULT_SEGMENTS, render_params: { font: "anton" },
    } }));
    const endpoint = mode === "edit" ? `/edit/${jobId}` : `/jobs/${jobId}/variant`;
    let release;
    const gate = new Promise(resolve => { release = resolve; });
    let submissions = 0, confirmations = 0;
    page.on("dialog", async dialog => { confirmations += 1; await dialog.dismiss(); });
    await page.route(`**${endpoint}`, async route => {
      submissions += 1;
      await gate;
      return route.fulfill(mode === "variant-overage"
        ? { status: 402, json: { detail: { code: "variant_overage_unconfirmed", cost_extra_usd: 1, existing_renders: 3, included_per_song: 3 } } }
        : { json: { job_id: "createdaudit06", status: "processing" } });
    });
    const welcome = page.getByRole("dialog", { name: "Nuevo editor de letras" });
    await page.addLocatorHandler(welcome, () => welcome.getByRole("button", { name: "Cancelar" }).click());
    await page.goto(`/videos/${jobId}/${mode === "edit" ? "edit-lyrics" : "variant"}`);
    await expect(page.locator('[data-wizard-step="2"]')).toBeVisible();
    if (mode === "edit") {
      await page.getByRole("button", { name: /3 Tipografía/ }).click();
      await page.getByRole("button", { name: /^Tipografía:?$/ }).first().click();
      await page.getByRole("option", { name: /Poppins/ }).click();
    }
    await page.getByRole("button", { name: mode === "edit" ? /4 Lyrics/ : /4 Resumen/ }).click();
    await page.getByRole("button", { name: mode === "edit" ? "Aprobar cambios y generar video" : "Crear variante", exact: true }).click();
    await expect.poll(() => submissions).toBe(1);
    if (!stay) {
      await page.getByRole("link", { name: "Inicio", exact: true }).click();
      await expect(page).toHaveURL(/\/dashboard$/);
    }
    const response = page.waitForResponse(result => new URL(result.url()).pathname === endpoint);
    release();
    await response;
    await page.waitForTimeout(300);
    await expect(page).toHaveURL(stay
      ? new RegExp(`/videos/${mode === "edit" ? jobId : "createdaudit06"}$`)
      : /\/dashboard$/);
    expect(confirmations).toBe(0);
    expect(submissions).toBe(1);
  });
}
