// Stateful campaign API mocked at the browser boundary. Stages are derived
// like campaign_pipeline.py so approvals, generation and delivery move songs
// between pipeline tabs exactly as in production.

export function song(n, stage, extra = {}) {
  const video = ["qc", "approved", "delivered"].includes(stage);
  return {
    item_id: `item-${n}`, ordinal: n, title: `Canción ${n}`, artist: "Otro artista", technical_code: `ARUM${n}`, filename: `audio-${n}.wav`,
    duration_seconds: 180, stage, phase: stage, job_id: `song-${n}`, current_job_id: `song-${n}`, current_is_variant: false,
    current_status: stage === "qc" ? "pending_review" : ["approved", "delivered"].includes(stage) ? "done" : "",
    upload_state: "uploaded", metadata_error: null, has_video: video, video_count: video ? 1 : 0, versions: [],
    approved_at: ["approved", "delivered"].includes(stage) ? "2026-09-15T00:00:00Z" : null,
    portals: stage === "delivered" ? ["argentina"] : [], portal_outdated: false, published_other_version: false, pending_change_requests: 0,
    ...extra,
  };
}

export async function installCampaignApi(page, { id = "workflow", name = "Campaña de prueba", kind = "lyric_video", songs, canManage = true, creative = {}, reviewRow = () => ({}), onRequest } = {}) {
  const state = {
    campaign: { id, name, status: "active", kind, created_by: 1, expected_count: songs.length, registered_count: songs.length, default_render_params: {}, created_at: "2026-09-01T00:00:00Z", reviewer_campaign_status: null },
    songs: songs.map((value) => ({ ...value })),
    calls: [], deliveries: [], approvals: [], generations: [],
    plan: { revision: 0 },
  };
  const counts = () => {
    const result = { audio: 0, lyrics: 0, ready: 0, rendering: 0, qc: 0, approved: 0, delivered: 0, attention: 0, discarded: 0 };
    state.songs.forEach((value) => { result[value.stage] += 1; });
    return result;
  };
  const find = (key) => state.songs.find((value) => value.item_id === key || value.job_id === key || value.current_job_id === key);
  const base = `/batch/campaigns/${id}`;
  await page.route("**/*", async (route) => {
    const request = route.request();
    const url = new URL(request.url());
    const path = url.pathname;
    const method = request.method();
    const json = (body, status = 200) => route.fulfill({ status, contentType: "application/json", body: JSON.stringify(body) });
    if (!["localhost", "127.0.0.1"].includes(url.hostname)) return route.fallback();
    state.calls.push({ path, method });
    // onRequest returns true once it has fulfilled the route itself.
    const handled = await onRequest?.({ route, request, path, method, url, state, json: async (body, status) => { await json(body, status); return true; } });
    if (handled) return undefined;
    if (path === "/service-status/summary") return json({ status: "operational", incidents: [] });
    if (path === "/batch/campaigns/access") return json({ enabled: true });
    if (path === "/batch/campaigns" && method === "GET") return json({ items: [{ ...state.campaign, pipeline: { counts: counts(), total: state.songs.length } }] });
    if (path === base && method === "GET") return json(state.campaign);
    if (path === base && method === "PATCH") { Object.assign(state.campaign, request.postDataJSON()); return json(state.campaign); }
    if (path === `${base}/pipeline`) return json({ campaign_id: id, kind, total: state.songs.length, active_total: state.songs.length - counts().discarded, counts: counts(), flags: {}, portal_status_available: true, can_manage: canManage, items: state.songs });
    if (path === `${base}/review-queue`) {
      const items = state.songs.map((value, index) => ({
        item_id: value.item_id, job_id: value.job_id, title: value.title, artist: value.artist,
        state: value.stage === "lyrics" ? "ready" : value.stage === "discarded" ? "discarded" : "approved",
        can_discard: value.stage === "lyrics", review_priority: "standard", timing_evidence: [], review_reasons: [], reference: { available: true },
        version: "studio", ordinal: index + 1, ...reviewRow(value),
      }));
      return json({ items, pages: 1, total: items.length, campaign_totals: { songs: items.length, approved_today: 0 }, review_minutes_today: { average: 6.1, songs: 4 } });
    }
    if (path === `${base}/creative`) {
      return json({ plan: state.plan, can_manage: canManage, operations: [], veo_model: "veo-3.1-lite-generate-001", veo_models: [{ id: "veo-3.1-lite-generate-001", label: "Veo Lite" }],
        fields: creative.fields || {
          font: { label: "Tipografía", group: "Letra", kind: "select", options: ["", "anton", "poppins-bold"] },
          effect: { label: "Efecto", group: "Movimiento y efectos", kind: "select", options: ["", "bokeh", "rain"] },
          movement_style: { label: "Movimiento", group: "Movimiento y efectos", kind: "select", options: ["", "foto-parallax", "estandar"] },
        },
        items: state.songs.map((value) => ({ id: value.item_id, job_id: value.job_id, title: value.title, artist: value.artist,
          status: value.stage === "ready" ? "lyrics_approved" : value.stage === "rendering" ? "queued" : "transcribed_pending", discarded: value.stage === "discarded",
          settings: value.settings || {}, assignment: value.assignment || {} })) });
    }
    if (path === `${base}/creative/report`) {
      return json({ campaign_id: id, name: state.campaign.name, at: "2026-09-29T12:00:00Z", contract: state.plan.contract || {}, groups: state.plan.groups ? state.plan.groups.map((group) => ({ id: group.id, name: group.name, target: 1, target_weight: group.weight, assigned: 1, generated: 0, approved: 0, verified: 0, delivered: 0 })) : [], history: [],
        videos: state.songs.filter((value) => value.has_video).map((value) => ({ job_id: value.current_job_id, title: value.title, artist: value.artist, status: value.current_status, approved_at: value.approved_at, evidence: { video_sha256: `sha-${value.item_id}` }, assignment: {}, compliance: "pending", video_url: `/download/${value.current_job_id}/video` })) });
    }
    if (path === `${base}/creative/preview`) {
      const body = request.postDataJSON();
      state.lastPreview = body;
      return json({ preview_id: "frozen", counts: body.groups.map((_, index) => (index === 0 ? Math.ceil(body.item_ids.length / body.groups.length) : Math.floor(body.item_ids.length / body.groups.length))), rounded: body.groups.length > 1 && body.item_ids.length % body.groups.length !== 0, skipped: [],
        changes: body.item_ids.map((itemId, index) => ({ item_id: itemId, artist: "A", title: itemId, group: body.groups[index % body.groups.length].name, before: {}, after: body.groups[index % body.groups.length].settings })) });
    }
    if (path === `${base}/creative/apply`) {
      const body = state.lastPreview;
      state.plan = { revision: state.plan.revision + 1, groups: body.groups, mode: body.mode, contract: body.contract ? { agreement: body.agreement, rounding_note: body.rounding_note, revision: state.plan.revision + 1, item_ids: body.item_ids, mode: body.mode } : undefined };
      body.item_ids.forEach((itemId, index) => { const value = find(itemId); value.settings = body.groups[index % body.groups.length].settings; value.assignment = { revision: state.plan.revision, group_name: body.groups[index % body.groups.length].name }; });
      return json({ revision: state.plan.revision });
    }
    if (path === "/backgrounds") return json([]);
    if (path.startsWith("/status/")) {
      const value = find(path.split("/").pop());
      if (!value) return route.fallback();
      return json({ job_id: value.job_id, artist: value.artist, song_title: value.title, status: "lyrics_approved", segments_revision: 7, segments_json: [{ start: 0, end: 2, text: "Letra aprobada" }] });
    }
    if (path === "/generate") {
      const body = request.postData() || "";
      state.generations.push(body);
      const jobId = body.match(/name="job_id"\r\n\r\n([^\r]+)/)?.[1];
      const value = find(jobId);
      if (value) value.stage = "rendering";
      return json({ ok: true, job_id: jobId, status: "queued" });
    }
    if (path.startsWith("/approve/")) {
      const value = find(path.split("/").pop());
      state.approvals.push({ job_id: value.current_job_id, ...request.postDataJSON() });
      Object.assign(value, { stage: "approved", current_status: "done", approved_at: "2026-09-29T00:00:00Z" });
      return json({ ok: true, status: "done" });
    }
    if (path === `${base}/deliveries` && method === "POST") {
      const body = request.postDataJSON();
      state.deliveries.push(body);
      body.job_ids.forEach((jobId) => { const value = find(jobId); value.stage = "delivered"; value.portals = [body.destination_portal]; });
      return json({ operation_id: "delivery-1", total_count: body.job_ids.length, status: "queued" });
    }
    if (path === "/batch/delivery-operations/delivery-1") {
      const last = state.deliveries.at(-1) || { job_ids: [], destination_portal: "chile" };
      return json({ operation_id: "delivery-1", status: "completed", destination_portal: last.destination_portal, total_count: last.job_ids.length, sent_count: last.job_ids.length, failed_count: 0, items: [] });
    }
    const itemAction = path.match(new RegExp(`^${base}/items/([^/]+)/(discard|restore)$`));
    if (itemAction) { find(itemAction[1]).stage = itemAction[2] === "discard" ? "discarded" : "lyrics"; return json({ ok: true }); }
    if (path.startsWith("/preview/") && path.endsWith("/video")) return route.fulfill({ status: 302, headers: { location: "/escenas_demo.mp4" } });
    if (path.startsWith("/preview/") && path.endsWith("/thumbnail")) return route.fulfill({ status: 302, headers: { location: "/fx_samples/foto_viva.jpg" } });
    if (path.startsWith("/media-token/")) return json({ token: "e2e-media-token" });
    return route.fallback();
  });
  const announcement = page.getByRole("dialog", { name: "Nuevo editor de letras" });
  await page.addLocatorHandler(announcement, () => announcement.getByRole("button", { name: "Cancelar" }).click());
  return state;
}

export async function expectNoHorizontalOverflow(page, expect) {
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
}
