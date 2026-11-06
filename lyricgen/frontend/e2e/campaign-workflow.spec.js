import { test, expect } from "@playwright/test";
import { installEditorHarness } from "./editor-harness";
import { expectNoHorizontalOverflow, installCampaignApi, song } from "./campaign-harness";

function threeHundredSongs() {
  return Array.from({ length: 300 }, (_, n) => {
    const index = n + 1;
    const stage = index <= 40 ? "lyrics" : index <= 44 ? "qc" : index <= 46 ? "approved" : index <= 60 ? "ready" : index <= 290 ? "delivered" : "discarded";
    return song(index, stage, index <= 2 || (index >= 41 && index <= 42)
      ? { title: `Corazón ${index}`, artist: "García" }
      : {});
  });
}

for (const width of [1440, 390]) {
  test(`campaign pipeline: search, review queue, QC focus mode and delivery at ${width}px`, async ({ page }) => {
    await page.setViewportSize({ width, height: 1000 });
    await installEditorHarness(page, { jobId: "song-1", role: "admin" });
    const api = await installCampaignApi(page, { songs: threeHundredSongs() });
    await page.goto("/campaigns/workflow");
    await expect(page.getByRole("tab", { name: /^Todas/ })).toContainText("290");
    await expect(page.getByRole("tab", { name: /^Letra/ })).toContainText("40");
    await expect(page.getByRole("tab", { name: /^Entregada/ })).toContainText("230");
    await page.screenshot({ path: `test-results/campaign-overview-${width}.png` });

    await page.getByRole("tab", { name: /^Letra/ }).click();
    await expect(page).toHaveURL(/view=lyrics/);
    const search = page.getByRole("searchbox", { name: "Buscar canción o artista" });
    await search.fill("garcia corazon");
    await expect(page.getByText("Corazón 1", { exact: true })).toBeVisible();
    await expect(page.getByText("Canción 3", { exact: true })).toHaveCount(0);
    await page.screenshot({ path: `test-results/campaign-letters-${width}.png` });

    await page.getByRole("tab", { name: /^QC video/ }).click();
    // The search travels with the operator between stages.
    await expect(search).toHaveValue("garcia corazon");
    await expect(page.getByText("Canción 43", { exact: true })).toHaveCount(0);
    await search.press("Escape");
    await expect(page.getByText("Canción 43", { exact: true })).toBeVisible();
    await expect(page.getByRole("button", { name: /^Revisar 40 letras/ })).toBeVisible();
    await page.getByRole("button", { name: "Revisar video · Corazón 41" }).click();
    await expect(page.getByRole("dialog", { name: "Reproducir Corazón 41" })).toBeVisible();
    const approve = page.getByRole("button", { name: "Aprobar y siguiente", exact: true });
    const bounds = await approve.boundingBox();
    expect(bounds.y + bounds.height).toBeLessThanOrEqual(1000);
    await page.screenshot({ path: `test-results/campaign-player-${width}.png` });
    await approve.click();
    await expect(page.getByRole("dialog", { name: "Reproducir Corazón 42" })).toBeVisible();
    await page.keyboard.press("a");
    await expect(page.getByRole("dialog", { name: "Reproducir Canción 43" })).toBeVisible();
    await page.keyboard.press("Escape");
    await expect(page.getByRole("dialog")).toHaveCount(0);
    expect(api.approvals.map((approval) => approval.job_id)).toEqual(["song-41", "song-42"]);
    expect(api.approvals[0].notes).toBe("Revisión final desde el reproductor de campaña");

    await page.getByRole("tab", { name: /^Aprobada/ }).click();
    await expect(page.getByRole("tab", { name: /^Aprobada/ })).toContainText("4");
    await page.getByRole("checkbox", { name: width < 768 ? "Seleccionar todas (4)" : "Seleccionar 4 canciones" }).check();
    const bar = page.getByRole("region", { name: "Acciones sobre la selección" });
    await bar.getByRole("button", { name: /Enviar 4 al portal/ }).click();
    const send = page.getByRole("dialog", { name: "Enviar videos aprobados" });
    await send.getByLabel("Portal de destino").selectOption("chile");
    await page.screenshot({ path: `test-results/campaign-send-${width}.png` });
    await send.getByRole("button", { name: "Confirmar envío", exact: true }).click();
    await expect(page.getByText(/4 de 4 enviados/)).toBeVisible();
    expect(api.deliveries).toHaveLength(1);
    expect(api.deliveries[0].job_ids.sort()).toEqual(["song-41", "song-42", "song-45", "song-46"]);
    await page.reload();
    await expect(page.getByText(/4 de 4 enviados/)).toBeVisible();
    await expect(page.getByRole("tab", { name: /^Entregada/ })).toContainText("234");
    await page.screenshot({ path: `test-results/campaign-delivery-${width}.png`, fullPage: true });
    await expectNoHorizontalOverflow(page, expect);
  });
}

for (const width of [1440, 390]) {
  test(`campaign portal filters and editor return context survive reload at ${width}px`, async ({ page }) => {
    await page.setViewportSize({ width, height: 1000 });
    await installEditorHarness(page, { jobId: "song-2", role: "admin" });
    const api = await installCampaignApi(page, { songs: [
      song(1, "approved", { title: "Mataz", artist: "Lucybell" }),
      song(2, "delivered", { title: "Carnaval", artist: "Lucybell", portals: ["chile"], pending_change_requests: 1 }),
      song(3, "delivered", { title: "Otra", portal_outdated: true, portals: ["argentina"] }),
    ] });
    await page.goto("/campaigns/workflow");
    const table = page.getByRole("table");
    await expect(table.getByText("En Chile", { exact: true })).toBeVisible();
    await expect(table.getByText("1 cambio pedido", { exact: true })).toBeVisible();
    await expect(table.getByText("Listo para reenviar", { exact: true })).toBeVisible();   // approved + portal has an older cut
    await page.getByLabel("Filtrar por envío al portal").selectOption("sent");
    await expect(page.getByText("Mataz", { exact: true })).toHaveCount(0);
    await page.getByRole("searchbox", { name: "Buscar canción o artista" }).fill("lucybell");
    await expect(page.getByText("Otra", { exact: true })).toHaveCount(0);
    await page.reload();
    await expect(page.getByLabel("Filtrar por envío al portal")).toHaveValue("sent");
    await expect(page.getByText("Carnaval", { exact: true })).toBeVisible();
    await page.getByRole("button", { name: "Detalle de Carnaval" }).click();
    const drawer = page.getByRole("dialog", { name: "Canción Carnaval" });
    await expect(drawer).toBeVisible();
    await page.screenshot({ path: `test-results/campaign-drawer-${width}.png` });
    await drawer.getByRole("button", { name: "Editar letra y tiempos" }).click();
    await expect(page).toHaveURL(/videos\/song-2\/edit-lyrics/);
    const back = new URL(new URL(page.url()).searchParams.get("return_to"), "http://localhost");
    expect(back.pathname).toBe("/campaigns/workflow");
    expect(back.searchParams.get("portal")).toBe("sent");
    expect(back.searchParams.get("q")).toBe("lucybell");
    expect(back.searchParams.get("focus")).toBe("song-2");
    expect(api.deliveries).toHaveLength(0);
    expect(api.approvals).toHaveLength(0);
  });
}
