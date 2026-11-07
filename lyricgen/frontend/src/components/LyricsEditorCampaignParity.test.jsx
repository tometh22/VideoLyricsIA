import { act, cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import LyricsEditor from "./LyricsEditor";
import { segmentsStore } from "../state/segmentsStore";
import { canonicalizeEditorSegments } from "../lib/segmentTiming";

vi.mock("../i18n", () => ({ useI18n: () => ({ t: (_key, fallback) => fallback }) }));
vi.mock("./OnboardingTour", () => ({ EditorTour: () => null }));
vi.mock("./ToastProvider", () => ({ useToast: () => ({ toast: vi.fn(), dismiss: vi.fn() }) }));
const job = "campaign-parity";
const rows = [
  { segment_id: "first", start: 0, end: 2, text: "primera frase" },
  { segment_id: "second", start: 3, end: 5, text: "segunda parte" },
  { segment_id: "third", start: 6, end: 8, text: "tercera frase" },
];
function open(segments = rows, overrides = {}) {
  render(<LyricsEditor segments={segments} transcribeJobId={job} filename="song.wav"
    requireLineReview disableAutosave disableBeforeUnload onApprove={vi.fn()} onBack={vi.fn()} {...overrides} />);
}
const input = () => screen.getByLabelText("Letra de esta línea");
afterEach(() => { cleanup(); segmentsStore._clearAll(); localStorage.clear(); });

describe("campaign review preserves existing editor contracts", () => {
  it("splits the selected middle line without jumping to the first line", () => {
    open();
    fireEvent.click(screen.getByTestId("review-line-2"));
    act(() => input().focus());
    input().setSelectionRange(8, 8);
    fireEvent.keyDown(input(), { key: "Enter" });
    expect(input()).toHaveValue("parte");
    expect(input()).toHaveFocus();
    expect(segmentsStore.get(job).map(row => row.text)).toEqual(["primera frase", "segunda", "parte", "tercera frase"]);
  });
  it("selects a newly added line instead of leaving the previous lyric in the panel", () => {
    open();
    fireEvent.click(screen.getByTestId("review-line-2"));
    fireEvent.click(screen.getByRole("button", { name: "Agregar línea", exact: true }));
    expect(input()).toHaveValue("");
    expect(segmentsStore.get(job)).toHaveLength(4);
  });
  it("deleting the selected middle line keeps the nearest surviving line selected", () => {
    open();
    fireEvent.click(screen.getByTestId("review-line-2"));
    fireEvent.click(screen.getByRole("button", { name: "Eliminar línea", exact: true }));
    expect(input()).toHaveValue("tercera frase");
  });
  it("duplicating an overlapping line cannot move untouched neighbours during canonicalization", () => {
    const source = [
      { segment_id: "first", start: 0, end: 3, text: "primera frase" },
      { segment_id: "second", start: 1, end: 5, text: "armonía" },
      { segment_id: "third", start: 4, end: 6, text: "otra frase" },
    ];
    open(source);
    fireEvent.click(screen.getByRole("button", { name: "Duplicar línea", exact: true }));
    const saved = canonicalizeEditorSegments(segmentsStore.get(job));
    expect(saved).toHaveLength(4);
    for (const original of source) expect(saved.find(row => row.segment_id === original.segment_id)).toMatchObject(original);
  });
  it("retains the line-specific unvalidated timing warning", () => {
    open(rows.map((row, index) => index === 1 ? { ...row, timing_validation: { status: "unvalidated" } } : row));
    fireEvent.click(screen.getByTestId("review-line-2"));
    expect(screen.getByTestId("selected-line-panel")).toHaveTextContent("Timing no validado");
  });
  it("undo restores both the document and the previously selected line", () => {
    open(); fireEvent.click(screen.getByTestId("review-line-2"));
    act(() => input().focus()); input().setSelectionRange(8, 8);
    fireEvent.keyDown(input(), { key: "Enter" });
    fireEvent.click(screen.getByRole("button", { name: "Deshacer", exact: true }));
    expect(input()).toHaveValue("segunda parte");
    expect(segmentsStore.get(job)).toHaveLength(3);
  });
  it("merging overlapping lines cannot truncate the first line's audio range", () => {
    open([{ ...rows[0], end: 5 }, { ...rows[1], start: 1, end: 2 }]);
    fireEvent.click(screen.getByRole("button", { name: "Unir con la línea siguiente" }));
    expect(segmentsStore.get(job)[0]).toMatchObject({ start: 0, end: 5 });
  });
  it("keeps the existing empty-line Backspace shortcut without merging nonempty lyrics", () => {
    open(); fireEvent.click(screen.getByTestId("review-line-2"));
    input().setSelectionRange(0, 0); fireEvent.keyDown(input(), { key: "Backspace" });
    expect(segmentsStore.get(job)).toHaveLength(3);
    fireEvent.change(input(), { target: { value: "" } });
    fireEvent.keyDown(input(), { key: "Backspace" });
    expect(segmentsStore.get(job)).toHaveLength(2);
    expect(input()).toHaveValue("primera frase");
  });
  it("keeps new blank lines inside the loaded audio duration", () => {
    open(rows, { audioUrl: "https://example.test/song.wav" });
    const audio = document.querySelector("audio");
    Object.defineProperty(audio, "duration", { configurable: true, value: 8 });
    fireEvent.loadedMetadata(audio);
    fireEvent.click(screen.getByTestId("review-line-3"));
    fireEvent.click(screen.getByRole("button", { name: "Agregar línea", exact: true }));
    const added = segmentsStore.get(job).find(row => !row.text);
    expect(added.end).toBeGreaterThan(added.start);
    expect(added.end).toBeLessThanOrEqual(8);
  });
  it("keeps individual reference suggestions available without applying them automatically", () => {
    open([{ ...rows[0], text: "Vos sos Roman" }], { referenceLyrics: "Vos sos Román" });
    expect(input()).toHaveValue("Vos sos Roman");
    fireEvent.click(screen.getByRole("button", { name: /Aplicar sugerencia: Vos sos Román/ }));
    expect(input()).toHaveValue("Vos sos Román");
  });
  it("still offers explicit propagation of a repeated chorus correction", () => {
    open([{ ...rows[0], text: "Quizá fue ayer" }, { ...rows[1], text: "Quizá fue ayer" }]);
    act(() => input().focus());
    fireEvent.change(input(), { target: { value: "Quizás fue ayer" } });
    fireEvent.blur(input());
    expect(screen.getByRole("button", { name: /Aplicar a todas/ })).toBeInTheDocument();
    expect(segmentsStore.get(job)[1].text).toBe("Quizá fue ayer");
    fireEvent.click(screen.getByRole("button", { name: "Deshacer", exact: true }));
    expect(input()).toHaveValue("Quizá fue ayer");
    expect(screen.queryByRole("button", { name: /Aplicar a todas/ })).not.toBeInTheDocument();
  });

});
