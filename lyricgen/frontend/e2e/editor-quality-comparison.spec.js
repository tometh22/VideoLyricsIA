import { expect, test } from "@playwright/test";
import { installEditorHarness } from "./editor-harness";

test("compara dos renders con audio, guarda un voto y mantiene el borrador", async ({ page }) => {
  const jobId = "quality-timing-ab";
  const line = { segment_id: "voice", start: 0.5, end: 2.6, text: "Cierre de una frase" };
  const segments = [line, { segment_id: "next", start: 3.5, end: 3.9, text: "Respuesta" }];
  const candidate = { ...line, end: 3.2 };
  const proposal = {
    id: "pilot", base_revision: 0, status: "pending", windows: [{
      id: "tail", start: 0.5, end: 3.5, suggestion_type: "timing", current_segments: [line], proposed_segments: [candidate],
    }],
  };
  const api = await installEditorHarness(page, { jobId, role: "admin", editorV2: true, segments });
  await page.route(`**/status/${jobId}`, route => route.fulfill({ json: {
    job_id: jobId, status: "transcribed_pending", campaign_id: "pilot", campaign_item_id: "one",
    filename: "audio.wav", segments_revision: 0, segments_json: segments,
  } }));
  await page.route(`**/editor/${jobId}`, route => route.request().method() === "GET"
    ? route.fulfill({ json: {
      job_id: jobId, revision: 0, segments, original_segments: segments,
      lock: { active: false }, quality_proposal: proposal,
    } })
    : route.fallback());
  const decisions = [];
  await page.route("**/analytics/events", route => {
    const { events } = route.request().postDataJSON();
    decisions.push(...events.filter(event => event.name === "editor_timing_ab_decided"));
    return route.fulfill({ json: { accepted: events.length, rejected: 0 } });
  });
  await page.goto(`/review/${jobId}?timing_ab=1`);
  await page.getByTestId("quality-proposal-disclosure").locator(":scope > summary").click();
  await page.getByRole("button", { name: "Comparar A/B" }).click();
  const comparison = page.getByRole("dialog", { name: "Comparar A/B" });
  await expect(comparison.getByRole("button", { name: "Prefiero A" })).toBeDisabled();
  for (const variant of ["A", "B"]) {
    await comparison.getByRole("button", { name: `Versión ${variant}` }).click();
    await expect(comparison.locator("canvas")).toBeVisible();
    await comparison.getByRole("button", { name: `Reproducir ${variant}` }).click();
    await expect(comparison.getByRole("img", { name: /Cierre de una frase/i })).toBeVisible();
    await expect(comparison.getByRole("button", { name: `Versión ${variant} ✓` })).toBeVisible();
  }
  await comparison.getByRole("button", { name: "Ambas necesitan edición" }).click();
  await expect(comparison.getByText(/Decisión guardada/)).toBeVisible();
  expect(decisions).toHaveLength(1);
  expect(decisions[0]).toMatchObject({
    job_id: jobId,
    properties: {
      proposal_id: "pilot", window_id: "tail", revision: 0, choice: "neither", position_ms: 0, duration_ms: 4000,
    },
  });
  expect(api.saves).toHaveLength(0);
  await page.keyboard.press("Escape");
  await expect(comparison).toHaveCount(0);
  await page.getByRole("button", { name: "Comparar A/B" }).click();
  await comparison.getByRole("button", { name: "Corregir en el editor" }).click();
  await expect(comparison).toHaveCount(0);
  await expect(page.getByRole("tab", { name: /Ajustar tiempos/ })).toHaveAttribute("aria-selected", "true");
  expect(decisions).toHaveLength(1);
  expect(api.saves).toHaveLength(0);
});
