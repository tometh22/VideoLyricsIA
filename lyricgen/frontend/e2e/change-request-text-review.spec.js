import { expect, test } from "@playwright/test";
import { installEditorHarness } from "./editor-harness.js";

test("closes a published typo request only after explicit manual review, without publishing again", async ({ page }) => {
  await installEditorHarness(page, { jobId: "umg-typo-review", role: "admin" });
  const writes = [];
  let closed = false;
  const token = "a".repeat(64);
  const comment = "3:28 me despirerta por las noches";
  await page.route("**/*", async route => {
    const request = route.request();
    const path = new URL(request.url()).pathname;
    const json = body => route.fulfill({ json: body });
    if (path === "/service-status/summary") return json({ status: "operational", incidents: [] });
    if (path === "/admin/stats") return json({ jobs: { pending_review: 0 } });
    if (path === "/admin/change-requests") return json({ pending_count: closed ? 0 : 1, resolved_count: closed ? 1 : 0,
      items: closed ? [] : [{ id: 135, comment,
        delivery: { job_id: "umg-typo-review", portal_id: "argentina", song: "La Flor De Mis Heridas", artist: "Bersuit Vergarabat" },
        publication: { revision: 6, job_status: "done", render_fingerprint: "cut-6", editor_revision: 141,
          render_matches_editor: true, answers_request: true, needs_publish: false, prores_pending: [] },
        workflow: { key: "review", activeStep: 3, label: "Publicado · revisá cada punto del pedido", tone: "action",
          allowed_actions: ["edit", "resolve", "confirm_publication"] },
      }] });
    if (path === "/admin/change-requests/135/review") return json({ segments: [
      { start: 208.05, end: 212.26, text: "me despierta por las noches" },
    ] });
    if (request.method() === "POST" && path.startsWith("/admin/")) {
      const body = request.postDataJSON();
      writes.push({ path, body });
      if (path !== "/admin/change-requests/135/confirm-publication") return route.fulfill({ status: 400, json: {} });
      if (!body.text_review_token) return route.fulfill({ status: 409, json: { detail: {
        code: "requested_text_not_found", review_token: token, missing: ["me despirerta por las noches"],
        message: "Revisá las diferencias contra el video publicado.",
      } } });
      expect(body).toEqual({ reviewed_render_fingerprint: "cut-6", reviewed_editor_revision: 141,
        confirmed_items: 1, text_review_token: token, text_review_reason: "Verifiqué despierta; el pedido tiene un typo." });
      closed = true;
      return json({ ok: true, resolved_by_revision: 6 });
    }
    return route.fallback();
  });
  await page.goto("/admin?section=cambios&change_request_id=135");
  const announcement = page.getByRole("button", { name: /Entendido|Entendí|Cancelar|Cerrar novedades/ }).first();
  if (await announcement.isVisible()) await announcement.click();
  await expect(page.getByRole("list", { name: "En el video cerca de 3:28" })).toContainText("me despierta por las noches");
  await expect(page.getByRole("button", { name: "Dar por resuelto", exact: true })).toBeDisabled();
  await page.getByLabel(`Está en el video: ${comment}`).check();
  await page.getByRole("button", { name: "Dar por resuelto", exact: true }).click();
  const review = page.getByRole("region", { name: "Revisión manual de las diferencias" });
  await expect(review).toContainText("me despirerta por las noches");
  const resolve = review.getByRole("button", { name: "Resolver con revisión manual" });
  await expect(resolve).toBeDisabled();
  await review.getByLabel("Motivo de la revisión manual").fill("Verifiqué despierta; el pedido tiene un typo.");
  await expect(resolve).toBeDisabled();
  await review.getByLabel(/Revisé estas diferencias en el video publicado/).check();
  await expect(resolve).toBeEnabled();
  expect((await review.boundingBox()).width).toBeGreaterThan(240);
  await review.screenshot({ path: "../../.context/umg-text-review.png" });
  await resolve.click();
  await expect(page.getByText(/Pedido resuelto con revisión manual de la versión 6/)).toBeVisible();
  expect(writes).toHaveLength(2);
  expect(writes.every(write => write.path.endsWith("/confirm-publication"))).toBe(true);
});
