import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import LyricsEditor from "./LyricsEditor";
import { segmentsStore } from "../state/segmentsStore";
vi.mock("../i18n", () => ({ useI18n: () => ({ t: (_key, fallback) => fallback }) }));
vi.mock("./OnboardingTour", () => ({ EditorTour: () => null }));
vi.mock("./ToastProvider", () => ({ useToast: () => ({ toast: vi.fn(), dismiss: vi.fn() }) }));
afterEach(() => { cleanup(); segmentsStore._clearAll(); localStorage.clear(); });
const reply = (body, status = 200) => ({ ok: status < 400, status, clone: () => ({ json: async () => body }) });

describe("sixth audit: approval identity after equivalent replacement", () => {
  it.each(["save", "approval"])("attests the persisted replacement identity after equivalent merge during %s", async stage => {
    const jobId = "approvalmerge6";
    const segments = [{ segment_id: "a", start: 0, end: 2, text: "Letra inicial" }];
    const remote = [{ ...segments[0], segment_id: "replacement-a" }];
    const document = { job_id: jobId, revision: 1, segments, original_segments: segments, lock: { active: false } };
    const writes = [];
    let reads = 0;
    const request = vi.fn(async (path, options = {}) => {
      if (path.endsWith("/lock/heartbeat")) return reply({ acquired: true });
      if (path === `/editor/${jobId}` && !options.method) {
        reads += 1;
        return reply(stage === "approval" && reads > 1 ? { ...document, revision: 3, segments: remote } : document);
      }
      if (path === `/editor/${jobId}` && options.method === "PATCH") {
        const payload = JSON.parse(options.body);
        writes.push(payload);
        if (stage === "save" && writes.length === 1) return reply({ detail: { server_revision: 2, server_segments: remote } }, 409);
        return reply({ revision: writes.length + 1, version_id: `v${writes.length + 1}` });
      }
      return reply({});
    });
    const onApprove = vi.fn().mockResolvedValue({ ok: true });
    if (stage === "approval") onApprove.mockResolvedValueOnce({ ok: false, reason: "conflict" });
    render(<LyricsEditor segments={segments} transcribeJobId={jobId} filename="song.wav"
      requireLineReview disableBeforeUnload user={{ id: 42, tenant_id: "audit6", features: { editor_v2: true } }}
      editorRequest={request} onApprove={onApprove} onBack={vi.fn()} submitLabel="Aprobar letra y timing" />);
    await waitFor(() => expect(screen.getByTestId("lyrics-editor")).toHaveAttribute("aria-busy", "false"));
    const approve = screen.getByRole("button", { name: "Aprobar letra y timing", exact: true });
    fireEvent.click(approve);
    await waitFor(() => expect(writes).toHaveLength(2));
    await waitFor(() => expect(approve).toBeEnabled());
    await waitFor(() => expect(onApprove).toHaveBeenCalledTimes(stage === "save" ? 1 : 2));
    expect(writes[1].segments[0].segment_id).toBe("replacement-a");
    expect(onApprove.mock.calls.at(-1)[0][0].segment_id).toBe("replacement-a");
    expect(onApprove.mock.calls.at(-1)[1].confirmedLineIds).toEqual(["replacement-a"]);
  });
});
