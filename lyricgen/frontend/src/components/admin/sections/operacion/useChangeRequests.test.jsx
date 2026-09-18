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
    ? Promise.reject(new Error("Cola no disponible")) : previous(url, opts));
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
