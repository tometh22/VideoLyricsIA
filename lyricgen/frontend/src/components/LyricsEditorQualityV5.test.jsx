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

function markAudioReady(container, duration = 90) {
  const audio = container.querySelector("audio");
  Object.defineProperty(audio, "duration", { configurable: true, value: duration });
  Object.defineProperty(audio, "readyState", { configurable: true, value: 1 });
  fireEvent.loadedMetadata(audio);
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
    const { container } = render(<LyricsEditor {...baseProps({
      transcriptionQuality: pending,
      editorRequest,
      disableAutosave: true,
    })} />);
    markAudioReady(container);

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

  it("permite generar mientras el análisis de calidad sigue pendiente", async () => {
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
    expect(approve).toHaveAttribute("data-quality-review-required", "false");
    await userEvent.click(approve);

    await waitFor(() => expect(onApprove).toHaveBeenCalledTimes(1));
    expect(editorRequest).not.toHaveBeenCalledWith(
      "/jobs/quality-v5-job/transcription-quality/acknowledge",
      expect.anything(),
    );
  });

  it("distingue una candidata de baja confianza y protege el preview hasta confirmarla", async () => {
    const quality = {
      ...V5_QUALITY,
      unsafe_windows: [{ id: "low-confidence", start: 42, end: 55, reasons: ["text_audio_mismatch"] }],
    };
    const { container } = render(<LyricsEditor {...baseProps({
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
    markAudioReady(container);

    const row = screen.getByTestId("lyric-row-1");
    expect(row).toHaveAttribute("data-unsafe-candidate", "true");
    expect(screen.getByTestId("unsafe-candidate-label-1")).toHaveTextContent(/Revisar esta parte/i);
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

  it("no permite confirmar desde el panel básico sin audio disponible", () => {
    render(<LyricsEditor {...baseProps({
      audioUrl: null,
      disableAutosave: true,
    })} />);
    expect(screen.getByRole("button", { name: "Confirmar zona 1" })).toBeDisabled();
    expect(screen.getByRole("button", { name: "Confirmar zona 2" })).toBeDisabled();
  });

  it("no permite confirmar desde el panel básico una ventana fuera del audio", () => {
    const quality = {
      ...V5_QUALITY,
      unsafe_windows: [{ id: "outside", start: 43, end: 52, reasons: ["timing"] }],
    };
    const { container } = render(<LyricsEditor {...baseProps({
      transcriptionQuality: quality,
      disableAutosave: true,
    })} />);
    markAudioReady(container, 30);
    expect(screen.getByRole("button", { name: "Confirmar zona 1" })).toBeDisabled();
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

  it("abre Ajustar tiempos en revisión guiada y deja la timeline como opción avanzada", async () => {
    render(<LyricsEditor {...baseProps({
      waveform: { duration: 90, peaks: [0.1, 0.35, 0.9, 0.45, 0.2] },
      disableAutosave: true,
    })} />);

    await userEvent.click(screen.getByRole("button", { name: "Revisar sincronización" }));
    expect(screen.getByTestId("guided-timing-review")).toBeInTheDocument();
    expect(screen.getByText(/Encontramos 2 partes que conviene revisar/i)).toBeInTheDocument();
    expect(screen.getByText("Audio de la canción")).toBeInTheDocument();
    expect(screen.queryByTestId("quality-review-panel")).toBeNull();
    expect(screen.getByTestId("guided-waveform").querySelector("canvas")).toBeInTheDocument();

    await userEvent.click(screen.getByRole("tab", { name: "Timeline avanzada" }));
    expect(screen.getByTestId("timeline-scroll")).toBeInTheDocument();
    expect(screen.getByText("Audio de la canción")).toBeInTheDocument();
    expect(screen.getAllByTestId("timeline-unsafe-window")).toHaveLength(2);
    expect(screen.getByTestId("timeline-scroll").querySelector("canvas")).toBeInTheDocument();
  });

  it("mantiene retry_failed como diagnóstico fatal sin ofrecer confirmación", async () => {
    render(<LyricsEditor {...baseProps({
      transcriptionQuality: { ...V5_QUALITY, decision: "retry_failed" },
      disableAutosave: true,
    })} />);

    expect(screen.queryByRole("button", { name: "Revisar sincronización" })).toBeNull();
    await userEvent.click(screen.getByRole("tab", { name: "Ajustar tiempos" }));
    expect(screen.getByRole("tab", { name: "Timeline avanzada" })).toHaveAttribute("aria-selected", "true");
    expect(screen.getByTestId("quality-review-panel")).toBeInTheDocument();
    expect(screen.getAllByTestId("timeline-unsafe-window")).toHaveLength(2);
    expect(screen.queryByRole("button", { name: /Confirmar y seguir/i })).toBeNull();
  });

  it("acepta explícitamente el contrato lyrics-quality-v6", async () => {
    render(<LyricsEditor {...baseProps({
      transcriptionQuality: { ...V5_QUALITY, policy_version: "lyrics-quality-v6", schema_version: 6 },
      disableAutosave: true,
    })} />);
    await userEvent.click(screen.getByRole("button", { name: "Revisar sincronización" }));
    expect(screen.getByTestId("guided-timing-review")).toBeInTheDocument();
    expect(screen.getByText(/Encontramos 2 partes/i)).toBeInTheDocument();
  });

  it("fusiona IDs de ventana duplicados para que una confirmación no oculte otra", () => {
    render(<LyricsEditor {...baseProps({
      transcriptionQuality: {
        ...V5_QUALITY,
        unsafe_windows: [
          { id: "duplicate", start: 43, end: 47, reasons: ["timing"] },
          { id: "duplicate", start: 60, end: 83, reasons: ["event_count"] },
        ],
      },
      disableAutosave: true,
    })} />);

    expect(screen.getAllByTestId("unsafe-quality-window-duplicate")).toHaveLength(1);
    expect(screen.getByText("0:43.0–1:23.0")).toBeInTheDocument();
    expect(screen.getByTestId("quality-review-progress")).toHaveTextContent("0 de 1");
  });

  it("envía payloads legacy v4 directamente a la timeline sin un falso estado limpio", async () => {
    render(<LyricsEditor {...baseProps({
      transcriptionQuality: {
        policy_version: "lyrics-quality-v4",
        mode: "enforce",
        decision: "review_required",
        render_blocked: true,
        unsafe_windows: [{ id: "legacy", start: 43, end: 52, reasons: ["timing"] }],
      },
      disableAutosave: true,
    })} />);

    expect(screen.queryByRole("button", { name: "Revisar sincronización" })).toBeNull();
    await userEvent.click(screen.getByRole("tab", { name: "Ajustar tiempos" }));
    expect(screen.getByRole("tab", { name: "Timeline avanzada" })).toHaveAttribute("aria-selected", "true");
    expect(screen.queryByRole("tab", { name: "Revisión guiada" })).toBeNull();
    expect(screen.getByTestId("timeline-scroll")).toBeInTheDocument();
    expect(screen.getAllByTestId("timeline-unsafe-window")).toHaveLength(1);
    expect(screen.getByTestId("quality-review-panel")).toBeInTheDocument();
    expect(screen.queryByText(/No encontramos partes dudosas/i)).toBeNull();
  });

  it("detiene el loop antes de confirmar y cambiar de ventana", async () => {
    const pauseSpy = vi.spyOn(HTMLMediaElement.prototype, "pause");
    const { container } = render(<LyricsEditor {...baseProps({
      waveform: { duration: 90, peaks: [0.1, 0.35, 0.9, 0.45, 0.2] },
      disableAutosave: true,
    })} />);
    const audio = container.querySelector("audio");
    Object.defineProperty(audio, "duration", { configurable: true, value: 90 });
    Object.defineProperty(audio, "readyState", { configurable: true, value: 1 });
    fireEvent.loadedMetadata(audio);

    await userEvent.click(screen.getByRole("button", { name: "Revisar sincronización" }));
    pauseSpy.mockClear();
    await userEvent.click(screen.getByRole("button", { name: /Reproducir este tramo en loop/i }));
    await userEvent.click(screen.getByRole("button", { name: "Sí, está bien" }));
    await userEvent.click(screen.getByRole("button", { name: /Confirmar y seguir/i }));
    expect(pauseSpy).toHaveBeenCalled();
    expect(screen.getByText("Parte 2 de 2")).toBeInTheDocument();
  });

  it("pausa el audio al desmontar el editor", async () => {
    const pauseSpy = vi.spyOn(HTMLMediaElement.prototype, "pause");
    const { container, unmount } = render(<LyricsEditor {...baseProps({ disableAutosave: true })} />);
    const audio = container.querySelector("audio");
    Object.defineProperty(audio, "duration", { configurable: true, value: 90 });
    Object.defineProperty(audio, "readyState", { configurable: true, value: 1 });
    fireEvent.loadedMetadata(audio);
    await userEvent.click(screen.getByRole("button", { name: "Revisar sincronización" }));
    await userEvent.click(screen.getByRole("button", { name: /Reproducir este tramo en loop/i }));
    pauseSpy.mockClear();
    unmount();
    expect(pauseSpy).toHaveBeenCalled();
  });

  it("permite recorrer las vistas de sincronización con flechas", async () => {
    render(<LyricsEditor {...baseProps({ disableAutosave: true })} />);
    await userEvent.click(screen.getByRole("button", { name: "Revisar sincronización" }));
    const guided = screen.getByRole("tab", { name: "Revisión guiada" });
    guided.focus();
    fireEvent.keyDown(guided, { key: "ArrowRight" });
    expect(screen.getByRole("tab", { name: "Timeline avanzada" })).toHaveAttribute("aria-selected", "true");
    fireEvent.keyDown(screen.getByRole("tab", { name: "Timeline avanzada" }), { key: "ArrowLeft" });
    expect(screen.getByRole("tab", { name: "Revisión guiada" })).toHaveAttribute("aria-selected", "true");
  });

  it("permite aprobar sin confirmar ventanas y mantiene la revisión como orientación", async () => {
    const onApprove = vi.fn();
    const editorRequest = vi.fn().mockResolvedValue({ ok: true });
    const { container } = render(<LyricsEditor {...baseProps({ onApprove, editorRequest, disableAutosave: true })} />);
    markAudioReady(container);

    const approve = screen.getByRole("button", { name: /Aprobar y generar/i });
    expect(approve).toHaveAttribute("data-quality-review-required", "false");
    await userEvent.click(approve);

    await waitFor(() => expect(onApprove).toHaveBeenCalledTimes(1));
    expect(editorRequest).not.toHaveBeenCalledWith(
      "/jobs/quality-v5-job/transcription-quality/acknowledge",
      expect.anything(),
    );
    expect(onApprove.mock.calls[0][1]).toMatchObject({ baseRevision: 7 });
  });

  it("invalida las confirmaciones si cambia texto o timing antes de aprobar", async () => {
    const onApprove = vi.fn();
    const { container } = render(<LyricsEditor {...baseProps({ onApprove, disableAutosave: true })} />);
    markAudioReady(container);

    await userEvent.click(screen.getByRole("button", { name: "Confirmar zona 1" }));
    await userEvent.click(screen.getByRole("button", { name: "Confirmar zona 2" }));
    expect(screen.getByTestId("quality-review-progress")).toHaveTextContent("Zonas confirmadas");

    fireEvent.change(screen.getByDisplayValue("Primera zona"), { target: { value: "Texto corregido" } });
    expect(screen.getByTestId("quality-review-progress")).toHaveTextContent("0 de 2 confirmadas");

    await userEvent.click(screen.getByRole("button", { name: /Aprobar y generar/i }));
    await waitFor(() => expect(onApprove).toHaveBeenCalledTimes(1));
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

  it("retry_failed se conserva como diagnóstico pero no frena el render", async () => {
    const onApprove = vi.fn();
    render(<LyricsEditor {...baseProps({
      transcriptionQuality: { ...V5_QUALITY, decision: "retry_failed" },
      onApprove, disableAutosave: true,
    })} />);

    expect(screen.getByTestId("quality-review-panel")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /Confirmar zona/i })).toBeNull();
    await userEvent.click(screen.getByRole("button", { name: /Aprobar y generar/i }));
    await waitFor(() => expect(onApprove).toHaveBeenCalledTimes(1));
  });

  it("observe NO enmascara la letra del preview (no hay forma de destapar)", () => {
    // Reemplazar la letra por "Letra sin confirmar" sólo tiene sentido en
    // `enforce`, donde existe "Confirmar zona". En `observe` (el modo de
    // producción) ese botón no existe, así que enmascarar dejaría la letra
    // oculta sin forma de recuperarla.
    render(<LyricsEditor {...baseProps({
      transcriptionQuality: { ...V5_QUALITY, mode: "observe" },
      disableAutosave: true,
    })} />);
    expect(screen.queryByText(/Letra sin confirmar/i)).toBeNull();
    expect(screen.queryByTestId(/unsafe-candidate-label-/)).toBeNull();
    expect(document.querySelectorAll('[data-unsafe-marker="true"]')).toHaveLength(0);
  });

  it("resume una ventana que cubre varias líneas como una sola parte a revisar", () => {
    const quality = {
      ...V5_QUALITY,
      unsafe_windows: [{ id: "long-chorus", start: 42, end: 58, reasons: ["text_audio_mismatch"] }],
    };
    render(<LyricsEditor {...baseProps({
      transcriptionQuality: quality,
      segments: [
        { start: 42, end: 46, text: "Línea uno" },
        { start: 47, end: 51, text: "Línea dos" },
        { start: 52, end: 57, text: "Línea tres" },
      ],
      disableAutosave: true,
    })} />);

    expect(screen.getAllByTestId(/lyric-row-/)).toHaveLength(3);
    expect(document.querySelectorAll('[data-unsafe-candidate="true"]')).toHaveLength(3);
    expect(document.querySelectorAll('[data-unsafe-marker="true"]')).toHaveLength(1);
    expect(screen.getAllByTestId(/unsafe-candidate-label-/)).toHaveLength(1);
    expect(screen.getByText(/1 parte a revisar/i)).toBeInTheDocument();
  });
});
