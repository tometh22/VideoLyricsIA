import { act, cleanup, fireEvent, render, screen, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import LyricsEditor from "./LyricsEditor";
import CampaignLyricsReview from "./CampaignLyricsReview";
import { segmentsStore } from "../state/segmentsStore";

vi.mock("../i18n", () => ({ useI18n: () => ({ t: (_key, fallback) => fallback }) }));
vi.mock("./OnboardingTour", () => ({ EditorTour: () => null }));
vi.mock("./ToastProvider", () => ({ useToast: () => ({ toast: vi.fn(), dismiss: vi.fn() }) }));
const job = "split-preview";
const words = [
  { word: "uno", start: 0.2, end: 0.6 }, { word: "dos", start: 0.8, end: 1.4 },
  { word: "tres", start: 2, end: 2.5 }, { word: "cuatro", start: 3, end: 3.5 },
];
const source = { segment_id: "source", start: 0, end: 4, text: "uno dos tres cuatro", words };
const neighbour = { segment_id: "neighbour", start: 4.2, end: 5, text: "otra línea" };
const input = () => screen.getByLabelText("Letra de esta línea");
const splitButton = () => screen.getByRole("button", { name: "Dividir aquí", exact: true });
const documentRows = () => segmentsStore.get(job);
function open(line = source) {
  render(<LyricsEditor segments={[line, neighbour]} transcribeJobId={job} filename="song.wav"
    requireLineReview disableAutosave disableBeforeUnload onApprove={vi.fn()} onBack={vi.fn()} />);
}
function select(start, end = start) {
  act(() => { input().focus(); input().setSelectionRange(start, end); });
  fireEvent.select(input());
}
afterEach(() => { cleanup(); segmentsStore._clearAll(); localStorage.clear(); });

describe("selected-line split preview", () => {
  it("shows the exact split before mutation and preserves word clocks, neighbours, focus and undo", async () => {
    open();
    const before = structuredClone(documentRows());
    expect(splitButton()).toBeDisabled();
    select(8);
    const preview = screen.getByRole("group", { name: "Vista previa de la división" });
    expect(within(preview).getByText("uno dos")).toBeVisible();
    expect(within(preview).getByText("tres cuatro")).toBeVisible();
    expect(within(preview).getByText("0:00.0 → 0:01.4")).toBeVisible();
    expect(within(preview).getByText("0:02.0 → 0:04.0")).toBeVisible();
    expect(screen.getByRole("status")).toHaveTextContent("tiempos por palabra");
    expect(documentRows()).toEqual(before);
    await userEvent.click(splitButton());
    expect(documentRows()).toHaveLength(3);
    expect(documentRows()[0]).toMatchObject({ text: "uno dos", start: 0, end: 1.4, words: words.slice(0, 2) });
    expect(documentRows()[1]).toMatchObject({ text: "tres cuatro", start: 2, end: 4, words: words.slice(2) });
    expect(documentRows()[2]).toEqual(before[1]);
    expect(input()).toHaveValue("tres cuatro");
    expect(input()).toHaveFocus();
    expect(splitButton()).toBeDisabled();
    await userEvent.click(screen.getByRole("button", { name: "Deshacer", exact: true }));
    expect(documentRows()).toEqual(before);
    expect(input()).toHaveValue(source.text);
  });

  it.each(["button", "Enter"])("marks estimated halves for review using %s", async (action) => {
    open({ ...source, words: undefined });
    select(8);
    expect(screen.getByRole("status")).toHaveTextContent("Timing estimado");
    if (action === "Enter") fireEvent.keyDown(input(), { key: "Enter" });
    else { await userEvent.tab(); expect(splitButton()).toHaveFocus(); await userEvent.keyboard(" "); }
    for (const row of documentRows().slice(0, 2)) {
      expect(row).toMatchObject({ review: true, timing_validation: { status: "unvalidated", reason: "estimated_split" } });
      expect(row.start).toBeGreaterThanOrEqual(0);
      expect(row.end).toBeLessThanOrEqual(4);
    }
    expect(screen.getByTestId("selected-line-panel")).toHaveTextContent("Corte estimado");
  });

  it("does not claim stale clocks match rewritten text with the same token count", () => {
    open({ ...source, text: "otra frase nueva aquí" });
    select(11);
    expect(screen.getByRole("status")).toHaveTextContent("Timing estimado");
    fireEvent.click(splitButton());
    expect(documentRows().slice(0, 2).every(row => !row.words?.length)).toBe(true);
  });

  it("recovers matching provider clocks and previews word-boundary snapping", () => {
    open({ ...source, words: undefined, provider_evidence: { words } });
    select(7); // after 'dos', before the whitespace
    expect(screen.getByRole("status")).toHaveTextContent("tiempos por palabra");
    fireEvent.keyDown(input(), { key: "Enter" });
    expect(documentRows()[0].words).toEqual(words.slice(0, 2));
    expect(documentRows()[1].start).toBe(2);
  });

  it("rejects edge positions, selections, composition and repeated Enter without mutating", () => {
    open(); const before = structuredClone(documentRows());
    for (const range of [[0, 0], [source.text.length, source.text.length], [4, 8]]) {
      select(...range); expect(splitButton()).toBeDisabled();
      fireEvent.keyDown(input(), { key: "Enter" });
    }
    select(8);
    fireEvent.compositionStart(input()); expect(splitButton()).toBeDisabled();
    fireEvent.keyDown(input(), { key: "Enter", isComposing: true });
    fireEvent.compositionEnd(input());
    fireEvent.keyDown(input(), { key: "Enter", repeat: true });
    expect(documentRows()).toEqual(before);
    fireEvent.click(screen.getByTestId("review-line-2"));
    expect(splitButton()).toBeDisabled();
    expect(screen.queryByRole("group", { name: "Vista previa de la división" })).toBeNull();
  });

  it("invalidates the cursor when another edit replaces the text, and refreshes timing-only previews", () => {
    const onSplit = vi.fn();
    const props = { segments: [{ ...source, _id: 0 }], focusedId: 0, markedIds: new Set(),
      onSplit, rowRefs: { current: {} } };
    const view = render(<CampaignLyricsReview {...props} />);
    select(8);
    view.rerender(<CampaignLyricsReview {...props} segments={[{ ...source, _id: 0, end: 6 }]} />);
    expect(screen.getByText("0:02.0 → 0:06.0")).toBeVisible();
    view.rerender(<CampaignLyricsReview {...props} segments={[{ ...source, _id: 0, text: "texto cambiado" }]} />);
    expect(splitButton()).toBeDisabled();
    fireEvent.click(splitButton()); expect(onSplit).not.toHaveBeenCalled();
  });
});

it("keeps a live campaign preview when typography controls are hidden", () => {
  render(<LyricsEditor segments={[source, neighbour]} transcribeJobId={job} filename="song.wav"
    requireLineReview hideTypographyControls textCase="original" disableAutosave disableBeforeUnload
    onApprove={vi.fn()} onBack={vi.fn()} />);
  const preview = screen.getByTestId("review-live-preview");
  expect(within(preview).getByText(source.text)).toBeVisible();
  fireEvent.change(input(), { target: { value: "mi letra corregida" } });
  expect(within(preview).getByText("mi letra corregida")).toBeVisible();
  expect(within(preview).queryByText(source.text)).toBeNull();
  expect(documentRows()[0]).toMatchObject({ text: "mi letra corregida", start: 0, end: 4 });
});
