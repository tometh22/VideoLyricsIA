import { expect, test } from "@playwright/test";
import { installEditorHarness } from "./editor-harness.js";

// Reclamos UMG 29-09-2026: "falta 'dormite ya'" y "falta el QUE del
// principio". El servidor marca lo que se escucha y no está en la letra; el
// editor exige decidir cada alerta antes de aprobar.
const DORMITE = {
  id: "dormite", key: "dormite ya@2", text: "dormite ya", start: 1.5, end: 1.8,
  sources: "both", action: "insert", line_segment_id: "seg-b", anchor_before: "linea", anchor_after: "",
  insert_at_word: 2, placement: "after",
};
const QUE = {
  id: "que", key: "que@3", text: "que", start: 2.8, end: 2.95,
  sources: "both", action: "insert", line_segment_id: "seg-d", anchor_before: "", anchor_after: "cuarta",
  insert_at_word: 0, placement: "before",
};

const SEGMENTS = [
  { _id: "line-a", segment_id: "seg-a", start: 0.4, end: 1.0, text: "Primera línea" },
  { _id: "line-b", segment_id: "seg-b", start: 1.2, end: 1.8, text: "Segunda línea" },
  { _id: "line-c", segment_id: "seg-c", start: 2.0, end: 2.6, text: "Tercera línea" },
  { _id: "line-d", segment_id: "seg-d", start: 2.8, end: 3.4, text: "Cuarta línea" },
];

// Imita heard_words.py: una alerta desaparece cuando la letra ya la tiene o
// cuando alguna línea guarda su clave como "no se canta".
function heardWords(segments) {
  const dismissed = new Set(segments.flatMap((segment) => segment.heard_dismissed || []));
  const text = (id) => String(segments.find((segment) => segment.segment_id === id)?.text || "").toLowerCase();
  return [
    !text("seg-b").includes("dormite ya") && !dismissed.has(DORMITE.key) ? DORMITE : null,
    !text("seg-d").startsWith("que") && !dismissed.has(QUE.key) ? QUE : null,
  ].filter(Boolean);
}

test("missing heard words are decided inside the editor before approval", async ({ page }) => {
  const harness = await installEditorHarness(page, { editorV2: true, segments: SEGMENTS, heardWords });
  await harness.open();

  const panel = page.getByTestId("heard-words-panel");
  await expect(panel).toContainText("2 por decidir");
  await expect(panel).toContainText("Segunda línea dormite ya");
  await expect(panel).toContainText("Que cuarta línea");
  await panel.screenshot({ path: "test-results/heard-words-panel.png" });
  await page.screenshot({ path: "test-results/heard-words-editor.png" });

  // Aprobar no avanza: lleva al panel.
  await page.getByRole("button", { name: /Aprobar y generar/i }).click();
  await expect(panel).toBeFocused();
  expect(harness.approvals).toHaveLength(0);

  // Un click agrega la frase en su lugar.
  await panel.getByRole("button", { name: "Agregar" }).first().click();
  await expect(page.locator('input[aria-label="Letra de la línea 2"]')).toHaveValue("Segunda línea dormite ya");
  await expect(panel).toContainText("1 por decidir");

  // Una tecla descarta la que no se canta, y queda guardado en la línea.
  await panel.press("n");
  await expect(panel).toHaveCount(0);
  await expect.poll(() => (harness.saves.at(-1)?.segments || [])
    .flatMap((segment) => segment.heard_dismissed || [])).toEqual(["que@3"]);

  await page.getByRole("button", { name: /Aprobar y generar/i }).click();
  await expect.poll(() => harness.approvals.length).toBe(1);
});
