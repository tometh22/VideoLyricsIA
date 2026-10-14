import { expect, test } from "@playwright/test";
import { installEditorHarness } from "./editor-harness.js";

test("resolves an already published correction by human review even with stale editor/master state", async ({ page }) => {
  await installEditorHarness(page, { jobId: "umg-human-review", role: "admin" });
  const writes = [];
  let closed = false;
  const token = "b".repeat(64);
  const comment = "Sacar el arma que aparece en el video";
  await page.route("**/*", async route => {
    const request = route.request();
    const path = new URL(request.url()).pathname;
    const json = body => route.fulfill({ json: body });
    if (path === "/service-status/summary") return json({ status: "operational", incidents: [] });
    if (path === "/admin/stats") return json({ jobs: { pending_review: 0 } });
    if (path === "/published-3.mp4") return route.fulfill({ contentType: "video/mp4", body: "" });
    if (path === "/admin/change-requests") return json({ pending_count: closed ? 0 : 1, resolved_count: closed ? 1 : 0,
      items: closed ? [] : [{ id: 170, comment,
        delivery: { job_id: "umg-human-review", portal_id: "argentina", song: "Amor Suicida", artist: "2 minutos", published_video_url: "/published-3.mp4" },
        publication: { revision: 3, job_status: "done", render_fingerprint: "candidate", editor_revision: 9,
          render_matches_editor: false, needs_publish: true, prores_pending: ["umg_master"] },
        published_review: { review_token: token, published_revision: 3 },
        workflow: { key: "render", activeStep: 2, label: "Aprobar letra y generar video", tone: "action",
          allowed_actions: ["edit", "resolve", "approve_in_editor"] },
      }] });
    if (request.method() === "POST" && path.startsWith("/admin/")) {
      const body = request.postDataJSON();
      writes.push({ path, body });
      expect(path).toBe("/admin/change-requests/170/confirm-publication");
      expect(body).toEqual({ reviewed_render_fingerprint: "candidate", reviewed_editor_revision: 9,
        confirmed_items: 1, publication_review_token: token,
        publication_review_reason: "Verifiqué el fondo de la versión publicada y el pedido está atendido.", confirmed_published_review: true });
      closed = true;
      return json({ ok: true, human_review: true, resolved_by_revision: 3 });
    }
    return route.fallback();
  });
  await page.goto("/admin?section=cambios&change_request_id=170");
  const announcement = page.getByRole("button", { name: /Entendido|Entendí|Cancelar|Cerrar novedades/ }).first();
  if (await announcement.isVisible()) await announcement.click();
  await page.getByRole("button", { name: "Resolver con revisión humana" }).click();
  const review = page.getByRole("region", { name: "Revisión humana de la publicación" });
  await expect(review.getByLabel("Versión publicada para revisión humana")).toHaveAttribute("src", "/published-3.mp4");
  const confirm = review.getByRole("button", { name: "Confirmar pedido resuelto" });
  await expect(confirm).toBeDisabled();
  await review.getByLabel(`Verificado en la publicación: ${comment}`).check();
  await review.getByLabel("Motivo del cierre tras revisión humana").fill("Verifiqué el fondo de la versión publicada y el pedido está atendido.");
  await expect(confirm).toBeDisabled();
  await review.getByLabel(/Revisé esta versión publicada/).check();
  await expect(confirm).toBeEnabled();
  expect(writes).toHaveLength(0);
  await review.screenshot({ path: "../../.context/umg-human-review.png" });
  await confirm.click();
  await expect(page.getByText(/Pedido resuelto con revisión humana de la versión 3/)).toBeVisible();
  expect(writes).toHaveLength(1);
});
