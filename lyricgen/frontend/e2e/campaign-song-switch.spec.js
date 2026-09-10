import { test, expect } from "@playwright/test";
import { installEditorHarness } from "./editor-harness";

test("campaign back then another song keeps URL, lyrics and audio bound to the selection", async ({ page }) => {
  await installEditorHarness(page, { jobId: "charly000001", role: "admin" });
  await installEditorHarness(page, { jobId: "latin0000001", role: "admin" });
  const songs = [{ job_id: "charly000001", title: "Transatlántico Art Deco", artist: "Charly García" },
    { job_id: "latin0000001", title: "Latin Geisha", artist: "Illya Kuryaki" }];
  await page.route("**/*", async route => {
    const url = new URL(route.request().url());
    const json = data => route.fulfill({ contentType: "application/json", body: JSON.stringify(data) });
    if (url.pathname === "/service-status/summary") return json({ status: "operational", incidents: [] });
    if (url.pathname.endsWith("/source-audio-url")) return json({ url: `/e2e/audio.wav?song=${url.pathname.split("/")[2]}` });
    if (url.pathname.endsWith("/waveform")) return json({ peaks: [0.1, 0.3, 0.5, 0.2] });
    if (url.pathname === "/batch/access") return json({ enabled: true });
    if (url.pathname.endsWith("/review-queue")) return json({
      campaign: { id: "c1", name: "Campaña UMG", status: "active", kind: "lyric_video" },
      items: songs.map((s, i) => ({ ...s, item_id: `i${i}`, state: "ready" })),
      campaign_totals: { songs: 2, approved: 0 }, pages: 1, counters: { ready: 2 }, scope: { total: 2 },
    });
    const song = songs.find(s => url.pathname === `/status/${s.job_id}`);
    if (song) {
      if (song.job_id.startsWith("latin")) await new Promise(resolve => setTimeout(resolve, 400));
      return json({ ...song, song_title: song.title, filename: `${song.title}.wav`,
        status: "transcribed_pending", campaign_id: "c1", segments_revision: 0,
        segments_json: [{ start: 0, end: 2, text: `Letra de ${song.title}` }],
      });
    }
    return route.fallback();
  });
  const announcement = page.getByRole("dialog");
  await page.addLocatorHandler(announcement, async () => {
    await announcement.getByRole("button", { name: "Cancelar" }).click();
  });
  await page.goto("/campaigns/c1");
  await page.getByRole("row").filter({ hasText: "Transatlántico Art Deco" }).getByRole("button", { name: "Revisar", exact: true }).click();
  await expect(page).toHaveURL(/review\/charly000001\?return_to=/);
  await expect(page.getByText("Letra de Transatlántico Art Deco", { exact: true }).first()).toBeVisible();
  await page.goBack();
  await page.screenshot({ path: "test-results/campaign-review-queue.png", fullPage: true });
  await page.getByRole("row").filter({ hasText: "Latin Geisha" }).getByRole("button", { name: "Revisar", exact: true }).click();
  await expect(page.getByRole("status").filter({ hasText: "Cargando la canción seleccionada" })).toBeVisible();
  await expect(page.getByText("Letra de Transatlántico Art Deco", { exact: true })).toHaveCount(0);
  await expect(page).toHaveURL(/review\/latin0000001\?return_to=/);
  await expect(page.getByText("Letra de Latin Geisha", { exact: true }).first()).toBeVisible();
  await expect(page).toHaveURL(/review\/latin0000001\?return_to=/);
  await expect(page.locator('audio[src*="latin0000001"]').first()).toBeAttached();
  await expect(page.locator('audio[src*="charly000001"]')).toHaveCount(0);
});
