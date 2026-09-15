import { test, expect } from "@playwright/test";
import { installEditorHarness } from "./editor-harness";

async function installCampaign(page) {
  await installEditorHarness(page, { jobId: "song-1", role: "admin" });
  const campaign = { id: "workflow", name: "Campaña de prueba · 300 canciones", kind: "lyric_video", status: "active", registered_count: 300 };
  const items = Array.from({ length: 300 }, (_, n) => ({ id: `item-${n}`, item_id: `item-${n}`, job_id: `song-${n}`, ordinal: n + 1,
    title: n < 2 ? `Corazón ${n + 1}` : `Canción ${n + 1}`, artist: n < 2 ? "García" : "Otro artista", technical_code: `ARUM${n}`, filename: `audio-${n}.wav`,
    state: "ready", status: "lyrics_approved", settings: {}, duration: 180 }));
  const videos = items.slice(0, 4).map((item, n) => ({ ...item, status: n < 2 ? "pending_review" : "done", approved_at: n < 2 ? null : "2026-09-15T00:00:00Z",
    assignment: {}, evidence: { video_sha256: `video-${n}` }, created_at: "2026-09-15T00:00:00Z", video_url: `/download/${item.job_id}/video` }));
  const calls = { approvals: [], deliveries: [] };
  const normalized = value => String(value).normalize("NFKD").replace(/\p{M}/gu, "").toLowerCase();
  await page.route("**/*", async route => {
    const req = route.request(), url = new URL(req.url()), path = url.pathname;
    const json = value => route.fulfill({ contentType: "application/json", body: JSON.stringify(value) });
    if (path === "/service-status/summary") return json({ status: "operational", incidents: [] });
    if (path === "/batch/campaigns/workflow") return json(campaign);
    if (path.endsWith("/review-queue")) {
      const query = normalized(url.searchParams.get("search") || "").split(/\s+/).filter(Boolean);
      const filtered = items.filter(item => query.every(word => normalized(`${item.artist} ${item.title} ${item.technical_code} ${item.filename}`).includes(word)));
      return json({ campaign, items: filtered, pages: 1, scope: { total: filtered.length }, counters: { ready: filtered.length }, campaign_totals: { songs: 300, approved: 0, discarded: 0 } });
    }
    if (path.endsWith("/creative")) return json({ plan: { revision: 0 }, can_manage: true, operations: [], fields: {}, items });
    if (path.endsWith("/creative/report")) return json({ videos, history: [], groups: [], contract: {} });
    if (path.startsWith("/approve/")) {
      const job_id = path.split("/").at(-1); calls.approvals.push({ job_id, ...req.postDataJSON() });
      Object.assign(videos.find(video => video.job_id === job_id), { status: "done", approved_at: "2026-09-15T00:00:00Z" });
      return json({ ok: true });
    }
    if (path.endsWith("/deliveries") && req.method() === "POST") {
      calls.deliveries.push(req.postDataJSON());
      return json({ operation_id: "delivery-1", total_count: req.postDataJSON().job_ids.length });
    }
    if (path === "/batch/delivery-operations/delivery-1") return json({ operation_id: "delivery-1", status: "completed", destination_portal: "chile", total_count: 2, sent_count: 2, items: [] });
    if (path.startsWith("/preview/") && path.endsWith("/video")) return route.fulfill({ status: 302, headers: { location: "/escenas_demo.mp4" } });
    if (path.startsWith("/preview/") && path.endsWith("/thumbnail")) return route.fulfill({ status: 302, headers: { location: "/fx_samples/foto_viva.jpg" } });
    return route.fallback();
  });
  const announcement = page.getByRole("dialog", { name: "Nuevo editor de letras" });
  await page.addLocatorHandler(announcement, () => announcement.getByRole("button", { name: "Cancelar" }).click());
  return { ...calls, videos };
}

for (const width of [1440, 390]) {
  test(`campaign search, pagination and approval-to-delivery workflow at ${width}px`, async ({ page }) => {
    await page.setViewportSize({ width, height: 1000 });
    const calls = await installCampaign(page);
    await page.goto("/campaigns/workflow");
    await expect(page.getByText("Corazón 1", { exact: true })).toBeVisible();
    await expect(page.getByText("Canción 26", { exact: true })).toHaveCount(0);
    await page.screenshot({ path: `test-results/campaign-letters-${width}.png` });
    await page.getByRole("button", { name: "Siguiente", exact: true }).click();
    await expect(page.getByText("Canción 26", { exact: true })).toBeVisible();
    const search = page.getByRole("searchbox", { name: "Buscar canción o artista" });
    await search.fill("garcia corazon");
    await expect(page.getByText("Corazón 1", { exact: true })).toBeVisible();
    await expect(page.getByText("Canción 26", { exact: true })).toHaveCount(0);
    await expect(page).not.toHaveURL(/rpage=2/);
    await page.getByRole("button", { name: /3. Revisar videos/ }).click();
    await expect(page.getByRole("searchbox", { name: "Buscar videos de la campaña" })).toHaveValue("garcia corazon");
    await page.getByRole("button", { name: "Reproducir", exact: true }).first().click();
    await expect(page.getByRole("dialog", { name: "Reproducir Corazón 1" })).toBeVisible();
    const approveBounds = await page.getByRole("button", { name: "Aprobar y siguiente", exact: true }).boundingBox();
    expect(approveBounds.y).toBeGreaterThanOrEqual(0);
    expect(approveBounds.y + approveBounds.height).toBeLessThanOrEqual(1000);
    await page.screenshot({ path: `test-results/campaign-player-${width}.png` });
    await page.getByRole("button", { name: "Aprobar y siguiente", exact: true }).click();
    await expect(page.getByRole("dialog", { name: "Reproducir Corazón 2" })).toBeVisible();
    expect(calls.approvals).toEqual([{ job_id: "song-0", notes: "Revisión final desde el reproductor de campaña" }]);
    await page.getByRole("button", { name: "Aprobar y siguiente", exact: true }).click();
    await expect(page.getByRole("dialog")).toHaveCount(0);
    await page.getByRole("button", { name: /4. Entregables/ }).click();
    await page.getByRole("button", { name: "Seleccionar aprobados del resultado (2)", exact: true }).click();
    await page.getByRole("button", { name: "Enviar seleccionados a un portal" }).click();
    await page.getByLabel("Portal de destino").selectOption("chile");
    await page.getByRole("button", { name: "Confirmar envío", exact: true }).click();
    await expect(page.getByText(/2 de 2 enviados/)).toBeVisible();
    expect(calls.deliveries).toHaveLength(1);
    expect(calls.deliveries[0].job_ids).toEqual(["song-0", "song-1"]);
    await page.reload();
    await expect(page.getByText(/2 de 2 enviados/)).toBeVisible();
    await page.screenshot({ path: `test-results/campaign-delivery-${width}.png`, fullPage: true });
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
  });
}


for (const width of [1440, 390]) {
  test(`campaign portal badges and sent filter retain editor return context at ${width}px`, async ({ page }) => {
    await page.setViewportSize({ width, height: 1000 });
    const { videos, deliveries, approvals } = await installCampaign(page);
    videos.forEach(video => Object.assign(video, { is_in_umg_portal: false, umg_portals: [] }));
    Object.assign(videos[1], { status: "done", approved_at: "2026-09-15", is_in_umg_portal: true, umg_portals: ["chile"], pending_change_requests: 1 });
    await page.goto("/campaigns/workflow?view=history");
    await expect(page.getByText("Enviado a Chile", { exact: true })).toBeVisible();
    await expect(page.getByText("1 cambio solicitado", { exact: true })).toBeVisible();
    await page.getByLabel("Filtrar por envío al portal").selectOption("sent");
    await expect(page.getByText("Corazón 1", { exact: true })).toHaveCount(0);
    await expect(page.getByText("Corazón 2", { exact: true })).toBeVisible();
    await page.getByRole("searchbox", { name: "Buscar videos de la campaña" }).fill("garcia");
    await page.reload();
    await expect(page.getByLabel("Filtrar por envío al portal")).toHaveValue("sent");
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
    await page.screenshot({ path: `test-results/campaign-portal-${width}.png`, fullPage: true });
    await page.getByRole("button", { name: "Editar", exact: true }).click();
    await expect(page).toHaveURL(/videos\/song-1\/edit-lyrics/);
    const back = new URL(new URL(page.url()).searchParams.get("return_to"), "http://localhost");
    expect(back.searchParams.get("portal_state")).toBe("sent");
    expect(back.searchParams.get("q")).toBe("garcia");
    expect(deliveries).toHaveLength(0);
    expect(approvals).toHaveLength(0);
  });
}
