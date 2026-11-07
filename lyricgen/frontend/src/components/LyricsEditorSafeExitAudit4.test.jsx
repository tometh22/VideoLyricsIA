import { act, cleanup, render } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import LyricsEditor from "./LyricsEditor";
import { segmentsStore } from "../state/segmentsStore";
const runtime = vi.hoisted(() => ({ flush: vi.fn() }));
vi.mock("../hooks/useEditorAutosave", () => ({ useEditorAutosave: () => ({ flush: runtime.flush }) }));
vi.mock("../i18n", () => ({ useI18n: () => ({ t: (_key, fallback) => fallback }) }));
vi.mock("./OnboardingTour", () => ({ EditorTour: () => null }));
vi.mock("./ToastProvider", () => ({ useToast: () => ({ toast: vi.fn(), dismiss: vi.fn() }) }));
afterEach(() => { cleanup(); segmentsStore._clearAll(); localStorage.clear(); vi.clearAllMocks(); });
function setup() {
  let finish;
  runtime.flush.mockImplementation(() => new Promise(resolve => { finish = resolve; }));
  let exit;
  const view = render(<LyricsEditor segments={[{ start: 0, end: 2, text: "Frase" }]}
    transcribeJobId="exit-audit-4" requireLineReview disableBeforeUnload
    user={{ id: 42, features: { editor_v2: true } }} editorRequest={() => new Promise(() => {})}
    onRegisterSafeExit={handler => { if (handler) exit = handler; }} onApprove={vi.fn()} onBack={vi.fn()} />);
  return { ...view, exit: (...args) => exit(...args), finish: () => finish({ ok: true }) };
}
describe("safe exit concurrency", () => {
  it("runs only one navigation action for repeated clicks while saving", async () => {
    let finish;
    const pending = new Promise(resolve => { finish = resolve; });
    const view = setup();
    runtime.flush.mockReturnValue(pending);
    const next = vi.fn();
    let first, second;
    act(() => { first = view.exit(next); second = view.exit(next); });
    await act(async () => { finish({ ok: true }); await Promise.all([first, second]); });
    expect(next).toHaveBeenCalledTimes(1);
  });
  it("does not navigate from an abandoned editor when its old save completes", async () => {
    const view = setup();
    const next = vi.fn();
    let pending;
    act(() => { pending = view.exit(next); });
    view.unmount();
    await act(async () => { view.finish(); await pending; });
    expect(next).not.toHaveBeenCalled();
  });
});
