/**
 * Regresión job 248d012f186a (staging, 2-oct-2026).
 *
 * "✂ Dividir" sobre "Aprendamos a perdonar y seremos perdonados" guardó
 * "Aprendamos a" 140.80–142.17 | "perdonar y seremos perdonados" 142.22–145.65
 * con `words: []` en las dos mitades: cortaba en el primer wrap del canvas,
 * interpolaba el tiempo por caracteres y descartaba el timing por palabra.
 */
import { render, cleanup, act, fireEvent, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import LyricsEditor from "./LyricsEditor";
import { segmentsStore } from "../state/segmentsStore";

vi.mock("../i18n", () => ({
  useI18n: () => ({ t: (_key, fallback) => fallback }),
}));
vi.mock("./OnboardingTour", () => ({ EditorTour: () => null }));
vi.mock("./ToastProvider", () => ({
  useToast: () => ({ toast: () => {}, dismiss: () => {} }),
  ToastProvider: ({ children }) => children,
}));

const JOB_ID = "job-split-words";
const WORDS = [
  { word: "Aprendamos", start: 140.8, end: 141.4 },
  { word: "a", start: 141.44, end: 141.52 },
  { word: "perdonar", start: 141.58, end: 142.62 },
  { word: "y", start: 143.32, end: 143.4 },
  { word: "seremos", start: 143.42, end: 144.04 },
  { word: "perdonados", start: 144.12, end: 145.46 },
];

afterEach(() => {
  cleanup();
  segmentsStore._clearAll();
});

function renderEditor() {
  render(
    <LyricsEditor
      segments={[
        { segment_id: "s1", start: 130.0, end: 133.0, text: "primera línea" },
        {
          segment_id: "s2", start: 140.8, end: 145.65,
          text: "Aprendamos a perdonar y seremos perdonados", words: WORDS,
        },
      ]}
      filename="song.mp3"
      audioFile={null}
      referenceLyrics=""
      onApprove={vi.fn()}
      onBack={vi.fn()}
      transcribeJobId={JOB_ID}
    />,
  );
}

function expectWordAwareHalves(segments, { textA, textB, aWords, aEnd, bStart }) {
  expect(segments).toHaveLength(3);
  const [, a, b] = segments;
  expect(a.text).toBe(textA);
  expect(b.text).toBe(textB);
  expect(a.words.map((w) => w.word)).toEqual(aWords);
  expect(a.words.length + b.words.length).toBe(WORDS.length);
  expect(a.start).toBeCloseTo(140.8, 3);
  expect(a.end).toBeCloseTo(aEnd, 3);
  expect(b.start).toBeCloseTo(bStart, 3);
  expect(b.end).toBeCloseTo(145.65, 3);
  // Identidad nueva y distinta en cada mitad (nunca la del padre).
  const ids = segments.map((row) => String(row.segment_id));
  expect(new Set(ids).size).toBe(3);
  expect(ids).not.toContain("s2");
}

describe("dividir una línea conserva el timing por palabra", () => {
  it("✂ Dividir corta en la pausa cantada y reparte las palabras", () => {
    renderEditor();
    const [button] = screen.getAllByRole("button", { name: /Dividir/ });
    act(() => { fireEvent.click(button); });
    expectWordAwareHalves(segmentsStore.get(JOB_ID), {
      textA: "Aprendamos a perdonar",
      textB: "y seremos perdonados",
      aWords: ["Aprendamos", "a", "perdonar"],
      aEnd: 142.62,
      bStart: 143.32,
    });
  });

  it("Enter en el cursor corta ahí y usa los tiempos reales de las palabras", () => {
    renderEditor();
    const input = screen.getByLabelText("Letra de la línea 2");
    const caret = "Aprendamos a ".length;
    input.setSelectionRange(caret, caret);
    act(() => { fireEvent.keyDown(input, { key: "Enter" }); });
    expectWordAwareHalves(segmentsStore.get(JOB_ID), {
      textA: "Aprendamos a",
      textB: "perdonar y seremos perdonados",
      aWords: ["Aprendamos", "a"],
      aEnd: 141.52,
      bStart: 141.58,
    });
  });
});
