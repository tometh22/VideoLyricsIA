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
const reply = body => ({ ok: true, status: 200, clone: () => ({ json: async () => body }) });

describe("fifth audit: approval belongs to its mounted editor", () => {
  it.each([false, true])("records a confirmed success after unmount only when not explicitly abandoned (%s)", async abandoned => {
    const jobId = "success5";
    const key = `genly_active_edit:audit5:42:${jobId}`;
    localStorage.setItem(key, "123");
    let release;
    const onApprove = vi.fn(() => new Promise(resolve => { release = resolve; }));
    const events = [];
    const request = vi.fn(async (path, options = {}) => {
      if (path === "/analytics/events") events.push(...JSON.parse(options.body).events);
      return { ...reply({ accepted: 1, rejected: 0 }), json: async () => ({ accepted: 1, rejected: 0 }) };
    });
    const view = render(<LyricsEditor segments={[{ segment_id: "a", start: 0, end: 2, text: "Letra revisada" }]}
      transcribeJobId={jobId} filename="song.wav" requireLineReview disableBeforeUnload disableAutosave
      user={{ id: 42, tenant_id: "audit5", features: { editor_v2: false } }} editorRequest={request}
      onApprove={onApprove} onBack={vi.fn()} submitLabel="Aprobar letra y timing" />);
    fireEvent.click(screen.getByRole("button", { name: "Aprobar letra y timing", exact: true }));
    expect(onApprove).toHaveBeenCalledOnce();
    const originalSession = events.find(event => event.name === "editor_opened")?.properties.session_id;
    view.unmount();
    expect(localStorage.getItem(key)).not.toBeNull();
    await act(async () => { release({ ok: true, abandoned }); });
    const approved = events.filter(event => event.name === "editor_approved");
    expect(approved).toHaveLength(abandoned ? 0 : 1);
    if (abandoned) expect(localStorage.getItem(key)).not.toBeNull();
    else {
      expect(localStorage.getItem(key)).toBeNull();
      expect(approved[0]).toMatchObject({ job_id: jobId, properties: { session_id: originalSession } });
    }
  });

  it("does not retain the approval lock when the job token changes", async () => {
    let releaseFirst;
    const onApprove = vi.fn()
      .mockImplementationOnce(() => new Promise(resolve => { releaseFirst = resolve; }))
      .mockResolvedValue({ ok: true });
    const editor = jobId => <LyricsEditor segments={[{ segment_id: "a", start: 0, end: 2, text: "Letra revisada" }]}
      transcribeJobId={jobId} filename="song.wav" requireLineReview disableBeforeUnload disableAutosave
      onApprove={onApprove} onBack={vi.fn()} submitLabel="Aprobar letra y timing" />;
    const view = render(editor("job-before"));
    fireEvent.click(screen.getByRole("button", { name: "Aprobar letra y timing", exact: true }));
    expect(onApprove).toHaveBeenCalledOnce();
    // App normally remounts the editor per job. This defensive lifecycle
    // fixture reuses it deliberately, so prepare the next store entry too.
    act(() => segmentsStore.replace("job-after", [
      { segment_id: "a", start: 0, end: 2, text: "Letra revisada" },
    ]));
    view.rerender(editor("job-after"));
    const approve = screen.getByRole("button", { name: "Aprobar letra y timing", exact: true });
    expect(approve).toBeEnabled();
    await act(async () => { fireEvent.click(approve); });
    expect(onApprove).toHaveBeenCalledTimes(2);
    await act(async () => { releaseFirst({ ok: false, reason: "conflict" }); });
    expect(approve).toBeEnabled();
    expect(onApprove).toHaveBeenCalledTimes(2);
  });

  it.each(["flush", "approval", "reconcile"])("does not advance approval after unmount during %s", async stage => {
    const jobId = "lifecycle5";
    const segments = [{ segment_id: "a", start: 0, end: 2, text: "Letra revisada" }];
    const document = { job_id: jobId, revision: 1, segments, original_segments: segments, lock: { active: false } };
    let release;
    const delayed = new Promise(resolve => { release = resolve; });
    let reads = 0;
    const request = vi.fn(async (path, options = {}) => {
      if (path.endsWith("/lock/heartbeat")) return reply({ acquired: true });
      if (path === `/editor/${jobId}` && !options.method) {
        reads += 1;
        if (stage === "reconcile" && reads === 2) return delayed;
        return reply(document);
      }
      return reply({});
    });
    runtime.flush = vi.fn().mockResolvedValue({ ok: true, revision: 2, segments });
    if (stage === "flush") runtime.flush.mockReturnValueOnce(delayed);
    const onApprove = vi.fn().mockResolvedValue({ ok: false, reason: "conflict" });
    if (stage === "approval") onApprove.mockReturnValueOnce(delayed);
    const view = render(<LyricsEditor segments={segments} transcribeJobId={jobId} filename="song.wav"
      requireLineReview disableBeforeUnload user={{ id: 42, tenant_id: "audit5", features: { editor_v2: true } }}
      editorRequest={request} onApprove={onApprove} onBack={vi.fn()} submitLabel="Aprobar letra y timing" />);
    await waitFor(() => expect(screen.getByTestId("lyrics-editor")).toHaveAttribute("aria-busy", "false"));
    fireEvent.click(screen.getByRole("button", { name: "Aprobar letra y timing", exact: true }));
    if (stage === "approval") await waitFor(() => expect(onApprove).toHaveBeenCalledOnce());
    if (stage === "reconcile") await waitFor(() => expect(reads).toBe(2));
    view.unmount();
    await act(async () => {
      if (stage === "flush") release({ ok: true, revision: 2, segments });
      else if (stage === "approval") release({ ok: false, reason: "conflict" });
      else release(reply({ ...document, revision: 3, segments: [{ ...segments[0], text: "Corrección remota" }] }));
    });
    expect(onApprove).toHaveBeenCalledTimes(stage === "flush" ? 0 : 1);
    expect(reads).toBe(stage === "reconcile" ? 2 : 1);
    expect(runtime.flush).toHaveBeenCalledOnce();
  });
});
