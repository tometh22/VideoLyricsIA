import { test, expect } from "@playwright/test";
import { installEditorHarness, createSyntheticWav } from "./editor-harness";

test("campaign refuses to open design after failed lyric save and retries without losing text", async ({ page }) => {
  const jobId = "auditthree01";
  const harness = await installEditorHarness(page, { jobId, role: "admin", editorV2: true,
    segments: [{ start: 0, end: 2, text: "Letra inicial" }] });
  let fail = true;
  let attempts = 0;
  await page.route("**/*", async route => {
    const request = route.request();
    const path = new URL(request.url()).pathname;
    if (path === `/status/${jobId}`) return route.fulfill({ json: {
      job_id: jobId, status: "transcribed_pending", campaign_id: "auditcamp01", campaign_item_id: "audititem01",
      filename: "audit.wav", segments_revision: 0, segments_json: [{ start: 0, end: 2, text: "Letra inicial" }],
    } });
    if (path === `/editor/${jobId}` && request.method() === "PATCH") {
      attempts++;
      if (fail) return route.fulfill({ status: 503, json: { detail: "Guardado temporalmente no disponible" } });
    }
    return route.fallback();
  });
  await page.goto(`/review/${jobId}`);
  await expect(page.getByTestId("lyrics-editor")).toHaveAttribute("aria-busy", "false");
  const input = page.getByLabel("Letra de esta línea", { exact: true });
  await input.fill("Texto humano que debe sobrevivir");
  await page.getByRole("button", { name: "Diseño de esta canción", exact: true }).click();
  await expect.poll(() => attempts).toBeGreaterThan(0);
  await expect(page.getByTestId("campaign-line-workspace")).toBeVisible();
  await expect(input).toHaveValue("Texto humano que debe sobrevivir");
  expect(harness.saves).toHaveLength(0);
  fail = false;
  await page.getByRole("button", { name: "Diseño de esta canción", exact: true }).click();
  await expect(page.getByRole("button", { name: "Volver a revisar letra", exact: true })).toBeVisible();
  expect(harness.saves.at(-1).segments[0].text).toBe("Texto humano que debe sobrevivir");
  await page.getByRole("button", { name: "Volver a revisar letra", exact: true }).click();
  await expect(input).toHaveValue("Texto humano que debe sobrevivir");
  await page.reload();
  await expect(input).toHaveValue("Texto humano que debe sobrevivir");
});


test("opening advanced timing reveals and selects a late campaign line", async ({ page }) => {
  const jobId = "auditlate001";
  const segments = [{ start: 0, end: 2, text: "Inicio" }, { start: 120, end: 123, text: "Final elegido" }];
  await installEditorHarness(page, { jobId, role: "admin", editorV2: true, segments });
  await page.route("**/e2e/audio.wav", route => route.fulfill({ contentType: "audio/wav", body: createSyntheticWav({ durationSeconds: 130 }) }));
  await page.route(`**/status/${jobId}`, route => route.fulfill({ json: {
    job_id: jobId, status: "transcribed_pending", campaign_id: "auditcamp01", campaign_item_id: "lateitem01",
    filename: "late.wav", segments_revision: 0, segments_json: segments,
  } }));
  await page.goto(`/review/${jobId}`);
  await expect(page.getByTestId("lyrics-editor")).toHaveAttribute("aria-busy", "false");
  await page.getByTestId("review-line-2").click();
  await page.getByRole("button", { name: "Ajustar esta línea en la timeline", exact: true }).click();
  const block = page.getByTestId("timeline-segment").filter({ hasText: "Final elegido" });
  await expect(block).toHaveAttribute("aria-pressed", "true");
  await expect.poll(() => page.getByTestId("timeline-scroll").evaluate(node => node.scrollLeft)).toBeGreaterThan(0);
  const inViewport = await block.evaluate(node => {
    const row = node.getBoundingClientRect();
    const container = node.closest('[data-testid="timeline-scroll"]').getBoundingClientRect();
    return row.right > container.left && row.left < container.right;
  });
  expect(inViewport).toBe(true);
});
