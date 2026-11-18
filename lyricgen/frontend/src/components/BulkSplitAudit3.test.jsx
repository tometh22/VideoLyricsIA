import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import LyricsEditor from "./LyricsEditor";
import { segmentsStore } from "../state/segmentsStore";

vi.mock("../i18n", () => ({ useI18n: () => ({ t: (_key, fallback) => fallback }) }));
vi.mock("./OnboardingTour", () => ({ EditorTour: () => null }));
vi.mock("./ToastProvider", () => ({ useToast: () => ({ toast: vi.fn(), dismiss: vi.fn() }) }));
afterEach(() => { cleanup(); segmentsStore._clearAll(); localStorage.clear(); vi.restoreAllMocks(); });

describe("bulk split is one reversible editor action", () => {
  it("splits two lines and one undo restores the document without phantom history", async () => {
    // Force three visual rows per phrase so approval opens the real wrap
    // dialog; the structural operation and history remain the actual editor.
    vi.spyOn(HTMLCanvasElement.prototype, "getContext").mockReturnValue({
      measureText: () => ({ width: 800 }),
    });
    const job = "bulk-split-audit-3";
    const rows = [
      { segment_id: "first", start: 0, end: 2, text: "primera segunda tercera" },
      { segment_id: "second", start: 3, end: 5, text: "cuarta quinta sexta" },
    ];
    const onApprove = vi.fn();
    render(<LyricsEditor segments={rows} transcribeJobId={job} filename="song.wav"
      requireLineReview disableAutosave disableBeforeUnload submitLabel="Aprobar prueba"
      onApprove={onApprove} onBack={vi.fn()} />);
    const undo = screen.getByRole("button", { name: "Deshacer", exact: true });
    expect(undo).toBeDisabled();
    fireEvent.click(screen.getByRole("button", { name: "Aprobar prueba" }));
    fireEvent.click(await screen.findByRole("button", { name: "Dividir automáticamente" }));
    expect(segmentsStore.get(job)).toHaveLength(4);
    expect(undo).toBeEnabled();
    fireEvent.click(undo);
    expect(segmentsStore.get(job).map(({ segment_id, start, end, text }) => ({ segment_id, start, end, text }))).toEqual(rows);
    expect(undo).toBeDisabled();
    expect(onApprove).not.toHaveBeenCalled();
  });
});
