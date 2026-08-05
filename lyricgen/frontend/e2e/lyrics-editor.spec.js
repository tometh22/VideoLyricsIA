import { expect, test } from "@playwright/test";
import { installEditorHarness, wavBuffer } from "./editor-harness";

async function openEditor(page) {
  await page.goto("/new");
  await page.locator('input[type="file"]').setInputFiles({ name: "song.wav", mimeType: "audio/wav", buffer: wavBuffer() });
  await page.locator('input[type="text"]').first().fill("Test Artist");
  await page.getByRole("button", { name: /Revisar lyrics/i }).click();
  await expect(page.getByText("Revisar lyrics").first()).toBeVisible();
  await expect(page.getByTestId("editor-basic-view")).toBeVisible();
}

test.describe("Editor 2.0", () => {
  test("starts basic and opens professional view", async ({ page }) => {
    await installEditorHarness(page);
    await openEditor(page);
    await expect(page.getByTestId("editor-basic-view")).toHaveAttribute("aria-selected", "true");
    await page.getByTestId("editor-advanced-view").click();
    await expect(page.getByTestId("lyrics-timeline-lane")).toBeVisible();
  });

  test("clicking empty timeline seeks without entering selection mode", async ({ page }) => {
    await installEditorHarness(page);
    await openEditor(page);
    await page.getByTestId("editor-advanced-view").click();
    const lane = page.getByTestId("lyrics-timeline-lane");
    await expect(lane).toBeVisible();
    const before = await page.locator("audio").evaluate((audio) => audio.currentTime);
    const box = await lane.boundingBox();
    await lane.click({ position: { x: Math.min(300, box.width - 20), y: Math.min(260, box.height - 20) } });
    await expect.poll(() => page.locator("audio").evaluate((audio) => audio.currentTime)).toBeGreaterThan(before);
  });

  test("paints selection and supports Cmd/Ctrl click", async ({ page }) => {
    await installEditorHarness(page);
    await openEditor(page);
    await page.getByTestId("editor-advanced-view").click();
    const lane = page.getByTestId("lyrics-timeline-lane");
    const box = await lane.boundingBox();
    await page.mouse.move(box.x + 260, box.y + 58);
    await page.mouse.down();
    await page.mouse.move(box.x + 260, box.y + 180, { steps: 8 });
    await page.mouse.up();
    const actions = page.getByTestId("timeline-primary-actions");
    await expect(actions).toHaveAttribute("data-selected-count", /[2-9]/);
    const before = Number(await actions.getAttribute("data-selected-count"));
    const blocks = page.locator("[data-timeline-block]");
    await blocks.nth(1).click({ modifiers: [process.platform === "darwin" ? "Meta" : "Control"] });
    await expect(actions).toHaveAttribute("data-selected-count", String(before - 1));
  });

  test("moves a selected group and autosaves a finite payload", async ({ page }) => {
    const harness = await installEditorHarness(page);
    await openEditor(page);
    await page.getByTestId("editor-advanced-view").click();
    const lane = page.getByTestId("lyrics-timeline-lane");
    const laneBox = await lane.boundingBox();
    await page.mouse.move(laneBox.x + 260, laneBox.y + 58);
    await page.mouse.down();
    await page.mouse.move(laneBox.x + 260, laneBox.y + 180, { steps: 8 });
    await page.mouse.up();
    const blocks = page.locator("[data-timeline-block]");
    const blockBox = await blocks.nth(1).boundingBox();
    await page.mouse.move(blockBox.x + 20, blockBox.y + 18);
    await page.mouse.down();
    await page.mouse.move(blockBox.x + 20, blockBox.y + 45, { steps: 6 });
    await page.mouse.up();
    await expect.poll(() => harness.saves.length).toBeGreaterThan(0);
    const last = harness.saves.at(-1);
    expect(last.segments.every((segment) => Number.isFinite(segment.start) && Number.isFinite(segment.end))).toBe(true);
    expect(last.segments.length).toBeGreaterThan(0);
  });

  test("shows saved status after a lyric edit and exposes version history", async ({ page }) => {
    await installEditorHarness(page);
    await openEditor(page);
    const line = page.locator('input[type="text"]').first();
    await line.fill("Primera línea corregida");
    await expect(page.getByText(/Guardado|Guardando/).first()).toBeVisible();
    await page.getByTestId("editor-advanced-view").click();
    await page.getByRole("main").getByRole("button", { name: "Historial" }).click();
    await expect(page.getByText("Versiones guardadas")).toBeVisible();
  });

  test("surfaces a revision conflict with an explicit team-version choice", async ({ page }) => {
    await installEditorHarness(page, { conflictOnce: true });
    await openEditor(page);
    await page.locator('input[type="text"]').first().fill("Cambio local");
    await expect(page.getByText("Otra persona guardó una versión más nueva")).toBeVisible({ timeout: 5000 });
    await expect(page.getByRole("button", { name: "Usar versión del equipo" })).toBeVisible();
    await page.getByRole("button", { name: "Usar versión del equipo" }).click();
    await expect(page.getByText("Guardado").first()).toBeVisible();
  });
});
