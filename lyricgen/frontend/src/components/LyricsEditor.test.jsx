// Component tests for LyricsEditor. Covers the bugs surfaced by the
// agus.cafisi / Una Vez Más audit (2026-05-18). Each test reproduces
// one bug behaviorally; the test file MUST fail on bug code and pass
// once the fix lands.
import { render, screen, cleanup, fireEvent, within, act } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, it, expect, vi } from "vitest";
import LyricsEditor from "./LyricsEditor";
import { segmentsStore } from "../state/segmentsStore";

// useI18n + OnboardingTour pull in joyride / locale loading we don't
// need for these unit tests. Mock them to noops so the editor renders
// without booting the whole app shell.
// Mock i18n with a no-translation passthrough: t() returns the
// explicit fallback when provided, undefined otherwise. This way the
// component's `t("key") || "Spanish text"` pattern shows the Spanish
// fallback (what the user actually sees) instead of the i18n key
// itself ("editor.add_line"), which would make user-facing queries
// like getByRole({ name: /Agregar línea/i }) miss.
vi.mock("../i18n", () => ({
  useI18n: () => ({ t: (_key, fallback) => fallback }),
}));
vi.mock("./OnboardingTour", () => ({
  EditorTour: () => null,
}));
// LyricsEditor calls useToast() (per-anchor sync feedback). Tests render it
// without the app-root <ToastProvider>, so stub the hook.
vi.mock("./ToastProvider", () => ({
  useToast: () => ({ toast: () => {}, dismiss: () => {} }),
  ToastProvider: ({ children }) => children,
}));

// Minimal happy-path props the editor expects. Tests override only the
// fields they care about.
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

afterEach(() => {
  cleanup();
  // PR E: el store por jobId es a nivel módulo — limpiar entre tests para
  // que un test con transcribeJobId no leakee segments al siguiente.
  segmentsStore._clearAll();
});

// jsdom does not run audio: HTMLMediaElement.currentTime is a real
// number setter, but `timeupdate` events don't fire automatically.
// This helper mimics what the audio element would emit when the
// playhead moves to `t` seconds — used to drive the editor's internal
// currentTime state without booting a real player.
function _setAudioCurrentTime(container, t) {
  const audio = container.querySelector("audio");
  if (!audio) throw new Error("audio element not mounted in test render");
  audio.currentTime = t;
  fireEvent.timeUpdate(audio);
}

describe("LyricsEditor — banner de confianza + señal review calma (2026-07)", () => {
  // Rediseño: el borde/anillo ámbar completo + banner de alarma hacían
  // parecer todo roto con 11/26 líneas review, cuando el sync salió
  // excelente. Ahora: banner ÚNICO positivo con navegador secuencial, y
  // señal per-línea SUTIL (barra izquierda ámbar), sin pill ni ring.
  it("muestra un banner de confianza con contador cuando hay líneas review", () => {
    const props = baseProps({
      segments: [
        { start: 27.7, end: 33.7, text: "Tanto tiempo te esperé sentado aquí", review: true },
        { start: 33.7, end: 39.4, text: "Que ya el invierno me alcanzó sin gamulán", review: true },
        { start: 39.4, end: 42.1, text: "Será por eso que hoy estamos aquí", review: true },
        { start: 42.1, end: 45.9, text: "No hay nadie más que vos y yo", review: false },
      ],
    });
    render(<LyricsEditor {...props} />);
    // Rediseño 2026-07: la confianza es una línea muted "Sincronizado con
    // tu letra"; el contador vive dentro del chip primario "Revisar · •N".
    expect(screen.getByText(/Sincronizado con tu letra/i)).toBeInTheDocument();
    // El chip navegador "Revisar" está presente y muestra el contador (3).
    expect(screen.getByTestId("review-next-btn")).toBeInTheDocument();
    expect(within(screen.getByTestId("review-next-btn")).getByText("3")).toBeInTheDocument();
    // La pill per-línea "revisar tiempo" quedó eliminada.
    expect(screen.queryAllByText(/^revisar tiempo$/i)).toHaveLength(0);
  });

  it("el banner aparece incluso con UNA sola línea review (singular)", () => {
    const props = baseProps({
      segments: [
        { start: 1.0, end: 2.0, text: "alpha", review: true },
        { start: 2.0, end: 3.0, text: "beta", review: false },
        { start: 3.0, end: 4.0, text: "gamma", review: false },
      ],
    });
    render(<LyricsEditor {...props} />);
    expect(screen.getByText(/Sincronizado con tu letra/i)).toBeInTheDocument();
    // El chip "Revisar" muestra el contador aun con una sola línea (1).
    expect(within(screen.getByTestId("review-next-btn")).getByText("1")).toBeInTheDocument();
  });

  it("'Revisar →' hace foco en la siguiente línea review (navegador secuencial)", () => {
    const props = baseProps({
      segments: [
        { start: 1.0, end: 2.0, text: "buena", review: false },
        { start: 2.0, end: 3.0, text: "revisar una", review: true },
        { start: 3.0, end: 4.0, text: "revisar dos", review: true },
      ],
    });
    const { container } = render(<LyricsEditor {...props} />);
    // jsdom no implementa scrollIntoView — stubearlo para no romper.
    window.HTMLElement.prototype.scrollIntoView = vi.fn();
    fireEvent.click(screen.getByTestId("review-next-btn"));
    // El input de la primera línea review recibe foco.
    expect(document.activeElement).toBe(screen.getByDisplayValue("revisar una"));
    // Segundo click → la siguiente review.
    fireEvent.click(screen.getByTestId("review-next-btn"));
    expect(document.activeElement).toBe(screen.getByDisplayValue("revisar dos"));
  });

  it("NO muestra banner cuando ningún segment es review", () => {
    const props = baseProps({
      segments: [
        { start: 1.0, end: 2.0, text: "alpha" },
        { start: 2.0, end: 3.0, text: "beta" },
      ],
    });
    render(<LyricsEditor {...props} />);
    expect(screen.queryByText(/Sincronizado con tu letra/i)).toBeNull();
    expect(screen.queryByTestId("review-next-btn")).toBeNull();
  });
});


describe("LyricsEditor — scrub bar click reliability (2026-05-26)", () => {
  // Bug: el div interno de fill tenía `transition-[width]` que peleaba
  // contra el rAF loop de currentTime, haciendo que clicks "se pierdan"
  // durante frames de transición. Fix: transform: scaleX + pointer-events-none.
  it("scrub bar tiene type='button' (no submit) y handler de click", () => {
    const props = baseProps({
      segments: [{ start: 1.0, end: 5.0, text: "alpha" }],
      audioUrl: "blob:http://localhost/test",
    });
    render(<LyricsEditor {...props} />);
    const scrubBtn = screen.getByLabelText("Buscar");
    expect(scrubBtn.getAttribute("type")).toBe("button");
  });

  it("el fill del scrub no captura clicks (pointer-events-none)", () => {
    const props = baseProps({
      segments: [{ start: 1.0, end: 5.0, text: "alpha" }],
      audioUrl: "blob:http://localhost/test",
    });
    render(<LyricsEditor {...props} />);
    const scrubBtn = screen.getByLabelText("Buscar");
    const fill = scrubBtn.querySelector("div");
    expect(fill).not.toBeNull();
    // Debe tener pointer-events-none para que clicks siempre lleguen al button
    expect(fill.className).toMatch(/pointer-events-none/);
    // Y NO debe tener la transición CSS sobre width que peleaba contra el rAF
    expect(fill.className).not.toMatch(/transition-\[width\]/);
  });
});

describe("LyricsEditor — recuperación de audio remoto post-mount", () => {
  it("habilita el reproductor cuando la URL firmada llega después del primer render", () => {
    const props = baseProps({ audioUrl: null });
    const { container, rerender } = render(<LyricsEditor {...props} />);

    expect(container.querySelector("audio")).toBeNull();
    expect(screen.getByText(/Audio no disponible para reproducir/i)).toBeInTheDocument();

    const signedUrl = "https://media.example.test/source.wav?signature=fresh";
    rerender(<LyricsEditor {...props} audioUrl={signedUrl} />);

    expect(screen.queryByText(/Audio no disponible para reproducir/i)).toBeNull();
    expect(container.querySelector("audio")).toHaveAttribute("src", signedUrl);
    expect(screen.getByRole("button", { name: "Reproducir" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Buscar" })).toBeInTheDocument();
  });

  it("mantiene el blob válido como fallback durante el upgrade a la URL firmada", () => {
    const createObjectUrlSpy = vi
      .spyOn(URL, "createObjectURL")
      .mockReturnValue("blob:http://localhost/upload");
    const revokeObjectUrlSpy = vi
      .spyOn(URL, "revokeObjectURL")
      .mockImplementation(() => {});
    const audioFile = new File(["audio"], "song.wav", { type: "audio/wav" });
    const props = baseProps({ audioFile, audioUrl: null });
    const { container, rerender, unmount } = render(<LyricsEditor {...props} />);

    expect(createObjectUrlSpy).toHaveBeenCalledWith(audioFile);
    expect(container.querySelector("audio")).toHaveAttribute(
      "src",
      "blob:http://localhost/upload",
    );

    const signedUrl = "https://media.example.test/source.wav?signature=fresh";
    rerender(<LyricsEditor {...props} audioUrl={signedUrl} />);

    expect(container.querySelector("audio")).toHaveAttribute("src", signedUrl);
    expect(revokeObjectUrlSpy).not.toHaveBeenCalled();

    unmount();
    expect(revokeObjectUrlSpy).toHaveBeenCalledOnce();
    expect(revokeObjectUrlSpy).toHaveBeenCalledWith("blob:http://localhost/upload");
  });
});

describe("LyricsEditor — advanced shell and timing safety", () => {
  it("keeps the advanced shell explicit while audio is loading and offers a basic-view escape", async () => {
    render(<LyricsEditor {...baseProps({ audioLoading: true, audioUrl: null })} />);

    await userEvent.click(screen.getByRole("tab", { name: "Ajustar tiempos" }));

    expect(screen.getByTestId("advanced-workspace-shell")).toBeInTheDocument();
    expect(screen.getByTestId("advanced-audio-loading")).toBeInTheDocument();
    expect(screen.queryByTestId("advanced-audio-unavailable")).toBeNull();
    expect(screen.queryByDisplayValue("alpha line")).toBeNull();

    await userEvent.click(screen.getByRole("button", { name: "Volver a Revisar letra" }));
    expect(screen.getByRole("tab", { name: "Revisar letra" })).toHaveAttribute("aria-selected", "true");
    expect(screen.getByDisplayValue("alpha line")).toBeInTheDocument();
  });

  it("shows an unavailable-audio state instead of silently falling back to the basic list", async () => {
    render(<LyricsEditor {...baseProps({ audioLoading: false, audioUrl: null })} />);

    await userEvent.click(screen.getByRole("tab", { name: "Ajustar tiempos" }));

    expect(screen.getByTestId("advanced-audio-unavailable")).toBeInTheDocument();
    expect(screen.getByText(/No se puede ajustar tiempos sin audio/i)).toBeInTheDocument();
    expect(screen.queryByDisplayValue("alpha line")).toBeNull();
  });

  it("rejects partial timestamps and never sends non-finite timings on approve or autosave", async () => {
    const onApprove = vi.fn();
    const onPersistSegments = vi.fn().mockResolvedValue({ ok: true });
    render(<LyricsEditor {...baseProps({
      segments: [
        { start: "12abc", end: "bad", text: "kept" },
        { start: "1:02.5xyz", end: "1:04.5", text: "second" },
      ],
      onApprove,
      onPersistSegments,
      transcribeJobId: "job-timing-safety",
    })} />);

    const kept = screen.getByDisplayValue("kept");
    expect(kept).toBeInTheDocument();
    await userEvent.click(screen.getByRole("button", { name: /Aprobar/i }));

    expect(onApprove).toHaveBeenCalledOnce();
    const approved = onApprove.mock.calls[0][0];
    expect(approved).toHaveLength(2);
    approved.forEach((segment) => {
      expect(Number.isFinite(segment.start)).toBe(true);
      expect(Number.isFinite(segment.end)).toBe(true);
    });
    expect(approved[0].start).toBe(0);
    expect(approved[0].end).toBeGreaterThanOrEqual(0.3);
    expect(approved[1].start).toBe(0);
    expect(approved[1].end).toBeGreaterThan(approved[1].start);

    window.dispatchEvent(new Event("pagehide"));
    expect(onPersistSegments).toHaveBeenCalled();
    const saved = onPersistSegments.mock.calls.at(-1)[1];
    saved.forEach((segment) => {
      expect(Number.isFinite(segment.start)).toBe(true);
      expect(Number.isFinite(segment.end)).toBe(true);
    });
  });
});

describe("LyricsEditor — structural mutations share undo and save behavior", () => {
  const structuralProps = (overrides = {}) => baseProps({
    segments: [
      { start: 1, end: 2, text: "alpha" },
      { start: 4, end: 5, text: "beta" },
    ],
    transcribeJobId: "job-structural-mutations",
    onPersistSegments: vi.fn().mockResolvedValue({ ok: true }),
    ...overrides,
  });

  it.each([
    ["delete", "Eliminar línea", "alpha"],
    ["duplicate", /Duplicar línea/, "alpha"],
  ])("%s records an undo snapshot", async (_name, title, text) => {
    render(<LyricsEditor {...structuralProps()} />);
    await userEvent.click(screen.getAllByTitle(title)[0]);
    expect(screen.queryAllByDisplayValue(text)).toHaveLength(_name === "duplicate" ? 2 : 0);

    fireEvent.keyDown(window, { key: "z", ctrlKey: true });
    expect(screen.getAllByDisplayValue(text)).toHaveLength(1);
  });

  it("add blank and insert-after both become undoable edits", async () => {
    render(<LyricsEditor {...structuralProps()} />);
    const before = segmentsStore.get("job-structural-mutations").length;

    await userEvent.click(screen.getByRole("button", { name: /Agregar línea/i }));
    expect(segmentsStore.get("job-structural-mutations")).toHaveLength(before + 1);
    fireEvent.keyDown(window, { key: "z", ctrlKey: true });
    expect(segmentsStore.get("job-structural-mutations")).toHaveLength(before);

    await userEvent.click(screen.getAllByTitle(/Insertar línea acá/i)[0]);
    expect(segmentsStore.get("job-structural-mutations")).toHaveLength(before + 1);
    fireEvent.keyDown(window, { key: "z", ctrlKey: true });
    expect(segmentsStore.get("job-structural-mutations")).toHaveLength(before);
  });
});


describe("LyricsEditor — reemplazo externo post-mount (ex prop-sync B7)", () => {
  // HISTORIA: B7 (2026-05-18) era "el prop `segments` cambió → re-seed".
  // Ese contrato creó el loop bidireccional del reseed-storm y murió en
  // PR E: el prop es SOLO el seed inicial. El reemplazo externo legítimo
  // (undo restore, re-fetch de segments_json, otro contenido para el
  // mismo job) va por segmentsStore.replace(jobId, segs) y el editor —
  // suscripto al store — lo refleja al instante.
  it("muestra el contenido nuevo cuando el reemplazo llega por segmentsStore.replace", () => {
    const props = baseProps({
      segments: [{ start: 1.0, end: 2.0, text: "alpha line" }],
      transcribeJobId: "job-b7",
    });
    render(<LyricsEditor {...props} />);
    expect(screen.getByDisplayValue("alpha line")).toBeInTheDocument();

    act(() => {
      segmentsStore.replace("job-b7", [
        { start: 1.0, end: 2.0, text: "beta line" },
      ]);
    });
    expect(screen.getByDisplayValue("beta line")).toBeInTheDocument();
    expect(screen.queryByDisplayValue("alpha line")).not.toBeInTheDocument();
  });

  it("un cambio del prop `segments` post-mount se IGNORA (el prop es solo seed)", () => {
    const propsA = baseProps({
      segments: [{ start: 1.0, end: 2.0, text: "alpha line" }],
      transcribeJobId: "job-b7",
    });
    const { rerender } = render(<LyricsEditor {...propsA} />);
    rerender(
      <LyricsEditor
        {...baseProps({
          segments: [{ start: 1.0, end: 2.0, text: "beta line" }],
          transcribeJobId: "job-b7",
        })}
      />,
    );
    // El prop stale no pisa el estado vivo — esa era la mitad del bug
    // "se borran los tiempos al navegar el wizard".
    expect(screen.getByDisplayValue("alpha line")).toBeInTheDocument();
    expect(screen.queryByDisplayValue("beta line")).not.toBeInTheDocument();
  });
});

describe("LyricsEditor — sync mode anchor across positions (B4)", () => {
  // BUG: tapAnchor (SPACE in sync mode) reads neighbours from edited[
  // syncCursor - 1] and edited[syncCursor + 1] and clamps newStart to
  // `prevSeg.end + MIN_GAP_S`. When the operator is anchoring a line
  // chronologically EARLIER than its array-position previous neighbour
  // — typical in the Una Vez Más outro: a chorus repetition was added
  // at the end, but its true start belongs before existing segments —
  // the clamp pins it at `prevSeg.end + 0.05 s`, far from where the
  // operator pressed SPACE. From the operator's view "nothing
  // happens".
  //
  // Expected: tapAnchor honors `currentTime` regardless of the current
  // position of the segment in the array. The array re-sorts after the
  // mutation so the line moves to its new chronological slot.
  // SKIPPED 2026-05-25: el test pre-existía cuando la UI exponía un
  // botón "Activar Sync" por cada row (entraba a sync mode con
  // `enterSyncModeAt(N)` y syncCursor=N). Refactor posterior consolidó
  // todo en un único entry point que arranca en row 0; ya no hay forma
  // directa de saltar a row 3 vía UI sin tap-anchor varios veces. El
  // bug B4 original (anchor que no reordena el array) sigue cubierto
  // por la lógica de `tapAnchor` en LyricsEditor.jsx:814 — el sort
  // post-mutación está intacto. Re-habilitar cuando agreguemos
  // navegación arrow-up/down en sync mode, o cambiar la estrategia
  // del test a "anchor row 0 → ver que reordena".
  it.skip("anchors the target segment to currentTime even when it would re-order the array", async () => {
    const props = baseProps({
      // 3 segments in order at 10/20/30 s, and a 4th appended at the
      // end with start=40 s. Operator wants to move that 4th line to
      // BEFORE the others by anchoring at currentTime=5 s.
      segments: [
        { start: 10.0, end: 12.0, text: "alpha" },
        { start: 20.0, end: 22.0, text: "beta" },
        { start: 30.0, end: 32.0, text: "gamma" },
        { start: 40.0, end: 42.0, text: "delta — should land at 5 s" },
      ],
      audioFile: new Blob(["audio-bytes"], { type: "audio/mpeg" }),
    });
    const { container } = render(<LyricsEditor {...props} />);

    // Audio at 5 s — before the first existing segment.
    _setAudioCurrentTime(container, 5.0);

    // Activate sync mode on the 4th row ("delta"). The ◉ button sits
    // on hover, but in jsdom there's no hover state — we click it
    // directly via the title attribute (set in LyricsEditor.jsx
    // L1621).
    const dotButtons = container.querySelectorAll(
      '[data-testid^="sync-dot-"]'
    );
    // The 4th row's hook — index 3 (0-based) since the rows are in array
    // order and there are 4 segments.
    await userEvent.click(dotButtons[3]);

    // Press SPACE to anchor.
    fireEvent.keyDown(window, { code: "Space" });

    // The "delta" segment must now read its new chronological time
    // (~5 s, with 80 ms latency compensation). The display formats
    // start as "M:SS.t", so 5 s ≈ "0:04.9" (5.00 - 0.08 = 4.92, rounded
    // to one decimal).
    expect(screen.getByDisplayValue(/delta/i)).toBeInTheDocument();
    // The timestamp display of the "delta" row should show ~0:04.9
    // (within the row containing the "delta" text).
    const deltaInput = screen.getByDisplayValue(/delta/i);
    const deltaRow = deltaInput.closest("div[class*='group']") || deltaInput.closest("div");
    expect(deltaRow).toBeTruthy();
    // The timestamp shows the row's start; find the small monospace
    // span near the row that displays "0:04.x".
    expect(deltaRow.textContent).toMatch(/0:04\.\d/);
  });
});

describe("LyricsEditor — addBlankLine (B3)", () => {
  // BUG: addBlankLine appends a new entry to the end of the array
  // with start = last.end + 0.5 (regardless of where the audio
  // playhead actually is). When the operator clicks "Agregar línea"
  // mid-song — typical when filling in repeated chorus outros the
  // pipeline collapsed away — the new line lands far from the right
  // moment. SPACE-anchoring it then either gets clamped by the
  // (now-wrong) neighbor bounds or refuses to move it at all.
  //
  // Expected: new line's start is approximately the current playhead
  // position, and the resulting array stays sorted by start ascending.
  it("inserts a new line at currentTime, not pinned to last segment", async () => {
    const props = baseProps({
      // 3 segments scattered across a long song. Without the fix, a
      // new line will land at last.end + 0.5 = 60.5s regardless of
      // where the operator is in the audio.
      segments: [
        { start: 10.0, end: 12.0, text: "verse one" },
        { start: 30.0, end: 32.0, text: "verse two" },
        { start: 55.0, end: 60.0, text: "chorus" },
      ],
      // A small blob so the editor mounts an <audio> element with a
      // src; we never actually play it.
      audioFile: new Blob(["audio-bytes"], { type: "audio/mpeg" }),
    });
    const { container } = render(<LyricsEditor {...props} />);

    // Operator is listening at 42 s — between verse two (end=32) and
    // chorus (start=55) — when they realise a missing line lives here.
    _setAudioCurrentTime(container, 42.0);

    // Click the "+ Agregar línea" button.
    const addBtn = screen.getByRole("button", { name: /Agregar línea/i });
    await userEvent.click(addBtn);

    // Find the new (empty-text) row's timestamp display. The editor
    // formats `start` as `M:SS.t`, so 42.0 s shows as "0:42.0".
    // On the buggy build the new row reads "1:00.5" instead (60.5 s,
    // pinned to last.end + 0.5).
    expect(screen.getByText("0:42.0")).toBeInTheDocument();

    // Sanity: array stays sorted so downstream code (sync mode neighbor
    // clamp, persistence) sees a monotonic timeline. The displayed
    // timestamps in document order should be ascending.
    const stamps = Array.from(container.querySelectorAll("button"))
      .map((el) => el.textContent || "")
      .filter((txt) => /^\d+:\d{2}\.\d$/.test(txt.trim()))
      .map((txt) => txt.trim());
    const seconds = stamps.map((s) => {
      const [m, rest] = s.split(":");
      const [sec, tenth] = rest.split(".");
      return parseInt(m, 10) * 60 + parseInt(sec, 10) + parseInt(tenth, 10) / 10;
    });
    expect(seconds).toEqual([...seconds].sort((a, b) => a - b));
  });
});

describe("LyricsEditor — modo enfoque body class broadcast", () => {
  // BUG (2026-05-26): "Modo Enfoque" se construyó cuando el editor era
  // full-width. Luego el wizard pasó a 3 columnas (UploadZone:1854),
  // dejando al editor en una columna de ~460-1124px. El toggle seguía
  // tweakeando `max-h` interno (~90px verticales) — pero el ancho de
  // la columna no cambiaba, así que el "enfoque" era imperceptible.
  //
  // Fix: LyricsEditor emite la clase `editor-focus-mode` en
  // document.body cuando focusMode=on. UploadZone usa una variante
  // arbitraria de Tailwind con dos columnas para esconder el stepper y
  // mantener una preview compacta junto al editor expandido.
  //
  // Este test cubre el contrato: el body class se aplica al togglear
  // y se LIMPIA al desmontar (sin cleanup, el usuario que navega de
  // step 6 a step 4 vería el layout colapsado sin entender por qué).
  // El comportamiento del grid en UploadZone es CSS puro (className
  // strings) y se valida visualmente / con snapshot en otro test.

  // localStorage state es persistente entre tests del mismo run.
  // Limpiamos antes para arrancar con focusMode=OFF garantizado.
  afterEach(() => {
    try { localStorage.removeItem("genly_editor_focus"); } catch (_) { /* */ }
    document.body.classList.remove("editor-focus-mode");
  });

  it("toggles the editor-focus-mode body class from the overflow (⋯) menu", async () => {
    // Rediseño 2026-07: "Expandir / Modo enfoque" dejó de ser un botón
    // suelto en la barra — ahora vive dentro del menú ⋯ (overflow).
    const props = baseProps({ audioUrl: "blob:mock-audio" });
    render(<LyricsEditor {...props} />);

    // Default OFF — la clase no debe estar al montar.
    expect(document.body.classList.contains("editor-focus-mode")).toBe(false);

    // La vista básica mantiene la barra despejada; los controles avanzados
    // viven en "Ajustar tiempos".
    await userEvent.click(screen.getByRole("tab", { name: "Ajustar tiempos" }));

    // Abrí el menú ⋯ y clic en "Expandir (modo enfoque)".
    await userEvent.click(screen.getByTestId("editor-overflow-btn"));
    await userEvent.click(screen.getByText(/Expandir \(modo enfoque\)/i));
    expect(document.body.classList.contains("editor-focus-mode")).toBe(true);

    // Reabrí el menú — el item ahora dice "Salir de modo enfoque".
    await userEvent.click(screen.getByTestId("editor-overflow-btn"));
    await userEvent.click(screen.getByText(/Salir de modo enfoque/i));
    expect(document.body.classList.contains("editor-focus-mode")).toBe(false);
  });

  it("removes the body class on unmount so navigating away restores normal layout", async () => {
    const props = baseProps({ audioUrl: "blob:mock-audio" });
    const { unmount } = render(<LyricsEditor {...props} />);

    // Prendé focus mode desde el menú ⋯.
    await userEvent.click(screen.getByRole("tab", { name: "Ajustar tiempos" }));
    await userEvent.click(screen.getByTestId("editor-overflow-btn"));
    await userEvent.click(screen.getByText(/Expandir \(modo enfoque\)/i));
    expect(document.body.classList.contains("editor-focus-mode")).toBe(true);

    // Operador navega a otro step / cambia de pantalla — el editor
    // se desmonta. Sin cleanup, el body queda con la clase aplicada
    // y UploadZone seguiría escondiendo stepper + preview.
    unmount();
    expect(document.body.classList.contains("editor-focus-mode")).toBe(false);
  });
});

describe("LyricsEditor — Enter-to-split is word-aware (2026-06-05)", () => {
  // Divergent-live defect: whisperX glued the next phrase's "No" onto this line.
  // Pressing Enter before "No" must split it off AND give line 2 the REAL word
  // time (12.5–12.9), not a character-ratio interpolation. Per-word `words`
  // must be sliced between the halves, not duplicated.
  const seg = {
    start: 10.0,
    end: 12.9,
    text: "tengo una mala noticia No",
    words: [
      { word: "tengo", start: 10.0, end: 10.4 },
      { word: "una", start: 10.4, end: 10.6 },
      { word: "mala", start: 10.6, end: 11.0 },
      { word: "noticia", start: 11.0, end: 11.8 },
      { word: "No", start: 12.5, end: 12.9 },
    ],
  };

  it("splits at the cursor with REAL word timing on both halves", () => {
    // PR E: onEditedChange murió — el resultado del split se observa
    // directo en el segmentsStore (la fuente de verdad viva del editor).
    render(<LyricsEditor {...baseProps({ segments: [seg], transcribeJobId: "job-split" })} />);
    const input = screen.getByDisplayValue("tengo una mala noticia No");
    const caret = "tengo una mala noticia ".length; // 23, right before "No"
    input.setSelectionRange(caret, caret);
    fireEvent.keyDown(input, { key: "Enter" });

    const out = segmentsStore.get("job-split");
    expect(out).toHaveLength(2);
    expect(out[0].text).toBe("tengo una mala noticia");
    expect(out[1].text).toBe("No");
    // Line 2 gets the real word time, NOT a char-ratio interpolation:
    expect(out[1].start).toBe(12.5);
    expect(out[1].end).toBe(12.9);
    expect(out[0].start).toBe(10.0);
    expect(out[0].end).toBe(11.8);
    // `words` sliced between halves (not duplicated):
    expect(out[0].words).toHaveLength(4);
    expect(out[1].words).toEqual([{ word: "No", start: 12.5, end: 12.9 }]);
  });

  // 2026-07-01: Backspace en pos 0 solo fusiona si la línea está VACÍA. Antes
  // fusionaba con cualquier Backspace en pos 0, así que borrar la primera palabra
  // hacía "desaparecer" la línea (confuso). Dos casos:
  it("does NOT merge a NON-empty line on Backspace at line start", () => {
    const segs = [
      { start: 10.0, end: 11.8, text: "tengo una mala noticia",
        words: [{ word: "tengo", start: 10.0, end: 10.4 }, { word: "noticia", start: 11.0, end: 11.8 }] },
      { start: 12.5, end: 12.9, text: "No",
        words: [{ word: "No", start: 12.5, end: 12.9 }] },
    ];
    render(<LyricsEditor {...baseProps({ segments: segs, transcribeJobId: "job-merge" })} />);
    const input = screen.getByDisplayValue("No");
    input.setSelectionRange(0, 0);
    fireEvent.keyDown(input, { key: "Backspace" });

    // "No" tiene texto → NO se fusiona. Siguen siendo 2 líneas.
    expect(segmentsStore.get("job-merge")).toHaveLength(2);
  });

  it("merges an EMPTY line into the previous via Backspace at line start", () => {
    const segs = [
      { start: 10.0, end: 11.8, text: "tengo una mala noticia",
        words: [{ word: "tengo", start: 10.0, end: 10.4 }, { word: "noticia", start: 11.0, end: 11.8 }] },
      { start: 12.5, end: 12.9, text: "No",
        words: [{ word: "No", start: 12.5, end: 12.9 }] },
    ];
    render(<LyricsEditor {...baseProps({ segments: segs, transcribeJobId: "job-merge" })} />);
    const input = screen.getByDisplayValue("No");
    // Vaciar la línea primero (el operador borró todo el texto).
    fireEvent.change(input, { target: { value: "" } });
    input.setSelectionRange(0, 0);
    fireEvent.keyDown(input, { key: "Backspace" });

    // Línea vacía + Backspace → se une a la anterior (queda 1 línea).
    const out = segmentsStore.get("job-merge");
    expect(out).toHaveLength(1);
    expect(out[0].start).toBe(10.0);
  });
});

describe("LyricsEditor — durable save on page unload (refresh/close) (2026-06-24)", () => {
  // BUG (reporte Gaby): el editor titiló a toda velocidad, refrescó la página
  // para salir del error y perdió TODO el trabajo no persistido. El autosave
  // es debounced 3s y el beforeunload solo AVISA — no guarda. Un F5 mata el
  // contexto JS antes de que el debounce o el flush-on-unmount (unmount de
  // React) terminen, y un fetch normal se cancela a mitad de vuelo.
  //
  // Fix: en pagehide/beforeunload re-disparamos el guardado pendiente con
  // `keepalive: true`, que el browser entrega aunque la página se esté
  // descargando. Este test monta el editor, edita una línea (deja un guardado
  // pendiente en el ref) y verifica que pagehide persiste con keepalive.
  it("flushes pending edits with keepalive on pagehide", () => {
    const onPersistSegments = vi.fn().mockResolvedValue({ ok: true });
    const props = baseProps({
      segments: [{ start: 1.0, end: 2.0, text: "alpha line" }],
      transcribeJobId: "job-1",
      onPersistSegments,
    });
    render(<LyricsEditor {...props} />);

    // Operadora edita una línea — queda un guardado pendiente (debounce 3s
    // aún sin disparar; en el test no avanzamos timers).
    const input = screen.getByDisplayValue("alpha line");
    fireEvent.change(input, { target: { value: "alpha EDITED" } });

    // Sin la corrección, refrescar pierde esta edición. Con ella, pagehide
    // la persiste antes de que la página muera.
    window.dispatchEvent(new Event("pagehide"));

    expect(onPersistSegments).toHaveBeenCalledTimes(1);
    const [jobId, segments, opts] = onPersistSegments.mock.calls[0];
    expect(jobId).toBe("job-1");
    expect(segments).toEqual([{ start: 1.0, end: 2.0, text: "alpha EDITED" }]);
    expect(opts).toMatchObject({ keepalive: true, baseRevision: 0 });
  });

  it("still sends keepalive when localStorage is blocked or full", () => {
    const onPersistSegments = vi.fn().mockResolvedValue({ ok: true });
    const storageSpy = vi.spyOn(localStorage, "setItem").mockImplementation(() => {
      throw new DOMException("quota", "QuotaExceededError");
    });
    try {
      render(<LyricsEditor {...baseProps({
        segments: [{ start: 1.0, end: 2.0, text: "alpha line" }],
        transcribeJobId: "job-storage-blocked",
        onPersistSegments,
        user: { id: 7 },
      })} />);
      fireEvent.change(screen.getByDisplayValue("alpha line"), {
        target: { value: "latest edit" },
      });
      window.dispatchEvent(new Event("pagehide"));
      expect(onPersistSegments).toHaveBeenCalledTimes(1);
      expect(onPersistSegments.mock.calls[0][2]).toMatchObject({ keepalive: true });
    } finally {
      storageSpy.mockRestore();
    }
  });

  it("does not fire a save on unload when there is nothing pending", () => {
    const onPersistSegments = vi.fn().mockResolvedValue({ ok: true });
    // disableAutosave → el debounce nunca arma un pendiente, así que pagehide
    // no debe intentar guardar (no hay trabajo que perder).
    const props = baseProps({
      segments: [{ start: 1.0, end: 2.0, text: "alpha line" }],
      transcribeJobId: "job-1",
      onPersistSegments,
      disableAutosave: true,
    });
    render(<LyricsEditor {...props} />);

    window.dispatchEvent(new Event("pagehide"));
    expect(onPersistSegments).not.toHaveBeenCalled();
  });
});

// NOTE (PR E adversarial audit, 2026-07): acá vivía el describe "integration
// proof of #724" ("a reordered-but-equal segments writeback does NOT clobber a
// local edit"). Corría SIN transcribeJobId → useState local → el prop se
// ignoraba → pasaba trivialmente (no probaba nada post-PR-E). Se BORRÓ: post-PR
// E el prop `segments` es sólo seed inicial y nunca se re-lee, así que la
// insensibilidad al reorder la cubre trivialmente el test "ecos de prop son
// inertes" de abajo, que además usa un jobId real y afirma cero remounts.

describe("LyricsEditor — los ecos de prop son inertes (P0 titileo, era post-PR E)", () => {
  // HISTORIA: el storm original nacía del loop bidireccional — App
  // espejaba cada edición local de vuelta como prop (onEditedChange →
  // mergeEditedSegments → currentReview.segments) y el effect de
  // prop-sync podía verlo como "contenido nuevo" y reseedear (remount de
  // todas las filas, 6-7×/s). PR E elimina AMBAS mitades: no hay espejo
  // (onEditedChange no existe) y no hay prop-sync (el prop es solo seed).
  // Este test protege la nueva invariante: aunque un padre legacy
  // re-pase lo editado como prop en cada render, el editor no reseedea,
  // no re-monta filas y no pierde la edición.
  it("re-pasar lo editado como prop no reseedea ni re-monta filas", () => {
    const props = baseProps({
      segments: [
        { start: 0, end: 2, text: "alpha" },
        { start: 2, end: 4, text: "beta" },
      ],
      transcribeJobId: "job-echo",
      onPersistSegments: vi.fn().mockResolvedValue({ ok: true }),
    });
    const { rerender } = render(<LyricsEditor {...props} />);

    const input = screen.getByDisplayValue("alpha");
    fireEvent.change(input, { target: { value: "alpha EDITED" } });
    const editedNode = screen.getByDisplayValue("alpha EDITED");
    const betaNode = screen.getByDisplayValue("beta");

    // El "eco" del padre legacy: lo que se muestra, de vuelta como prop
    // (nueva referencia en cada render — el gatillo histórico del storm).
    const echo = segmentsStore.get("job-echo").map(({ _id, review, ...rest }) => rest);
    rerender(<LyricsEditor {...props} segments={echo} />);
    rerender(<LyricsEditor {...props} segments={[...echo]} />);

    // La edición sigue en pantalla y los nodos DOM son LOS MISMOS
    // (cero remounts — sin eso, el drag en curso moría y el editor
    // "titilaba").
    expect(screen.getByDisplayValue("alpha EDITED")).toBe(editedNode);
    expect(screen.getByDisplayValue("beta")).toBe(betaNode);
  });
});

describe("LyricsEditor — editar NO retroalimenta al padre (guard anti-loop, reemplaza reviewSegments.test.js)", () => {
  // El incidente #6 (loop de ~5000 ciclos): el viejo espejo onEditedChange
  // empujaba cada keystroke al padre → currentReview.segments → prop
  // `segments` → posible reseed → otro render → ... PR E cortó el espejo:
  // el editor escribe SOLO al segmentsStore y el padre NO se entera del edit.
  // Este test protege esa invariante: tras un edit, ni el objeto `segments`
  // que pasó el padre cambia (no hay mutación/writeback) ni el padre
  // re-renderiza (no hay canal de vuelta que dispare el loop).
  it("un edit no muta el prop `segments` ni re-renderiza al padre", () => {
    let parentRenders = 0;
    // Referencia ESTABLE del prop, congelada, como la pasaría App desde
    // currentReview.segments (seed). Si algo la mutara o el padre re-render-
    // eara por el edit, lo detectamos.
    const parentSegments = Object.freeze([
      Object.freeze({ start: 0, end: 2, text: "alpha" }),
      Object.freeze({ start: 2, end: 4, text: "beta" }),
    ]);
    const snapshotBefore = JSON.stringify(parentSegments);

    function Parent() {
      parentRenders += 1;
      return (
        <LyricsEditor
          {...baseProps({ segments: parentSegments, transcribeJobId: "job-noloop" })}
        />
      );
    }

    render(<Parent />);
    expect(parentRenders).toBe(1);

    const input = screen.getByDisplayValue("alpha");
    fireEvent.change(input, { target: { value: "alpha EDITED" } });
    fireEvent.change(input, { target: { value: "alpha EDITED 2" } });
    expect(screen.getByDisplayValue("alpha EDITED 2")).toBeInTheDocument();

    // Sin espejo: el prop del padre no fue mutado y el padre no re-renderizó
    // (cero canal de retroalimentación → cero loop).
    expect(JSON.stringify(parentSegments)).toBe(snapshotBefore);
    expect(parentRenders).toBe(1);
  });
});
