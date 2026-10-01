import { test, expect } from "@playwright/test";
import { installEditorHarness } from "./editor-harness";
import { installCampaignApi, song } from "./campaign-harness";

async function harness(page) {
  await installEditorHarness(page, { jobId: "song-1", role: "admin", editorV2: true });
  const songs = Array.from({ length: 39 }, (_, n) => song(n + 1, n === 0 ? "ready" : "lyrics", { artist: "Artista Chile" }));
  return installCampaignApi(page, { id: "chile1", name: "Campaña Chile", songs });
}

test("39-song contract assignment survives reload, generates only approved songs and reports compliance", async ({ page }) => {
  const api = await harness(page);
  await page.goto("/campaigns/chile1?view=config&section=style");
  await expect(page.getByText("39 canciones en este reparto")).toBeVisible();
  await page.getByLabel("Requisito del grupo 1").selectOption("photo_effect");
  await page.getByLabel("Nombre del grupo 1").fill("Foto fija con efecto");
  await page.getByLabel("Cantidad del grupo 1").fill("50");
  await page.getByRole("button", { name: "Agregar estilo" }).click();
  await page.getByLabel("Nombre del grupo 2").fill("Fondo Veo");
  await page.getByLabel("Cantidad del grupo 2").fill("50");
  await page.getByLabel("Requisito del grupo 2").selectOption("veo");
  await expect(page.getByText("Modelo de fondo: Veo Lite.")).toBeVisible();
  await expect(page.getByLabel("Modelo del grupo 2")).toHaveCount(0);
  await page.getByLabel("Registrar este reparto como acuerdo contractual").check();
  await page.getByLabel("Acuerdo contractual", { exact: true }).fill("Contrato Chile: mitad foto con efecto, mitad Veo");
  await page.getByLabel("Aceptación del redondeo").fill("Aceptado 20 fotos y 19 Veo");
  await page.getByLabel("Motivo del cambio").fill("Acuerdo de campaña");
  await page.screenshot({ path: "test-results/campaign-style-settings.png", fullPage: true });
  await page.getByRole("button", { name: "Ver reparto antes de guardar" }).click();
  await expect(page.getByText(/20 \(51.28%\)/)).toBeVisible();
  expect(api.generations).toHaveLength(0);
  expect(api.lastPreview.item_ids).toHaveLength(39);
  expect(api.lastPreview.groups[0].settings.effect).toBe("bokeh");
  expect(api.lastPreview.groups[1].model).toBe("veo-3.1-lite-generate-001");
  await page.getByRole("button", { name: "Guardar esta asignación" }).click();
  await expect(page.getByRole("status").filter({ hasText: "Asignación guardada" })).toBeVisible();
  await page.reload();
  await expect(page.getByLabel("Nombre del grupo 1")).toHaveValue("Foto fija con efecto");

  await page.getByRole("tab", { name: /^Lista/ }).click();
  await expect(page.getByText("Foto fija con efecto", { exact: true })).toBeVisible();
  await page.getByRole("button", { name: "Generar · Canción 1" }).click();
  const dialog = page.getByRole("dialog", { name: "Confirmar generación" });
  await expect(dialog).toContainText("Foto fija con efecto");
  await dialog.getByRole("button", { name: "Confirmar generación", exact: true }).click();
  await expect(page.getByText(/1 trabajo enviado/)).toBeVisible();
  expect(api.generations).toHaveLength(1);
  expect(api.generations[0]).toContain('name="effect"\r\n\r\nbokeh');
  expect(api.generations[0]).toContain('name="campaign_creative_revision"\r\n\r\n1');
  expect(api.generations[0]).toContain('name="base_revision"\r\n\r\n7');
  await expect(page.getByRole("tab", { name: /^Generando/ })).toContainText("1");

  await page.goto("/campaigns/chile1?view=config&section=contract");
  await expect(page.getByText("Contrato Chile: mitad foto con efecto, mitad Veo")).toBeVisible();
  await page.screenshot({ path: "test-results/chile-campaign-contract.png", fullPage: true });
});

test("individual campaign typography autosaves and reopens without approving or generating", async ({ page }) => {
  await installEditorHarness(page, { jobId: "creative0001", role: "admin" });
  let settings = { font: "anton", effect: "rain", movement_style: "foto-parallax", font_scale: 1.2,
    title_template: "badge", title_artist_font: "anton", title_song_font: "poppins-bold", title_size: 1.3,
    background_hint: "Paisaje de Chile", bg_verbatim: true, match_lyrics: false };
  let revision = 2;
  const saves = [], paidCalls = [];
  await page.route("**/*", async route => {
    const req = route.request(), path = new URL(req.url()).pathname;
    const json = value => route.fulfill({ contentType: "application/json", body: JSON.stringify(value) });
    if (path === "/service-status/summary") return json({ status: "operational", incidents: [] });
    if (path.endsWith("/source-audio-url")) return json({ url: "/e2e/audio.wav" });
    if (path.endsWith("/waveform")) return json({ peaks: [.1, .3, .2] });
    if (path === "/status/creative0001") return json({ job_id: "creative0001", artist: "Artista", song_title: "Canción creativa",
      filename: "audio.wav", status: "transcribed_pending", campaign_id: "c1", campaign_item_id: "i1", segments_revision: 0,
      segments_json: [{ start: 0, end: 2, text: "Letra de prueba" }],
      campaign: { default_render_params: {}, render_overrides: { ...settings, creative_assignment: { revision, requirement: "photo_effect" } } } });
    if (path === "/batch/campaigns/c1/creative/items/i1") {
      const body = req.postDataJSON(); expect(body.revision).toBe(revision);
      saves.push(body); settings = { ...settings, ...body.settings }; revision++;
      return json({ revision });
    }
    if (path === "/generate" || path.endsWith("/approve-lyrics")) { paidCalls.push(path); return json({}); }
    return route.fallback();
  });
  const announcement = page.getByRole("dialog", { name: "Nuevo editor de letras" });
  await page.addLocatorHandler(announcement, async () => announcement.getByRole("button", { name: "Cancelar" }).click());
  await page.goto("/review/creative0001?return_to=%2Fcampaigns%2Fc1");
  await expect(page.getByText("Letra de prueba", { exact: true }).first()).toBeVisible();
  await page.locator('[data-wizard-step="4"]').click();
  const font = page.getByRole("button", { name: /^Tipografía:?$/ }).first();
  await expect(font).toContainText("Anton");
  await font.click();
  await page.getByRole("option", { name: /Poppins/ }).click();
  await expect.poll(() => saves.length).toBe(1);
  expect(saves[0].settings.font).toBe("poppins-bold");
  expect(saves[0].settings.effect).toBe("rain");
  expect(saves[0].settings.title_template).toBe("badge");
  expect(saves[0].settings.scene_source).toBe("prompt_literal");
  await page.reload();
  await expect(page.getByText("Letra de prueba", { exact: true }).first()).toBeVisible();
  await page.locator('[data-wizard-step="4"]').click();
  await expect(page.getByRole("button", { name: /^Tipografía:?$/ }).first()).toContainText("Poppins");
  expect(paidCalls).toEqual([]);
});

test("printed campaign reports paginate outside the scrolling app and do not hide other screens", async ({ page }) => {
  await harness(page);
  const announcement = page.getByRole("dialog", { name: "Nuevo editor de letras" });
  await page.addLocatorHandler(announcement, async () => announcement.getByRole("button", { name: "Cancelar" }).click());
  await page.goto("/campaigns/chile1?view=config&section=style");
  await page.getByLabel("Registrar este reparto como acuerdo contractual").check();
  await page.getByLabel("Acuerdo contractual", { exact: true }).fill("Contrato impreso");
  await page.getByLabel("Motivo del cambio").fill("Acuerdo");
  await page.getByRole("button", { name: "Ver reparto antes de guardar" }).click();
  await page.getByRole("button", { name: "Guardar esta asignación" }).click();
  await expect(page.getByRole("status").filter({ hasText: "Asignación guardada" })).toBeVisible();
  await page.goto("/campaigns/chile1?view=contract");
  await page.locator("#campaign-contract-report").waitFor();
  await page.locator("#campaign-contract-report").evaluate(report => {
    for (let i = 0; i < 60; i++) {
      const line = document.createElement("p");
      line.textContent = `Registro de auditoría ${i + 1}: canción, estilo y evidencia conservados.`;
      report.append(line);
    }
    report.lastElementChild.dataset.printEnd = "true";
  });
  await page.emulateMedia({ media: "print" });
  const layout = await page.locator("[data-print-end]").evaluate(end => {
    const clipped = [];
    const bottom = end.getBoundingClientRect().bottom;
    for (let node = end.parentElement; node; node = node.parentElement) {
      const style = getComputedStyle(node);
      if (["hidden", "auto", "scroll", "clip"].includes(style.overflowY) && node.getBoundingClientRect().bottom < bottom - 1) clipped.push(node.tagName);
    }
    return { clipped, background: getComputedStyle(document.body).backgroundColor, documentHeight: document.documentElement.scrollHeight };
  });
  expect(layout.clipped).toEqual([]);
  expect(layout.background).toBe("rgb(255, 255, 255)");
  expect(layout.documentHeight).toBeGreaterThan(1500);
  await page.pdf({ path: "test-results/campaign-contract-print.pdf", format: "A4", printBackground: true });
  await page.emulateMedia({ media: "screen" });
  await page.goto("/campaigns/chile1?view=history");
  await expect(page.getByRole("heading", { name: "Video por revisar" })).toBeVisible();
  await expect(page.getByText("No hay videos por revisar")).toBeVisible();
});
