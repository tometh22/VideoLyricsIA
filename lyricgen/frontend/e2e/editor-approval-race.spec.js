import { test, expect } from "@playwright/test";
import { installEditorHarness } from "./editor-harness";

async function openDeferredApproval(page) {
  const jobId = "approvalrace";
  const segments = [{ segment_id: "line-a", start: 0, end: 2, text: "Letra lista para aprobar" }];
  const editor = await installEditorHarness(page, { jobId, role: "admin", editorV2: true, segments });
  let release;
  const responseGate = new Promise(resolve => { release = resolve; });
  const approvalRequests = [];
  await page.route("**/*", async route => {
    const request = route.request(), path = new URL(request.url()).pathname;
    if (path === `/status/${jobId}`) return route.fulfill({ json: {
      job_id: jobId, filename: "approval.wav", status: "transcribed_pending", campaign_id: "audit4", campaign_item_id: "item-a",
      segments_revision: 0, segments_json: segments,
    } });
    if (path.endsWith(`/jobs/${jobId}/approve-lyrics`)) {
      approvalRequests.push(request.postDataJSON());
      const response = await responseGate;
      return route.fulfill(response);
    }
    return route.fallback();
  });
  const announcement = page.getByRole("dialog", { name: "Nuevo editor de letras" });
  await page.addLocatorHandler(announcement, async () => announcement.getByRole("button", { name: "Cancelar" }).click());
  await page.goto(`/review/${jobId}?return_to=%2Fcampaigns%2Faudit4`);
  await expect(page.getByTestId("lyrics-editor")).toHaveAttribute("aria-busy", "false");
  return { editor, approvalRequests, release };
}

test("an in-flight approval blocks text, global undo and editor exits, then restores focus after failure", async ({ page }) => {
  const harness = await openDeferredApproval(page);
  const input = page.getByLabel("Letra de esta línea", { exact: true });
  await input.fill("Corrección confirmada");
  const approve = page.getByRole("button", { name: "Aprobar letra y timing", exact: true });
  await approve.click();
  await expect.poll(() => harness.approvalRequests.length).toBe(1);
  await expect(page.getByTestId("lyrics-editor")).toHaveAttribute("inert", "");
  await expect(page.getByRole("status").filter({ hasText: "Aplicando cambios" })).toBeVisible();
  // Real DOM focus and keyboard events: synthetic fill() alone would bypass
  // native inert and cannot prove that a human is prevented from editing.
  await input.evaluate(node => node.focus());
  await expect(input).not.toBeFocused();
  await page.keyboard.type("Texto que no debe entrar");
  await page.keyboard.press("ControlOrMeta+z");
  await expect(input).toHaveValue("Corrección confirmada");
  await page.getByRole("button", { name: "Guardar borrador y salir", exact: true }).click();
  await expect(page).toHaveURL(/\/review\/approvalrace/);
  const failed = page.waitForResponse(response => response.url().endsWith("/approve-lyrics"));
  harness.release({ status: 503, json: { detail: "temporary approval failure" } });
  await failed;
  await expect(page.getByTestId("lyrics-editor")).not.toHaveAttribute("inert", "");
  await expect(approve).toBeFocused();
  await input.fill("Nueva corrección después del error");
  await expect.poll(() => harness.editor.saves.at(-1)?.segments[0]?.text).toBe("Nueva corrección después del error");
  expect(harness.approvalRequests).toHaveLength(1);
});

test("a successful approval response cannot navigate back over a newer sidebar route", async ({ page }) => {
  const harness = await openDeferredApproval(page);
  await page.getByRole("button", { name: "Aprobar letra y timing", exact: true }).click();
  await expect.poll(() => harness.approvalRequests.length).toBe(1);
  await page.getByRole("link", { name: "Inicio", exact: true }).click();
  await expect(page).toHaveURL(/\/dashboard$/);
  const completed = page.waitForResponse(response => response.url().endsWith("/approve-lyrics"));
  harness.release({ status: 200, json: { status: "lyrics_approved", approved_version_id: "approved-audit4" } });
  await completed;
  await page.waitForTimeout(250);
  await expect(page).toHaveURL(/\/dashboard$/);
  await expect(page.getByTestId("lyrics-editor")).toHaveCount(0);
});
