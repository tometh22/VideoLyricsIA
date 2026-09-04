import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import LyricsEditor, { visibleReviewLineIds } from "./LyricsEditor";
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

describe("LyricsEditor — gate de confirmación por línea", () => {
  it("considera las ediciones de texto y timing como confirmaciones durables", async () => {
    const onApprove = vi.fn();
    const first = render(<LyricsEditor {...props({ onApprove })} />);
    const approve = screen.getByRole("button", { name: "Aprobar letra y timing" });

    expect(approve).toBeDisabled();
    expect(approve).toHaveAttribute("data-review-incomplete", "true");
    expect(screen.getByTestId("line-review-progress")).toHaveTextContent("0/2");
    expect(screen.getByText(/no hace falta aprobar para guardarlos/i)).toBeInTheDocument();

    fireEvent.change(screen.getByLabelText("Letra de la línea 1"), {
      target: { value: "Primera línea corregida" },
    });
    expect(screen.getByTestId("line-review-progress")).toHaveTextContent("1/2");

    fireEvent.doubleClick(screen.getByLabelText(/Doble click para editar el tiempo de la línea 2/i));
    const timing = screen.getByLabelText("Tiempo de inicio de la línea 2");
    fireEvent.change(timing, { target: { value: "0:07.0" } });
    fireEvent.keyDown(timing, { key: "Enter" });

    await waitFor(() => expect(screen.getByTestId("line-review-progress")).toHaveTextContent("2/2"));
    expect(approve).not.toBeDisabled();

    await userEvent.click(approve);
    await waitFor(() => expect(onApprove).toHaveBeenCalledTimes(1));
    expect(onApprove.mock.calls[0][1]).toEqual(expect.objectContaining({
      confirmedLineIds: ["line-1", "line-2"],
    }));

    first.unmount();
    segmentsStore._clearAll();
    render(<LyricsEditor {...props()} />);
    expect(screen.getByTestId("line-review-progress")).toHaveTextContent("2/2");
  });

  it("confirma en bloque sólo las líneas y ventanas visibles", async () => {
    const onApprove = vi.fn();
    const editorRequest = vi.fn().mockResolvedValue(new Response(JSON.stringify({ ok: true }), {
      status: 200,
      headers: { "Content-Type": "application/json" },
    }));
    const transcriptionQuality = {
      policy_version: "lyrics-quality-v5",
      mode: "observe",
      decision: "review_required",
      evaluated_revision: 7,
      segments_hash: "campaign-hash",
      unsafe_windows: [
        { id: "visible-window", start: 0.5, end: 2.5, reasons: ["text_mismatch"] },
        { id: "hidden-window", start: 4.5, end: 6.5, reasons: ["timing"] },
      ],
    };
    render(<LyricsEditor {...props({ transcriptionQuality, editorRequest, onApprove })} />);

    const firstRow = screen.getByTestId("lyric-row-1");
    const secondRow = screen.getByTestId("lyric-row-2");
    firstRow.getBoundingClientRect = () => ({ top: 100, bottom: 150 });
    secondRow.getBoundingClientRect = () => ({ top: 900, bottom: 950 });
    Object.defineProperty(window, "innerHeight", { configurable: true, value: 800 });

    await userEvent.click(screen.getByRole("button", { name: "Confirmar bloque visible" }));
    expect(screen.getByTestId("line-review-progress")).toHaveTextContent("1/2");
    expect(screen.getByTestId("campaign-window-review-progress")).toHaveTextContent("1/2");
    expect(firstRow).toHaveAttribute("data-line-confirmed", "true");
    expect(secondRow).toHaveAttribute("data-line-confirmed", "false");

    secondRow.getBoundingClientRect = () => ({ top: 200, bottom: 250 });
    await userEvent.click(screen.getByRole("button", { name: "Confirmar bloque visible" }));
    expect(screen.getByTestId("line-review-progress")).toHaveTextContent("2/2");
    expect(screen.getByTestId("campaign-window-review-progress")).toHaveTextContent("2/2");

    const approve = screen.getByRole("button", { name: "Aprobar letra y timing" });
    expect(approve).not.toBeDisabled();
    await userEvent.click(approve);

    await waitFor(() => expect(editorRequest).toHaveBeenCalledWith(
      "/jobs/campaign-review-job/transcription-quality/acknowledge",
      expect.objectContaining({ method: "POST" }),
    ));
    const acknowledgementCall = editorRequest.mock.calls.find(
      ([path]) => path === "/jobs/campaign-review-job/transcription-quality/acknowledge",
    );
    const acknowledgement = JSON.parse(acknowledgementCall[1].body);
    expect(acknowledgement.confirmed_window_ids).toEqual(["visible-window", "hidden-window"]);
    expect(onApprove).toHaveBeenCalledTimes(1);
  });
});

describe("visibleReviewLineIds", () => {
  it("excluye filas fuera del viewport", () => {
    const rows = {
      1: { getBoundingClientRect: () => ({ top: -10, bottom: 10 }) },
      2: { getBoundingClientRect: () => ({ top: 500, bottom: 550 }) },
      3: { getBoundingClientRect: () => ({ top: 900, bottom: 950 }) },
    };
    expect(visibleReviewLineIds([{ _id: 1 }, { _id: 2 }, { _id: 3 }], rows, 800)).toEqual([1, 2]);
  });
});
