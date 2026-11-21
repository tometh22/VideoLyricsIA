import { test, expect } from "@playwright/test";
import { installEditorHarness } from "./editor-harness";

test("recovers the real campaign preview after retry with automatic variant settings", async ({ page }) => {
  const jobId = "synthetic-preview-retry";
  const segments = [{ segment_id: "synthetic-line", start: 0, end: 3, text: "Una frase de prueba" }];
  await installEditorHarness(page, { jobId, role: "admin", editorV2: true, segments });
  await page.route(`**/status/${jobId}`, route => route.fulfill({ json: {
    job_id: jobId, status: "transcribed_pending", campaign_id: "synthetic-preview", campaign_item_id: "one",
    filename: "Synthetic.wav", segments_revision: 0, segments_json: segments,
  } }));
  // Keep the real worker, font bytes and render track installed by the harness.
  // Only the initial transport failure is injected; retry falls through to it.
  let unavailable = true;
  const payloads = [];
  await page.route(`**/editor/${jobId}/text-preview`, route => {
    payloads.push(route.request().postDataJSON());
    return unavailable ? route.fulfill({ status: 503, json: { detail: "Temporarily unavailable" } }) : route.fallback();
  });
  await page.goto(`/review/${jobId}`);
  const preview = page.getByTestId("native-lyric-preview");
  await expect(preview.getByText("No se pudo mostrar la vista previa del texto.")).toBeVisible();
  expect(payloads.at(-1).settings.lyric_variant).toBe("");
  unavailable = false;
  await preview.getByRole("button", { name: "Reintentar" }).click();
  await expect(preview.getByRole("img")).toHaveAttribute("aria-busy", "false", { timeout: 20000 });
  await expect(preview.locator("canvas")).toBeVisible();
  await expect(preview.getByText("No se pudo mostrar la vista previa del texto.")).toHaveCount(0);
});
