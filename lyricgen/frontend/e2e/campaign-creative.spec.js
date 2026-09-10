import { test, expect } from "@playwright/test";
import { installEditorHarness } from "./editor-harness";

async function harness(page) {
  await installEditorHarness(page, { jobId: "chilejob0001", role: "admin", editorV2: true });
  const campaign = { id: "chile1", name: "Campaña Chile", kind: "lyric_video", status: "active", registered_count: 39 };
  let head = { plan: { revision: 0 }, can_manage: true, veo_model: "veo-3.1-fast-generate-001", veo_models: [{ id: "veo-3.1-fast-generate-001", label: "Veo Fast" }, { id: "veo-3.1-lite-generate-001", label: "Veo Lite (vista previa)" }], operations: [], fields: {
    font: { label: "Tipografía", group: "Letra", kind: "select", options: ["", "anton", "poppins-bold"] },
    font_scale: { label: "Tamaño de letra", group: "Letra", kind: "number", min: .6, max: 1.5, step: .05 },
    effect: { label: "Efecto", group: "Movimiento y efectos", kind: "select", options: ["", "bokeh", "rain"] },
    movement_style: { label: "Movimiento", group: "Movimiento y efectos", kind: "select", options: ["", "foto-parallax", "estandar"] },
  }, items: Array.from({ length: 39 }, (_, n) => ({ id: `i${n}`, job_id: `chilejob${String(n + 1).padStart(4, "0")}`, ordinal: n + 1,
    title: `Canción ${n + 1}`, artist: "Artista Chile", status: n === 0 ? "lyrics_approved" : "transcribed_pending", settings: {} })) };
  const calls = { previews: [], generations: [] };
  const videos = [];
  await page.route("**/*", async route => {
    const req = route.request(), path = new URL(req.url()).pathname;
    const json = value => route.fulfill({ contentType: "application/json", body: JSON.stringify(value) });
    if (path === "/service-status/summary") return json({ status: "operational", incidents: [] });
    if (path.endsWith("/review-queue")) return json({ campaign, items: [], campaign_totals: { songs: 39, approved: 1 }, pages: 1, scope: { total: 0 }, counters: {} });
    if (path === "/batch/campaigns/chile1/creative") return json(head);
    if (path === "/backgrounds") return json([]);
    if (path.endsWith("/creative/report")) return json({ campaign_id: "chile1", name: campaign.name, at: new Date().toISOString(), contract: head.plan.contract || {}, groups: [], videos, history: [] });
    if (path.endsWith("/creative/preview")) {
      const body = req.postDataJSON(); calls.previews.push(body);
      return json({ preview_id: "frozen-39", counts: [20, 19], rounded: true, skipped: [], changes: head.items.map((item, n) => ({ item_id: item.id, artist: item.artist, title: item.title, group: body.groups[n < 20 ? 0 : 1].name, before: {}, after: body.groups[n < 20 ? 0 : 1].settings })) });
    }
    if (path.endsWith("/creative/apply")) {
      expect(req.postDataJSON()).toEqual({ preview_id: "frozen-39" });
      const body = calls.previews.at(-1);
      head = { ...head, plan: { revision: 1, groups: body.groups, mode: body.mode, contract: { agreement: body.agreement, rounding_note: body.rounding_note, revision: 1, item_ids: body.item_ids } },
        items: head.items.map((item, n) => ({ ...item, settings: body.groups[n < 20 ? 0 : 1].settings,
          assignment: { revision: 1, group_name: body.groups[n < 20 ? 0 : 1].name } })) };
      return json({ revision: 1 });
    }
    if (path === "/status/chilejob0001") return json({ job_id: "chilejob0001", status: "lyrics_approved", segments_revision: 7, segments_json: [{ start: 0, end: 2, text: "Letra aprobada" }] });
    if (path === "/generate") {
      calls.generations.push(req.postData()); head.items[0].status = "queued";
      videos.push({ job_id: "chilejob0001", artist: "Artista Chile", title: "Canción 1", status: "queued", created_at: new Date().toISOString(),
        assignment: head.items[0].assignment, settings: head.items[0].settings, evidence: {}, compliance: "pending", open_path: "/videos/chilejob0001" });
      return json({ job_id: "chilejob0001", status: "queued" });
    }
    return route.fallback();
  });
  return calls;
}

test("39-song contract assignment survives reload, generates only approved selections and has its own video history", async ({ page }) => {
  const calls = await harness(page);
  const announcement = page.getByRole("dialog", { name: "Nuevo editor de letras" });
  await page.addLocatorHandler(announcement, async () => { await announcement.getByRole("button", { name: "Cancelar" }).click(); });
  await page.goto("/campaigns/chile1?view=creative");
  await page.getByRole("button", { name: /Seleccionar todas las coincidencias/ }).click();
  await expect(page.getByText("39 seleccionadas")).toBeVisible();
  await page.getByLabel("Requisito del grupo 1").selectOption("photo_effect");
  await page.getByLabel("Nombre del grupo 1").fill("Foto fija con efecto");
  await page.getByLabel("Cantidad del grupo 1").fill("50");
  await page.getByRole("button", { name: "Agregar estilo" }).click();
  await page.getByLabel("Nombre del grupo 2").fill("Fondo Veo");
  await page.getByLabel("Cantidad del grupo 2").fill("50");
  await page.getByLabel("Requisito del grupo 2").selectOption("veo");
  await page.getByLabel("Modelo del grupo 2").selectOption("veo-3.1-lite-generate-001");
  await page.getByLabel("Registrar este reparto como acuerdo contractual").check();
  await page.getByLabel("Acuerdo contractual", { exact: true }).fill("Contrato Chile: mitad foto con efecto, mitad Veo");
  await page.getByLabel("Aceptación del redondeo").fill("Aceptado 20 fotos y 19 Veo");
  await page.getByLabel("Motivo del cambio").fill("Acuerdo de campaña");
  await page.getByRole("button", { name: "Ver reparto antes de guardar" }).click();
  await expect(page.getByText(/20 \(51.28%\)/)).toBeVisible();
  expect(calls.generations).toHaveLength(0);
  expect(calls.previews[0].item_ids).toHaveLength(39);
  expect(calls.previews[0].groups[0].settings.effect).toBe("bokeh");
  expect(calls.previews[0].groups[1].model).toBe("veo-3.1-lite-generate-001");
  await page.getByRole("button", { name: "Guardar esta asignación" }).click();
  await expect(page.getByRole("status").filter({ hasText: "Asignación guardada" })).toBeVisible();
  await page.reload();
  await expect(page.getByLabel("Nombre del grupo 1")).toHaveValue("Foto fija con efecto");
  await page.getByRole("button", { name: /Seleccionar todas las coincidencias/ }).click();
  await page.getByRole("button", { name: "Preparar generación de 1 aprobadas seleccionadas" }).click();
  await page.getByRole("button", { name: "Confirmar generación", exact: true }).click();
  await expect(page.getByRole("status").filter({ hasText: "1 trabajos enviados" })).toBeVisible();
  expect(calls.generations).toHaveLength(1);
  expect(calls.generations[0]).toContain('name="effect"\r\n\r\nbokeh');
  expect(calls.generations[0]).toContain('name="campaign_creative_revision"\r\n\r\n1');
  expect(calls.generations[0]).toContain('name="base_revision"\r\n\r\n7');
  await page.getByRole("button", { name: "Historial de videos", exact: true }).click();
  await expect(page).toHaveURL(/view=history/);
  await expect(page.getByRole("heading", { name: "Videos de esta campaña (1)" })).toBeVisible();
  await expect(page.getByRole("heading", { name: "Canción 1", exact: true })).toBeVisible();
  await page.screenshot({ path: "test-results/chile-campaign-video-history.png", fullPage: true });
  await page.getByRole("button", { name: "Contrato y cumplimiento", exact: true }).click();
  await expect(page.getByText("Contrato Chile: mitad foto con efecto, mitad Veo")).toBeVisible();
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
  await page.goto("/campaigns/chile1?view=history");
  await expect(page.getByRole("heading", { name: "Videos de esta campaña (0)" })).toBeVisible();
});
