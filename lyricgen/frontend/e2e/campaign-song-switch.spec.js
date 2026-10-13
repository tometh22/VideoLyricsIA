import { test, expect } from "@playwright/test";
import { installEditorHarness } from "./editor-harness";
import { installCampaignApi, song } from "./campaign-harness";

test("campaign back then another song keeps URL, lyrics and audio bound to the selection", async ({ page }) => {
  await installEditorHarness(page, { jobId: "charly000001", role: "admin" });
  await installEditorHarness(page, { jobId: "latin0000001", role: "admin" });
  const songs = [{ job_id: "charly000001", title: "Transatlántico Art Deco", artist: "Charly García" },
    { job_id: "latin0000001", title: "Latin Geisha", artist: "Illya Kuryaki" }];
  await installCampaignApi(page, { id: "c1", name: "Campaña UMG", songs: songs.map((value, index) => song(index + 1, "lyrics", { ...value, current_job_id: value.job_id })) });
  await page.route("**/*", async route => {
    const url = new URL(route.request().url());
    const json = data => route.fulfill({ contentType: "application/json", body: JSON.stringify(data) });
    if (url.pathname.endsWith("/source-audio-url")) return json({ url: `/e2e/audio.wav?song=${url.pathname.split("/")[2]}` });
    if (url.pathname.endsWith("/waveform")) return json({ peaks: [0.1, 0.3, 0.5, 0.2] });
    const match = songs.find(s => url.pathname === `/status/${s.job_id}`);
    if (match) {
      if (match.job_id.startsWith("latin")) await new Promise(resolve => setTimeout(resolve, 400));
      return json({ ...match, song_title: match.title, filename: `${match.title}.wav`,
        status: "transcribed_pending", campaign_id: "c1", campaign_item_id: `item-${songs.indexOf(match) + 1}`, segments_revision: 0,
        segments_json: [{ start: 0, end: 2, text: `Letra de ${match.title}` }],
      });
    }
    return route.fallback();
  });
  await page.goto("/campaigns/c1?view=lyrics");
  await page.getByRole("button", { name: "Revisar · Transatlántico Art Deco" }).click();
  await expect(page).toHaveURL(/review\/charly000001\?return_to=/);
  await expect(page.getByText("Letra de Transatlántico Art Deco", { exact: true }).first()).toBeVisible();
  await page.goBack();
  await page.screenshot({ path: "test-results/campaign-review-queue.png", fullPage: true });
  await page.getByRole("button", { name: "Revisar · Latin Geisha" }).click();
  await expect(page.getByRole("status").filter({ hasText: "Cargando la canción seleccionada" })).toBeVisible();
  await expect(page.getByText("Letra de Transatlántico Art Deco", { exact: true })).toHaveCount(0);
  await expect(page).toHaveURL(/review\/latin0000001\?return_to=/);
  await expect(page.getByText("Letra de Latin Geisha", { exact: true }).first()).toBeVisible();
  await expect(page).toHaveURL(/review\/latin0000001\?return_to=/);
  await expect(page.locator('audio[src*="latin0000001"]').first()).toBeAttached();
  await expect(page.locator('audio[src*="charly000001"]')).toHaveCount(0);
  await page.getByRole("button", { name: "Descartar canción", exact: true }).click();
  await expect(page.getByRole("dialog", { name: "Descartar canción" })).toBeVisible();
  await expect(page.getByRole("dialog").getByRole("heading")).toContainText("Latin Geisha");
});

test("approved campaign lyrics without a video open, autosave and reload in the transcript editor", async ({ page }) => {
  const jobId = "approved0001";
  const harness = await installEditorHarness(page, { jobId, role: "admin", editorV2: true,
    segments: [{ start: 0, end: 2, text: "Transcripción aprobada" }] });
  const forbidden = [];
  await installCampaignApi(page, { id: "approved-campaign", name: "Campaña aprobada", songs: [
    song(1, "ready", { job_id: jobId, current_job_id: jobId, item_id: "approved-item", title: "Canción sin video" }),
  ] });
  await page.route("**/*", async route => {
    const req = route.request(), url = new URL(req.url()), path = url.pathname;
    const json = data => route.fulfill({ contentType: "application/json", body: JSON.stringify(data) });
    if (path === `/status/${jobId}`) return json({ job_id: jobId, status: "lyrics_approved",
      campaign_id: "approved-campaign", campaign_item_id: "approved-item", filename: "approved.wav",
      song_title: "Canción sin video", segments_revision: 0,
      segments_json: [{ start: 0, end: 2, text: "Transcripción aprobada" }], video_url: null,
    });
    if (path === "/generate" || path.endsWith("/approve-lyrics") || path.startsWith("/preview/") && path.endsWith("/video")) {
      forbidden.push(path); return json({});
    }
    return route.fallback();
  });
  await page.goto("/campaigns/approved-campaign?tab=approved&order=effort");
  await page.getByRole("button", { name: "Detalle de Canción sin video" }).click();
  await page.getByRole("dialog", { name: "Canción Canción sin video" }).getByRole("button", { name: "Editar letra y tiempos" }).click();
  await expect(page).toHaveURL(/\/review\/approved0001\?return_to=/);
  expect(new URL(page.url()).searchParams.get("return_to")).toContain("tab=approved&order=effort");
  await expect(page.getByLabel("Letra de la línea 1")).toHaveValue("Transcripción aprobada");
  await expect.poll(() => harness.heartbeats.length).toBeGreaterThan(0);
  await page.waitForFunction(() => [...document.querySelectorAll("audio")].some(a => a.readyState >= 1 && a.duration > 0));
  expect(harness.saves).toHaveLength(0);
  await page.getByLabel("Letra de la línea 1").fill("Transcripción corregida");
  await expect.poll(() => harness.saves.length).toBeGreaterThan(0);
  expect(harness.saves.at(-1).segments[0].text).toBe("Transcripción corregida");
  await page.reload();
  await expect(page.getByLabel("Letra de la línea 1")).toHaveValue("Transcripción corregida");
  expect(forbidden).toEqual([]);
});
