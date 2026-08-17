import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it, vi } from "vitest";
import LyricsEditor, { isServerQualityAcknowledgementCurrent } from "./LyricsEditor";
import { segmentsStore } from "../state/segmentsStore";

vi.mock("../i18n", () => ({
  useI18n: () => ({ t: () => undefined }),
}));
vi.mock("./OnboardingTour", () => ({ EditorTour: () => null }));

const toastSpy = vi.fn();
vi.mock("./ToastProvider", () => ({
  useToast: () => ({ toast: toastSpy, dismiss: () => {} }),
  ToastProvider: ({ children }) => children,
}));

const V5_QUALITY = {
  policy_version: "lyrics-quality-v5",
  mode: "enforce",
  decision: "review_required",
  render_blocked: true,
  evaluated_revision: 7,
  segments_hash: "hash-v5-revision-7",
  unsafe_windows: [
    { id: "chorus", start: 43, end: 52.5, reasons: ["text_mismatch"] },
    { window_id: "outro", range: { start: 60.85, end: 83.27 }, risks: [{ code: "event_count" }] },
  ],
};

function baseProps(overrides = {}) {
  return {
    segments: [
      { start: 42, end: 55, text: "Primera zona" },
      { start: 60, end: 84, text: "Segunda zona" },
    ],
    filename: "song.mp3",
    audioFile: null,
    audioUrl: "https://media.example.test/song.wav",
    referenceLyrics: "",
    transcriptionQuality: V5_QUALITY,
    transcribeJobId: "quality-v5-job",
    segmentsRevision: 7,
    onApprove: vi.fn(),
    onBack: vi.fn(),
    ...overrides,
  };
}

afterEach(() => {
  cleanup();
  toastSpy.mockClear();
  segmentsStore._clearAll();
});

describe("LyricsEditor — revisión focalizada transcription quality v5", () => {
  it("actualiza analysis_pending después de abrir sin pisar la edición local", async () => {
    let resolveQuality;
    const editorRequest = vi.fn().mockReturnValue(new Promise((resolve) => {
      resolveQuality = resolve;
    }));
    const pending = {
      ...V5_QUALITY,
      analysis_status: "pending",
      analysis_pending: true,
      unsafe_windows: [{ id: "pending-outro", start: 60, end: 84, reasons: ["event_count"] }],
    };
    render(<LyricsEditor {...baseProps({
      transcriptionQuality: pending,
      editorRequest,
      disableAutosave: true,
    })} />);

    expect(screen.getByTestId("quality-analysis-pending")).toHaveTextContent(/Comprobando letra y timing/i);
    fireEvent.change(screen.getByDisplayValue("Primera zona"), {
      target: { value: "Mi corrección mientras analiza" },
    });

    resolveQuality(new Response(JSON.stringify({
      revision: 7,
      segments: [{ start: 42, end: 55, text: "Respuesta vieja del servidor" }],
      transcription_quality: V5_QUALITY,
    }), { status: 200, headers: { "Content-Type": "application/json" } }));

    await waitFor(() => expect(screen.getByTestId("quality-review-panel")).toBeInTheDocument());
    expect(screen.queryByTestId("quality-analysis-pending")).toBeNull();
    expect(screen.getByDisplayValue("Mi corrección mientras analiza")).toBeInTheDocument();
    expect(screen.queryByDisplayValue("Respuesta vieja del servidor")).toBeNull();
    expect(editorRequest).toHaveBeenCalledWith(
      "/editor/quality-v5-job",
      expect.objectContaining({ method: "GET", cache: "no-store" }),
    );
  });

  it("rechaza un resultado terminal obsoleto del mismo revision con otro hash/job", async () => {
    const pending = {
      ...V5_QUALITY,
      analysis_status: "pending",
      analysis_pending: true,
      analysis_job_id: "quality:new-snapshot",
      segments_hash: "new-snapshot-hash",
    };
    const stale = {
      ...V5_QUALITY,
      analysis_status: "complete",
      analysis_pending: false,
      analysis_job_id: "quality:old-snapshot",
      segments_hash: "old-snapshot-hash",
    };
    const editorRequest = vi.fn().mockResolvedValue(new Response(JSON.stringify({
      transcription_quality: stale,
    }), { status: 200, headers: { "Content-Type": "application/json" } }));

    render(<LyricsEditor {...baseProps({
      transcriptionQuality: pending, editorRequest, disableAutosave: true,
    })} />);

    await waitFor(() => expect(editorRequest).toHaveBeenCalled());
    expect(screen.getByTestId("quality-analysis-pending")).toBeInTheDocument();
    expect(screen.queryByTestId("quality-review-panel")).toBeNull();
  });

  it("rechaza evidencia terminal de otra configuración aunque coincidan revision/hash/job", async () => {
    const pending = {
      ...V5_QUALITY,
      analysis_status: "pending", analysis_pending: true,
      analysis_job_id: "quality:same-snapshot",
      pipeline_config_fingerprint: "config-current",
    };
    const stale = {
      ...V5_QUALITY,
      analysis_status: "complete", analysis_pending: false,
      analysis_job_id: "quality:same-snapshot",
      pipeline_config_fingerprint: "config-old",
      quality_fingerprint: "old-evidence",
    };
    const editorRequest = vi.fn().mockResolvedValue(new Response(JSON.stringify({
      transcription_quality: stale,
    }), { status: 200, headers: { "Content-Type": "application/json" } }));
    render(<LyricsEditor {...baseProps({
      transcriptionQuality: pending, editorRequest, disableAutosave: true,
    })} />);
    await waitFor(() => expect(editorRequest).toHaveBeenCalled());
    expect(screen.getByTestId("quality-analysis-pending")).toBeInTheDocument();
    expect(screen.queryByTestId("quality-review-panel")).toBeNull();
  });

  it("mantiene el gate enforce mientras analysis_pending y no envía acknowledgement", async () => {
    const onApprove = vi.fn();
    const editorRequest = vi.fn(() => new Promise(() => {}));
    render(<LyricsEditor {...baseProps({
      transcriptionQuality: {
        ...V5_QUALITY, analysis_status: "pending", analysis_pending: true,
      },
      editorRequest,
      onApprove,
      disableAutosave: true,
    })} />);

    const approve = screen.getByRole("button", { name: /Aprobar y generar/i });
    expect(approve).toHaveAttribute("data-quality-status", "analysis_pending");
    expect(approve).toHaveAttribute("data-quality-review-required", "true");
    await userEvent.click(approve);

    expect(onApprove).not.toHaveBeenCalled();
    expect(editorRequest).not.toHaveBeenCalledWith(
      "/jobs/quality-v5-job/transcription-quality/acknowledge",
      expect.anything(),
    );
    expect(toastSpy).toHaveBeenCalledWith(expect.objectContaining({
      message: expect.stringMatching(/todavía está terminando/i),
      tone: "info",
    }));
  });

  it("distingue una candidata de baja confianza y protege el preview hasta confirmarla", async () => {
    const quality = {
      ...V5_QUALITY,
      unsafe_windows: [{ id: "low-confidence", start: 42, end: 55, reasons: ["text_audio_mismatch"] }],
    };
    render(<LyricsEditor {...baseProps({
      transcriptionQuality: quality,
      segments: [
        {
          start: 42,
          end: 55,
          text: "Gracias inventado",
          words: [{ word: "Gracias", start: 42, end: 42.4, score: 0.11 }],
        },
      ],
      disableAutosave: true,
    })} />);

    const row = screen.getByTestId("lyric-row-1");
    expect(row).toHaveAttribute("data-unsafe-candidate", "true");
    expect(screen.getByTestId("unsafe-candidate-label-1")).toHaveTextContent(/Candidata insegura/i);
    expect(screen.getByTestId("unsafe-quality-window-low-confidence"))
      .toHaveAttribute("data-quality-confirmed", "false");

    await userEvent.click(screen.getByRole("button", { name: /Reproducir desde 0:42\.0/i }));
    const preview = screen.getByTestId("lyrics-preview-shell");
    expect(within(preview).getByText(/Letra sin confirmar/i)).toBeInTheDocument();
    expect(within(preview).queryByText(/Gracias inventado/i)).toBeNull();
    expect(screen.getByTestId("unsafe-preview-notice")).toBeInTheDocument();

    await userEvent.click(screen.getByRole("button", { name: "Confirmar zona 1" }));
    expect(row).toHaveAttribute("data-unsafe-candidate", "false");
    expect(within(preview).getByText(/Gracias inventado/i)).toBeInTheDocument();
    expect(screen.queryByTestId("unsafe-preview-notice")).toBeNull();
  });

  it("abre el editor, mantiene edición/reproducción y navega cada ventana al tiempo exacto", async () => {
    window.HTMLElement.prototype.scrollIntoView = vi.fn();
    const { container } = render(<LyricsEditor {...baseProps()} />);

    expect(screen.getByTestId("lyrics-editor")).toBeInTheDocument();
    expect(screen.getByDisplayValue("Primera zona")).toBeEnabled();
    expect(screen.getByRole("button", { name: "Reproducir" })).toBeEnabled();

    const panel = screen.getByTestId("quality-review-panel");
    expect(within(panel).getByText("0:43.0–0:52.5")).toBeInTheDocument();
    expect(within(panel).getByText("1:00.8–1:23.2")).toBeInTheDocument();
    expect(within(panel).getByText("Letra incierta")).toBeInTheDocument();
    expect(within(panel).getByText("Cantidad o estructura vocal incierta")).toBeInTheDocument();

    await userEvent.click(screen.getByRole("button", { name: /Ir a la zona 2/i }));
    expect(container.querySelector("audio").currentTime).toBeCloseTo(60.85, 4);
    expect(window.HTMLElement.prototype.scrollIntoView).toHaveBeenCalled();

    fireEvent.change(screen.getByDisplayValue("Primera zona"), { target: { value: "Primera zona corregida" } });
    expect(screen.getByDisplayValue("Primera zona corregida")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Reproducir" })).toBeEnabled();
  });

  it("impide aprobar hasta confirmar todas las ventanas y reconoce la revisión exacta", async () => {
    const onApprove = vi.fn();
    const editorRequest = vi.fn().mockResolvedValue({ ok: true });
    render(<LyricsEditor {...baseProps({ onApprove, editorRequest, disableAutosave: true })} />);

    const approve = screen.getByRole("button", { name: /Aprobar y generar/i });
    expect(approve).toHaveAttribute("data-quality-review-required", "true");
    await userEvent.click(approve);

    expect(onApprove).not.toHaveBeenCalled();
    expect(editorRequest).not.toHaveBeenCalledWith(
      "/jobs/quality-v5-job/transcription-quality/acknowledge",
      expect.anything(),
    );
    expect(toastSpy).toHaveBeenCalledWith(expect.objectContaining({
      message: expect.stringMatching(/2 zonas inseguras/i),
      tone: "info",
    }));

    await userEvent.click(screen.getByRole("button", { name: "Confirmar zona 1" }));
    expect(screen.getByTestId("quality-review-progress")).toHaveTextContent("1 de 2 confirmadas");
    await userEvent.click(screen.getByRole("button", { name: "Confirmar zona 2" }));
    expect(screen.getByTestId("quality-review-progress")).toHaveTextContent("Zonas confirmadas");
    expect(approve).toHaveAttribute("data-quality-review-required", "false");

    await userEvent.click(approve);
    await waitFor(() => expect(onApprove).toHaveBeenCalledTimes(1));
    expect(editorRequest).toHaveBeenCalledWith(
      "/jobs/quality-v5-job/transcription-quality/acknowledge",
      expect.objectContaining({
        method: "POST",
        body: JSON.stringify({
          base_revision: 7,
          confirmed_window_ids: ["chorus", "outro"],
        }),
      }),
    );
    expect(onApprove.mock.calls[0][1]).toMatchObject({ baseRevision: 7 });
  });

  it("invalida las confirmaciones si cambia texto o timing antes de aprobar", async () => {
    const onApprove = vi.fn();
    render(<LyricsEditor {...baseProps({ onApprove, disableAutosave: true })} />);

    await userEvent.click(screen.getByRole("button", { name: "Confirmar zona 1" }));
    await userEvent.click(screen.getByRole("button", { name: "Confirmar zona 2" }));
    expect(screen.getByTestId("quality-review-progress")).toHaveTextContent("Zonas confirmadas");

    fireEvent.change(screen.getByDisplayValue("Primera zona"), { target: { value: "Texto corregido" } });
    expect(screen.getByTestId("quality-review-progress")).toHaveTextContent("0 de 2 confirmadas");

    await userEvent.click(screen.getByRole("button", { name: /Aprobar y generar/i }));
    expect(onApprove).not.toHaveBeenCalled();
  });

  it("solo reutiliza una confirmación del servidor si coincide el fingerprint de evidencia", () => {
    const matching = {
      ...V5_QUALITY,
      quality_fingerprint: "evidence-current",
      acknowledgement: {
        revision: 7,
        segments_hash: "hash-v5-revision-7",
        quality_fingerprint: "evidence-current",
        confirmed_window_ids: ["chorus", "outro"],
      },
    };
    expect(isServerQualityAcknowledgementCurrent({
      quality: matching, revision: 7,
    })).toBe(true);
    expect(isServerQualityAcknowledgementCurrent({
      quality: matching, revision: 7, dirty: true,
    })).toBe(false);
    expect(isServerQualityAcknowledgementCurrent({
      quality: {
        ...matching,
        acknowledgement: {
          ...matching.acknowledgement,
          quality_fingerprint: "stale-evidence",
        },
      },
      revision: 7,
    })).toBe(false);
  });

  it("mantiene compatible el payload v4: muestra y navega ventanas sin agregar el gate v5", async () => {
    const onApprove = vi.fn();
    const editorRequest = vi.fn().mockResolvedValue({ ok: true });
    const v4 = {
      ...V5_QUALITY,
      policy_version: "lyrics-quality-v4",
      unsafe_windows: [{ start: 43, end: 52.5, reasons: ["text_mismatch"] }],
    };
    render(<LyricsEditor {...baseProps({
      transcriptionQuality: v4,
      onApprove,
      editorRequest,
      disableAutosave: true,
    })} />);

    expect(screen.getByTestId("quality-review-panel")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /Ir a la zona 1/i })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /Confirmar zona/i })).toBeNull();

    const approve = screen.getByRole("button", { name: /Aprobar y generar/i });
    expect(approve).toHaveAttribute("data-quality-review-required", "false");
    await userEvent.click(approve);
    await waitFor(() => expect(onApprove).toHaveBeenCalledTimes(1));
  });

  it("observe muestra el diagnóstico pero nunca llama al acknowledgement", async () => {
    const onApprove = vi.fn();
    const editorRequest = vi.fn().mockResolvedValue({ ok: true });
    render(<LyricsEditor {...baseProps({
      transcriptionQuality: { ...V5_QUALITY, mode: "observe" },
      onApprove, editorRequest, disableAutosave: true,
    })} />);

    expect(screen.queryByTestId("quality-review-panel")).toBeNull();
    await userEvent.click(screen.getByRole("button", { name: /Aprobar y generar/i }));
    await waitFor(() => expect(onApprove).toHaveBeenCalledTimes(1));
    expect(editorRequest).not.toHaveBeenCalledWith(
      "/jobs/quality-v5-job/transcription-quality/acknowledge",
      expect.anything(),
    );
  });

  it("retry_failed no se presenta como confirmable ni llega al render", async () => {
    const onApprove = vi.fn();
    render(<LyricsEditor {...baseProps({
      transcriptionQuality: { ...V5_QUALITY, decision: "retry_failed" },
      onApprove, disableAutosave: true,
    })} />);

    expect(screen.getByTestId("quality-review-panel")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /Confirmar zona/i })).toBeNull();
    await userEvent.click(screen.getByRole("button", { name: /Aprobar y generar/i }));
    expect(onApprove).not.toHaveBeenCalled();
    expect(toastSpy).toHaveBeenCalledWith(expect.objectContaining({ tone: "error" }));
  });
});
