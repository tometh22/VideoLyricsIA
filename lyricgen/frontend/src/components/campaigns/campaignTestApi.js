// Stateful fake of the campaign API for component tests. Stages are derived
// the same way the backend does it so actions (discard, approve, generate)
// move songs between pipeline stages.
import { vi } from "vitest";

export function json(body, status = 200) {
  return new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json" } });
}

export function makeSong(n, stage, extra = {}) {
  const id = `i${n}`;
  return {
    item_id: id, ordinal: n, title: `Canción ${n}`, artist: n % 2 ? "Charly García" : "Divididos", technical_code: `ARF${n}`, filename: `c${n}.wav`,
    duration_seconds: 180 + n, stage, phase: stage, job_id: `j${n}`, job_status: "", current_job_id: `j${n}`, current_status: "",
    current_is_variant: false, upload_state: "uploaded", metadata_error: null, has_video: ["qc", "approved", "delivered"].includes(stage),
    approved_at: ["approved", "delivered"].includes(stage) ? "2026-09-20T00:00:00Z" : null, video_count: ["qc", "approved", "delivered"].includes(stage) ? 1 : 0,
    versions: [], portals: stage === "delivered" ? ["argentina"] : [], portal_outdated: false, published_other_version: false, pending_change_requests: 0,
    ...(["approved", "delivered"].includes(stage) ? { current_status: "done" } : stage === "qc" ? { current_status: "pending_review" } : {}),
    ...extra,
  };
}

export function createCampaignApi({ kind = "lyric_video", songs, canManage = true, reviewRows, creativeExtra = {}, onRequest } = {}) {
  const state = {
    campaign: { id: "c1", name: "Campaña de prueba", status: "active", kind, created_by: 1, expected_count: songs.length, registered_count: songs.length, default_render_params: {}, reviewer_campaign_status: null, created_at: "2026-09-01T00:00:00Z" },
    songs: songs.map((song) => ({ ...song })),
    calls: [],
    generateResponses: [],
  };
  const counts = () => {
    const result = { audio: 0, lyrics: 0, ready: 0, rendering: 0, qc: 0, approved: 0, delivered: 0, attention: 0, discarded: 0 };
    state.songs.forEach((song) => { result[song.stage] += 1; });
    return result;
  };
  const rows = () => (reviewRows ? reviewRows(state) : state.songs.map((song) => ({
    item_id: song.item_id, job_id: song.job_id, title: song.title, artist: song.artist,
    state: song.stage === "lyrics" ? "ready" : song.stage === "discarded" ? "discarded" : "approved",
    can_discard: ["lyrics", "attention"].includes(song.stage), review_priority: "standard", timing_evidence: [], review_reasons: [], reference: { available: true },
    discard: song.stage === "discarded" ? { reason: song.discardReason || "Descartada" } : null,
  })));
  const find = (id) => state.songs.find((song) => song.item_id === id || song.job_id === id || song.current_job_id === id);
  const fetchMock = vi.fn(async (input, options = {}) => {
    const url = new URL(String(input), "http://test");
    const path = url.pathname;
    const method = (options.method || "GET").toUpperCase();
    const body = typeof options.body === "string" ? JSON.parse(options.body) : options.body;
    state.calls.push({ path, method, body, search: url.search, headers: options.headers });
    const custom = onRequest?.({ path, method, body, url, state });
    if (custom) return custom;
    if (path === "/batch/campaigns" && method === "GET") return json({ items: [{ ...state.campaign, pipeline: { counts: counts(), total: state.songs.length } }] });
    if (path === "/batch/campaigns/c1" && method === "GET") return json(state.campaign);
    if (path === "/batch/campaigns/c1" && method === "PATCH") { Object.assign(state.campaign, body); return json(state.campaign); }
    if (path === "/batch/campaigns/c1/pipeline") {
      return json({ campaign_id: "c1", kind, total: state.songs.length, active_total: state.songs.length - counts().discarded, counts: counts(), flags: {}, portal_status_available: true, can_manage: canManage, items: state.songs.map((song) => ({ ...song })) });
    }
    if (path === "/batch/campaigns/c1/review-queue") {
      const items = rows();
      return json({ items, pages: 1, total: items.length, campaign_totals: { songs: items.length, approved_today: 0 }, review_minutes_today: { average: 4.2, songs: 3 } });
    }
    if (path === "/batch/campaigns/c1/creative") {
      return json({ plan: { revision: 0 }, can_manage: canManage, operations: [], fields: {
        font: { label: "Tipografía", group: "Letra", kind: "select", options: ["", "anton"] },
        effect: { label: "Efecto", group: "Movimiento y efectos", kind: "select", options: ["", "bokeh"] },
      }, items: state.songs.map((song) => ({ id: song.item_id, job_id: song.job_id, title: song.title, artist: song.artist, status: song.stage === "ready" ? "lyrics_approved" : "transcribed", discarded: song.stage === "discarded", settings: { font: "anton" }, assignment: song.group ? { group_name: song.group, revision: 1 } : {} })), ...creativeExtra });
    }
    if (path === "/batch/campaigns/c1/creative/report") {
      return json({ campaign_id: "c1", name: state.campaign.name, at: "2026-09-29T00:00:00Z", contract: {}, groups: [], history: [], videos: state.songs.filter((song) => song.has_video).map((song) => ({ job_id: song.current_job_id, title: song.title, artist: song.artist, status: song.current_status, evidence: { video_sha256: `sha-${song.item_id}` }, assignment: {}, video_url: `/download/${song.current_job_id}/video` })) });
    }
    const itemAction = path.match(/^\/batch\/campaigns\/c1\/items\/([^/]+)\/(discard|restore|retry)$/);
    if (itemAction) {
      const song = find(itemAction[1]);
      if (itemAction[2] === "discard") { song.stage = "discarded"; song.discardReason = body.reason; }
      if (itemAction[2] === "restore") song.stage = "lyrics";
      if (itemAction[2] === "retry") song.stage = "audio";
      return json({ ok: true });
    }
    const patchItem = path.match(/^\/batch\/campaigns\/c1\/items\/([^/]+)$/);
    if (patchItem && method === "PATCH") { Object.assign(find(patchItem[1]), body, { metadata_error: null }); return json({ ok: true, metadata_error: null }); }
    if (path.startsWith("/approve/")) {
      const song = find(path.split("/").pop());
      Object.assign(song, { stage: "approved", current_status: "done", approved_at: "2026-09-29T00:00:00Z" });
      return json({ ok: true, status: "done" });
    }
    if (path.startsWith("/status/")) return json({ job_id: path.split("/").pop(), artist: "A", song_title: "T", segments_json: [], segments_revision: 3 });
    if (path === "/generate") {
      const jobId = body.get("job_id");
      const scripted = state.generateResponses.shift();
      if (scripted) return scripted;
      const song = find(jobId);
      song.stage = "rendering";
      return json({ ok: true, job_id: jobId });
    }
    if (path === "/batch/campaigns/c1/deliveries") return json({ operation_id: "op1", total_count: body.job_ids.length, status: "queued" });
    if (path === "/batch/delivery-operations/op1") return json({ operation_id: "op1", status: "completed", destination_portal: "chile", total_count: 1, sent_count: 1, failed_count: 0, items: [] });
    if (path === "/backgrounds") return json([]);
    if (path === "/batch/campaigns/c1/creative/preview") return json({ preview_id: "p1", counts: [body.item_ids.length], rounded: false, skipped: [], changes: body.item_ids.map((id) => ({ item_id: id, artist: "A", title: id, group: body.groups[0].name, before: {}, after: body.groups[0].settings })) });
    if (path === "/batch/campaigns/c1/creative/apply") return json({ revision: 1 });
    if (path.startsWith("/batch/art-track-campaigns/c1/delivery-preview")) return json({ eligible_count: 2, hostname: "umgchile.genly.pro" });
    throw new Error(`Unexpected request ${method} ${path}`);
  });
  return { state, fetchMock };
}
