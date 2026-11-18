import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import LyricsEditor from "./LyricsEditor";
import { segmentsStore } from "../state/segmentsStore";

const runtime = vi.hoisted(() => ({ flush: null }));
vi.mock("../hooks/useEditorAutosave", () => ({ useEditorAutosave: () => ({ flush: runtime.flush }) }));
vi.mock("../i18n", () => ({ useI18n: () => ({ t: (_key, fallback) => fallback }) }));
vi.mock("./OnboardingTour", () => ({ EditorTour: () => null }));
vi.mock("./ToastProvider", () => ({ useToast: () => ({ toast: vi.fn(), dismiss: vi.fn() }) }));
afterEach(() => { cleanup(); segmentsStore._clearAll(); localStorage.clear(); });

describe("fourth audit: approval snapshot during a slow flush", () => {
  it("does not approve an older snapshot after the operator types a correction while saving", async () => {
    const segments = [{ segment_id: "a", start: 0, end: 2, text: "Letra inicial" }];
    let release;
    runtime.flush = vi.fn(() => new Promise(resolve => { release = resolve; }));
    const onApprove = vi.fn().mockResolvedValue({ ok: true });
    const request = vi.fn(async path => {
      const body = path.endsWith("/lock/heartbeat") ? { acquired: true }
        : { job_id: "approveaudit", revision: 1, segments, original_segments: segments, lock: { active: false } };
      return { ok: true, status: 200, clone: () => ({ json: async () => body }) };
    });
    render(<LyricsEditor segments={segments} transcribeJobId="approveaudit" filename="song.wav"
      requireLineReview disableBeforeUnload user={{ id: 42, tenant_id: "audit", features: { editor_v2: true } }}
      editorRequest={request} onApprove={onApprove} onBack={vi.fn()} submitLabel="Aprobar letra y timing" />);
    await waitFor(() => expect(screen.getByTestId("lyrics-editor")).toHaveAttribute("aria-busy", "false"));
    fireEvent.click(screen.getByRole("button", { name: "Aprobar letra y timing", exact: true }));
    expect(runtime.flush).toHaveBeenCalledOnce();
    fireEvent.change(screen.getByLabelText("Letra de esta línea"), { target: { value: "Corrección mientras guardaba" } });
    await act(async () => { release({ ok: true, revision: 2, segments }); });
    expect(screen.getByLabelText("Letra de esta línea")).toHaveValue("Corrección mientras guardaba");
    expect(onApprove).not.toHaveBeenCalled();
    const corrected = segmentsStore.get("approveaudit");
    runtime.flush.mockResolvedValueOnce({ ok: true, revision: 3, segments: corrected });
    await act(async () => {
      fireEvent.click(screen.getByRole("button", { name: "Aprobar letra y timing", exact: true }));
    });
    expect(onApprove).toHaveBeenCalledOnce();
    expect(onApprove.mock.calls[0][0][0].text).toBe("Corrección mientras guardaba");
  });
});
