import { expect, test } from "@playwright/test";
import { installEditorHarness } from "./editor-harness.js";

// Revisión rápida (pedidos UMG #112-#131, 29-09-2026): los puntos llegan con
// su arreglo y el revisor los resuelve de a uno, con el teclado, antes de
// que "Aprobar" deje avanzar.
const SEGMENTS = [
  { _id: "line-a", segment_id: "seg-a", start: 0.4, end: 1.0, text: "¿Cuando vuelva?" },
  { _id: "line-b", segment_id: "seg-b", start: 1.2, end: 1.8, text: "Tu garantía de reloco se fundió" },
  { _id: "line-c", segment_id: "seg-c", start: 2.0, end: 2.6, text: "Vos sos Román" },
  { _id: "line-d", segment_id: "seg-d", start: 2.8, end: 3.4, text: "Con tus pasos" },
];

function item(id, required, title, segmentId, start, before, after, fix, extra = {}) {
  return {
    id, kind: extra.kind || "heard_different", required, title, why: extra.why || "Coinciden Gemini y el testigo",
    action: extra.action || "Corregir", dismiss: extra.dismiss || "Está bien así", keys: [id],
    start, end: start + 0.5, sources: ["gemini", "witness"], alternatives: extra.alternatives || [],
    occurrences: [{ line_segment_id: segmentId, start, end: start + 0.5, before, after, fix }],
  };
}

// Imita lyric_review.py sobre la letra que el editor va guardando.
function lyricReview(segments, official) {
  const text = (id) => String(segments.find((s) => s.segment_id === id)?.text || "");
  const dismissed = new Set(segments.flatMap((s) => s.qa_dismissed || []));
  const items = [];
  const add = (value) => { if (!value.keys.every((key) => dismissed.has(key))) items.push(value); };
  if (/[¿?]/.test(text("seg-a"))) {
    add(item("q", true, "Signos de pregunta", "seg-a", 0.4, text("seg-a"), text("seg-a").replace(/[¿?]/g, ""),
      { type: "punctuation", mode: "remove_question" }, { kind: "question_marks", why: "«Cuando» sin tilde no pregunta" }));
  }
  if (!text("seg-b").includes("dormite")) {
    add(item("dormite", true, "Falta texto", "seg-b", 1.5, text("seg-b"), `${text("seg-b")} dormite ya`,
      { type: "insert", text: "dormite ya", anchor_before: "fundio", anchor_after: "", insert_at_word: 6 },
      { kind: "missing", action: "Agregar", dismiss: "No se canta", why: "Coinciden la máquina y el testigo" }));
  }
  if (/\btus\b/.test(text("seg-d"))) {
    add(item("tres", true, "Se escucha distinto", "seg-d", 2.8, text("seg-d"), text("seg-d").replace("tus", "tres"),
      { type: "replace", find: "tus", replace: "tres" }));
  }
  if (text("seg-c").includes("Román")) {
    add(item("roman", false, "Se escucha distinto", "seg-c", 2.0, text("seg-c"), text("seg-c").replace("Román", "lo más"),
      { type: "replace", find: "Román", replace: "lo más" },
      { why: "Lo indica la letra oficial", alternatives: [{ replace: "la mar", why: "Lo indica el testigo", sources: ["witness"] }] }));
  }
  return {
    schema: "lyric-review-v1", mode: "enforce", items,
    required_count: items.filter((i) => i.required).length,
    suggested_count: items.filter((i) => !i.required).length,
    sources: { witness: true, gemini: true, official: Boolean(official), official_origin: official ? "operator" : "" },
    risk: { level: "normal", reasons: [] },
  };
}

test("quick review is resolved from the keyboard before approval", async ({ page }) => {
  const harness = await installEditorHarness(page, { editorV2: true, segments: SEGMENTS, lyricReview });
  await harness.open();

  const panel = page.getByTestId("lyric-review-panel");
  await expect(panel).toContainText("3 para decidir antes de aprobar");
  await expect(panel).toContainText("Tu garantía de reloco se fundió dormite ya");
  await expect(panel).toContainText("Ver 1 sugerencia (no bloquean)");
  await panel.screenshot({ path: "test-results/lyric-review-panel.png" });
  await page.screenshot({ path: "test-results/lyric-review-editor.png" });

  // Aprobar no avanza: lleva al panel.
  await page.getByRole("button", { name: /Aprobar y generar/i }).click();
  await expect(panel).toBeFocused();
  expect(harness.approvals).toHaveLength(0);

  // A aplica y pasa al siguiente; N descarta.
  await panel.press("a");
  await expect(page.locator('input[aria-label="Letra de la línea 1"]')).toHaveValue("Cuando vuelva");
  await panel.press("a");
  await expect(page.locator('input[aria-label="Letra de la línea 2"]')).toHaveValue("Tu garantía de reloco se fundió dormite ya");
  await panel.press("n");
  await expect.poll(() => (harness.saves.at(-1)?.segments || [])
    .flatMap((segment) => segment.qa_dismissed || [])).toEqual(["tres"]);
  await expect(panel).toContainText("todo listo para aprobar");

  // La sugerencia queda a la vista con su otra opción, sin bloquear.
  await panel.getByRole("button", { name: "la mar" }).click();
  await expect(page.locator('input[aria-label="Letra de la línea 3"]')).toHaveValue("Vos sos la mar");

  // La letra oficial pegada se usa sólo para comparar.
  await panel.getByRole("button", { name: "Pegar letra oficial" }).click();
  await panel.getByLabel("Letra oficial").fill("Cuando vuelvas\nTu garantía de reloj se fundió, dormite ya");
  await panel.getByRole("button", { name: "Comparar con esta letra" }).click();
  await expect(panel).toContainText("letra oficial (pegada)");
  await panel.screenshot({ path: "test-results/lyric-review-done.png" });

  await page.getByRole("button", { name: /Aprobar y generar/i }).click();
  await expect.poll(() => harness.approvals.length).toBe(1);
});
