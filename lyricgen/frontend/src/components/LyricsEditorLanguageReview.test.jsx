/**
 * Regression for the "Cambiar el mundo" (Lerner) staging report: a chorus
 * decoded in the wrong language reached the editor with NO warning and an
 * enabled approve button. The server now flags an output↔reference discrepancy
 * (folded into languageUncertain) and blocks approval until it is resolved.
 *
 * These cover the editor side of that contract: the discrepancy banner marks
 * the offending lines, approval is blocked while unresolved, an explicit human
 * "the lyrics are correct" action is offered, and a persisted resolution
 * releases the block.
 */
import { render, screen, cleanup, fireEvent, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import LyricsEditor from "./LyricsEditor";

vi.mock("../i18n", () => ({
  useI18n: () => ({ t: (_key, fallback) => fallback }),
}));
vi.mock("./OnboardingTour", () => ({ EditorTour: () => null }));

const toastSpy = vi.fn();
vi.mock("./ToastProvider", () => ({
  useToast: () => ({ toast: toastSpy, dismiss: () => {} }),
  ToastProvider: ({ children }) => children,
}));

function baseProps(overrides = {}) {
  return {
    segments: [{ start: 1.0, end: 2.0, text: "alpha line" }],
    filename: "song.mp3",
    audioFile: null,
    referenceLyrics: "",
    onApprove: vi.fn(),
    onBack: vi.fn(),
    transcribeJobId: "job-lang-test",
    submitLabel: "Aprobar y generar",
    ...overrides,
  };
}

afterEach(() => {
  cleanup();
  toastSpy.mockClear();
});

describe("LyricsEditor — revisión de idioma / discrepancia", () => {
  it("marca las líneas, bloquea aprobar y ofrece confirmación humana", async () => {
    const onApprove = vi.fn();
    const onResolveLanguageReview = vi.fn();
    render(<LyricsEditor {...baseProps({
      onApprove,
      onResolveLanguageReview,
      requireLineReview: true,   // campaign review (Agus's UMG path)
      languageUncertain: true,
      needsLanguageReview: true,
      outputReferenceDivergence: true,
      outputReferenceUnexplainedIndices: [3, 6],
    })} />);

    // Offending lines are surfaced (1-indexed).
    expect(screen.getByText(/Revisá las líneas: 4, 7/)).toBeInTheDocument();

    // Approval is blocked while unresolved.
    fireEvent.click(screen.getByRole("button", { name: /Aprobar y generar/i }));
    await waitFor(() =>
      expect(toastSpy).toHaveBeenCalledWith(expect.objectContaining({ tone: "info" })),
    );
    expect(onApprove).not.toHaveBeenCalled();

    // The explicit human resolution action is offered and wired.
    fireEvent.click(screen.getByTestId("resolve-language-review"));
    expect(onResolveLanguageReview).toHaveBeenCalledTimes(1);
  });

  it("una resolución persistida libera el bloqueo y oculta el botón", () => {
    render(<LyricsEditor {...baseProps({
      languageUncertain: true,
      needsLanguageReview: true,
      outputReferenceDivergence: true,
      outputReferenceUnexplainedIndices: [1],
      languageReviewResolved: true,
      onResolveLanguageReview: vi.fn(),
    })} />);

    expect(screen.getByText(/Ya podés aprobar/i)).toBeInTheDocument();
    expect(screen.queryByTestId("resolve-language-review")).not.toBeInTheDocument();
  });
});
