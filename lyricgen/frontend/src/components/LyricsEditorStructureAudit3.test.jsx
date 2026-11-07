import { act, cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import LyricsEditor from "./LyricsEditor";
import { segmentsStore } from "../state/segmentsStore";
import { canonicalizeEditorSegments } from "../lib/segmentTiming";
import { planSegmentSplit } from "../lib/splitWords";

vi.mock("../i18n", () => ({ useI18n: () => ({ t: (_key, fallback) => fallback }) }));
vi.mock("./OnboardingTour", () => ({ EditorTour: () => null }));
vi.mock("./ToastProvider", () => ({ useToast: () => ({ toast: vi.fn(), dismiss: vi.fn() }) }));
const job = "structure-audit-3";
function open(segments, overrides = {}) {
  render(<LyricsEditor segments={segments} transcribeJobId={job} filename="song.wav"
    requireLineReview disableAutosave disableBeforeUnload onApprove={vi.fn()} onBack={vi.fn()} {...overrides} />);
}
const input = () => screen.getByLabelText("Letra de esta línea");
afterEach(() => { cleanup(); segmentsStore._clearAll(); localStorage.clear(); });

describe("third audit: structure and deferred propagation", () => {
  it("splitting a long line preserves overlapping neighbours and both planned bounds", () => {
    const neighbour = { segment_id: "neighbour", start: 2, end: 3, text: "armonía" };
    open([
      { segment_id: "source", start: 0, end: 10, text: "primera segunda" },
      neighbour,
    ]);
    act(() => input().focus());
    input().setSelectionRange(8, 8);
    fireEvent.keyDown(input(), { key: "Enter" });
    const saved = canonicalizeEditorSegments(segmentsStore.get(job));
    expect(saved.find(row => row.segment_id === "neighbour")).toMatchObject(neighbour);
    expect(saved.find(row => row.text === "segunda").end).toBe(10);
  });

  it("a pending chorus correction never overwrites a target edited since the prompt was opened", () => {
    open([
      { segment_id: "a", start: 0, end: 2, text: "Quizá fue ayer" },
      { segment_id: "b", start: 3, end: 5, text: "Quizá fue ayer" },
      { segment_id: "c", start: 6, end: 8, text: "Quizá fue ayer" },
    ]);
    act(() => input().focus());
    fireEvent.change(input(), { target: { value: "Quizás fue ayer" } });
    fireEvent.blur(input());
    expect(screen.getByRole("button", { name: /Aplicar a todas/ })).toBeInTheDocument();
    fireEvent.click(screen.getByTestId("review-line-2"));
    // An explicit suggestion or remote edit can update a target while the old prompt remains.
    fireEvent.change(input(), { target: { value: "Esta vez es distinto" } });
    fireEvent.click(screen.getByRole("button", { name: /Aplicar a todas/ }));
    const saved = segmentsStore.get(job);
    expect(saved.find(row => row.segment_id === "b").text).toBe("Esta vez es distinto");
    expect(saved.find(row => row.segment_id === "c").text).toBe("Quizás fue ayer");
  });
  it("does not apply an obsolete chorus correction after its source was edited again", () => {
    open([
      { segment_id: "a", start: 0, end: 2, text: "Quizá fue ayer" },
      { segment_id: "b", start: 3, end: 5, text: "Quizá fue ayer" },
    ]);
    act(() => input().focus());
    fireEvent.change(input(), { target: { value: "Quizás fue ayer" } });
    fireEvent.blur(input());
    act(() => input().focus());
    fireEvent.change(input(), { target: { value: "Quizás fue hoy" } });
    fireEvent.blur(input());
    const apply = screen.queryByRole("button", { name: /Aplicar a todas/ });
    if (apply) fireEvent.click(apply);
    expect(segmentsStore.get(job).find(row => row.segment_id === "b").text).not.toBe("Quizás fue ayer");
  });

  it("splitting a short line cannot create negative or out-of-parent timing", () => {
    const parent = { start: 0.1, end: 0.3, text: "a b" };
    const split = planSegmentSplit(parent, { charOffset: 2 });
    expect(split).not.toBeNull();
    for (const half of [split.a, split.b]) {
      expect(half.start).toBeGreaterThanOrEqual(parent.start);
      expect(half.end).toBeLessThanOrEqual(parent.end);
      expect(half.end).toBeGreaterThan(half.start);
    }
    expect(split.a.end).toBeLessThanOrEqual(split.b.start);
  });

  it("reference splitting also preserves a neighbouring overlapping line", () => {
    const neighbour = { segment_id: "neighbour", start: 0.5, end: 1, text: "armonía" };
    open([
      { segment_id: "source", start: 0, end: 3, text: "primera parte segunda parte" },
      neighbour,
    ], { referenceLyrics: "primera parte\nsegunda parte" });
    fireEvent.click(screen.getByRole("button", { name: /Aplicar corrección/ }));
    const saved = canonicalizeEditorSegments(segmentsStore.get(job));
    expect(saved).toHaveLength(3);
    expect(saved.find(row => row.segment_id === "neighbour")).toMatchObject(neighbour);
  });

  it("declines an unsplittable 10ms interval instead of expanding it", () => {
    expect(planSegmentSplit({ start: 0, end: 0.01, text: "a b" }, { charOffset: 2 })).toBeNull();
  });

  it.each(["source", "target"])("deleting the %s dismisses the pending correction", (which) => {
    open([
      { segment_id: "a", start: 0, end: 2, text: "Quizá fue ayer" },
      { segment_id: "b", start: 3, end: 5, text: "Quizá fue ayer" },
    ]);
    act(() => input().focus());
    fireEvent.change(input(), { target: { value: "Quizás fue ayer" } });
    fireEvent.blur(input());
    if (which === "target") fireEvent.click(screen.getByTestId("review-line-2"));
    fireEvent.click(screen.getByRole("button", { name: "Eliminar línea", exact: true }));
    expect(screen.queryByRole("button", { name: /Aplicar a todas/ })).not.toBeInTheDocument();
  });

  it.each([
    [{ word: "a", start: 0, end: 0.97 }, { word: "b", start: 0.995, end: 1 }],
    [{ word: "a", start: 0, end: 0.005 }, { word: "b", start: 0.2, end: 1 }],
  ])("word-aware cuts must leave two persistable halves", (...words) => {
    const split = planSegmentSplit({ start: 0, end: 1, text: "a b", words }, { charOffset: 2 });
    expect(split.timing).toBe("char_ratio");
    for (const half of [split.a, split.b]) {
      expect(half.start).toBeGreaterThanOrEqual(0);
      expect(half.end).toBeLessThanOrEqual(1);
      expect(half.end - half.start).toBeGreaterThanOrEqual(0.01);
    }
  });

});
