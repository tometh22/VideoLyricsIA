import { expect, test } from "@playwright/test";
import { DEFAULT_SEGMENTS, installEditorHarness } from "./editor-harness.js";

for (const surface of ["rail", "block"]) {
  test(`clicking the timeline ${surface} enables nudge and Delete without a separate focus step`, async ({ page }) => {
    const harness = await installEditorHarness(page, { editorV2: true });
    await harness.open();
    await page.getByRole("tab", { name: "Ajustar tiempos", exact: true }).click();
    await expect(page.getByTestId("timeline-segment")).toHaveCount(DEFAULT_SEGMENTS.length);
    const target = surface === "rail" ? page.getByTestId("timeline-label-row").nth(1)
      : page.getByTestId("timeline-segment-body").nth(1);
    await target.click();
    await expect(page.getByTestId("timeline-label-row").nth(1)).toHaveAttribute("data-selected", "true");
    // No focus() call: mouse selection is the actual user workflow.
    await page.keyboard.press("ArrowRight");
    await expect.poll(() => harness.saves.some(save => save.segments.some(row => row.text === "Segunda línea"
      && Math.abs(Number(row.start) - 1.3) < 0.001 && Math.abs(Number(row.end) - 1.9) < 0.001))).toBe(true);
    await page.keyboard.press("Delete");
    await expect(page.getByTestId("timeline-segment")).toHaveCount(DEFAULT_SEGMENTS.length - 1);
    await expect.poll(() => harness.saves.some(save => save.segments.length === DEFAULT_SEGMENTS.length - 1)).toBe(true);
  });
}

test("pointer focus preserves inline spaces, caret arrows and text deletion", async ({ page }) => {
  const harness = await installEditorHarness(page, { editorV2: true });
  await harness.open();
  await page.getByRole("tab", { name: "Ajustar tiempos", exact: true }).click();
  const second = page.getByTestId("timeline-segment").nth(1);
  await second.getByTestId("timeline-segment-body").dblclick();
  const input = second.locator("input");
  await expect(input).toBeFocused();
  await input.press("End");
  await input.pressSequentially(" con espacios");
  await input.press("ControlOrMeta+A");
  await input.press("ArrowLeft");
  await input.press("ArrowRight");
  await input.press("Delete");
  await expect(input).toHaveValue("Sgunda línea con espacios");
  await expect(input).toBeFocused();
  await expect(page.getByTestId("timeline-segment")).toHaveCount(DEFAULT_SEGMENTS.length);
  await expect.poll(() => page.locator("audio").evaluate(audio => audio.paused)).toBe(true);
  await expect.poll(() => harness.saves.some(save => save.segments.some(row => row.text === "Sgunda línea con espacios"
    && Number(row.start) === 1.2 && Number(row.end) === 1.8))).toBe(true);
});
