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
    await expect(page.locator('input[aria-label="Letra de la línea 1"]')).toHaveValue("Primera línea");

    await openAdvanced(page);
    await expect(page.getByTestId("timeline-segment")).toHaveCount(DEFAULT_SEGMENTS.length);
    await expect(page.getByTestId("editor-mode-explainer")).toContainText("Timeline y edición en grupo");

    await page.getByRole("tab", { name: "Revisar letra" }).click();
    await expect(page.getByRole("tab", { name: "Revisar letra" })).toHaveAttribute("aria-selected", "true");
    await expect(page.locator('input[aria-label="Letra de la línea 1"]')).toHaveValue("Primera línea");
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
    await expect(page.getByText("5 líneas", { exact: true })).toBeVisible();
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

    const rows = page.getByTestId("timeline-label-row");
    const firstRow = await rows.first().boundingBox();
    const lastRow = await rows.last().boundingBox();
    expect(firstRow).not.toBeNull();
    expect(lastRow).not.toBeNull();
    await drag(
      page,
      { x: lastRow.x + lastRow.width / 2, y: lastRow.y + lastRow.height / 2 },
      { x: firstRow.x + firstRow.width / 2, y: firstRow.y + firstRow.height / 2 },
    );
    await selectionAtLeast(page, 2);

    await page.getByRole("button", { name: "Limpiar selección" }).click();
    await page.waitForTimeout(100);
    const firstRowAfterClear = await rows.first().boundingBox();
    const lastRowAfterClear = await rows.last().boundingBox();
    expect(firstRowAfterClear).not.toBeNull();
    expect(lastRowAfterClear).not.toBeNull();
    await drag(
      page,
      { x: firstRowAfterClear.x + firstRowAfterClear.width / 2, y: firstRowAfterClear.y + firstRowAfterClear.height / 2 },
      { x: lastRowAfterClear.x + lastRowAfterClear.width / 2, y: lastRowAfterClear.y + lastRowAfterClear.height / 2 },
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

  test("deletes a timing line directly or as a selection and restores with undo", async ({ page }) => {
    const harness = await installEditorHarness(page);
    await harness.open();
    await openAdvanced(page);

    const lines = page.getByTestId("timeline-segment");
    const modifier = modifierForCurrentPlatform();
    const deleteLineButtons = page.getByTestId("timeline-delete-line");

    await expect(deleteLineButtons).toHaveCount(DEFAULT_SEGMENTS.length);
    await expect(deleteLineButtons.nth(0)).toHaveAccessibleName("Eliminar línea 1");
    await deleteLineButtons.nth(0).click();
    await expect(lines).toHaveCount(DEFAULT_SEGMENTS.length - 1);
    await page.keyboard.press(`${modifier}+z`);
    await expect(lines).toHaveCount(DEFAULT_SEGMENTS.length);

    await lines.nth(0).click({ modifiers: [modifier] });
    await lines.nth(1).click({ modifiers: [modifier] });
    await selectionCount(page, 2);
    page.once("dialog", (dialog) => dialog.accept());
    await page.getByRole("button", { name: "Eliminar 2 líneas" }).click();
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
    const rows = page.getByTestId("timeline-label-row");
    await page.getByTestId("timeline-segment").nth(2).click({ modifiers: [modifierForCurrentPlatform()] });
    await expect(rows.nth(2)).toHaveAttribute("data-selected", "true");

    await ruler.click({ position: { x: (laneBox.x - rulerBox.x) + 1.3 * pxPerSec, y: rulerBox.height / 2 } });

    await expect(rows.nth(1)).toHaveAttribute("data-active", "true");
    await page.getByRole("button", { name: "Reproducir" }).click();
    await expect(rows.nth(1)).toHaveAttribute("data-playing", "true");
    await expect(rows.nth(1)).toContainText("Sonando");
    // Freeze the short synthetic fixture before testing selection colors.
    // Otherwise it can naturally advance to another 600 ms line while the
    // browser assertions run, making playback timing part of a color test.
    await page.getByRole("button", { name: "Pausar" }).click();
    await expect(rows.nth(1)).toHaveAttribute("data-playing", "false");
    await page.locator("audio").evaluate((audio) => {
      audio.currentTime = 1.3;
      audio.dispatchEvent(new Event("timeupdate"));
    });
    await expect(rows.nth(1)).toHaveAttribute("data-active", "true");
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

  test("ripple-trims a packed shared boundary by default without shortening lyrics", async ({ page }) => {
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
    const savedSegments = harness.saves.at(-1).segments;
    const saved = savedSegments.find((segment) => segment.text === "Línea para estirar");
    const savedNext = savedSegments.find((segment) => segment.text === "Línea siguiente");
    expect(Number(saved.end)).toBeCloseTo(2.9, 2);
    expect(Number(savedNext.start)).toBeCloseTo(2.95, 2);
    expect(Number(savedNext.end)).toBeCloseTo(3.75, 2);
    expect(Number(savedNext.start) - Number(saved.end)).toBeCloseTo(0.05, 2);
    expect(Number(savedNext.end) - Number(savedNext.start)).toBeGreaterThanOrEqual(0.3);
    expect(saved.locked).toBe(true);
    expect(savedNext.locked).toBe(true);
  });

  test("Solo esta línea stops a right-edge trim before the next lyric", async ({ page }) => {
    const harness = await installEditorHarness(page, {
      segments: [
        { _id: "wide", start: 0.4, end: 2.4, text: "Línea para estirar" },
        { _id: "next", start: 2.7, end: 3.5, text: "Línea siguiente" },
      ],
    });
    await harness.open();
    await openAdvanced(page);
    await page.getByRole("button", { name: "Solo esta línea", exact: true }).first().click();

    const block = page.getByTestId("timeline-segment").first();
    const edge = block.getByTestId("timeline-edge-end");
    const edgeBox = await edge.boundingBox();
    expect(edgeBox).not.toBeNull();
    await drag(
      page,
      { x: edgeBox.x + edgeBox.width / 2, y: edgeBox.y + edgeBox.height / 2 },
      { x: edgeBox.x + edgeBox.width / 2 + 24, y: edgeBox.y + edgeBox.height / 2 },
    );

    await expect.poll(() => harness.saves.length).toBeGreaterThan(0);
    const savedSegments = harness.saves.at(-1).segments;
    const saved = savedSegments.find((segment) => segment.text === "Línea para estirar");
    const savedNext = savedSegments.find((segment) => segment.text === "Línea siguiente");
    expect(Number(saved.end)).toBeCloseTo(2.65, 2);
    expect(Number(savedNext.start)).toBeCloseTo(2.7, 2);
    expect(Number(savedNext.end)).toBeCloseTo(3.5, 2);
  });

  test("moves a short line from its body while resize handles stay outside", async ({ page }) => {
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
    const body = block.getByTestId("timeline-segment-body");
    const startEdge = block.getByTestId("timeline-edge-start");
    const endEdge = block.getByTestId("timeline-edge-end");
    await expect(body).toHaveCSS("width", "28px");
    await expect(startEdge).toHaveCSS("width", "22px");
    await expect(endEdge).toHaveCSS("width", "22px");

    const beforeLeft = await block.evaluate((element) => parseFloat(element.style.left));
    const bodyBox = await body.boundingBox();
    const startBox = await startEdge.boundingBox();
    const endBox = await endEdge.boundingBox();
    expect(bodyBox).not.toBeNull();
    expect(startBox.x + startBox.width).toBeLessThanOrEqual(bodyBox.x + 0.5);
    expect(endBox.x).toBeGreaterThanOrEqual(bodyBox.x + bodyBox.width - 0.5);

    await drag(
      page,
      { x: bodyBox.x + bodyBox.width / 2, y: bodyBox.y + bodyBox.height / 2 },
      { x: bodyBox.x + bodyBox.width / 2 + 12, y: bodyBox.y + bodyBox.height / 2 },
    );
    await expect.poll(async () => block.evaluate((element) => parseFloat(element.style.left))).toBeGreaterThan(beforeLeft + 8);
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

  test("keeps the typography warning visible and allows approval from the bottom of the editor", async ({ page }) => {
    const longLine = Array.from({ length: 80 }, () => "palabra").join(" ");
    const harness = await installEditorHarness(page, {
      segments: [{ _id: "oversized", start: 1, end: 8, text: `${longLine}.` }],
    });
    await harness.open();
    await page.getByLabel("Letra de la línea 1").fill(longLine);

    const panel = page.locator(".wizard-controls-panel");
    await panel.evaluate((element) => { element.scrollTop = element.scrollHeight; });
    await page.getByRole("button", { name: /Aprobar y generar/i }).click();

    const dialog = page.getByRole("dialog", { name: /Una línea puede ocupar 3 renglones/i });
    await expect(dialog).toBeVisible();
    const box = await dialog.boundingBox();
    expect(box).not.toBeNull();
    expect(box.y).toBeGreaterThanOrEqual(0);
    expect(box.y + box.height).toBeLessThanOrEqual(page.viewportSize().height);

    await dialog.getByRole("button", { name: "Aprobar igualmente" }).click();
    await expect.poll(() => harness.approvals.length).toBe(1);
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

  test("recovers audio after DB backpressure while autosave and the editor lock remain live", async ({ page }) => {
    const harness = await installEditorHarness(page, { audio: "temporary", editorV2: true });
    await harness.open();

    const input = page.locator('input[value="Primera línea"]');
    await input.fill("Primera línea tras presión DB");
    await expect.poll(() => harness.saves.length).toBeGreaterThan(0);
    await expect.poll(() => harness.heartbeats.length).toBeGreaterThan(0);
    await expect.poll(() => harness.sourceAudioRequests).toBe(3);

    await expect(page.getByTestId("wizard-player-slot").getByRole("button", { name: "Reproducir", exact: true })).toBeVisible();
    await openAdvanced(page);
    await expect(page.getByTestId("timeline-lane")).toBeVisible();
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
