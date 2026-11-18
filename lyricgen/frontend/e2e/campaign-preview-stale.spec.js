import { test, expect } from "@playwright/test";
import { installEditorHarness } from "./editor-harness";

for (const mobile of [false, true]) {
  test(`campaign preview and approval with an outdated timing proposal (${mobile ? "mobile" : "desktop"})`, async ({ page }, testInfo) => {
    const jobId = "campaignpreview01";
    const segments = [
      { segment_id: "one", start: 0, end: 1.5, text: "Yo convidándote a mi fiesta estoy" },
      { segment_id: "two", start: 1.6, end: 3.8, text: "Una segunda frase" },
    ];
    const qualityProposal = {
      id: "outdated", base_revision: 0, status: "pending", operator_suggestion_only: true,
      windows: [{ id: "tail", start: 0, end: 1.5, suggestion_type: "timing",
        current_segments: [segments[0]], proposed_segments: [{ ...segments[0], end: 1.2 }] }],
    };
    const lyricReview = () => ({ mode: "enforce", items: [{ id: "ai", required: true, kind: "heard_different",
      title: "Se escucha distinto", keys: ["ai"], occurrences: [{ line_segment_id: "one", start: 0, end: 1.5,
        before: segments[0].text, after: "Una propuesta incorrecta", fix: { type: "set_text", text: "Una propuesta incorrecta" } }] }] });
    const harness = await installEditorHarness(page, { jobId, role: "admin", editorV2: true,
      initialRevision: 2, segments, qualityProposal, lyricReview });
    const approvals = [];
    await page.route(`**/status/${jobId}`, route => route.fulfill({ json: {
      job_id: jobId, status: "transcribed_pending", campaign_id: "preview-campaign", campaign_item_id: "preview-item",
      filename: "song.wav", segments_revision: 2, segments_json: segments,
    } }));
    await page.route("**/batch/campaigns/preview-campaign/jobs/*/approve-lyrics", async route => {
      approvals.push(route.request().postDataJSON());
      await route.fulfill({ json: { status: "lyrics_approved", job_id: jobId } });
    });
    await page.setViewportSize(mobile ? { width: 390, height: 844 } : { width: 1440, height: 1000 });
    await page.goto(`/review/${jobId}`);
    const preview = page.getByTestId("review-live-preview");
    await expect(preview).toBeVisible();
    await expect(preview.getByText(segments[0].text.toUpperCase(), { exact: true })).toBeVisible();
    await page.getByTestId("review-line-2").click();
    await expect(preview.getByText("UNA SEGUNDA FRASE", { exact: true })).toBeVisible();
    await page.getByTestId("review-line-1").click();
    const stale = page.getByTestId("quality-proposal-panel");
    await expect(stale).toHaveAttribute("data-proposal-state", "stale");
    const disclosure = stale.getByTestId("quality-proposal-disclosure");
    await expect(disclosure).not.toHaveAttribute("open");
    await disclosure.locator(":scope > summary").click();
    await expect(stale.getByRole("button", { name: "Aceptar", exact: true })).toBeDisabled();
    await disclosure.locator(":scope > summary").click();
    await stale.getByRole("button", { name: "Ocultar propuesta" }).click();
    await expect(stale).toHaveCount(0);
    await page.getByLabel("Letra de esta línea", { exact: true }).fill("Esta es mi letra revisada");
    await expect(preview.getByText("ESTA ES MI LETRA REVISADA", { exact: true })).toBeVisible();
    await preview.scrollIntoViewIfNeeded();
    await preview.screenshot({ path: testInfo.outputPath("preview-panel.png") });
    await page.screenshot({ path: testInfo.outputPath("campaign-preview.png"), fullPage: true });
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth + 1)).toBe(true);
    await page.locator('[data-tour="editor-approve-floating"]').click();
    await expect.poll(() => approvals.length).toBe(1);
    expect(harness.saves.at(-1).segments[0]).toMatchObject({ text: "Esta es mi letra revisada", start: 0, end: 1.5 });
  });
}
