import { test, expect } from "@playwright/test";
import { installEditorHarness } from "./editor-harness";

for (const mobile of [false, true]) {
  test.describe(mobile ? "mobile split" : "desktop split", () => {
    test.use({ viewport: mobile ? { width: 390, height: 844 } : { width: 1440, height: 1000 }, hasTouch: mobile, isMobile: mobile });
    test("previews, splits, autosaves and undoes word timing with the visible action", async ({ page }, testInfo) => {
      const jobId = "splitpreview01";
      const text = "Pero te quedas, ¿dónde más vas a ir?";
      const words = text.split(" ").map((word, index) => ({ word, start: 0.2 + index * 0.4, end: 0.5 + index * 0.4 }));
      const segments = [{ segment_id: "source", start: 0.1, end: 3.8, text, words }];
      const harness = await installEditorHarness(page, { jobId, role: "admin", editorV2: true, segments });
      await page.route(`**/status/${jobId}`, route => route.fulfill({ contentType: "application/json", body: JSON.stringify({
        job_id: jobId, status: "transcribed_pending", campaign_id: "split-campaign", campaign_item_id: "split-item",
        filename: "Alicia.wav", segments_revision: 0, segments_json: segments,
      }) }));
      await page.goto(`/review/${jobId}`);
      const input = page.getByLabel("Letra de esta línea", { exact: true });
      const button = page.getByRole("button", { name: "Dividir aquí", exact: true });
      await expect(button).toBeDisabled();
      if (mobile) {
        const x = await input.evaluate((node, prefix) => {
          const style = getComputedStyle(node);
          const context = document.createElement("canvas").getContext("2d");
          context.font = `${style.fontWeight} ${style.fontSize} ${style.fontFamily}`;
          return parseFloat(style.paddingLeft) + context.measureText(prefix).width;
        }, text.slice(0, text.indexOf("¿")));
        await input.tap({ position: { x, y: 24 } });
      } else {
        await input.focus();
        for (let i = 0; i < text.length; i++) await input.press("ArrowLeft");
        for (let i = 0; i < text.indexOf("¿"); i++) await input.press("ArrowRight");
      }
      const preview = page.getByRole("group", { name: "Vista previa de la división" });
      await expect(preview.getByText("Pero te quedas,", { exact: true })).toBeVisible();
      await expect(preview.getByText("¿dónde más vas a ir?", { exact: true })).toBeVisible();
      await expect(page.getByRole("status").filter({ hasText: "tiempos por palabra" })).toBeVisible();
      expect(harness.saves).toHaveLength(0);
      await expect(button).toBeEnabled();
      await button.scrollIntoViewIfNeeded();
      await page.screenshot({ path: testInfo.outputPath("split-preview.png"), fullPage: true });
      await page.getByTestId("selected-line-panel").screenshot({ path: testInfo.outputPath("split-panel.png") });
      expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth + 1)).toBe(true);
      if (mobile) await button.tap(); else await button.click();
      await expect(input).toHaveValue("¿dónde más vas a ir?");
      await expect(input).toBeFocused();
      await expect.poll(() => harness.saves.length).toBeGreaterThan(0);
      const saved = harness.saves.at(-1).segments;
      expect(saved).toHaveLength(2);
      expect(saved[0].start).toBe(0.1);
      expect(saved[0].end).toBeCloseTo(words[2].end);
      expect(saved[1].start).toBeCloseTo(words[3].start);
      expect(saved[1].end).toBe(3.8);
      expect(saved[0].words).toEqual(words.slice(0, 3));
      expect(saved[1].words).toEqual(words.slice(3));
      await page.getByRole("button", { name: "Deshacer", exact: true }).click();
      await expect(input).toHaveValue(text);
      await expect.poll(() => harness.saves.at(-1).segments.length).toBe(1);
      // Rewriting the text invalidates the old clocks: the UI and saved rows
      // must both acknowledge an estimated split, rather than claim accuracy.
      await input.fill("Ahora cambia toda esta frase");
      // Arrow keys are portable: Home scrolls instead of moving the caret
      // in Chromium on macOS.
      for (let i = 0; i < "Ahora cambia toda esta frase".length; i++) await input.press("ArrowLeft");
      for (let i = 0; i < 6; i++) await input.press("ArrowRight");
      await expect(page.getByRole("status").filter({ hasText: "Timing estimado" })).toBeVisible();
      if (mobile) await button.tap(); else await button.click();
      await expect.poll(() => harness.saves.at(-1).segments[0].timing_validation?.reason).toBe("estimated_split");
      const estimated = harness.saves.at(-1).segments;
      expect(estimated).toHaveLength(2);
      expect(estimated.every(row => row.review && row.timing_validation.status === "unvalidated")).toBe(true);
      expect(estimated.every(row => !row.words?.length)).toBe(true);
    });
  });
}
