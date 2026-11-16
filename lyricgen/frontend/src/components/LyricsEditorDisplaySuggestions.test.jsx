/**
 * DISPLAY_SEGMENTATION_MODE=suggest: the editor shows the server's split/merge
 * proposals as one-click suggestions. Nothing is applied until the reviewer
 * clicks, and applying keeps the per-word clocks.
 */
import { act, cleanup, fireEvent, render, screen, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import LyricsEditor from "./LyricsEditor";
import { segmentsStore } from "../state/segmentsStore";

vi.mock("../i18n", () => ({
  useI18n: () => ({ t: (_key, fallback) => fallback }),
}));
vi.mock("./OnboardingTour", () => ({ EditorTour: () => null }));
vi.mock("./ToastProvider", () => ({
  useToast: () => ({ toast: vi.fn(), dismiss: vi.fn() }),
  ToastProvider: ({ children }) => children,
}));

const JOB = "display-suggest-job";
const STORE = `${JOB}-store`;
const USER = { id: 7, username: "reviewer", tenant_id: "suggest-team", features: { editor_v2: true } };
const w = (word, start, end) => ({ word, start, end });
const LONG_TEXT = "Ahora tengo un culo inédito que se lleva todos los méritos";
const LONG_WORDS = [
  w("Ahora", 22.64, 22.9), w("tengo", 22.95, 23.2), w("un", 23.25, 23.3), w("culo", 23.35, 23.7),
  w("inédito", 23.75, 24.14), w("que", 24.46, 24.6), w("se", 24.62, 24.7), w("lleva", 24.75, 25.0),
  w("todos", 25.05, 25.3), w("los", 25.32, 25.4), w("méritos", 25.45, 25.88),
];
const SEGMENTS = [
  { segment_id: "s1", start: 10, end: 12, text: "primera línea", words: [w("primera", 10.1, 10.8), w("línea", 10.9, 11.8)] },
  { segment_id: "s2", start: 22.4, end: 26.0, text: LONG_TEXT, words: LONG_WORDS },
  { segment_id: "s3", start: 27.0, end: 27.4, text: "En", words: [w("En", 27.05, 27.3)] },
  { segment_id: "s4", start: 27.5, end: 29.0, text: "la noche", words: [w("la", 27.5, 27.6), w("noche", 27.7, 28.9)] },
];
const SUGGESTIONS = {
  schema: "display-segmentation-suggestions-v1", revision: 0,
  items: [
    { id: "split-s2", kind: "split", index: 1, segment_id: "s2", text: LONG_TEXT, start: 22.4, end: 26.0,
      cuts: [5], basis: ["word_pause"],
      parts: [{ text: "Ahora tengo un culo inédito", token_range: [0, 5] },
        { text: "que se lleva todos los méritos", token_range: [5, 11] }] },
    { id: "merge-s3", kind: "merge", index: 2, segment_id: "s3", next_segment_id: "s4",
      text: "En", next_text: "la noche", merged_text: "En la noche", start: 27.0, end: 29.0,
      reason: "open_ended_line" },
  ],
};

function reply(body, status = 200) {
  return { ok: status >= 200 && status < 300, status, clone: () => ({ json: async () => body }) };
}

function makeRequest(document) {
  return vi.fn(async (path, options = {}) => {
    if (path === `/editor/${JOB}` && (!options.method || options.method === "GET")) return reply(document);
    if (path.endsWith("/lock/heartbeat")) return reply({ acquired: true, user: USER });
    if (path.endsWith("/lock") && options.method === "DELETE") return reply({ released: true });
    return reply({}, 404);
  });
}

function renderEditor(document, segments = SEGMENTS) {
  return render(<LyricsEditor
    segments={segments}
    filename="song.wav"
    user={USER}
    transcribeJobId={JOB}
    storeKey={STORE}
    editorRequest={makeRequest(document)}
    onPersistSegments={vi.fn()}
    onApprove={vi.fn()}
    onBack={vi.fn()}
  />);
}

const suggestDocument = {
  job_id: JOB, revision: 0, segments: SEGMENTS, original_segments: SEGMENTS,
  lock: { active: false },
  display_segmentation_mode: "suggest",
  display_segmentation_suggestions: SUGGESTIONS,
};

afterEach(() => {
  cleanup();
  localStorage.clear();
  segmentsStore._clearAll();
});

describe("display segmentation suggestions in the editor", () => {
  it("renders one-click suggestions and applies nothing by itself", async () => {
    renderEditor(suggestDocument);
    const panel = await screen.findByTestId("display-seg-suggestions");
    expect(within(panel).getByRole("button", { name: `Cortar acá: ${LONG_TEXT}` })).toBeEnabled();
    expect(within(panel).getByRole("button", { name: "Unir con la siguiente: En" })).toBeEnabled();
    expect(segmentsStore.get(STORE).map((s) => s.text)).toEqual(SEGMENTS.map((s) => s.text));
  });

  it("'Cortar acá' splits the line keeping each half's word clocks", async () => {
    renderEditor(suggestDocument);
    const panel = await screen.findByTestId("display-seg-suggestions");
    act(() => { fireEvent.click(within(panel).getByRole("button", { name: `Cortar acá: ${LONG_TEXT}` })); });
    const rows = segmentsStore.get(STORE);
    expect(rows.map((s) => s.text)).toEqual([
      "primera línea", "Ahora tengo un culo inédito", "que se lleva todos los méritos", "En", "la noche",
    ]);
    const [, a, b] = rows;
    expect(a.words.map((x) => x.word)).toEqual(["Ahora", "tengo", "un", "culo", "inédito"]);
    expect(b.words.map((x) => x.word)).toEqual(["que", "se", "lleva", "todos", "los", "méritos"]);
    expect(a.start).toBeCloseTo(22.4, 3);
    expect(a.end).toBeCloseTo(24.14, 3);
    expect(b.start).toBeCloseTo(24.46, 3);
    expect(b.end).toBeCloseTo(26.0, 3);
    expect(new Set(rows.map((s) => String(s.segment_id))).size).toBe(rows.length);
    // The applied suggestion disappears; the merge one stays.
    expect(screen.queryByRole("button", { name: `Cortar acá: ${LONG_TEXT}` })).toBeNull();
    expect(screen.getByRole("button", { name: "Unir con la siguiente: En" })).toBeInTheDocument();
  });

  it("'Unir con la siguiente' joins the lines and their words", async () => {
    renderEditor(suggestDocument);
    const panel = await screen.findByTestId("display-seg-suggestions");
    act(() => { fireEvent.click(within(panel).getByRole("button", { name: "Unir con la siguiente: En" })); });
    const rows = segmentsStore.get(STORE);
    const merged = rows.find((s) => s.text === "En la noche");
    expect(merged.words.map((x) => x.word)).toEqual(["En", "la", "noche"]);
    expect(merged.start).toBeCloseTo(27.0, 3);
    expect(merged.end).toBeCloseTo(29.0, 3);
    expect(rows).toHaveLength(3);
  });

  it("'Descartar' hides one suggestion and 'Descartar todas' hides the panel", async () => {
    renderEditor(suggestDocument);
    const panel = await screen.findByTestId("display-seg-suggestions");
    act(() => { fireEvent.click(within(panel).getByRole("button", { name: `Descartar: ${LONG_TEXT}` })); });
    expect(screen.queryByRole("button", { name: `Cortar acá: ${LONG_TEXT}` })).toBeNull();
    expect(segmentsStore.get(STORE)).toHaveLength(SEGMENTS.length);
    act(() => { fireEvent.click(screen.getByRole("button", { name: "Descartar todas" })); });
    expect(screen.queryByTestId("display-seg-suggestions")).toBeNull();
    expect(segmentsStore.get(STORE).map((s) => s.text)).toEqual(SEGMENTS.map((s) => s.text));
  });

  it("'Aplicar todas' applies every suggestion in one step", async () => {
    renderEditor(suggestDocument);
    await screen.findByTestId("display-seg-suggestions");
    act(() => { fireEvent.click(screen.getByRole("button", { name: "Aplicar todas" })); });
    const rows = segmentsStore.get(STORE);
    expect(rows.map((s) => s.text)).toEqual([
      "primera línea", "Ahora tengo un culo inédito", "que se lleva todos los méritos", "En la noche",
    ]);
    expect(rows.every((s) => Array.isArray(s.words) && s.words.length === s.text.split(" ").length)).toBe(true);
    expect(screen.queryByTestId("display-seg-suggestions")).toBeNull();
  });

  it("shows nothing in shadow mode", async () => {
    const { display_segmentation_mode: _m, display_segmentation_suggestions: _s, ...shadowDocument } = suggestDocument;
    renderEditor(shadowDocument);
    await screen.findByLabelText("Letra de la línea 2");
    await act(async () => { await Promise.resolve(); });
    expect(screen.queryByTestId("display-seg-suggestions")).toBeNull();
  });
});

describe("manual split keeps word clocks", () => {
  it("✂ Dividir on a row that lost its words recovers them from the recognizer evidence", async () => {
    const { words, ...lost } = SEGMENTS[1];
    const segments = [SEGMENTS[0], { ...lost, provider_evidence: { words } }];
    const document = { job_id: JOB, revision: 0, segments, original_segments: segments, lock: { active: false } };
    renderEditor(document, segments);
    await screen.findByLabelText("Letra de la línea 2");
    const input = screen.getByLabelText("Letra de la línea 2");
    const caret = "Ahora tengo un culo inédito ".length;
    input.setSelectionRange(caret, caret);
    act(() => { fireEvent.keyDown(input, { key: "Enter" }); });
    const [, a, b] = segmentsStore.get(STORE);
    expect(a.text).toBe("Ahora tengo un culo inédito");
    expect(a.words).toHaveLength(5);
    expect(b.words).toHaveLength(6);
    expect(a.end).toBeCloseTo(24.14, 3);
    expect(b.start).toBeCloseTo(24.46, 3);
  });
});
