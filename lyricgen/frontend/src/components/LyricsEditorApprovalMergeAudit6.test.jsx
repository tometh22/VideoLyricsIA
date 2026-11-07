import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import LyricsEditor from "./LyricsEditor";
import { segmentsStore } from "../state/segmentsStore";
vi.mock("../i18n", () => ({ useI18n: () => ({ t: (_key, fallback) => fallback }) }));
vi.mock("./OnboardingTour", () => ({ EditorTour: () => null }));
vi.mock("./ToastProvider", () => ({ useToast: () => ({ toast: vi.fn(), dismiss: vi.fn() }) }));
afterEach(() => { cleanup(); segmentsStore._clearAll(); localStorage.clear(); });
const reply = (body, status = 200) => ({ ok: status < 400, status, clone: () => ({ json: async () => body }) });

describe("sixth audit: approval after autosave reconciliation", () => {
  it.each([false, true])("requires fresh confirmation for semantic changes but permits metadata-only merge (%s)", async metadataOnly => {
    const jobId = "approvalmerge6";
    const segments = [{ segment_id: "a", start: 0, end: 2, text: "Letra inicial" }];
    const remote = [{ ...segments[0], ...(metadataOnly ? { review: true } : { text: "Corrección remota" }) }];
    const document = { job_id: jobId, revision: 1, segments, original_segments: segments, lock: { active: false } };
    const writes = [];
    const request = vi.fn(async (path, options = {}) => {
      if (path.endsWith("/lock/heartbeat")) return reply({ acquired: true });
      if (path === `/editor/${jobId}` && !options.method) return reply(document);
      if (path === `/editor/${jobId}` && options.method === "PATCH") {
        const payload = JSON.parse(options.body);
        writes.push(payload);
        if (writes.length === 1) return reply({ detail: { server_revision: 2, server_segments: remote } }, 409);
        return reply({ revision: writes.length + 1, version_id: `v${writes.length + 1}` });
      }
      return reply({});
    });
    const onApprove = vi.fn().mockResolvedValue({ ok: true });
    render(<LyricsEditor segments={segments} transcribeJobId={jobId} filename="song.wav"
      requireLineReview disableBeforeUnload user={{ id: 42, tenant_id: "audit6", features: { editor_v2: true } }}
      editorRequest={request} onApprove={onApprove} onBack={vi.fn()} submitLabel="Aprobar letra y timing" />);
    await waitFor(() => expect(screen.getByTestId("lyrics-editor")).toHaveAttribute("aria-busy", "false"));
    const approve = screen.getByRole("button", { name: "Aprobar letra y timing", exact: true });
    fireEvent.click(approve);
    await waitFor(() => expect(writes).toHaveLength(2));
    await waitFor(() => expect(approve).toBeEnabled());
    if (metadataOnly) {
      expect(onApprove).toHaveBeenCalledOnce();
      return;
    }
    expect(writes[1].segments[0].text).toBe("Corrección remota");
    expect(onApprove).not.toHaveBeenCalled();
    fireEvent.click(approve);
    await waitFor(() => expect(onApprove).toHaveBeenCalledOnce());
    expect(onApprove.mock.calls[0][0][0].text).toBe("Corrección remota");
  });
});
