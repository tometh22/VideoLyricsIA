import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
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
  vi.unstubAllGlobals();
  segmentsStore._clearAll();
});

describe("LyricsEditor — aprobación por canción", () => {
  it("mueve el resaltado con el audio sin cambiar la línea que se está editando", async () => {
    let frame;
    vi.stubGlobal("requestAnimationFrame", vi.fn((callback) => { frame = callback; return 1; }));
    vi.stubGlobal("cancelAnimationFrame", vi.fn());
    const { container } = render(<LyricsEditor {...props({ audioUrl: "https://example.com/song.mp3" })} />);
    const audio = container.querySelector("audio");
    const first = screen.getByTestId("review-line-1");
    const second = screen.getByTestId("review-line-2");
    const input = screen.getByLabelText("Letra de esta línea");
    expect(first).toHaveAttribute("aria-current", "true");

    Object.defineProperty(audio, "paused", { configurable: true, value: false });
    fireEvent.play(audio);
    act(() => { audio.currentTime = 1.5; frame(); });
    expect(first).toHaveClass("bg-brand/20");

    // Keep a text edit in progress as playback crosses to the next lyric.
    fireEvent.focus(input);
    fireEvent.change(input, { target: { value: "Primera corregida" } });
    act(() => { audio.currentTime = 5.5; frame(); });
    expect(second).toHaveAttribute("aria-current", "true");
    expect(second).toHaveClass("bg-brand/20");
    expect(first).not.toHaveClass("bg-brand/20");
    expect(first).toHaveAttribute("data-selected", "true");
    expect(input).toHaveValue("Primera corregida");
    expect(screen.getByTestId("selected-line-panel")).toHaveTextContent("Línea 1");

    // Backward playback/seek follows the same clock rather than a row counter.
    act(() => { audio.currentTime = 1.5; frame(); });
    expect(first).toHaveAttribute("aria-current", "true");
    expect(second).not.toHaveAttribute("aria-current");

    Object.defineProperty(audio, "paused", { configurable: true, value: true });
    fireEvent.pause(audio);
    await userEvent.click(second);
    expect(second).toHaveAttribute("aria-current", "true");
    expect(input).toHaveValue("Segunda línea");
  });

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

 it("previews the selected paused line when two lines share their start", async () => {
   const user = userEvent.setup();
   render(<LyricsEditor {...props({segments:[
     {segment_id:"tie-a",start:1,end:2,text:"Primera frase empatada"},
     {segment_id:"tie-b",start:1,end:3,text:"Segunda frase empatada"},
   ]})} />);
   await user.click(screen.getByTestId("review-line-2"));
   const preview = screen.getByTestId("review-live-preview");
   expect(preview).toHaveTextContent(/Segunda frase empatada/i);
   expect(preview).not.toHaveTextContent(/Primera frase empatada/i);
   const input = screen.getByLabelText("Letra de esta línea",{exact:true});
   await user.clear(input);
   await user.type(input,"Segunda corregida en pausa");
   expect(preview).toHaveTextContent(/Segunda corregida en pausa/i);
 });

it.each(["original", "split", "undo"])("attests duplicate legacy occurrences after %s", async operation => {
  const onApprove = vi.fn();
  const rows = [
    { id: "16", start: 1, end: 3, text: "Primera línea completa" },
    { id: "16", start: 5, end: 7, text: "Segunda línea completa" },
    { segment_id: "index:0", start: 8, end: 9, text: "Tercera" },
  ];
  render(<LyricsEditor {...props({ segments: rows, onApprove })} />);
  if (operation !== "original") {
    const field = screen.getByLabelText("Letra de esta línea");
    act(() => { field.focus(); field.setSelectionRange(8, 8); });
    fireEvent.select(field);
    await userEvent.click(screen.getByRole("button", { name: "Dividir aquí", exact: true }));
    if (operation === "undo") await userEvent.click(screen.getByRole("button", { name: "Deshacer", exact: true }));
  }
  const snapshot = structuredClone(segmentsStore.get("campaign-review-job"));
  await userEvent.click(screen.getByRole("button", { name: "Aprobar letra y timing", exact: true }));
  await waitFor(() => expect(onApprove).toHaveBeenCalledTimes(1));
  const ids = onApprove.mock.calls[0][1].confirmedLineIds;
  if (operation === "split") {
    expect(ids).toHaveLength(4);
    expect(ids.slice(0, 2)).toEqual(snapshot.slice(0, 2).map(row => row.segment_id));
    expect(ids.slice(2)).toEqual(["16", "index:0"]);
  } else expect(ids).toEqual(["index:0#1", "index:1", "index:0"]);
  expect(new Set(ids).size).toBe(ids.length);
  expect(onApprove.mock.calls[0][0].map(({ text, start, end }) => ({ text, start, end })))
    .toEqual((operation === "split" ? snapshot : rows).map(({ text, start, end }) => ({ text, start, end })));
});
