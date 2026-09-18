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
