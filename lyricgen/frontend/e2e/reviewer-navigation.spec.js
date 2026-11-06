import { test, expect } from "@playwright/test";
import { installEditorHarness } from "./editor-harness";
import { installCampaignApi, song } from "./campaign-harness";

for (const path of ["/admin/cola", "/campaigns/campaign-1?view=lyrics"]) {
  test(`${path}: pipeline tabs, browser history and approved-lyrics return`, async ({ page }) => {
    await installEditorHarness(page, { jobId: "j2", role: "admin" });
    const mutations = [];
    await installCampaignApi(page, {
      id: "campaign-1",
      songs: [
        song(1, "lyrics", { title: "Lista para revisar", job_id: "j1", current_job_id: "j1" }),
        song(2, "ready", { title: "Letra ya aprobada", job_id: "j2", current_job_id: "j2", item_id: "i2" }),
        song(3, "attention", { title: "Falló procesamiento", job_id: "j3", current_job_id: "j3", error: "Falló la transcripción" }),
      ],
      onRequest: async ({ route, request, url, method, json }) => {
        if (request.isNavigationRequest() && url.pathname === "/admin/cola") {
          await route.fulfill({ response: await route.fetch({ url: `${url.origin}/` }) });
          return true;
        }
        if (url.pathname === "/auth/me") return json({ id: "e2e-user", email: "e2e@example.test", role: "admin", features: {} });
        if (url.pathname.startsWith("/batch/") && method !== "GET") { mutations.push(url.pathname); return json({}, 405); }
        if (url.pathname === "/status/j2") return json({ job_id: "j2", status: "lyrics_approved",
          campaign_id: "campaign-1", campaign_item_id: "i2", filename: "approved.wav",
          song_title: "Letra ya aprobada", segments_revision: 0,
          segments_json: [{ start: 0, end: 2, text: "Texto aprobado sin video" }], video_url: null });
        if (url.pathname.endsWith("/source-audio-url")) return json({ url: "/e2e/audio.wav" });
        if (url.pathname.endsWith("/waveform")) return json({ peaks: [.1, .2, .1] });
        return false;
      },
    });
    await page.goto(path);
    await expect(page).toHaveURL(/\/campaigns\/campaign-1\?view=lyrics/);
    await expect(page.getByText("Lista para revisar", { exact: true })).toBeVisible();
    await expect(page.getByText("Letra ya aprobada", { exact: true })).toHaveCount(0);
    await page.getByRole("tab", { name: /^Lista/ }).click();
    await expect(page.getByText("Letra ya aprobada", { exact: true })).toBeVisible();
    await expect(page.getByRole("tab", { name: /^Lista/ })).toHaveAttribute("aria-selected", "true");
    await page.getByRole("tab", { name: /^Lista/ }).press("ArrowRight");
    await expect(page.getByRole("tab", { name: /^Generando/ })).toHaveAttribute("aria-selected", "true");
    await page.goBack();
    await expect(page.getByRole("tab", { name: /^Lista/ })).toHaveAttribute("aria-selected", "true");
    await page.getByRole("tab", { name: /^Atención/ }).click();
    await expect(page.getByText("Falló la transcripción")).toBeVisible();
    await page.getByRole("tab", { name: /^Lista/ }).click();
    await page.getByRole("button", { name: "Detalle de Letra ya aprobada" }).click();
    await page.getByRole("dialog", { name: "Canción Letra ya aprobada" }).getByRole("button", { name: "Editar letra y tiempos" }).click();
    await expect(page).toHaveURL(/\/review\/j2\?return_to=/);
    await expect(page.getByLabel("Letra de la línea 1")).toHaveValue("Texto aprobado sin video");
    await page.getByRole("button", { name: "Volver", exact: true }).click();
    await expect(page).toHaveURL(/view=ready/);
    await expect(page.getByRole("tab", { name: /^Lista/ })).toHaveAttribute("aria-selected", "true");
    await expect(page.getByText("Letra ya aprobada", { exact: true })).toBeVisible();
    expect(mutations).toEqual([]);
  });
}
