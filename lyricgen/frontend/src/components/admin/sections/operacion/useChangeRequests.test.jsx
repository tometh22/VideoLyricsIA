import { act, cleanup, renderHook, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import useChangeRequests from "./useChangeRequests";

const mocks = vi.hoisted(() => ({ fetchJson: vi.fn(), flashError: vi.fn() }));
vi.mock("../../AdminContext", () => ({ useAdmin: () => ({ flashError: mocks.flashError }) }));
vi.mock("../../adminApi", () => ({ API: "", fetchJson: mocks.fetchJson }));
afterEach(cleanup);
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
