import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import LyricsEditor from "./LyricsEditor";
import { segmentsStore } from "../state/segmentsStore";

vi.mock("../i18n", () => ({ useI18n: () => ({ t: () => undefined }) }));
vi.mock("./OnboardingTour", () => ({ EditorTour: () => null }));
vi.mock("./ToastProvider", () => ({ useToast: () => ({ toast: vi.fn(), dismiss: vi.fn() }) }));
const job = "line-editing";
const rows = [
  { segment_id: "a", start: 0, end: 2, text: "Primera" },
  { segment_id: "b", start: 2, end: 4, text: "Segunda" },
  { segment_id: "c", start: 4, end: 6, text: "Tercera" },
];
function open(overrides = {}) {
  return render(<LyricsEditor segments={rows} transcribeJobId={job} filename="song.wav"
    requireLineReview disableAutosave disableBeforeUnload onApprove={vi.fn()} onBack={vi.fn()} {...overrides} />);
}
const saved = () => segmentsStore.get(job);
const texts = () => saved().map(row => row.text);
afterEach(() => { cleanup(); segmentsStore._clearAll(); localStorage.clear(); });

describe("campaign list structure editing", () => {
  it("adds directly below the displayed first line even before an explicit selection", () => {
    open();
    fireEvent.click(screen.getByRole("button", { name: "Agregar línea", exact: true }));
    expect(texts()).toEqual(["Primera", "", "Segunda", "Tercera"]);
    expect(screen.getByLabelText("Letra de esta línea")).toHaveValue("");
    expect(screen.getByLabelText("Letra de esta línea")).toHaveFocus();
  });
  it("inserts below a long overlapping phrase without moving its neighbours", () => {
    const overlapping = [{ ...rows[0], end: 5 }, rows[1], rows[2]];
    open({ segments: overlapping });
    fireEvent.click(screen.getByRole("button", { name: "Agregar línea", exact: true }));
    expect(texts()).toEqual(["Primera", "", "Segunda", "Tercera"]);
    for (const row of overlapping) expect(saved().find(item => item.segment_id === row.segment_id)).toMatchObject(row);
    expect(saved()[1].timing_validation.status).toBe("unvalidated");
  });
  it("moves via drag, persists immediately, and restores the order with Undo", async () => {
    const persist = vi.fn().mockResolvedValue({ ok: true });
    open({ disableAutosave: false, onPersistSegments: persist });
    const dataTransfer = { setData: vi.fn(), effectAllowed: "", dropEffect: "" };
    fireEvent.dragStart(screen.getByRole("button", { name: "Mover línea 1" }), { dataTransfer });
    const target = screen.getByTestId("review-line-3").parentElement;
    const over = new Event("dragover", { bubbles: true, cancelable: true });
    Object.defineProperties(over, { dataTransfer: { value: dataTransfer }, clientY: { value: 10 } });
    fireEvent(target, over);
    fireEvent.drop(target, { dataTransfer });
    expect(texts()).toEqual(["Segunda", "Tercera", "Primera"]);
    expect(saved().find(row => row.segment_id === "a").timing_validation.status).toBe("unvalidated");
    expect(saved().find(row => row.segment_id === "b")).toMatchObject(rows[1]);
    await waitFor(() => expect(persist).toHaveBeenCalled());
    expect(persist.mock.calls.at(-1)[1].map(row => row.text)).toEqual(texts());
    fireEvent.click(screen.getByRole("button", { name: "Deshacer", exact: true }));
    expect(texts()).toEqual(["Primera", "Segunda", "Tercera"]);
  });
  it("moves with the keyboard and does nothing for a cancelled drag", () => {
    open();
    fireEvent.keyDown(screen.getByRole("button", { name: "Mover línea 2" }), { key: "ArrowUp" });
    expect(texts()).toEqual(["Segunda", "Primera", "Tercera"]);
    fireEvent.dragStart(screen.getByRole("button", { name: "Mover línea 1" }), { dataTransfer: { setData() {} } });
    fireEvent.dragEnd(screen.getByRole("button", { name: "Mover línea 1" }));
    expect(texts()).toEqual(["Segunda", "Primera", "Tercera"]);
  });
  it("deletes directly from a row, saves it, survives reload and can delete the last row", async () => {
    const persist = vi.fn().mockResolvedValue({ ok: true });
    const view = open({ disableAutosave: false, onPersistSegments: persist });
    fireEvent.click(screen.getByRole("button", { name: "Eliminar línea 2", exact: true }));
    expect(texts()).toEqual(["Primera", "Tercera"]);
    await waitFor(() => expect(persist).toHaveBeenCalled());
    const snapshot = persist.mock.calls.at(-1)[1];
    view.unmount(); segmentsStore._clearAll(); localStorage.clear();
    open({ segments: snapshot });
    expect(texts()).toEqual(["Primera", "Tercera"]);
    fireEvent.click(screen.getByRole("button", { name: "Eliminar línea 1", exact: true }));
    fireEvent.click(screen.getByRole("button", { name: "Eliminar línea 1", exact: true }));
    expect(saved()).toHaveLength(0);
    fireEvent.click(screen.getByRole("button", { name: "Agregar línea", exact: true }));
    expect(saved()).toHaveLength(1);
  });
});
