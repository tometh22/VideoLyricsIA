// Tests for repeat-line propagation in LyricsEditor: editing one occurrence
// of a repeated chorus line offers to apply the same text to the other
// identical lines. Origin: Babasónicos "Quizá→Quizás" 1-of-5 and Bersuit
// "enfermera del amor" 3-of-4 incidents — operator fixed one occurrence,
// the rest kept the old text and the render looked unchanged.
import { render, screen, cleanup, fireEvent, within } from "@testing-library/react";
import { afterEach, describe, it, expect, vi } from "vitest";
import LyricsEditor, { smartLower, normalizeLineForMatch } from "./LyricsEditor";

vi.mock("../i18n", () => ({
  useI18n: () => ({ t: (_key, fallback) => fallback }),
}));
vi.mock("./OnboardingTour", () => ({ EditorTour: () => null }));
vi.mock("./ToastProvider", () => ({
  useToast: () => ({ toast: () => {}, dismiss: () => {} }),
  ToastProvider: ({ children }) => children,
}));

afterEach(() => cleanup());

function baseProps(overrides = {}) {
  return {
    segments: [{ start: 1.0, end: 2.0, text: "alpha line" }],
    filename: "song.mp3",
    audioFile: null,
    referenceLyrics: "",
    onApprove: vi.fn(),
    onBack: vi.fn(),
    ...overrides,
  };
}

// Edit a line's text input to `newText` and blur it, mimicking the operator
// typing a correction and leaving the field.
function editLine(input, newText) {
  fireEvent.focus(input);
  fireEvent.change(input, { target: { value: newText } });
  fireEvent.blur(input, { target: { value: newText } });
}

const PROMPT = /se repite en otras/i;

describe("smartLower — mirrors backend pipeline._smart_lower", () => {
  it("preserves an interior proper noun", () => {
    expect(smartLower("quizás llegue a Guinea")).toBe("quizás llegue a Guinea");
  });
  it("lowercases the first word even if capitalized", () => {
    expect(smartLower("Quizás llegue")).toBe("quizás llegue");
  });
  it("lowercases a proper noun that is the first word (documented tradeoff)", () => {
    expect(smartLower("Guinea me espera")).toBe("guinea me espera");
  });
  it("is a no-op on already-lowercase text", () => {
    expect(smartLower("solo minúsculas")).toBe("solo minúsculas");
  });
  it("preserves internal whitespace", () => {
    expect(smartLower("a  b  Guinea")).toBe("a  b  Guinea");
  });
  it("handles empty string", () => {
    expect(smartLower("")).toBe("");
  });
});

describe("normalizeLineForMatch", () => {
  it("trims and collapses internal whitespace", () => {
    expect(normalizeLineForMatch("  hola   mundo  ")).toBe("hola mundo");
  });
  it("is case- and accent-sensitive (no folding)", () => {
    expect(normalizeLineForMatch("Quizá")).not.toBe(normalizeLineForMatch("quiza"));
  });
  it("handles null/undefined", () => {
    expect(normalizeLineForMatch(undefined)).toBe("");
  });
});

describe("LyricsEditor — repeat-line propagation", () => {
  // The Babasónicos case: a chorus line repeated 3x, operator fixes one.
  function chorusProps(overrides = {}) {
    return baseProps({
      segments: [
        { start: 10.0, end: 12.0, text: "Quizá fue en la mañana" },
        { start: 20.0, end: 22.0, text: "un verso distinto" },
        { start: 30.0, end: 32.0, text: "Quizá fue en la mañana" },
        { start: 40.0, end: 42.0, text: "Quizá fue en la mañana" },
      ],
      ...overrides,
    });
  }

  it("offers propagation when editing one of several identical lines", () => {
    render(<LyricsEditor {...chorusProps()} />);
    const inputs = screen.getAllByDisplayValue("Quizá fue en la mañana");
    expect(inputs).toHaveLength(3);
    editLine(inputs[0], "Quizás fue en la mañana, vendados");
    // Prompt names the OTHER 2 occurrences (3 total minus the edited one).
    expect(screen.getByText(PROMPT)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /Aplicar a todas \(2\)/i })).toBeInTheDocument();
  });

  it("applies the full new text to all occurrences on 'Aplicar a todas'", () => {
    render(<LyricsEditor {...chorusProps()} />);
    const inputs = screen.getAllByDisplayValue("Quizá fue en la mañana");
    editLine(inputs[0], "Quizás fue en la mañana, vendados");
    fireEvent.click(screen.getByRole("button", { name: /Aplicar a todas/i }));
    // All 3 chorus lines now read the new text; the unrelated line is untouched.
    expect(screen.getAllByDisplayValue("Quizás fue en la mañana, vendados")).toHaveLength(3);
    expect(screen.queryByDisplayValue("Quizá fue en la mañana")).not.toBeInTheDocument();
    expect(screen.getByDisplayValue("un verso distinto")).toBeInTheDocument();
  });

  it("'Solo esta' changes only the edited line, leaving the rest", () => {
    render(<LyricsEditor {...chorusProps()} />);
    const inputs = screen.getAllByDisplayValue("Quizá fue en la mañana");
    editLine(inputs[0], "Quizás fue en la mañana, vendados");
    fireEvent.click(screen.getByTestId("repeat-only-this-btn"));
    expect(screen.getByDisplayValue("Quizás fue en la mañana, vendados")).toBeInTheDocument();
    // The other two stale occurrences stay as they were.
    expect(screen.getAllByDisplayValue("Quizá fue en la mañana")).toHaveLength(2);
  });

  it("does NOT prompt when editing a unique (non-repeated) line", () => {
    render(<LyricsEditor {...chorusProps()} />);
    const unique = screen.getByDisplayValue("un verso distinto");
    editLine(unique, "otro verso");
    expect(screen.queryByText(PROMPT)).not.toBeInTheDocument();
  });

  it("does NOT group lines that differ only in case/accent", () => {
    const props = baseProps({
      segments: [
        { start: 1.0, end: 2.0, text: "Quizá" },
        { start: 3.0, end: 4.0, text: "quiza" },
      ],
    });
    render(<LyricsEditor {...props} />);
    editLine(screen.getByDisplayValue("Quizá"), "Quizás");
    expect(screen.queryByText(PROMPT)).not.toBeInTheDocument();
  });

  it("does not touch an occurrence the operator already diverged by hand", () => {
    const props = baseProps({
      segments: [
        { start: 10.0, end: 12.0, text: "Quizá fue en la mañana" },
        { start: 20.0, end: 22.0, text: "Quizá fue en la mañana" },
        { start: 30.0, end: 32.0, text: "Quizá fue en la TARDE" }, // already different
      ],
    });
    render(<LyricsEditor {...props} />);
    const inputs = screen.getAllByDisplayValue("Quizá fue en la mañana");
    expect(inputs).toHaveLength(2);
    editLine(inputs[0], "Quizás fue en la mañana");
    // Only the 1 other identical line is offered, not the hand-diverged one.
    expect(screen.getByRole("button", { name: /Aplicar a todas \(1\)/i })).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: /Aplicar a todas/i }));
    expect(screen.getByDisplayValue("Quizá fue en la TARDE")).toBeInTheDocument();
  });
});
