import { act, cleanup, renderHook, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import useChangeRequests from "./useChangeRequests";

const mocks = vi.hoisted(() => ({ fetchJson: vi.fn(), flashError: vi.fn() }));
vi.mock("../../AdminContext", () => ({ useAdmin: () => ({ flashError: mocks.flashError }) }));
vi.mock("../../adminApi", () => ({ API: "", fetchJson: mocks.fetchJson }));
afterEach(() => { cleanup(); vi.useRealTimers(); });
beforeEach(() => {
  vi.clearAllMocks();
  mocks.fetchJson.mockImplementation(async url => {
    if (url.startsWith("/admin/change-requests?")) return { items: [] };
    if (url === "/status/job-85") return { status: "pending_review",
      umg_spec: { frame_size: "UHD-4K", fps: 25, prores_profile: 4 } };
    if (url === "/enable-prores/job-85") return { ok: true, enqueued: ["umg_master"], status: "queued" };
    throw new Error(`Unexpected URL ${url}`);
  });
});
it("prepares the exact stored ProRes format without publishing or approving", async () => {
  const { result } = renderHook(() => useChangeRequests());
  await waitFor(() => expect(result.current.crLoading).toBe(false));
  await act(() => result.current.prepareProRes("job-85", 85));
  expect(mocks.fetchJson).toHaveBeenCalledWith("/enable-prores/job-85", expect.objectContaining({
    method: "POST", body: JSON.stringify({ umg_frame_size: "UHD-4K", umg_fps: "25", umg_prores_profile: "4" }),
  }));
  expect(mocks.fetchJson.mock.calls.some(([url]) => /from-job|\/approve\//.test(url))).toBe(false);
  expect(result.current.crPublishNotice).toMatchObject({ requestId: 85, tone: "wait", text: expect.stringContaining("encolada") });
  expect(result.current.crPublishingId).toBeNull();
});
it("keeps a persistent request-scoped error when updating the master fails", async () => {
  const previous = mocks.fetchJson.getMockImplementation();
  mocks.fetchJson.mockImplementation((url, opts) => url.startsWith("/enable-prores/")
    ? Promise.reject(Object.assign(new Error("Cola no disponible"), { status: 409 })) : previous(url, opts));
  const { result } = renderHook(() => useChangeRequests());
  await act(() => result.current.prepareProRes("job-85", 85));
  expect(result.current.crPublishNotice).toMatchObject({ requestId: 85, tone: "error", text: expect.stringContaining("Cola no disponible") });
});
it("does not announce work when the server confirms no enqueued files", async () => {
  const previous = mocks.fetchJson.getMockImplementation();
  mocks.fetchJson.mockImplementation((url, opts) => url.startsWith("/enable-prores/")
    ? Promise.resolve({ ok: true, enqueued: [] }) : previous(url, opts));
  const { result } = renderHook(() => useChangeRequests());
  await act(() => result.current.prepareProRes("job-85", 85));
  expect(result.current.crPublishNotice.tone).toBe("error");
});
it.each([{}, { ok: false }, { ok: true }, { ok: true, enqueued: "umg_master" }])(
  "treats an unverified master acknowledgement as unknown and refreshes without retrying: %j", async receipt => {
    const previous = mocks.fetchJson.getMockImplementation();
    mocks.fetchJson.mockImplementation((url, opts) => url.startsWith("/enable-prores/")
      ? Promise.resolve(receipt) : previous(url, opts));
    const { result } = renderHook(() => useChangeRequests());
    await waitFor(() => expect(result.current.crLoading).toBe(false));
    const readsBefore = mocks.fetchJson.mock.calls.filter(([url]) => url.startsWith("/admin/change-requests?")).length;
    await act(() => result.current.prepareProRes("job-85", 85));
    expect(result.current.crPublishNotice).toMatchObject({ requestId: 85, tone: "wait", outcomeUnknown: true });
    expect(result.current.crPublishNotice.text).not.toContain("No se pudo");
    expect(mocks.fetchJson.mock.calls.filter(([url]) => url.startsWith("/admin/change-requests?")).length).toBeGreaterThan(readsBefore);
    expect(mocks.fetchJson.mock.calls.filter(([url]) => url.startsWith("/enable-prores/"))).toHaveLength(1);
  },
);
it.each([{}, { ok: false }, { ok: true }, { ok: true, resolved_at: "not-a-date" }])(
  "does not claim a manual close from an unverified acknowledgement: %j", async receipt => {
    const previous = mocks.fetchJson.getMockImplementation();
    mocks.fetchJson.mockImplementation((url, opts) => url.endsWith("/85/resolve")
      ? Promise.resolve(receipt) : previous(url, opts));
    const { result } = renderHook(() => useChangeRequests());
    await waitFor(() => expect(result.current.crLoading).toBe(false));
    const readsBefore = mocks.fetchJson.mock.calls.filter(([url]) => url.startsWith("/admin/change-requests?")).length;
    await act(() => result.current.resolveChangeRequest(85, "Revisado manualmente"));
    expect(result.current.crPublishNotice).toMatchObject({ requestId: 85, tone: "wait", outcomeUnknown: true });
    expect(result.current.crPublishNotice.text).toContain("No pudimos confirmar el cierre");
    expect(mocks.fetchJson.mock.calls.filter(([url]) => url.startsWith("/admin/change-requests?")).length).toBeGreaterThan(readsBefore);
    expect(mocks.fetchJson.mock.calls.filter(([url]) => url.endsWith("/resolve"))).toHaveLength(1);
  },
);
it.each([{ ok: true, resolved_at: "2026-09-18T12:00:00Z" }, { ok: true, already_resolved: true }])(
  "accepts the actual closure contract without claiming content or portal verification: %j", async receipt => {
    const previous = mocks.fetchJson.getMockImplementation();
    mocks.fetchJson.mockImplementation((url, opts) => url.endsWith("/85/resolve")
      ? Promise.resolve(receipt) : previous(url, opts));
    const { result } = renderHook(() => useChangeRequests());
    await waitFor(() => expect(result.current.crLoading).toBe(false));
    await act(() => result.current.resolveChangeRequest(85, "Revisado manualmente"));
    expect(result.current.crPublishNotice).toMatchObject({ requestId: 85, tone: "ok" });
    expect(result.current.crPublishNotice.text).toContain("registro");
    expect(result.current.crPublishNotice.text).not.toContain("resuelto en el portal");
  },
);
it("announces a prepared master even when lyrics still await review and publication is not ready", async () => {
  vi.useFakeTimers();
  let pending = ["umg_master"];
  const previous = mocks.fetchJson.getMockImplementation();
  mocks.fetchJson.mockImplementation((url, opts) => url.startsWith("/admin/change-requests?")
    ? Promise.resolve({ items: [{ id: 85, publication: {
      job_status: "pending_review", prores_pending: pending, needs_publish: false,
    } }] }) : previous(url, opts));
  const { result } = renderHook(() => useChangeRequests());
  await act(async () => {});
  await act(() => result.current.prepareProRes("job-85", 85));
  pending = [];
  await act(() => vi.advanceTimersByTimeAsync(5000));
  expect(result.current.crPublishNotice).toMatchObject({ requestId: 85, tone: "ok",
    text: expect.stringContaining("Terminó la preparación") });
  expect(result.current.crPublishNotice.text).toContain("no confirma que el pedido esté corregido");
});

it("reviews then renders exactly the saved revision once without publishing", async () => {
  const previous = mocks.fetchJson.getMockImplementation();
  mocks.fetchJson.mockImplementation((url, opts) => {
    if (url.endsWith('/85/review')) return Promise.resolve({ change_request_id: 85, editor_revision: 58, segments: [{ text: 'Completa' }] });
    if (url.endsWith('/85/render')) return Promise.resolve({ status: 'editing' });
    return previous(url, opts);
  });
  const { result } = renderHook(() => useChangeRequests());
  await act(() => result.current.reviewForRender(85));
  expect(result.current.crRenderReview.editor_revision).toBe(58);
  await act(async () => { await Promise.all([result.current.confirmRender(), result.current.confirmRender()]); });
  const renders = mocks.fetchJson.mock.calls.filter(([url]) => url.endsWith('/render'));
  expect(renders).toHaveLength(1);
  expect(JSON.parse(renders[0][1].body)).toEqual({ editor_revision: 58 });
  expect(mocks.fetchJson.mock.calls.some(([url]) => url.includes('from-job'))).toBe(false);
  expect(result.current.crPublishNotice.text).toContain('todavía no se publicó');
});

function deferred() {
  let resolve;
  let reject;
  const promise = new Promise((yes, no) => { resolve = yes; reject = no; });
  return { promise, resolve, reject };
}

it("ignores a pending-list response that arrives after the resolved filter", async () => {
  const old = deferred();
  mocks.fetchJson.mockImplementation(url => url.includes("status=resolved")
    ? Promise.resolve({ items: [{ id: 2, resolved_at: "now" }], pending_count: 7 }) : old.promise);
  const { result } = renderHook(() => useChangeRequests());
  await act(async () => { result.current.setCrStatusFilter("resolved"); });
  await waitFor(() => expect(result.current.changeRequests[0]?.id).toBe(2));
  await act(async () => {
    old.resolve({ items: [{ id: 1 }], pending_count: 999 });
    await old.promise;
  });
  expect(result.current.changeRequests.map(item => item.id)).toEqual([2]);
  expect(result.current.crPendingCount).toBe(7);
});

it("does not let an old full-proposal fetch replace a newer comparison", async () => {
  const old = deferred();
  let count = 0;
  const previous = mocks.fetchJson.getMockImplementation();
  mocks.fetchJson.mockImplementation((url, opts) => url.endsWith("/proposals/current")
    ? (++count === 1 ? old.promise : Promise.resolve({ proposal: { id: "p", content_hash: "new" } })) : previous(url, opts));
  const { result } = renderHook(() => useChangeRequests());
  let first;
  await act(async () => { first = result.current.loadChangeRequestProposal(85); });
  await act(() => result.current.loadChangeRequestProposal(85));
  await act(async () => { old.resolve({ proposal: { id: "p", content_hash: "old" } }); await first; });
  expect(result.current.crProposalDetails[85].content_hash).toBe("new");
});

it("invalidates the loaded proposal when polling observes another preview hash", async () => {
  vi.useFakeTimers();
  let hash = "old";
  mocks.fetchJson.mockImplementation(url => {
    if (url.endsWith("/proposals/current")) return Promise.resolve({ proposal: { id: "p", status: "ready", content_hash: "old" } });
    return Promise.resolve({ items: [{ id: 85, proposal: { id: "p", status: "ready", content_hash: hash }, publication: { job_status: "editing" } }] });
  });
  const { result } = renderHook(() => useChangeRequests());
  await act(async () => {});
  await act(() => result.current.loadChangeRequestProposal(85));
  expect(result.current.crProposalDetails[85].content_hash).toBe("old");
  hash = "new";
  await act(() => vi.advanceTimersByTimeAsync(5000));
  expect(result.current.crProposalDetails[85]).toBeUndefined();
});

it("applies the displayed hash once and keeps its success attached to the request", async () => {
  const response = deferred();
  const previous = mocks.fetchJson.getMockImplementation();
  mocks.fetchJson.mockImplementation((url, opts) => url.endsWith("/apply") ? response.promise : previous(url, opts));
  const { result } = renderHook(() => useChangeRequests());
  let applying;
  await act(async () => {
    applying = result.current.applyChangeRequestProposal(85, "p85", ["op1"], 4, "visible-hash");
    await result.current.applyChangeRequestProposal(85, "p85", ["op1"], 4, "visible-hash");
  });
  await waitFor(() => expect(mocks.fetchJson.mock.calls.filter(([url]) => url.endsWith("/apply"))).toHaveLength(1));
  const [, options] = mocks.fetchJson.mock.calls.find(([url]) => url.endsWith("/apply"));
  expect(JSON.parse(options.body)).toMatchObject({ expected_proposal_hash: "visible-hash", base_revision: 4, operation_ids: ["op1"] });
  await act(async () => { response.resolve({ ok: true, revision: 5, applied: true, proposal: { id: "p85", status: "applied" } }); await applying; });
  expect(result.current.crPublishNotice).toMatchObject({ requestId: 85, tone: "ok" });
});

it("a preview conflict reloads read-only and never auto-applies unseen changes", async () => {
  const previous = mocks.fetchJson.getMockImplementation();
  mocks.fetchJson.mockImplementation((url, opts) => {
    if (url.endsWith("/apply")) return Promise.reject(Object.assign(new Error("proposal_changed"), { status: 409 }));
    if (url.endsWith("/proposals/current")) return Promise.resolve({ proposal: { id: "p85", content_hash: "someone-elses-hash" } });
    return previous(url, opts);
  });
  const { result } = renderHook(() => useChangeRequests());
  await act(() => result.current.applyChangeRequestProposal(85, "p85", ["op1"], 4, "visible-hash"));
  expect(mocks.fetchJson.mock.calls.filter(([url]) => url.endsWith("/apply"))).toHaveLength(1);
  expect(result.current.crProposalDetails[85].content_hash).toBe("someone-elses-hash");
  expect(result.current.crPublishNotice.text).toContain("revisala antes");
});

it("refuses missing preview hashes and empty manual resolution motives without writes", async () => {
  const { result } = renderHook(() => useChangeRequests());
  await act(() => result.current.applyChangeRequestProposal(85, "p85", ["op1"], 4));
  await act(() => result.current.adjustChangeRequestProposal(85, "p85", "op1", "text", 4));
  await act(() => result.current.resolveChangeRequest(85, "  "));
  expect(mocks.fetchJson.mock.calls.some(([, options]) => options?.method === "POST" || options?.method === "PATCH")).toBe(false);
  expect(result.current.crPublishNotice).toMatchObject({ requestId: 85, tone: "error" });
});

it.each([undefined, 502])("reports uncertain publication after lost response/status %s, not confirmed failure", async status => {
  const previous = mocks.fetchJson.getMockImplementation();
  mocks.fetchJson.mockImplementation((url, opts) => url.includes("/deliveries/from-job/")
    ? Promise.reject(Object.assign(new Error("connection lost after commit"), { status })) : previous(url, opts));
  const { result } = renderHook(() => useChangeRequests());
  await act(() => result.current.publishDeliveryUpdate("job-85", "chile", 85, { editor_revision: 4, render_fingerprint: "render4" }));
  expect(result.current.crPublishNotice).toMatchObject({ requestId: 85, tone: "wait" });
  expect(result.current.crPublishNotice.text).toContain("podría haberse publicado");
  expect(result.current.crPublishNotice.text).not.toContain("No se publicó");
  expect(mocks.fetchJson.mock.calls.filter(([, options]) => options?.method === "POST")).toHaveLength(1);
});

it("confirms a lost publication response from the request's publication resolution", async () => {
  const previous = mocks.fetchJson.getMockImplementation();
  mocks.fetchJson.mockImplementation((url, opts) => url.includes("/deliveries/from-job/")
    ? Promise.reject(new TypeError("connection lost"))
    : url.includes("status=all")
      ? Promise.resolve({ items: [{ id: 85, resolved_at: "2026-09-28T20:00:00Z",
        resolved_by_revision: 3, resolution_source: "publication" }] })
      : previous(url, opts));
  const { result } = renderHook(() => useChangeRequests());
  await act(() => result.current.publishDeliveryUpdate("job-85", "chile", 85,
    { editor_revision: 4, render_fingerprint: "render4" }));
  expect(result.current.crPublishNotice.outcomeUnknown).toBe(true);
  await act(() => result.current.reconcilePublication(85));
  expect(mocks.fetchJson).toHaveBeenCalledWith(expect.stringContaining("change_request_id=85"));
  expect(result.current.crPublishNotice).toMatchObject({ tone: "ok", text: expect.stringContaining("versión 3") });
});

it("keeps publication success when the follow-up list refresh fails", async () => {
  let failList = false;
  mocks.fetchJson.mockImplementation((url) => {
    if (url.startsWith("/admin/change-requests?")) {
      if (failList) throw new Error("list unavailable");
      return Promise.resolve({ items: [] });
    }
    if (url.includes("/deliveries/from-job/")) return Promise.resolve({ ok: true, content_changed: true,
      revision: 3, portal_id: "chile", job_id: "job-85", resolved_change_requests: [85] });
    throw new Error(`Unexpected URL ${url}`);
  });
  const { result } = renderHook(() => useChangeRequests());
  await waitFor(() => expect(result.current.crLoading).toBe(false));
  failList = true;
  await act(() => result.current.publishDeliveryUpdate("job-85", "chile", 85,
    { editor_revision: 4, render_fingerprint: "render4" }));
  expect(result.current.crPublishNotice).toMatchObject({ tone: "ok", text: expect.stringContaining("Publicada la versión 3") });
});

it("does not default a missing publication destination to Argentina", async () => {
  const { result } = renderHook(() => useChangeRequests());
  await act(() => result.current.publishDeliveryUpdate("job-85", null, 85, { editor_revision: 4, render_fingerprint: "r4" }));
  expect(mocks.fetchJson.mock.calls.some(([url]) => url.includes("from-job"))).toBe(false);
  expect(result.current.crPublishNotice.text).toContain("destino");
});

it("background retry and remount reuse the same paid intention after a lost response", async () => {
  const previous = mocks.fetchJson.getMockImplementation();
  let calls = 0;
  mocks.fetchJson.mockImplementation((url, opts) => {
    if (url.endsWith("/proposals/current")) return Promise.resolve({ proposal: backgroundProposal() });
    if (url === "/edit/job-85") return ++calls === 1
      ? Promise.reject(new TypeError("lost response")) : Promise.resolve({ deduplicated: true, status: "editing" });
    return previous(url, opts);
  });
  const first = renderHook(() => useChangeRequests());
  await act(() => first.result.current.loadChangeRequestProposal(85));
  await act(() => first.result.current.regenerateBackgroundFromProposal(85, "p85", "bg1", "job-85", "Sin armas", "veo", "bg-hash"));
  expect(first.result.current.crPublishNotice).toMatchObject({ requestId: 85, tone: "wait" });
  first.unmount();
  const second = renderHook(() => useChangeRequests());
  await act(() => second.result.current.loadChangeRequestProposal(85));
  await act(() => second.result.current.regenerateBackgroundFromProposal(85, "p85", "bg1", "job-85", "Sin armas", "veo", "bg-hash"));
  const writes = mocks.fetchJson.mock.calls.filter(([url]) => url === "/edit/job-85");
  expect(writes).toHaveLength(2);
  expect(writes[0][1].headers["Idempotency-Key"]).toBe(writes[1][1].headers["Idempotency-Key"]);
  expect(second.result.current.crPublishNotice.text).toContain("ya fue recibido");
  expect(JSON.parse(writes[0][1].body)).toMatchObject({ expected_proposal_hash: "bg-hash",
    change_request_id: 85, change_request_proposal_id: "p85", change_request_operation_id: "bg1", editor_revision: 4 });
});

function backgroundProposal(extra = {}) {
  return { id: "p85", job_id: "job-85", content_hash: "bg-hash", status: "ready", base_revision: 4,
    lyrics_context: { revision: 4, matches_base: true },
    operations: [{ id: "bg1", regeneration_supported: true }], ...extra };
}

it.each([{}, { ok: true }, { ok: true, revision: 2 }, { ok: true, revision: 2, content_changed: "false" }])(
  "never calls an incomplete publication acknowledgement success: %j", async response => {
    const previous = mocks.fetchJson.getMockImplementation();
    mocks.fetchJson.mockImplementation((url, opts) => url.includes("/deliveries/from-job/") ? Promise.resolve(response) : previous(url, opts));
    const { result } = renderHook(() => useChangeRequests());
    await act(() => result.current.publishDeliveryUpdate("job-85", "chile", 85, { editor_revision: 4, render_fingerprint: "r4" }));
    expect(result.current.crPublishNotice).toMatchObject({ tone: "wait", outcomeUnknown: true });
    expect(result.current.crPublishNotice.text).not.toContain("Reenviado");
  });

it("historical apply replay refreshes comparison and does not claim current persistence", async () => {
  const previous = mocks.fetchJson.getMockImplementation();
  mocks.fetchJson.mockImplementation((url, opts) => {
    if (url.endsWith("/apply")) return Promise.resolve({ ok: true, applied: false, idempotent: true, revision: 5, proposal: { id: "p85" } });
    if (url.endsWith("/proposals/current")) return Promise.resolve({ proposal: { id: "p85", lyrics_context: { revision: 9, matches_base: false } } });
    return previous(url, opts);
  });
  const { result } = renderHook(() => useChangeRequests());
  await act(() => result.current.applyChangeRequestProposal(85, "p85", ["op"], 4, "hash"));
  expect(result.current.crProposalDetails[85].lyrics_context.revision).toBe(9);
  expect(result.current.crPublishNotice.text).toContain("recibo histórico no confirma");
});

it("older completion cannot clear busy for a newer same-request comparison", async () => {
  const old = deferred(), newer = deferred();
  const previous = mocks.fetchJson.getMockImplementation();
  let count = 0;
  mocks.fetchJson.mockImplementation((url, opts) => url.endsWith("/proposals/current")
    ? (++count === 1 ? old.promise : newer.promise) : previous(url, opts));
  const { result } = renderHook(() => useChangeRequests());
  let a, b;
  await act(async () => { a = result.current.loadChangeRequestProposal(85); b = result.current.loadChangeRequestProposal(85); });
  await act(async () => { old.resolve({ proposal: { id: "old" } }); await a; });
  expect(result.current.crProposalBusyId).toBe(85);
  await act(async () => { newer.resolve({ proposal: { id: "new" } }); await b; });
  expect(result.current.crProposalBusyId).toBeNull();
  expect(result.current.crProposalDetails[85].id).toBe("new");
});

it.each([{ status: "stale" }, { lyrics_context: { revision: 4, matches_base: false } },
  { operations: [{ id: "bg1", regeneration_supported: false }] }, { content_hash: "changed" }])(
  "blocks stale or unsupported background proposal before paid request %j", async invalid => {
    const previous = mocks.fetchJson.getMockImplementation();
    mocks.fetchJson.mockImplementation((url, opts) => url.endsWith("/proposals/current")
      ? Promise.resolve({ proposal: backgroundProposal(invalid) }) : previous(url, opts));
    const { result } = renderHook(() => useChangeRequests());
    await act(() => result.current.loadChangeRequestProposal(85));
    await act(() => result.current.regenerateBackgroundFromProposal(85, "p85", "bg1", "job-85", "Sin armas", "veo", "bg-hash"));
    expect(mocks.fetchJson.mock.calls.some(([url]) => url.startsWith("/edit/"))).toBe(false);
  });

it("closing review invalidates its late network response", async () => {
  const pending = deferred();
  const previous = mocks.fetchJson.getMockImplementation();
  mocks.fetchJson.mockImplementation((url, opts) => url.endsWith("/review") ? pending.promise : previous(url, opts));
  const { result } = renderHook(() => useChangeRequests());
  let loading;
  await act(async () => { loading = result.current.reviewForRender(85); });
  act(() => result.current.setCrRenderReview(null));
  await act(async () => { pending.resolve({ change_request_id: 85 }); await loading; });
  expect(result.current.crRenderReview).toBeNull();
});
it("follows a request being interpreted until its proposal is ready", async () => {
  const previous = mocks.fetchJson.getMockImplementation();
  let reads = 0;
  mocks.fetchJson.mockImplementation((url, opts) => {
    if (url === "/admin/change-requests/7/proposals") return Promise.resolve({ proposal: { id: "p", status: "interpreting" } });
    if (url === "/admin/change-requests/7/proposals/current") {
      reads += 1;
      return Promise.resolve({ proposal: { id: "p", status: reads < 2 ? "interpreting" : "ready" } });
    }
    return previous(url, opts);
  });
  vi.useFakeTimers({ shouldAdvanceTime: true });
  const { result } = renderHook(() => useChangeRequests());
  await act(() => result.current.generateChangeRequestProposal(7));
  expect(result.current.crProposalDetails[7].status).toBe("interpreting");
  await act(async () => { await vi.advanceTimersByTimeAsync(4000); });
  await act(async () => { await vi.advanceTimersByTimeAsync(4000); });
  await waitFor(() => expect(result.current.crProposalDetails[7].status).toBe("ready"));
  const before = reads;
  await act(async () => { await vi.advanceTimersByTimeAsync(12000); });
  expect(reads).toBe(before);
});

function rejectPublication(error) {
  const previous = mocks.fetchJson.getMockImplementation();
  mocks.fetchJson.mockImplementation((url, opts) => url.includes("/deliveries/from-job/")
    ? Promise.reject(error) : previous(url, opts));
}
const publicationError = (status, detail) => Object.assign(
  new Error(typeof detail === "string" ? detail : detail?.code || `HTTP ${status}`),
  { status, detail, code: typeof detail === "object" ? detail?.code : detail },
);
const PUBLICATION = { editor_revision: 4, render_fingerprint: "render4" };

it.each([
  [409, { code: "language_review_unresolved" }, "La letra no coincide con el idioma de la referencia. Abrí el editor y confirmá el idioma."],
  [402, { code: "quota_exceeded" }, "Falta crédito en la cuenta del video"],
  [409, "change_request_job_mismatch", "pertenece a otro video"],
  [409, "El corte cambió durante la publicación.", "El video cambió mientras se publicaba. Actualizá y reintentá."],
  [422, { code: "algo_raro" }, "HTTP 422"],
])("explains publication failure %s %j in plain Spanish with what to do", async (status, detail, expected) => {
  rejectPublication(publicationError(status, detail));
  const { result } = renderHook(() => useChangeRequests());
  await act(() => result.current.publishDeliveryUpdate("job-85", "chile", 85, PUBLICATION));
  expect(result.current.crPublishNotice).toMatchObject({ requestId: 85, tone: "error" });
  expect(result.current.crPublishNotice.text).toContain(expected);
  expect(result.current.crPublishNotice.text).not.toContain("El servidor no aceptó");
  expect(result.current.crPublishNotice.outcomeUnknown).toBeUndefined();
});

it("keeps the review link for a blocked publication without technical words", async () => {
  rejectPublication(publicationError(409, { code: "delivery_qc_blocked", delivery_qc: { reason: "fresh_preflight_required" } }));
  const { result } = renderHook(() => useChangeRequests());
  await act(() => result.current.publishDeliveryUpdate("job-85", "chile", 85, PUBLICATION));
  const notice = result.current.crPublishNotice;
  expect(notice.actionHref).toContain("/videos/job-85?qc_focus=findings&return_to=");
  expect(notice.actionHref).toContain(encodeURIComponent("change_request_id=85"));
  expect(notice.actionLabel).toBe("Revisar este corte antes de publicar");
  expect(`${notice.text} ${notice.actionLabel}`).not.toMatch(/preflight|fingerprint|\bQC\b/i);
});

// --- One click publishes even when the professional master is not ready ---------

const PUBLISH_BODY = { editor_revision: 4, render_fingerprint: "render4" };
const preparing = { ok: false, status: "preparing_prores", retry_after: 10, stale: [], enqueued: ["umg_master"] };
const published = { ok: true, content_changed: true, revision: 2, portal_id: "chile", job_id: "job-85", resolved_change_requests: [85] };

function scriptPublish(steps) {
  const previous = mocks.fetchJson.getMockImplementation();
  const queue = [...steps];
  mocks.fetchJson.mockImplementation((url, opts) => {
    if (!url.includes("/deliveries/from-job/")) return previous(url, opts);
    const next = queue.length > 1 ? queue.shift() : queue[0];
    return next instanceof Error ? Promise.reject(next) : Promise.resolve(next);
  });
}
const posts = () => mocks.fetchJson.mock.calls.filter(([url]) => url.includes("/deliveries/from-job/"));

it("publishes by itself once the professional master is ready, asking again without a second click", async () => {
  scriptPublish([preparing, preparing, published]);
  const { result } = renderHook(() => useChangeRequests());
  await waitFor(() => expect(result.current.crLoading).toBe(false));
  vi.useFakeTimers();
  let done;
  await act(async () => { done = result.current.publishDeliveryUpdate("job-85", "chile", 85, PUBLISH_BODY); await vi.advanceTimersByTimeAsync(100); });
  expect(posts()).toHaveLength(1);
  expect(result.current.crPublishingId).toBe(85);              // the button stays busy: nothing to press again
  expect(result.current.crPublishNotice).toMatchObject({ tone: "wait", text: expect.stringContaining("Se publica solo cuando esté listo") });
  await act(async () => { result.current.publishDeliveryUpdate("job-85", "chile", 85, PUBLISH_BODY); });   // a second click is ignored
  expect(posts()).toHaveLength(1);
  await act(async () => { await vi.advanceTimersByTimeAsync(10_000); });
  expect(posts()).toHaveLength(2);
  await act(async () => { await vi.advanceTimersByTimeAsync(10_000); await done; });
  expect(posts()).toHaveLength(3);
  expect(new Set(posts().map(([, options]) => options.body)).size).toBe(1);   // the same reviewed cut every time
  expect(result.current.crPublishNotice).toMatchObject({ tone: "ok", text: expect.stringContaining("Publicada la versión 2") });
  expect(result.current.crPublishingId).toBeNull();
});

it("hands control back with a clear message if the master takes far too long", async () => {
  scriptPublish([preparing]);
  const { result } = renderHook(() => useChangeRequests());
  await waitFor(() => expect(result.current.crLoading).toBe(false));
  vi.useFakeTimers();
  let done;
  await act(async () => { done = result.current.publishDeliveryUpdate("job-85", "chile", 85, PUBLISH_BODY); await vi.advanceTimersByTimeAsync(16 * 60 * 1000); await done; });
  expect(result.current.crPublishNotice).toMatchObject({ tone: "error", text: expect.stringContaining("tarda más de lo normal") });
  expect(result.current.crPublishNotice.text).toContain("no se perdió nada");
  expect(result.current.crPublishingId).toBeNull();
  expect(posts().length).toBeGreaterThan(5);
});

it("stops waiting and explains it when publishing fails while the master is being prepared", async () => {
  scriptPublish([preparing, Object.assign(new Error("changed"), { status: 409 })]);
  const { result } = renderHook(() => useChangeRequests());
  await waitFor(() => expect(result.current.crLoading).toBe(false));
  vi.useFakeTimers();
  let done;
  await act(async () => { done = result.current.publishDeliveryUpdate("job-85", "chile", 85, PUBLISH_BODY); await vi.advanceTimersByTimeAsync(10_500); await done; });
  expect(posts()).toHaveLength(2);
  expect(result.current.crPublishNotice).toMatchObject({ tone: "error", text: expect.stringContaining("El video cambió mientras se publicaba") });
  expect(result.current.crPublishingId).toBeNull();
});

it("does not replace the publication confirmation with the old 'preparation finished' notice", async () => {
  // The master finishing is reported by the list reload that follows a publish; it must not
  // say "this does not publish" right after the video was published.
  let pending = true;
  const previous = mocks.fetchJson.getMockImplementation();
  mocks.fetchJson.mockImplementation((url, opts) => {
    if (url.startsWith("/admin/change-requests?")) {
      return Promise.resolve({ items: [{ id: 85, publication: { job_status: "done", prores_pending: pending ? ["umg_master"] : [] } }] });
    }
    if (url.includes("/deliveries/from-job/")) { pending = false; return Promise.resolve(published); }
    return previous(url, opts);
  });
  const { result } = renderHook(() => useChangeRequests());
  await waitFor(() => expect(result.current.crLoading).toBe(false));
  await act(() => result.current.publishDeliveryUpdate("job-85", "chile", 85, PUBLISH_BODY));
  expect(result.current.crPublishNotice).toMatchObject({ tone: "ok", text: expect.stringContaining("Publicada la versión 2") });
  expect(result.current.crPublishNotice.text).not.toContain("no confirma");
});


// --- What the client sees ------------------------------------------------------

it("warns when the publication succeeded but the delivery is hidden from the client", async () => {
  scriptPublish([{ ...published, hidden_from_client: true, client_visibility: "hidden" }]);
  const { result } = renderHook(() => useChangeRequests());
  await waitFor(() => expect(result.current.crLoading).toBe(false));
  await act(() => result.current.publishDeliveryUpdate("job-85", "chile", 85, PUBLISH_BODY));
  const text = result.current.crPublishNotice.text;
  expect(text).toContain("Publicada la versión 2");
  expect(text).toContain("el cliente todavía no la ve");
  expect(text).toContain("Qué ve el cliente");
});

it("changes what the client sees for ONE delivery and reloads the list", async () => {
  const previous = mocks.fetchJson.getMockImplementation();
  mocks.fetchJson.mockImplementation((url, opts) => url.includes("/admin/deliveries/9/visibility")
    ? Promise.resolve({ ok: true, client_visibility: "hidden", hidden_from_client: true })
    : previous(url, opts));
  const { result } = renderHook(() => useChangeRequests());
  await waitFor(() => expect(result.current.crLoading).toBe(false));
  await act(() => result.current.setDeliveryVisibility(9, "hidden"));
  const call = mocks.fetchJson.mock.calls.find(([url]) => url.includes("/admin/deliveries/9/visibility"));
  expect(call[1]).toMatchObject({ method: "PUT", body: JSON.stringify({ mode: "hidden" }) });
  expect(result.current.crVisibilityBusyId).toBeNull();
});

it("switches the publication mode once and does not let a stale list flip it back", async () => {
  const previous = mocks.fetchJson.getMockImplementation();
  mocks.fetchJson.mockImplementation((url, opts) => {
    if (url.includes("/admin/publication-settings")) return Promise.resolve({ publication_mode: "pointer", source: "panel" });
    if (url.startsWith("/admin/change-requests?")) return Promise.resolve({ items: [], publication_mode: "snapshot", can_change_publication_mode: true });
    return previous(url, opts);
  });
  const { result } = renderHook(() => useChangeRequests());
  await waitFor(() => expect(result.current.crLoading).toBe(false));
  expect(result.current.crCanChangeMode).toBe(true);
  await act(() => result.current.changePublicationMode("pointer"));
  expect(result.current.crPublicationMode).toBe("pointer");
  // Another replica still answers "snapshot" for a few seconds: the switch stays put.
  await act(() => result.current.refreshChangeRequests({ silent: true }));
  expect(result.current.crPublicationMode).toBe("pointer");
});
it("closes a request the published cut already answers, bound to that cut, without a note", async () => {
  const previous = mocks.fetchJson.getMockImplementation();
  mocks.fetchJson.mockImplementation((url, opts) => url === "/admin/change-requests/85/confirm-publication"
    ? Promise.resolve({ ok: true, resolved_by_revision: 2, resolved_at: "2026-10-03T03:00:00Z" }) : previous(url, opts));
  const { result } = renderHook(() => useChangeRequests());
  await waitFor(() => expect(result.current.crLoading).toBe(false));
  await act(() => result.current.confirmChangeRequestPublication(85, { render_fingerprint: "cut-2", editor_revision: 14 }, 3));
  const call = mocks.fetchJson.mock.calls.find(([url]) => url.endsWith("/confirm-publication"));
  expect(JSON.parse(call[1].body)).toEqual({ reviewed_render_fingerprint: "cut-2", reviewed_editor_revision: 14, confirmed_items: 3 });
  expect(result.current.crPublishNotice).toMatchObject({ requestId: 85, tone: "ok" });
  expect(result.current.crPublishNotice.text).toContain("versión 2");
});
it("never confirms a publication close without the reviewed cut identity", async () => {
  const { result } = renderHook(() => useChangeRequests());
  await waitFor(() => expect(result.current.crLoading).toBe(false));
  await act(() => result.current.confirmChangeRequestPublication(85, { editor_revision: 14 }));
  expect(mocks.fetchJson.mock.calls.some(([url]) => url.endsWith("/confirm-publication"))).toBe(false);
  expect(result.current.crPublishNotice).toMatchObject({ requestId: 85, tone: "error" });
});
