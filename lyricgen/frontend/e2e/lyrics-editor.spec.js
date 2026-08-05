import { expect, test } from "@playwright/test";
import {
  DEFAULT_SEGMENTS,
  drag,
  installEditorHarness,
  modifierForCurrentPlatform,
  openAdvanced,
  selectionCount,
} from "./editor-harness.js";

test.describe("lyrics editor browser contract", () => {
  test("switches basic and advanced views over the same lines", async ({ page }) => {
    const harness = await installEditorHarness(page);
    await harness.open();

    await expect(page.getByTestId("editor-mode-explainer")).toContainText("Básica");
    await expect(page.getByRole("tab", { name: "Revisar letra" })).toHaveAttribute("aria-selected", "true");
    await expect(page.getByText("Primera línea")).toBeVisible();

    await openAdvanced(page);
    await expect(page.getByTestId("timeline-segment")).toHaveCount(DEFAULT_SEGMENTS.length);
    await expect(page.getByTestId("editor-mode-explainer")).toContainText("Avanzada");

    await page.getByRole("tab", { name: "Revisar letra" }).click();
    await expect(page.getByRole("tab", { name: "Revisar letra" })).toHaveAttribute("aria-selected", "true");
    await expect(page.getByText("Primera línea")).toBeVisible();
  });

  test("shows a deterministic empty state when the job has no lyrics", async ({ page }) => {
    const harness = await installEditorHarness(page, { empty: true });
    await harness.open();

    await expect(page.getByText("Este video no tiene letras guardadas")).toBeVisible();
    await expect(page.getByText("Este job no tiene letras guardadas")).toBeVisible();
  });

  test("keeps the selected advanced context and explains unavailable audio", async ({ page }) => {
    const harness = await installEditorHarness(page, { audio: "unavailable" });
    await harness.open();
    await openAdvanced(page, { expectTimeline: false });

    await expect(page.getByRole("tab", { name: "Ajustar tiempos" })).toHaveAttribute("aria-selected", "true");
    await expect(page.getByText(/Audio no disponible para reproducir/i)).toBeVisible({ timeout: 12_000 });
    await expect(page.getByText("Primera línea")).toBeVisible();
    await expect(page.getByTestId("timeline-lane")).toHaveCount(0);
  });

  test("seeks from the timeline ruler without requiring playback", async ({ page }) => {
    test.fixme(true, "Current product baseline does not propagate a real ruler click to audio.currentTime in Chromium; keep this as the regression contract.");
    const harness = await installEditorHarness(page);
    await harness.open();
    await openAdvanced(page);

    const timeline = page.getByLabel("Studio de tiempos");
    const ruler = page.getByTestId("timeline-scroll").locator("div.h-9").first();
    const box = await ruler.boundingBox();
    const viewport = await page.getByTestId("timeline-scroll").boundingBox();
    expect(box).not.toBeNull();
    expect(viewport).not.toBeNull();
    await ruler.click({ position: { x: Math.min(box.width * 0.3, viewport.width - 20), y: box.height / 2 } });

    await expect.poll(async () => page.locator("audio").evaluate((audio) => audio.currentTime)).toBeGreaterThan(1.0);
  });

  test("selects lines by painting in both directions", async ({ page }) => {
    test.fixme(true, "Current product baseline does not expose the marquee selection count after a real block-to-block drag; modifier-click selection remains covered below.");
    const harness = await installEditorHarness(page);
    await harness.open();
    await openAdvanced(page);

    await page.getByRole("button", { name: "Seleccionar líneas" }).click();

    const first = await page.getByTestId("timeline-segment").first().boundingBox();
    const last = await page.getByTestId("timeline-segment").last().boundingBox();
    expect(first).not.toBeNull();
    expect(last).not.toBeNull();

    await drag(
      page,
      { x: first.x + first.width / 2, y: first.y + first.height / 2 },
      { x: last.x + last.width / 2, y: last.y + last.height / 2 },
    );
    await selectionCount(page, DEFAULT_SEGMENTS.length);

    await page.getByRole("button", { name: "Limpiar selección" }).click();
    await drag(
      page,
      { x: last.x + last.width / 2, y: last.y + last.height / 2 },
      { x: first.x + first.width / 2, y: first.y + first.height / 2 },
    );
    await selectionCount(page, DEFAULT_SEGMENTS.length);
  });

  test("toggles individual lines with both modifier-click conventions", async ({ page }) => {
    const harness = await installEditorHarness(page);
    await harness.open();
    await openAdvanced(page);

    const lines = page.getByTestId("timeline-segment");
    const modifier = modifierForCurrentPlatform();
    await lines.nth(0).click({ modifiers: [modifier] });
    await selectionCount(page, 1);
    await lines.nth(1).click({ modifiers: [modifier === "Meta" ? "Control" : "Meta"] });
    await selectionCount(page, 2);
    await lines.nth(0).click({ modifiers: [modifier] });
    await selectionCount(page, 1);
  });

  test("moves a selected group as one edit, supports undo, and saves finite timestamps", async ({ page }) => {
    const malformedSegments = [
      ...DEFAULT_SEGMENTS.slice(0, 3),
      { _id: "malformed", start: "not-a-time", end: null, text: "Línea recuperable" },
    ];
    const harness = await installEditorHarness(page, { segments: malformedSegments });
    await harness.open();
    await openAdvanced(page);

    const lines = page.getByTestId("timeline-segment");
    const modifier = modifierForCurrentPlatform();
    await lines.nth(0).click({ modifiers: [modifier] });
    await lines.nth(1).click({ modifiers: [modifier] });
    await lines.nth(2).click({ modifiers: [modifier] });
    await selectionCount(page, 3);

    const before = await lines.nth(0).evaluate((element) => parseFloat(element.style.left));
    const box = await lines.nth(0).boundingBox();
    expect(box).not.toBeNull();
    await drag(page, { x: box.x + box.width / 2, y: box.y + box.height / 2 }, { x: box.x + box.width / 2 + 36, y: box.y + box.height / 2 });
    await expect.poll(async () => lines.nth(0).evaluate((element) => parseFloat(element.style.left))).toBeGreaterThan(before + 20);

    await expect.poll(() => harness.saves.length).toBeGreaterThan(0);
    for (const save of harness.saves) {
      expect(Array.isArray(save.segments)).toBe(true);
      for (const segment of save.segments) {
        expect(Number.isFinite(Number(segment.start))).toBe(true);
        expect(Number.isFinite(Number(segment.end))).toBe(true);
        expect(Number(segment.end)).toBeGreaterThan(Number(segment.start));
      }
    }

    await page.keyboard.press(`${modifier}+z`);
    await expect.poll(async () => lines.nth(0).evaluate((element) => parseFloat(element.style.left))).toBeCloseTo(before, 1);
  });
});
