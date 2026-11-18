import { act, cleanup, render, screen, waitFor } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";
import LyricsEditor from "./LyricsEditor";
import { segmentsStore } from "../state/segmentsStore";
const runtime = vi.hoisted(() => ({ history: null, toast: vi.fn() }));
vi.mock("./VersionHistory", () => ({ default: props => { runtime.history = props; return null; } }));
vi.mock("../i18n", () => ({ useI18n: () => ({ t: (_key, fallback) => fallback }) }));
vi.mock("./OnboardingTour", () => ({ EditorTour: () => null }));
vi.mock("./ToastProvider", () => ({ useToast: () => ({ toast: runtime.toast, dismiss: vi.fn() }) }));
afterEach(() => { cleanup(); segmentsStore._clearAll(); localStorage.clear(); });
const reply = (body, status = 200) => ({ ok: status < 400, status, clone: () => ({ json: async () => body }) });
it("re-enables the editor and preserves its text when restore fails after checkpoint", async () => {
  const segments = [{ segment_id: "a", start: 0, end: 2, text: "Letra actual" }];
  let release;
  const pending = new Promise(resolve => { release = resolve; });
  const request = vi.fn(async (path, options = {}) => {
    if (path.endsWith("/restore")) return pending;
    if (options.method === "PATCH") return reply({ revision: 2 });
    if (path.endsWith("/lock/heartbeat")) return reply({ acquired: true });
    return reply({ job_id: "restoreui6", revision: 1, segments, original_segments: segments });
  });
  render(<LyricsEditor segments={segments} transcribeJobId="restoreui6" filename="song.wav"
    requireLineReview disableBeforeUnload user={{ id: 42, tenant_id: "audit6", features: { editor_v2: true } }}
    editorRequest={request} onApprove={vi.fn()} onBack={vi.fn()} />);
  const editor = screen.getByTestId("lyrics-editor");
  await waitFor(() => expect(editor).toHaveAttribute("aria-busy", "false"));
  let restore;
  act(() => { restore = runtime.history.onRestore("old-version"); });
  await waitFor(() => expect(request.mock.calls.some(([path]) => path.endsWith("/restore"))).toBe(true));
  expect(editor).toHaveAttribute("inert");
  expect(runtime.history.busy).toBe(true);
  await act(async () => { release(reply({}, 503)); await restore; });
  expect(editor).not.toHaveAttribute("inert");
  expect(runtime.history.busy).toBe(false);
  expect(segmentsStore.get("restoreui6")[0].text).toBe("Letra actual");
  expect(runtime.toast).toHaveBeenCalledWith(expect.objectContaining({ tone: "error" }));
});
