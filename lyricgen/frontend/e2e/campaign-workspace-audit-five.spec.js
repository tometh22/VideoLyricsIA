import { test, expect } from "@playwright/test";
import AxeBuilder from "@axe-core/playwright";
import { installEditorHarness } from "./editor-harness";

test("campaign workspace preserves keyboard edits and remains accessible at desktop and narrow widths", async ({ page }) => {
  const jobId = "auditfiveui";
  const segments = Array.from({ length: 30 }, (_, index) => ({ segment_id: `line-${index}`,
    start: index * .1, end: index * .1 + .08, text: `Línea ${index + 1} con acentos y palabras`, review: index % 3 === 0 }));
  const api = await installEditorHarness(page, { jobId, role: "admin", editorV2: true, segments });
  await page.route(`**/status/${jobId}`, route => route.fulfill({ json: {
    job_id: jobId, status: "transcribed_pending", campaign_id: "auditfive", campaign_item_id: "one",
    filename: "audio.wav", segments_revision: 0, segments_json: segments,
  } }));
  const announcement = page.getByRole("dialog", { name: "Nuevo editor de letras" });
  await page.addLocatorHandler(announcement, async () => announcement.getByRole("button", { name: "Cancelar" }).click());
  await page.goto(`/review/${jobId}`);
  const workspace = page.getByTestId("campaign-line-workspace");
  await expect(workspace).toBeVisible();
  const first = page.getByTestId("review-line-1");
  await first.focus();
  await page.keyboard.press("ArrowDown");
  await expect(page.getByTestId("review-line-2")).toBeFocused();
  const input = page.getByLabel("Letra de esta línea", { exact: true });
  await expect(input).toHaveValue(segments[1].text);
  await input.fill("Corrección con espacios y acentos");
  await page.getByTestId("review-line-30").click();
  await expect(input).toHaveValue(segments[29].text);
  await expect.poll(() => api.saves.at(-1)?.segments.find(row => row.segment_id === "line-1")?.text).toBe("Corrección con espacios y acentos");
  for (const viewport of [{ width: 1440, height: 900 }, { width: 390, height: 844 }]) {
    await page.setViewportSize(viewport);
    await input.scrollIntoViewIfNeeded();
    await expect(input).toBeVisible();
    await workspace.screenshot({ path: test.info().outputPath(`campaign-workspace-${viewport.width}.png`), animations: "disabled" });
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= document.documentElement.clientWidth + 1)).toBe(true);
    const results = await new AxeBuilder({ page }).include('[data-testid="campaign-line-workspace"]')
      .withTags(["wcag2a", "wcag2aa", "wcag21a", "wcag21aa"]).analyze();
    expect(results.violations.filter(v => ["critical", "serious"].includes(v.impact))).toEqual([]);
  }
  await page.getByRole("button", { name: "Solo marcadas", exact: true }).click();
  await expect(page.getByTestId("review-line-2")).toHaveCount(0);
  await first.click();
  await first.press("ArrowDown");
  await expect(page.getByTestId("review-line-4")).toBeFocused();
  await expect(input).toHaveValue(segments[3].text);
});
