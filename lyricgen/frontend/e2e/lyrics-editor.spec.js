import { expect, test } from "@playwright/test";
import AxeBuilder from "@axe-core/playwright";
import {
  DEFAULT_SEGMENTS,
  drag,
  installEditorHarness,
  modifierForCurrentPlatform,
  openAdvanced,
  selectionAtLeast,
  selectionCount,
} from "./editor-harness.js";

test.describe("lyrics editor browser contract", () => {
  test("switches basic and advanced views over the same lines", async ({ page }) => {
    const harness = await installEditorHarness(page);
    await harness.open();

    await expect(page.getByTestId("editor-mode-explainer")).toContainText("Corregir texto y aprobar");
    await expect(page.getByRole("tab", { name: "Revisar letra" })).toHaveAttribute("aria-selected", "true");
    await expect(page.getByText("Primera línea")).toBeVisible();

    await openAdvanced(page);
    await expect(page.getByTestId("timeline-segment")).toHaveCount(DEFAULT_SEGMENTS.length);
    await expect(page.getByTestId("editor-mode-explainer")).toContainText("Timeline y edición en grupo");

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
    await expect(page.getByTestId("advanced-audio-unavailable")).toContainText("No se puede ajustar tiempos sin audio", { timeout: 12_000 });
    await expect(page.getByText("Primera línea")).toBeVisible();
    await expect(page.getByTestId("timeline-lane")).toHaveCount(0);
  });

  test("seeks from the timeline ruler without requiring playback", async ({ page }) => {
    const harness = await installEditorHarness(page);
    await harness.open();
    await openAdvanced(page);

    const ruler = page.getByTestId("timeline-ruler");
    const box = await ruler.boundingBox();
    const lane = await page.getByTestId("timeline-lane").boundingBox();
    const viewport = await page.getByTestId("timeline-scroll").boundingBox();
    expect(box).not.toBeNull();
    expect(lane).not.toBeNull();
    expect(viewport).not.toBeNull();
    await ruler.click({ position: { x: Math.min((lane.x - box.x) + lane.width * 0.3, viewport.width - 20), y: box.height / 2 } });

    await expect.poll(async () => page.locator("audio").evaluate((audio) => audio.currentTime)).toBeGreaterThan(0.5);
  });

  test("selects lines by painting in both directions", async ({ page }) => {
    const harness = await installEditorHarness(page);
    await harness.open();
    await openAdvanced(page);

    await expect(page.getByTestId("timeline-selection-help")).toContainText("Arrastrá el fondo");

    const scroll = await page.getByTestId("timeline-scroll").boundingBox();
    const lane = await page.getByTestId("timeline-lane").boundingBox();
    expect(scroll).not.toBeNull();
    expect(lane).not.toBeNull();

    await drag(
      page,
      { x: scroll.x + scroll.width - 12, y: lane.y + lane.height - 10 },
      { x: scroll.x + 12, y: lane.y + 10 },
    );
    await selectionAtLeast(page, 2);

    await page.getByRole("button", { name: "Limpiar selección" }).click();
    await page.waitForTimeout(100);
    await drag(
      page,
      { x: scroll.x + 12, y: lane.y + 10 },
      { x: scroll.x + scroll.width - 12, y: lane.y + lane.height - 10 },
    );
    await selectionAtLeast(page, 2);
  });

  test("toggles individual lines with modifiers and selects ranges with Shift-click", async ({ page }) => {
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
    await lines.nth(2).click({ modifiers: ["Shift"] });
    await selectionCount(page, 3);
  });

  test("moves a selected group as one edit, supports undo, and saves finite timestamps", async ({ page }) => {
    const malformedSegments = [
      { _id: "a", start: 0.4, end: 1, text: "Primera línea" },
      { _id: "b", start: 1.2, end: 1.8, text: "Segunda línea" },
      { _id: "c", start: 2, end: 2.6, text: "Tercera línea" },
      { _id: "d", start: 5, end: 5.6, text: "Cuarta línea" },
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
    const driverBefore = await lines.nth(2).evaluate((element) => parseFloat(element.style.left));
    const box = await lines.nth(2).boundingBox();
    expect(box).not.toBeNull();
    await drag(page, { x: box.x + box.width / 2, y: box.y + box.height / 2 }, { x: box.x + box.width / 2 + 36, y: box.y + box.height / 2 });
    await expect.poll(async () => lines.nth(2).evaluate((element) => parseFloat(element.style.left))).toBeGreaterThan(driverBefore + 20);
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

  test("deletes selected timing lines and restores them with undo", async ({ page }) => {
    const harness = await installEditorHarness(page);
    await harness.open();
    await openAdvanced(page);

    const lines = page.getByTestId("timeline-segment");
    const modifier = modifierForCurrentPlatform();
    await lines.nth(0).click({ modifiers: [modifier] });
    await lines.nth(1).click({ modifiers: [modifier] });
    await selectionCount(page, 2);
    page.once("dialog", (dialog) => dialog.accept());
    await page.getByRole("button", { name: "Eliminar" }).click();
    await expect(lines).toHaveCount(DEFAULT_SEGMENTS.length - 2);
    await page.keyboard.press(`${modifier}+z`);
    await expect(lines).toHaveCount(DEFAULT_SEGMENTS.length);
  });

  test("separates cyan playback state from purple selection state", async ({ page }) => {
    const harness = await installEditorHarness(page);
    await harness.open();
    await openAdvanced(page);

    const ruler = page.getByTestId("timeline-ruler");
    const rulerBox = await ruler.boundingBox();
    const lane = page.getByTestId("timeline-lane");
    const laneBox = await lane.boundingBox();
    const pxPerSec = Number(await lane.getAttribute("data-px-per-sec"));
    expect(rulerBox).not.toBeNull();
    expect(laneBox).not.toBeNull();
    await ruler.click({ position: { x: (laneBox.x - rulerBox.x) + 1.3 * pxPerSec, y: rulerBox.height / 2 } });

    const rows = page.getByTestId("timeline-label-row");
    await expect(rows.nth(1)).toHaveAttribute("data-active", "true");
    await page.getByRole("button", { name: "Reproducir" }).click();
    await expect(rows.nth(1)).toHaveAttribute("data-playing", "true");
    await expect(rows.nth(1)).toContainText("Sonando");

    await page.getByTestId("timeline-segment").nth(2).click({ modifiers: [modifierForCurrentPlatform()] });
    await expect(rows.nth(2)).toHaveAttribute("data-selected", "true");
    await expect(rows.nth(2)).toHaveAttribute("data-active", "false");
    await expect(rows.nth(1)).toHaveClass(/bg-cyan/);
    await expect(rows.nth(2)).toHaveClass(/bg-brand/);
    await expect(page.getByTestId("timeline-active-playhead")).toBeVisible();
  });

  test("scrolls long lyrics in the editor instead of trapping the wheel inside the timeline", async ({ page }) => {
    const longSegments = Array.from({ length: 18 }, (_, index) => ({
      _id: `long-${index}`,
      start: index * 0.2,
      end: index * 0.2 + 0.16,
      text: `Línea extensa ${index + 1}`,
    }));
    const harness = await installEditorHarness(page, { segments: longSegments });
    await harness.open();
    await openAdvanced(page);

    const panel = page.locator(".wizard-controls-panel");
    const timelineScroll = page.getByTestId("timeline-scroll");
    await expect.poll(() => panel.evaluate((element) => element.scrollHeight > element.clientHeight)).toBe(true);
    await timelineScroll.hover();
    await page.mouse.wheel(0, 700);
    await expect.poll(() => panel.evaluate((element) => element.scrollTop)).toBeGreaterThan(100);
    await expect.poll(() => timelineScroll.evaluate((element) => element.scrollTop)).toBe(0);
    await expect(page.getByTestId("timeline-label-row").last()).toContainText("Línea extensa 18");
    await expect(page.getByTestId("timeline-label-row").last()).toBeVisible();
  });

  test("resizes a line from a generous edge hit area with one continuous drag", async ({ page }) => {
    const harness = await installEditorHarness(page, {
      segments: [
        { _id: "wide", start: 0.4, end: 2.4, text: "Línea para estirar" },
        { _id: "next", start: 2.7, end: 3.5, text: "Línea siguiente" },
      ],
    });
    await harness.open();
    await openAdvanced(page);

    const block = page.getByTestId("timeline-segment").first();
    const edge = block.getByTestId("timeline-edge-end");
    await expect(edge).toHaveCSS("width", "22px");
    const beforeWidth = await block.evaluate((element) => parseFloat(element.style.width));
    const edgeBox = await edge.boundingBox();
    expect(edgeBox).not.toBeNull();
    await drag(
      page,
      { x: edgeBox.x + edgeBox.width / 2, y: edgeBox.y + edgeBox.height / 2 },
      { x: edgeBox.x + edgeBox.width / 2 + 24, y: edgeBox.y + edgeBox.height / 2 },
    );

    await expect.poll(async () => block.evaluate((element) => parseFloat(element.style.width))).toBeGreaterThan(beforeWidth + 10);
    await expect.poll(() => harness.saves.length).toBeGreaterThan(0);
    const saved = harness.saves.at(-1).segments.find((segment) => segment.text === "Línea para estirar");
    expect(Number(saved.end)).toBeCloseTo(2.65, 2);
    expect(Number(saved.end)).toBeLessThanOrEqual(2.65 + Number.EPSILON * 4);
  });

  test("keeps short-line geometry real while handles remain easy to hit", async ({ page }) => {
    const harness = await installEditorHarness(page, {
      segments: [
        { _id: "short", start: 0.4, end: 0.7, text: "Oh" },
        { _id: "next", start: 1.2, end: 2, text: "Siguiente" },
      ],
    });
    await harness.open();
    await openAdvanced(page);
    const block = page.getByTestId("timeline-segment").first();
    const width = await block.evaluate((element) => parseFloat(element.style.width));
    const zoom = Number(await page.getByTestId("timeline-lane").getAttribute("data-px-per-sec"));
    expect(width).toBeCloseTo(0.3 * zoom, 1);
    await expect(block.getByTestId("timeline-edge-start")).toHaveCSS("width", "22px");
    await expect(block.getByTestId("timeline-edge-end")).toHaveCSS("width", "22px");
  });

  test("supports keyboard tabs and accessible selection nudges", async ({ page }) => {
    const harness = await installEditorHarness(page);
    await harness.open();
    const basic = page.getByRole("tab", { name: "Revisar letra" });
    await basic.focus();
    await page.keyboard.press("ArrowRight");
    await expect(page.getByRole("tab", { name: "Ajustar tiempos" })).toHaveAttribute("aria-selected", "true");
    const block = page.getByTestId("timeline-segment").first();
    await block.focus();
    await page.keyboard.press("Enter");
    await selectionCount(page, 1);
    const before = await block.evaluate((element) => parseFloat(element.style.left));
    await page.keyboard.press("ArrowRight");
    await expect.poll(async () => block.evaluate((element) => parseFloat(element.style.left))).toBeGreaterThan(before);
  });

  test("uses the durable editor contract when editor_v2 is enabled", async ({ page }) => {
    const harness = await installEditorHarness(page, { editorV2: true });
    await harness.open();
    const input = page.locator('input[value="Primera línea"]');
    await input.fill("Primera línea durable");
    await expect.poll(() => harness.saves.length).toBeGreaterThan(0);
    expect(harness.saves[0]).toMatchObject({ base_revision: 0, checkpoint: "draft" });

    await page.getByRole("button", { name: /Aprobar y generar/i }).click();
    await expect.poll(() => harness.approvals.length).toBe(1);
    const approved = harness.approvals[0];
    const persisted = harness.saves.at(-1);
    expect(approved.editor_revision).toBeGreaterThan(0);
    expect(approved.editor_version_id).toBe(`version-${approved.editor_revision}`);
    expect(approved.segments).toEqual(persisted.segments.map(({ _id, ...segment }) => segment));
  });

  test("passes the automated accessibility audit in both editor views", async ({ page }) => {
    const harness = await installEditorHarness(page);
    await harness.open();
    for (const mode of ["basic", "advanced"]) {
      if (mode === "advanced") await openAdvanced(page);
      const results = await new AxeBuilder({ page })
        .include('[data-testid="lyrics-editor"]')
        .withTags(["wcag2a", "wcag2aa", "wcag21a", "wcag21aa"])
        .analyze();
      const blocking = results.violations.filter((violation) => ["critical", "serious"].includes(violation.impact));
      expect(blocking, `${mode}: ${blocking.map((violation) => violation.id).join(", ")}`).toEqual([]);
    }
  });

  test("has no global overflow or CTA overlap across product breakpoints", async ({ page }) => {
    const segments = Array.from({ length: 80 }, (_, index) => ({
      _id: `line-${index}`,
      start: index * 0.5,
      end: index * 0.5 + 0.35,
      text: `Línea ${index + 1}`,
    }));
    const harness = await installEditorHarness(page, { segments });
    await harness.open();
    for (const viewport of [
      { width: 1366, height: 768 },
      { width: 1440, height: 900 },
      { width: 2048, height: 1100 },
      { width: 834, height: 1112 },
      { width: 390, height: 844 },
    ]) {
      await page.setViewportSize(viewport);
      await expect.poll(() => page.evaluate(() => {
        const root = document.documentElement;
        if (root.scrollWidth <= root.clientWidth + 1) return [];
        return [...document.querySelectorAll("body *")]
          .map((element) => {
            const rect = element.getBoundingClientRect();
            return { element, rect };
          })
          .filter(({ rect }) => rect.right > root.clientWidth + 1 || rect.left < -1)
          .slice(0, 8)
          .map(({ element, rect }) => ({
            tag: element.tagName.toLowerCase(),
            testId: element.getAttribute("data-testid"),
            className: typeof element.className === "string" ? element.className.slice(0, 160) : "",
            left: Math.round(rect.left),
            right: Math.round(rect.right),
          }));
      })).toEqual([]);
      await expect(page.getByRole("button", { name: /Aprobar y generar/i })).toBeVisible();
    }
  });
});
