import { expect, test } from "@playwright/test";
import { DEFAULT_SEGMENTS, EDITOR_JOB_ID, installEditorHarness } from "./editor-harness.js";

const key = `genly_editor_draft:e2e-team:e2e-user:${EDITOR_JOB_ID}`;
async function seedAndOpen(page, raw) {
  await page.evaluate(({ key, raw }) => localStorage.setItem(key, raw), { key, raw });
  await page.reload();
  await page.getByRole("button", { name: /4 Lyrics/ }).click();
}
test("local recovery preserves server state across reload, return, and explicit discard", async ({ page }) => {
  const harness = await installEditorHarness(page, { editorV2: true, preserveStorage: true });
  await harness.open();
  const rows = DEFAULT_SEGMENTS.map(s => ({ ...s, start: +s.start, end: +s.end }));
  const raw = JSON.stringify({ segments: rows.map((s,i) => i ? s : { ...s, text: "Cambio local pendiente" }), base_revision: 0 });
  await seedAndOpen(page, raw);
  let dialog = page.getByRole("dialog", { name: "Revisar copia local" });
  await expect(dialog).toContainText("Cambio local pendiente");
  await expect(dialog).toContainText("Primera línea");
  await expect(page.getByText("No se pudo guardar", { exact: true })).toHaveCount(0);
  expect(harness.saves).toHaveLength(0); expect(harness.approvals).toHaveLength(0);
  await page.reload(); await page.getByRole("button", { name: /4 Lyrics/ }).click();
  await expect(dialog).toBeVisible();
  await dialog.getByRole("button", { name: "Descartar copia local y usar guardada" }).click();
  await expect(dialog).toHaveCount(0);
  await expect(page.locator('input[aria-label="Letra de la línea 1"]')).toHaveValue("Primera línea");
  expect(harness.saves).toHaveLength(0); expect(harness.approvals).toHaveLength(0);
  await seedAndOpen(page, '{"segments":');
  await expect(dialog).toContainText("No pudimos interpretar");
  await page.screenshot({ path: "test-results/local-draft-unreadable.png", fullPage: true });
  await dialog.getByRole("button", { name: "Volver sin cambiar nada" }).click();
  await page.goto(`/videos/${EDITOR_JOB_ID}/edit-lyrics`);
  await page.getByRole("button", { name: /4 Lyrics/ }).click();
  await expect(dialog).toContainText("No pudimos interpretar");
  expect(await page.evaluate(key => localStorage.getItem(key), key)).toBe('{"segments":');
  expect(harness.saves).toHaveLength(0); expect(harness.approvals).toHaveLength(0);
});
