import { expect, test } from "@playwright/test";
import { createSyntheticWav, installEditorHarness } from "./editor-harness.js";

test('reviews saved lyrics, confirms one render, then publishes to Chile without visiting the editor', async ({ page }) => {
  await installEditorHarness(page, { jobId: 'job-85', role: 'admin' });
  let stage = 'saved';
  let polls = 0;
  const writes = [];
  const text = 'Respirarse, emborrachar, morir y seguir viviendo';
  await page.route('**/*', async route => {
    const request = route.request();
    const path = new URL(request.url()).pathname;
    const json = body => route.fulfill({ json: body });
    if (path === '/service-status/summary') return json({ status: 'operational', incidents: [] });
    if (path === '/admin/stats') return json({});
    if (path === '/admin/change-requests/85/review') return json({ change_request_id: 85, editor_revision: 58,
      comment: 'Usar frase completa', segments: [{ start: 144.26, end: 149.58, text }] });
    if (path === '/admin/change-requests') {
      if (stage === 'rendering' && ++polls >= 2) stage = 'rendered';
      return json({ pending_count: stage === 'published' ? 0 : 1, proposal_enabled: true,
        items: stage === 'published' ? [] : [{ id: 85, comment: 'Usar frase completa',
          proposal: { id: 'p85', status: 'applied' }, delivery: { job_id: 'job-85', portal_id: 'chile', song: 'Prueba', artist: 'Test' },
          publication: { job_status: stage === 'rendering' ? 'editing' : 'pending_review',
            can_render: stage !== 'rendering', render_matches_editor: stage === 'rendered',
            needs_publish: stage === 'rendered', editor_revision: 58, render_fingerprint: 'render58', prores_pending: [] } }] });
    }
    if (request.method() === 'POST' && (path.endsWith('/85/render') || path.includes('/deliveries/from-job/'))) {
      writes.push({ path, body: request.postDataJSON() });
      stage = path.endsWith('/85/render') ? 'rendering' : 'published';
      return json(stage === 'rendering' ? { status: 'editing' } : { content_changed: true, revision: 2, resolved_change_requests: [85] });
    }
    return route.fallback();
  });
  await page.goto('/admin?section=cambios&change_request_id=85');
  const announcement = page.getByRole('button', { name: /Entendido|Entendí|Cancelar|Cerrar novedades/ }).first();
  if (await announcement.isVisible()) await announcement.click();
  await page.getByRole('button', { name: 'Revisar y confirmar render', exact: true }).click();
  const dialog = page.getByRole('dialog', { name: 'Revisar letra y confirmar render' });
  await expect(dialog).toContainText(text);
  const approve = dialog.getByRole('button', { name: 'Aprobar y re-renderizar' });
  await expect(approve).toBeDisabled();
  await dialog.getByRole('checkbox').check();
  await approve.click();
  await expect(page.getByRole('button', { name: 'Publicar actualización' })).toBeVisible({ timeout: 12000 });
  expect(writes).toEqual([{ path: '/admin/change-requests/85/render', body: { editor_revision: 58 } }]);
  page.once('dialog', dialog => dialog.accept());
  await page.getByRole('button', { name: 'Publicar actualización' }).click();
  await expect(page.getByRole('status', { name: 'Resultado del pedido' })).toContainText('Publicada la versión 2');
  expect(writes[1].body).toEqual({ portal_id: 'chile', change_request_id: 85,
    reviewed_render_fingerprint: 'render58', reviewed_editor_revision: 58 });
  expect(page.url()).toContain('/admin?');
});

test("keeps the player running through polling, prepares without publishing, and opens saved-change verification", async ({ page }) => {
  await installEditorHarness(page, { jobId: "job-85", role: "admin" });
  let polls = 0;
  const mutations = [];
  const media = createSyntheticWav({ durationSeconds: 30 });
  const segment = { _id: "line-1", start: 1, end: 4, text: "Respirarse, emborrachar, morir y seguir viviendo" };
  await page.route("**/*", async route => {
    const req = route.request();
    const url = new URL(req.url());
    const json = body => route.fulfill({ json: body });
    if (url.pathname === "/e2e/request-video.wav") return route.fulfill({ contentType: "audio/wav", body: media });
    if (url.pathname === "/service-status/summary") return json({ status: "operational", incidents: [] });
    if (url.pathname === "/admin/stats") return json({});
    if (url.pathname === "/admin/change-requests") {
      polls += 1;
      return json({ pending_count: 1, resolved_count: 0, proposal_enabled: true, proposal_apply_enabled: true,
        items: [{ id: 85, comment: "1:02: " + segment.text, submitted_at: "2026-09-17T00:00:00Z",
          proposal: { id: "proposal-85", status: "applied", applied_revision: 3, applicable_count: 1 },
          publication: { job_status: "pending_review", prores_configured: true, prores_pending: ["umg_master"], revision: 1 },
          delivery: { job_id: "job-85", artist: "Test", song: "Pedido 85", portal_id: "argentina",
            video_url: `/e2e/request-video.wav?X-Amz-Date=${polls}` } }] });
    }
    if (url.pathname === "/admin/change-requests/85/proposals/current") return json({ proposal: {
      id: "proposal-85", status: "applied", applied_revision: 3, base_revision: 2,
      operations: [{ id: "op-1", kind: "replace_text", status: "applied", applicable: true,
        current_segments: [{ ...segment, text: "Respirarse emborrachar" }], proposed_segments: [segment] }],
      // The editor can regenerate local ids on a later save. The comparison
      // must still verify the actual row, rather than demanding reapplication.
      lyrics_context: { revision: 4, segments: [{ ...segment, _id: "new-editor-local-id" }] },
    } });
    if (url.pathname === "/status/job-85") return json({ job_id: "job-85", status: "pending_review",
      umg_spec: { frame_size: "HD", fps: 29.97, prores_profile: 3 } });
    if (req.method() === "POST" && (url.pathname.startsWith("/enable-prores/") || url.pathname.includes("/deliveries/from-job/"))) {
      mutations.push({ path: url.pathname, body: req.postDataJSON() });
      return json({ ok: true, enqueued: ["umg_master"], status: "queued" });
    }
    return route.fallback();
  });
  await page.addLocatorHandler(page.getByRole("dialog"), async () => {
    await page.getByRole("dialog").getByRole("button", { name: "Cancelar" }).click();
  });
  await page.goto("/admin?section=cambios&change_request_id=85");
  const video = page.getByLabel("Video de Test — Pedido 85", { exact: true });
  await expect(video).toBeVisible();
  await expect.poll(() => video.evaluate(el => el.readyState)).toBeGreaterThanOrEqual(2);
  await video.evaluate(el => { el.muted = true; return el.play(); });
  const source = await video.getAttribute("src");
  await page.getByRole("button", { name: "Actualizar archivo profesional", exact: true }).click();
  await expect(page.getByRole("status", { name: "Estado de la acción" })).toContainText("Actualización del .mov encolada");
  await expect.poll(() => video.evaluate(el => el.currentTime), { timeout: 12_000 }).toBeGreaterThan(6);
  expect(await video.evaluate(el => el.paused)).toBe(false);
  expect(await video.getAttribute("src")).toBe(source);
  expect(polls).toBeGreaterThan(2);
  expect(mutations).toEqual([{ path: "/enable-prores/job-85",
    body: { umg_frame_size: "HD", umg_fps: "29.97", umg_prores_profile: "3" } }]);
  await page.getByRole("button", { name: "Ver propuesta y letra guardada" }).click();
  await expect(page.getByRole("region", { name: "Verificación de la letra guardada" })).toContainText(segment.text);
  await expect(page.getByText("Coincide con el pedido", { exact: true })).toBeVisible();
  await expect(page.getByRole("button", { name: "Volver a analizar con la letra actual" })).toBeDisabled();
});
