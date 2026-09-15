/**
 * Versión B, parte 2 — botón "Re-sincronizar con IA" del LyricsEditor.
 *
 * Contrato:
 *  - Gate: solo aparece con onReanchor + transcribeJobId + user.features
 *    .anchor_lyrics === true (flag ANCHOR_LYRICS_ENABLED del backend).
 *  - Click: flushea el estado local a /save-segments ANTES del reanchor
 *    (el backend re-ancla segments_json — sin el flush re-anclaría texto
 *    viejo), llama onReanchor(jobId) y reemplaza `edited` con los
 *    segments re-anclados que devuelve el endpoint.
 *  - Decline / error → toast de error y los timings quedan como estaban.
 */
import { render, screen, cleanup, fireEvent, waitFor, act } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import LyricsEditor from "./LyricsEditor";

vi.mock("../i18n", () => ({
  useI18n: () => ({ t: () => "" }),
}));
vi.mock("./OnboardingTour", () => ({
  EditorTour: () => null,
}));

const toastSpy = vi.fn();
vi.mock("./ToastProvider", () => ({
  useToast: () => ({ toast: toastSpy, dismiss: () => {} }),
  ToastProvider: ({ children }) => children,
}));

const SEGMENTS = [
  { start: 0.0, end: 2.0, text: "linea uno" },
  { start: 2.0, end: 4.0, text: "linea dos" },
  { start: 4.0, end: 6.0, text: "linea tres" },
];

function baseProps(overrides = {}) {
  return {
    segments: SEGMENTS,
    filename: "song.mp3",
    audioFile: null,
    referenceLyrics: "",
    onApprove: vi.fn(),
    onBack: vi.fn(),
    transcribeJobId: "job-reanchor",
    user: { features: { anchor_lyrics: true } },
    disableAutosave: true,
    ...overrides,
  };
}

afterEach(() => {
  cleanup();
  toastSpy.mockReset();
});

describe("Re-sincronizar con IA", () => {
  it("NO se muestra sin features.anchor_lyrics", () => {
    render(
      <LyricsEditor {...baseProps({
        user: { features: {} },
        onReanchor: vi.fn(),
      })} />,
    );
    expect(screen.queryByTestId("reanchor-btn")).toBeNull();
  });

  it("NO se muestra sin onReanchor (padre sin soporte)", () => {
    render(<LyricsEditor {...baseProps()} />);
    expect(screen.queryByTestId("reanchor-btn")).toBeNull();
  });

  it("click → flush a save-segments, luego reanchor, y aplica los segments nuevos", async () => {
    const calls = [];
    const onPersistSegments = vi.fn(async (jobId, cleaned) => {
      calls.push(["persist", jobId, cleaned.map((s) => s.text)]);
      return { ok: true };
    });
    const onReanchor = vi.fn(async (jobId) => {
      calls.push(["reanchor", jobId]);
      return {
        ok: true,
        count: 3,
        review_count: 1,
        segments: [
          { start: 0.4, end: 2.1, text: "linea uno" },
          { start: 2.6, end: 4.2, text: "linea dos", review: true },
          { start: 4.8, end: 6.3, text: "linea tres" },
        ],
      };
    });
    render(
      <LyricsEditor {...baseProps({ onPersistSegments, onReanchor })} />,
    );

    fireEvent.click(screen.getByRole("tab", { name: "Ajustar tiempos" }));
    fireEvent.click(screen.getByTestId("editor-overflow-btn"));

    fireEvent.click(screen.getByTestId("reanchor-btn"));

    await waitFor(() => expect(onReanchor).toHaveBeenCalledWith("job-reanchor", 0));
    // El flush al backend corre ANTES del reanchor, con el texto actual.
    expect(calls[0][0]).toBe("persist");
    expect(calls[0][1]).toBe("job-reanchor");
    expect(calls[0][2]).toEqual(["linea uno", "linea dos", "linea tres"]);
    expect(calls[1][0]).toBe("reanchor");
    // Toast de éxito con los contadores del endpoint.
    await waitFor(() => expect(toastSpy).toHaveBeenCalled());
    expect(toastSpy.mock.calls[0][0].tone).toBe("success");
    expect(toastSpy.mock.calls[0][0].message).toContain("3");
    expect(toastSpy.mock.calls[0][0].message).toBe("3 líneas re-sincronizadas");
  });

  it("decline del backend (ok:false) → toast de error, sin romper el editor", async () => {
    const onReanchor = vi.fn(async () => ({ ok: false, reason: "declined" }));
    render(
      <LyricsEditor {...baseProps({
        onPersistSegments: vi.fn(async () => ({ ok: true })),
        onReanchor,
      })} />,
    );

    fireEvent.click(screen.getByRole("tab", { name: "Ajustar tiempos" }));
    fireEvent.click(screen.getByTestId("editor-overflow-btn"));

    fireEvent.click(screen.getByTestId("reanchor-btn"));

    await waitFor(() => expect(toastSpy).toHaveBeenCalled());
    expect(toastSpy.mock.calls[0][0].tone).toBe("error");
    // Los segments del editor siguen siendo los originales. Sin audio, la
    // vista avanzada conserva su shell explícito en vez de degradar a la lista.
    expect(screen.getByTestId("advanced-audio-unavailable")).toBeInTheDocument();
    expect(screen.queryByDisplayValue("linea uno")).toBeNull();
  });

  it("excepción del callback → toast de error (nunca crashea)", async () => {
    const onReanchor = vi.fn(async () => { throw new Error("network"); });
    render(
      <LyricsEditor {...baseProps({
        onPersistSegments: vi.fn(async () => ({ ok: true })),
        onReanchor,
      })} />,
    );
    fireEvent.click(screen.getByRole("tab", { name: "Ajustar tiempos" }));
    fireEvent.click(screen.getByTestId("editor-overflow-btn"));
    fireEvent.click(screen.getByTestId("reanchor-btn"));
    await waitFor(() => expect(toastSpy).toHaveBeenCalled());
    expect(toastSpy.mock.calls[0][0].tone).toBe("error");
  });
});

describe("Pegar letra oficial y re-sincronizar", () => {
  const openPaste = () => {
    fireEvent.click(screen.getByRole("tab", { name: "Ajustar tiempos" }));
    fireEvent.click(screen.getByTestId("editor-overflow-btn"));
    fireEvent.click(screen.getByTestId("paste-lyrics-btn"));
  };

  it("NO se muestra sin features.anchor_lyrics", () => {
    render(<LyricsEditor {...baseProps({ user: { features: {} }, onReanchor: vi.fn() })} />);
    expect(screen.queryByTestId("paste-lyrics-btn")).toBeNull();
  });

  it("envía lyrics_text al callback (tras flush) y aplica los segments nuevos", async () => {
    const onPersistSegments = vi.fn(async () => ({ ok: true }));
    const onReanchor = vi.fn(async () => ({
      ok: true, count: 4, review_count: 1, lines_replaced: 1, lines_kept: 3,
      segments: [
        { start: 0.4, end: 2.1, text: "linea uno" },
        { start: 2.6, end: 4.2, text: "linea dos" },
        { start: 4.8, end: 6.3, text: "linea tres oficial", review: true },
        { start: 6.9, end: 8.0, text: "linea cuatro" },
      ],
    }));
    render(<LyricsEditor {...baseProps({ onPersistSegments, onReanchor })} />);
    openPaste();
    const pasted = "linea uno\nlinea dos\nlinea tres oficial\nlinea cuatro";
    fireEvent.change(screen.getByTestId("paste-lyrics-textarea"), { target: { value: pasted } });
    expect(screen.getByTestId("paste-lyrics-count")).toHaveTextContent("4 líneas");
    fireEvent.click(screen.getByTestId("paste-lyrics-submit"));
    await waitFor(() => expect(onReanchor).toHaveBeenCalledWith("job-reanchor", 0, {
      lyrics_text: pasted, confirm_structure: false,
    }));
    expect(onPersistSegments).toHaveBeenCalled();
    await waitFor(() => expect(toastSpy).toHaveBeenCalled());
    expect(toastSpy.mock.calls[0][0].tone).toBe("success");
    expect(toastSpy.mock.calls[0][0].message).toBe("Letra aplicada: 1 líneas nuevas, 3 conservadas");
    expect(screen.queryByTestId("paste-lyrics-textarea")).toBeNull();
  });

  it("con menos de 3 líneas el envío queda deshabilitado", () => {
    const onReanchor = vi.fn();
    render(<LyricsEditor {...baseProps({ onPersistSegments: vi.fn(async () => ({ ok: true })), onReanchor })} />);
    openPaste();
    fireEvent.change(screen.getByTestId("paste-lyrics-textarea"), { target: { value: "una\ndos" } });
    expect(screen.getByTestId("paste-lyrics-submit")).toBeDisabled();
    expect(onReanchor).not.toHaveBeenCalled();
  });

  it("409 reference_structure_unconfirmed → muestra el reporte y permite forzar con confirm_structure", async () => {
    const onReanchor = vi.fn()
      .mockResolvedValueOnce({
        ok: false, status: 409, code: "reference_structure_unconfirmed",
        structure: {
          supported: false, reasons: ["line_count_divergent", "reference_contains_unmatched_passage"],
          metrics: { reference_token_coverage: 0.79, longest_unmatched_content_run: 8 },
          pasted_line_count: 12, current_line_count: 3,
        },
      })
      .mockResolvedValueOnce({
        ok: true, count: 12, review_count: 12, lines_replaced: 12, lines_kept: 0,
        segments: Array.from({ length: 12 }, (_, i) => ({ start: i, end: i + 0.9, text: `l${i}`, review: true })),
      });
    render(<LyricsEditor {...baseProps({ onPersistSegments: vi.fn(async () => ({ ok: true })), onReanchor })} />);
    openPaste();
    const other = Array.from({ length: 12 }, (_, i) => `estrofa ${i}`).join("\n");
    fireEvent.change(screen.getByTestId("paste-lyrics-textarea"), { target: { value: other } });
    fireEvent.click(screen.getByTestId("paste-lyrics-submit"));
    const report = await screen.findByTestId("paste-lyrics-structure");
    expect(report).toHaveTextContent("12 líneas pegadas vs 3");
    expect(screen.queryByTestId("reanchor-progress-overlay")).toBeNull();
    expect(report).toHaveTextContent("Hay diferencias con el texto del editor");
    expect(report).toHaveTextContent("tramo de 8 palabras diferentes entre ambos textos");
    expect(report).toHaveTextContent("Coincidencia de palabras con el texto del editor: 79%");
    expect(report).toHaveTextContent("Este aviso no demuestra que tu letra esté mal");
    expect(report).not.toHaveTextContent("se reconoce en el audio");
    expect(report).not.toHaveTextContent("otra versión");
    // Nada se aplicó todavía: sigue abierto y sin toast.
    expect(toastSpy).not.toHaveBeenCalled();
    fireEvent.click(screen.getByTestId("paste-lyrics-confirm-anyway"));
    await waitFor(() => expect(onReanchor).toHaveBeenCalledTimes(2));
    expect(onReanchor.mock.calls[1][2]).toEqual({ lyrics_text: other, confirm_structure: true });
    await waitFor(() => expect(toastSpy).toHaveBeenCalled());
    expect(toastSpy.mock.calls[0][0].tone).toBe("success");
  });

  it("'Usar la letra de la planilla' prellena el textarea con la referencia de campaña", () => {
    render(<LyricsEditor {...baseProps({
      onPersistSegments: vi.fn(async () => ({ ok: true })),
      onReanchor: vi.fn(),
      sourceReference: { status: "candidate", artist: "X", track: "Y", text: "a\nb\nc" },
    })} />);
    openPaste();
    fireEvent.click(screen.getByTestId("paste-lyrics-use-sheet"));
    expect(screen.getByTestId("paste-lyrics-textarea")).toHaveValue("a\nb\nc");
    expect(screen.getByTestId("paste-lyrics-submit")).toBeEnabled();
  });
});

describe("Recuperación tras respuesta perdida (2026-09-14)", () => {
  const RECOVERED = [
    { start: 0.4, end: 2.1, text: "linea uno" },
    { start: 2.6, end: 4.2, text: "linea dos", review: true },
    { start: 4.8, end: 6.3, text: "linea tres" },
  ];
  const openPaste = () => {
    fireEvent.click(screen.getByRole("tab", { name: "Ajustar tiempos" }));
    fireEvent.click(screen.getByTestId("editor-overflow-btn"));
    fireEvent.click(screen.getByTestId("paste-lyrics-btn"));
  };

  it("pegar letra: 409 del duplicado + revisión avanzada en el servidor → éxito y modal cerrado", async () => {
    const onReanchor = vi.fn(async () => ({ ok: false, reason: "http-409", status: 409, code: "stale_revision" }));
    const onReanchorReconcile = vi.fn(async (jobId, base) => ({ ok: true, recovered: true, revision: base + 1, count: 3, review_count: 1, segments: RECOVERED }));
    render(<LyricsEditor {...baseProps({ onPersistSegments: vi.fn(async () => ({ ok: true })), onReanchor, onReanchorReconcile })} />);
    openPaste();
    fireEvent.change(screen.getByTestId("paste-lyrics-textarea"), { target: { value: "linea uno\nlinea dos\nlinea tres" } });
    fireEvent.click(screen.getByTestId("paste-lyrics-submit"));
    await waitFor(() => expect(onReanchorReconcile).toHaveBeenCalledWith("job-reanchor", 0));
    await waitFor(() => expect(toastSpy).toHaveBeenCalled());
    expect(toastSpy.mock.calls[0][0].tone).toBe("success");
    expect(screen.queryByTestId("paste-lyrics-textarea")).toBeNull();
  });

  it("re-sincronizar con IA: red cortada + revisión avanzada → éxito", async () => {
    const onReanchor = vi.fn(async () => { throw new Error("network"); });
    const onReanchorReconcile = vi.fn(async (jobId, base) => ({ ok: true, recovered: true, revision: base + 1, count: 3, review_count: 0, segments: RECOVERED }));
    render(<LyricsEditor {...baseProps({ onPersistSegments: vi.fn(async () => ({ ok: true })), onReanchor, onReanchorReconcile })} />);
    fireEvent.click(screen.getByRole("tab", { name: "Ajustar tiempos" }));
    fireEvent.click(screen.getByTestId("editor-overflow-btn"));
    fireEvent.click(screen.getByTestId("reanchor-btn"));
    await waitFor(() => expect(toastSpy).toHaveBeenCalled());
    expect(toastSpy.mock.calls[0][0].tone).toBe("success");
  });

  it("re-sincronizar con IA: fallo real (la revisión NO avanzó) → error, sin tocar nada", async () => {
    const onReanchor = vi.fn(async () => ({ ok: false, reason: "declined" }));
    const onReanchorReconcile = vi.fn(async () => ({ ok: false, reason: "not-advanced", revision: 0 }));
    render(<LyricsEditor {...baseProps({ onPersistSegments: vi.fn(async () => ({ ok: true })), onReanchor, onReanchorReconcile, reanchorWaitMs: 60, reanchorPollMs: 10 })} />);
    fireEvent.click(screen.getByRole("tab", { name: "Ajustar tiempos" }));
    fireEvent.click(screen.getByTestId("editor-overflow-btn"));
    fireEvent.click(screen.getByTestId("reanchor-btn"));
    await waitFor(() => expect(toastSpy).toHaveBeenCalled());
    expect(onReanchorReconcile).not.toHaveBeenCalled();
    expect(toastSpy.mock.calls[0][0].tone).toBe("error");
  });
});

describe("Botón visible 'Pegar letra oficial' (2026-09-14)", () => {
  it("aparece en la pestaña de texto (vista por defecto) y abre el modal sin pasar por Herramientas", () => {
    render(<LyricsEditor {...baseProps({ onPersistSegments: vi.fn(async () => ({ ok: true })), onReanchor: vi.fn() })} />);
    // Vista por defecto = "Revisar letra": no hay menú Herramientas ahí…
    expect(screen.queryByTestId("editor-overflow-btn")).toBeNull();
    // …pero el CTA sí está.
    fireEvent.click(screen.getByTestId("paste-lyrics-cta"));
    expect(screen.getByTestId("paste-lyrics-textarea")).toBeInTheDocument();
  });

  it("también está en 'Ajustar tiempos' y respeta el gate de features", () => {
    const { unmount } = render(<LyricsEditor {...baseProps({ onPersistSegments: vi.fn(async () => ({ ok: true })), onReanchor: vi.fn() })} />);
    fireEvent.click(screen.getByRole("tab", { name: "Ajustar tiempos" }));
    expect(screen.getByTestId("paste-lyrics-cta")).toBeInTheDocument();
    unmount();
    render(<LyricsEditor {...baseProps({ user: { features: {} }, onReanchor: vi.fn() })} />);
    expect(screen.queryByTestId("paste-lyrics-cta")).toBeNull();
  });
});

describe("Seguir esperando al servidor (2026-09-14, caso Agus)", () => {
  const RECOVERED = [
    { start: 0.4, end: 2.1, text: "linea uno" },
    { start: 2.6, end: 4.2, text: "linea dos", review: true },
    { start: 4.8, end: 6.3, text: "linea tres" },
  ];
  const openPaste = () => {
    fireEvent.click(screen.getByRole("tab", { name: "Ajustar tiempos" }));
    fireEvent.click(screen.getByTestId("editor-overflow-btn"));
    fireEvent.click(screen.getByTestId("paste-lyrics-btn"));
  };

  it("la revisión avanza recién en el 3er sondeo → muestra la espera y termina en éxito", async () => {
    const onReanchor = vi.fn(async () => { throw new Error("connection reset"); });
    let polls = 0;
    const onReanchorReconcile = vi.fn(async (jobId, base) => {
      polls += 1;
      if (polls < 3) return { ok: false, reason: "not-advanced", revision: base };
      return { ok: true, recovered: true, revision: base + 1, count: 3, review_count: 1, segments: RECOVERED };
    });
    render(<LyricsEditor {...baseProps({ onPersistSegments: vi.fn(async () => ({ ok: true })), onReanchor, onReanchorReconcile, reanchorWaitMs: 5000, reanchorPollMs: 20 })} />);
    openPaste();
    fireEvent.change(screen.getByTestId("paste-lyrics-textarea"), { target: { value: "linea uno\nlinea dos\nlinea tres" } });
    fireEvent.click(screen.getByTestId("paste-lyrics-submit"));
    await screen.findByTestId("paste-lyrics-waiting");
    expect(screen.getByTestId("reanchor-progress-overlay")).toHaveTextContent("Estamos comprobando si la re-sincronización terminó");
    await waitFor(() => expect(toastSpy).toHaveBeenCalled(), { timeout: 3000 });
    expect(polls).toBeGreaterThanOrEqual(3);
    expect(toastSpy.mock.calls[0][0].tone).toBe("success");
    expect(screen.queryByTestId("paste-lyrics-textarea")).toBeNull();
  });

  it("si el servidor nunca avanza dentro del plazo → error, sin aplicar nada", async () => {
    const onReanchor = vi.fn(async () => ({ ok: false, reason: "network" }));
    const onReanchorReconcile = vi.fn(async (jobId, base) => ({ ok: false, reason: "not-advanced", revision: base }));
    render(<LyricsEditor {...baseProps({ onPersistSegments: vi.fn(async () => ({ ok: true })), onReanchor, onReanchorReconcile, reanchorWaitMs: 60, reanchorPollMs: 10 })} />);
    openPaste();
    fireEvent.change(screen.getByTestId("paste-lyrics-textarea"), { target: { value: "linea uno\nlinea dos\nlinea tres" } });
    fireEvent.click(screen.getByTestId("paste-lyrics-submit"));
    await waitFor(() => expect(screen.getByRole("alert")).toHaveTextContent(/No se pudo confirmar/), { timeout: 3000 });
    expect(onReanchorReconcile.mock.calls.length).toBeGreaterThanOrEqual(2);
    expect(screen.queryByTestId("paste-lyrics-waiting")).toBeNull();
    expect(screen.queryByTestId("reanchor-progress-overlay")).toBeNull();
    expect(toastSpy).not.toHaveBeenCalled();
  });
});


describe("Loader bloqueante de re-sincronización", () => {
  function start(mode) {
    fireEvent.click(screen.getByRole("tab", { name: "Ajustar tiempos" }));
    fireEvent.click(screen.getByTestId("editor-overflow-btn"));
    if (mode === "paste") {
      fireEvent.click(screen.getByTestId("paste-lyrics-btn"));
      fireEvent.change(screen.getByTestId("paste-lyrics-textarea"), { target: { value: "linea uno\nlinea dos\nlinea tres" } });
      fireEvent.click(screen.getByTestId("paste-lyrics-submit"));
    } else fireEvent.click(screen.getByTestId("reanchor-btn"));
  }

  it.each(["editor", "paste"])("%s: a failed save never recovers an unrelated revision as re-sync success", async (mode) => {
    const onPersistSegments = vi.fn(async () => ({ ok: false, reason: "network" }));
    const onReanchor = vi.fn();
    const onReanchorReconcile = vi.fn(async () => ({ ok: true, revision: 9, segments: SEGMENTS }));
    render(<LyricsEditor {...baseProps({ onPersistSegments, onReanchor, onReanchorReconcile })} />);
    start(mode);
    await waitFor(() => expect(screen.queryByTestId("reanchor-progress-overlay")).toBeNull());
    expect(onReanchor).not.toHaveBeenCalled();
    expect(onReanchorReconcile).not.toHaveBeenCalled();
    if (mode === "paste") {
      expect(screen.getByRole("alert")).toHaveTextContent("no se pudieron guardar tus cambios");
      expect(screen.getByTestId("paste-lyrics-textarea")).toHaveValue("linea uno\nlinea dos\nlinea tres");
    } else {
      expect(toastSpy).toHaveBeenCalledWith(expect.objectContaining({
        tone: "error", message: expect.stringContaining("no se pudieron guardar tus cambios"),
      }));
    }
  });

  it("confirmation shows the completed task's real error and keeps the pasted lyrics", async () => {
    const onReanchor = vi.fn()
      .mockResolvedValueOnce({ ok: false, code: "reference_structure_unconfirmed", structure: {
        pasted_line_count: 40, current_line_count: 18, reasons: ["line_count_divergent"], metrics: {},
      } })
      .mockResolvedValueOnce({ ok: false, status: 409, terminal: true,
        detail: "El audio original ya no está disponible para este job." });
    const onReanchorReconcile = vi.fn(async () => ({ ok: true, revision: 9, segments: SEGMENTS }));
    render(<LyricsEditor {...baseProps({ onReanchor, onReanchorReconcile })} />);
    start("paste");
    await waitFor(() => expect(screen.getByTestId("paste-lyrics-confirm-anyway")).toBeEnabled());
    fireEvent.click(screen.getByTestId("paste-lyrics-confirm-anyway"));
    expect(await screen.findByText("El audio original ya no está disponible para este job.")).toHaveAttribute("role", "alert");
    expect(onReanchor).toHaveBeenLastCalledWith("job-reanchor", 0, {
      lyrics_text: "linea uno\nlinea dos\nlinea tres", confirm_structure: true,
    });
    expect(onReanchorReconcile).not.toHaveBeenCalled();
    expect(screen.getByTestId("paste-lyrics-textarea")).toHaveValue("linea uno\nlinea dos\nlinea tres");
    expect(toastSpy).not.toHaveBeenCalled();
  });

  it("an alignment decline never loads unrelated server lyrics", async () => {
    const onReanchor = vi.fn(async () => ({ ok: false, reason: "declined" }));
    const onReanchorReconcile = vi.fn(async () => ({ ok: true, revision: 9, segments: SEGMENTS }));
    render(<LyricsEditor {...baseProps({ onReanchor, onReanchorReconcile })} />);
    start("paste");
    await waitFor(() => expect(screen.getByRole("alert")).toHaveTextContent("alineación utilizable"));
    expect(onReanchorReconcile).not.toHaveBeenCalled();
    expect(toastSpy).not.toHaveBeenCalled();
  });

  it.each(["editor", "paste"])("%s: bloquea desde el guardado hasta aplicar la respuesta", async (mode) => {
    let finishSave;
    let finishReanchor;
    const onPersistSegments = vi.fn(() => new Promise((resolve) => { finishSave = resolve; }));
    const onReanchor = vi.fn(() => new Promise((resolve) => { finishReanchor = resolve; }));
    const { container } = render(<LyricsEditor {...baseProps({ onPersistSegments, onReanchor })} />);
    start(mode);
    const loader = screen.getByRole("dialog", { name: "Re-sincronizando…" });
    expect(loader).toHaveFocus();
    expect(container).toHaveAttribute("inert");
    expect(screen.getByTestId("lyrics-editor")).toHaveAttribute("aria-busy", "true");
    expect(onReanchor).not.toHaveBeenCalled();
    fireEvent.keyDown(window, { key: "Escape" });
    fireEvent.click(screen.getByTestId("reanchor-progress-overlay"));
    expect(loader).toBeInTheDocument();
    await act(async () => { finishSave({ ok: true, revision: 1 }); });
    await waitFor(() => expect(onReanchor).toHaveBeenCalledTimes(1));
    expect(loader).toBeInTheDocument();
    await act(async () => { finishReanchor({ ok: true, revision: 2, segments: SEGMENTS, count: 3 }); });
    await waitFor(() => expect(screen.queryByTestId("reanchor-progress-overlay")).toBeNull());
    expect(container).not.toHaveAttribute("inert");
    expect(screen.getByTestId("lyrics-editor")).toHaveAttribute("aria-busy", "false");
    expect(toastSpy).toHaveBeenCalledWith(expect.objectContaining({ tone: "success" }));
  });

  it.each(["editor", "paste"])("%s: desbloquea al fallar sin descartar el texto", async (mode) => {
    let fail;
    const onReanchor = vi.fn(() => new Promise((_, reject) => { fail = reject; }));
    const { container } = render(<LyricsEditor {...baseProps({ onReanchor })} />);
    start(mode);
    expect(screen.getByTestId("reanchor-progress-overlay")).toBeInTheDocument();
    await act(async () => { fail(new Error("network")); });
    await waitFor(() => expect(screen.queryByTestId("reanchor-progress-overlay")).toBeNull());
    expect(container).not.toHaveAttribute("inert");
    if (mode === "paste") {
      expect(screen.getByTestId("paste-lyrics-textarea")).toHaveValue("linea uno\nlinea dos\nlinea tres");
      expect(screen.getByRole("alert")).toHaveTextContent("No se pudo confirmar");
    } else expect(toastSpy).toHaveBeenCalledWith(expect.objectContaining({ tone: "error" }));
  });
});
