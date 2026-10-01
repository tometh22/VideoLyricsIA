import { act, cleanup, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import LyricsEditor from "./LyricsEditor";
import { segmentsStore } from "../state/segmentsStore";

vi.mock("../i18n", () => ({
  useI18n: () => ({ t: () => undefined }),
}));
vi.mock("./OnboardingTour", () => ({ EditorTour: () => null }));
vi.mock("./ToastProvider", () => ({
  useToast: () => ({ toast: vi.fn(), dismiss: () => {} }),
  ToastProvider: ({ children }) => children,
}));

const SEGMENTS = [
  { segment_id: "line-1", start: 1, end: 2, text: "Primera línea" },
  { segment_id: "line-2", start: 5, end: 6, text: "Segunda línea" },
];

function props(overrides = {}) {
  return {
    segments: SEGMENTS,
    filename: "campaign-song.mp3",
    transcribeJobId: "campaign-review-job",
    segmentsRevision: 7,
    requireLineReview: true,
    disableAutosave: true,
    disableBeforeUnload: true,
    submitLabel: "Aprobar letra y timing",
    onApprove: vi.fn(),
    onBack: vi.fn(),
    ...overrides,
  };
}

beforeEach(() => {
  localStorage.clear();
  segmentsStore._clearAll();
});

afterEach(() => {
  cleanup();
  segmentsStore._clearAll();
});

describe("LyricsEditor — aprobación por canción", () => {
  it("no recorta ni persiste timings al abrir una campaña de 41 líneas", async () => {
    const longSegments = Array.from({ length: 41 }, (_, index) => ({
      segment_id: `long-line-${index + 1}`,
      start: index * 10,
      end: index * 10 + 9,
      text: `Línea ${index + 1}`,
    }));
    const originalEnds = longSegments.map((segment) => segment.end);
    const onPersistSegments = vi.fn().mockResolvedValue({ ok: true, revision: 8 });

    vi.useFakeTimers();
    try {
      render(<LyricsEditor {...props({
        segments: longSegments,
        transcribeJobId: "campaign-41-long-lines",
        disableAutosave: false,
        onPersistSegments,
      })} />);
      await act(async () => { await vi.advanceTimersByTimeAsync(4_000); });
      expect(segmentsStore.get("campaign-41-long-lines").map((segment) => segment.end))
        .toEqual(originalEnds);
      expect(screen.getByTestId("line-review-gate")).toHaveTextContent("Revisión por canción");
      expect(screen.queryByRole("button", { name: /Confirmar línea/i })).toBeNull();
      expect(onPersistSegments).not.toHaveBeenCalled();
    } finally {
      vi.useRealTimers();
    }
  });

  it("aprueba la canción con un clic y vincula todas las líneas del snapshot", async () => {
    const onApprove = vi.fn();
    render(<LyricsEditor {...props({ onApprove })} />);
    const approve = screen.getByRole("button", { name: "Aprobar letra y timing" });

    expect(approve).not.toBeDisabled();
    expect(approve).toHaveAttribute("data-review-incomplete", "false");
    expect(screen.getByText(/confirma una vez la canción completa/i)).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /Confirmar línea/i })).toBeNull();

    await userEvent.click(approve);
    await waitFor(() => expect(onApprove).toHaveBeenCalledTimes(1));
    expect(onApprove.mock.calls[0][1]).toEqual(expect.objectContaining({
      confirmedLineIds: ["line-1", "line-2"],
    }));
  });

  it("aprueba documentos legacy sin segment_id usando identidades ordenadas", async () => {
    const onApprove = vi.fn();
    const legacySegments = [
      { start: 1, end: 2, text: "Primera línea" },
      { start: 5, end: 6, text: "Segunda línea" },
    ];
    render(<LyricsEditor {...props({ segments: legacySegments, onApprove })} />);

    await userEvent.click(screen.getByRole("button", { name: "Aprobar letra y timing" }));

    await waitFor(() => expect(onApprove).toHaveBeenCalledTimes(1));
    expect(onApprove.mock.calls[0][1]).toEqual(expect.objectContaining({
      confirmedLineIds: ["index:0", "index:1"],
    }));
  });

  it("muestra las ventanas como guía sin exigir clicks ni acknowledgement", async () => {
    const onApprove = vi.fn();
    const editorRequest = vi.fn().mockResolvedValue(new Response(JSON.stringify({ ok: true }), {
      status: 200,
      headers: { "Content-Type": "application/json" },
    }));
    const transcriptionQuality = {
      policy_version: "lyrics-quality-v5",
      mode: "enforce",
      decision: "review_required",
      evaluated_revision: 7,
      segments_hash: "campaign-hash",
      unsafe_windows: [
        { id: "visible-window", start: 0.5, end: 2.5, reasons: ["text_mismatch"] },
        { id: "hidden-window", start: 4.5, end: 6.5, reasons: ["timing"] },
      ],
    };
    render(<LyricsEditor {...props({ transcriptionQuality, editorRequest, onApprove })} />);

    expect(screen.getByTestId("campaign-guidance-count")).toHaveTextContent("2 partes sugeridas");
    expect(screen.queryByRole("button", { name: /Confirmar línea/i })).toBeNull();
    expect(screen.queryByRole("button", { name: /Confirmar zona/i })).toBeNull();

    const approve = screen.getByRole("button", { name: "Aprobar letra y timing" });
    expect(approve).not.toBeDisabled();
    await userEvent.click(approve);

    await waitFor(() => expect(onApprove).toHaveBeenCalledTimes(1));
    expect(editorRequest).not.toHaveBeenCalledWith(
      "/jobs/campaign-review-job/transcription-quality/acknowledge",
      expect.anything(),
    );
  });

  it("expone al encabezado la misma salida que guarda antes de volver", async () => {
    const onBack = vi.fn();
    const onRegisterSafeExit = vi.fn();
    render(<LyricsEditor {...props({ onBack, onRegisterSafeExit })} />);

    await waitFor(() => expect(onRegisterSafeExit).toHaveBeenCalledWith(expect.any(Function)));
    const safeExit = onRegisterSafeExit.mock.calls.find(
      ([handler]) => typeof handler === "function",
    )[0];
    await act(async () => { await safeExit(); });

    expect(onBack).toHaveBeenCalledTimes(1);
  });
});
