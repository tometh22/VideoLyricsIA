import { expect, test } from "@playwright/test";
import { installEditorHarness } from "./editor-harness.js";

// Un pedido corregido a medias (estribillo arreglado una sola vez) no se
// cierra con un clic: el punto ✘ pide una justificación interna, que viaja
// aparte de la nota que ve el cliente.
test("a half-fixed request cannot be closed until its failing point is justified", async ({ page }) => {
  const jobId = "e2e-verify-142";
  await installEditorHarness(page, { jobId, role: "admin" });
  const resolveBodies = [];
  await page.route("**/*", async route => {
    const request = route.request();
    const path = new URL(request.url()).pathname;
    const json = body => route.fulfill({ json: body });
    if (path === "/service-status/summary") return json({ status: "operational", incidents: [] });
    if (path === "/admin/stats") return json({ jobs: { pending_review: 0 } });
    if (path === "/admin/change-requests") return json({ proposal_enabled: false,
      pending_count: 1, resolved_count: 0, items: [{ id: 142,
        comment: "2:39 Vení sumate a la ronda de los martes // 2:49 Vení sumate a la ronda de los martes",
        submitted_at: "2026-10-02T12:00:00Z",
        recurrences: [{ point_id: "p-b", request_id: 120, excerpt: "2:49 Vení sumate" }],
        delivery: { id: 9, job_id: jobId, portal_id: "argentina", song: "Canción QA", artist: "QA" },
        publication: { revision: 2, job_status: "done", render_fingerprint: "render-7", editor_revision: 7,
          render_matches_editor: true, needs_publish: false, prores_pending: [] },
        workflow: { key: "review", activeStep: 3, label: "Publicación registrada · revisar pedido",
          detail: "Verificá lo solicitado antes de cerrar con un motivo.", tone: "action", allowed_actions: ["resolve"] },
      }] });
    if (path === "/admin/change-requests/142/verification") return json({ enabled: true,
      recurrences: [{ point_id: "p-b", request_id: 120, excerpt: "2:49 Vení sumate" }],
      verification: { revision: 7, counts: { ok: 1, fail: 1, manual: 0 }, points: [
        { id: "p-a", kind: "text", status: "ok", reason: "found_near", excerpt: "2:39 Vení sumate a la ronda de los martes",
          line: { start: 159, end: 162, text: "Vení sumate a la ronda de los martes" } },
        { id: "p-b", kind: "text", status: "fail", reason: "found_only_elsewhere", excerpt: "2:49 Vení sumate a la ronda de los martes",
          line: { start: 169, end: 172, text: "Ven y súmate a la ronda de los martes" } },
      ] } });
    if (path === "/admin/change-requests/142/resolve" && request.method() === "POST") {
      resolveBodies.push(request.postDataJSON());
      return json({ ok: true, resolved_at: "2026-10-02T13:00:00Z", updated_at: "2026-10-02T13:00:00Z" });
    }
    return route.fallback();
  });

  await page.goto("/admin?section=cambios&change_request_id=142");
  const announcement = page.getByRole("button", { name: /Entendido|Entendí|Cancelar|Cerrar novedades/ }).first();
  if (await announcement.isVisible()) await announcement.click();

  const check = page.getByRole("region", { name: "Verificación automática del pedido" });
  await expect(check.getByRole("note")).toContainText("Reincidencia");
  await expect(check.getByLabel(/No verificado: 2:49/)).toContainText("Ven y súmate a la ronda de los martes");
  await expect(check.getByLabel(/^Verificado: 2:39/)).toBeVisible();

  // Ya publicado: cerrar con nota es la acción principal y el formulario
  // está abierto. Con la nota escrita, el ✘ sin justificar sigue bloqueando.
  await page.getByLabel("Motivo del cierre sin publicar").fill("Corregido en el video publicado");
  const confirm = page.getByRole("button", { name: "Dar por resuelto", exact: true });
  await expect(confirm).toBeDisabled();

  await check.getByLabel(/Justificación interna: 2:49/).fill("El cliente pidió dejar esa repetición como se canta");
  await expect(confirm).toBeEnabled();
  await confirm.click();
  await expect.poll(() => resolveBodies.length).toBe(1);
  expect(resolveBodies[0]).toEqual({
    resolution_note: "Corregido en el video publicado",
    point_overrides: { "p-b": "El cliente pidió dejar esa repetición como se canta" },
  });
});
