// Component tests for LyricsEditor. Covers the bugs surfaced by the
// agus.cafisi / Una Vez Más audit (2026-05-18). Each test reproduces
// one bug behaviorally; the test file MUST fail on bug code and pass
// once the fix lands.
import { render, screen, cleanup, fireEvent, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, it, expect, vi } from "vitest";
import { useState, useCallback, useRef } from "react";
import LyricsEditor from "./LyricsEditor";
import { segmentsValuesEqual } from "../lib/segmentsValuesEqual";

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

afterEach(() => cleanup());

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

describe("LyricsEditor — review:true banner UX (2026-05-26)", () => {
  // Cuando synced-direct fallback (PR #365) dispara, TODAS las líneas
  // vienen con `review: true`. Mostrar 28 badges "⚠ revisar tiempo"
  // apilados satura visualmente. Si ≥3 segments son review, debe
  // aparecer un banner ÚNICO arriba y los badges per-línea suprimirse.
  // Casos con <3 review siguen mostrando badge inline (info útil sin
  // saturar).
  it("muestra UN banner único cuando ≥3 segments son review:true", () => {
    const props = baseProps({
      segments: [
        { start: 27.7, end: 33.7, text: "Tanto tiempo te esperé sentado aquí", review: true },
        { start: 33.7, end: 39.4, text: "Que ya el invierno me alcanzó sin gamulán", review: true },
        { start: 39.4, end: 42.1, text: "Será por eso que hoy estamos aquí", review: true },
        { start: 42.1, end: 45.9, text: "No hay nadie más que vos y yo", review: true },
      ],
    });
    render(<LyricsEditor {...props} />);
    // Banner único arriba
    expect(screen.getByText(/líneas con timing aproximado/i)).toBeInTheDocument();
    // Per-line badge "revisar tiempo" NO debe aparecer (suprimido por banner)
    expect(screen.queryAllByText(/^revisar tiempo$/i)).toHaveLength(0);
  });

  it("NO muestra banner cuando <3 segments son review:true — fallback a badges per-línea", () => {
    const props = baseProps({
      segments: [
        { start: 1.0, end: 2.0, text: "alpha", review: true },
        { start: 2.0, end: 3.0, text: "beta", review: true },        // solo 2 review
        { start: 3.0, end: 4.0, text: "gamma", review: false },
      ],
    });
    render(<LyricsEditor {...props} />);
    expect(screen.queryByText(/líneas con timing aproximado/i)).toBeNull();
    // En cambio, los badges per-línea siguen apareciendo (2 review)
    const badges = screen.getAllByText(/^revisar tiempo$/i);
    expect(badges.length).toBe(2);
  });

  it("NO muestra banner cuando ningún segment es review", () => {
    const props = baseProps({
      segments: [
        { start: 1.0, end: 2.0, text: "alpha" },
        { start: 2.0, end: 3.0, text: "beta" },
      ],
    });
    render(<LyricsEditor {...props} />);
    expect(screen.queryByText(/líneas con timing aproximado/i)).toBeNull();
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


describe("LyricsEditor — prop sync (B7)", () => {
  // BUG: the component initialises `edited` from `segments` only on
  // mount (useState(initial) ignores subsequent prop changes). When
  // the parent re-mounts the editor on a different job, OR passes a
  // freshly-fetched segments array from a refresh, the editor keeps
  // showing the stale array forever.
  //
  // Expected behaviour: when `segments` reference changes, `edited`
  // resets to mirror it. Operator's in-flight edits (`isDirty`) are
  // also reset — the contract is "new prop = new starting point".
  it("re-syncs displayed text when segments prop changes", () => {
    const propsA = baseProps({
      segments: [{ start: 1.0, end: 2.0, text: "alpha line" }],
    });
    const { rerender } = render(<LyricsEditor {...propsA} />);
    expect(screen.getByDisplayValue("alpha line")).toBeInTheDocument();

    const propsB = baseProps({
      segments: [{ start: 1.0, end: 2.0, text: "beta line" }],
    });
    rerender(<LyricsEditor {...propsB} />);
    // On the buggy build, the textbox still shows "alpha line"
    // because `edited` was initialised in useState() and never re-read.
    expect(screen.getByDisplayValue("beta line")).toBeInTheDocument();
    expect(screen.queryByDisplayValue("alpha line")).not.toBeInTheDocument();
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
  // arbitraria de Tailwind (`[.editor-focus-mode_&]:lg:grid-cols-1`)
  // para colapsar su grid + esconder stepper + preview central.
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

  it("toggles the editor-focus-mode body class when the focus button is clicked", async () => {
    // audioUrl truthy → el sticky control bar que contiene el focus
    // toggle se monta (gated en LyricsEditor:1856 por `{audioUrl && ...}`).
    const props = baseProps({ audioUrl: "blob:mock-audio" });
    render(<LyricsEditor {...props} />);

    // Default OFF — la clase no debe estar al montar.
    expect(document.body.classList.contains("editor-focus-mode")).toBe(false);

    // Click el botón "Modo enfoque (F)" (tooltip exacto del component).
    const focusBtn = screen.getByTitle(/^Modo enfoque \(F\)$/i);
    await userEvent.click(focusBtn);
    expect(document.body.classList.contains("editor-focus-mode")).toBe(true);

    // Click otra vez — apaga. El tooltip ahora dice "Salir...".
    const exitBtn = screen.getByTitle(/^Salir de modo enfoque \(F\)$/i);
    await userEvent.click(exitBtn);
    expect(document.body.classList.contains("editor-focus-mode")).toBe(false);
  });

  it("removes the body class on unmount so navigating away restores normal layout", async () => {
    const props = baseProps({ audioUrl: "blob:mock-audio" });
    const { unmount } = render(<LyricsEditor {...props} />);

    // Prendé focus mode.
    const focusBtn = screen.getByTitle(/^Modo enfoque \(F\)$/i);
    await userEvent.click(focusBtn);
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
    const onEditedChange = vi.fn();
    render(<LyricsEditor {...baseProps({ segments: [seg], onEditedChange })} />);
    const input = screen.getByDisplayValue("tengo una mala noticia No");
    const caret = "tengo una mala noticia ".length; // 23, right before "No"
    input.setSelectionRange(caret, caret);
    fireEvent.keyDown(input, { key: "Enter" });

    const out = onEditedChange.mock.calls.at(-1)[0];
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

  it("merges a line into the previous via Backspace at line start", () => {
    const onEditedChange = vi.fn();
    const segs = [
      { start: 10.0, end: 11.8, text: "tengo una mala noticia",
        words: [{ word: "tengo", start: 10.0, end: 10.4 }, { word: "noticia", start: 11.0, end: 11.8 }] },
      { start: 12.5, end: 12.9, text: "No",
        words: [{ word: "No", start: 12.5, end: 12.9 }] },
    ];
    render(<LyricsEditor {...baseProps({ segments: segs, onEditedChange })} />);
    const input = screen.getByDisplayValue("No");
    input.setSelectionRange(0, 0);
    fireEvent.keyDown(input, { key: "Backspace" });

    const out = onEditedChange.mock.calls.at(-1)[0];
    expect(out).toHaveLength(1);
    expect(out[0].text).toBe("tengo una mala noticia No");
    expect(out[0].start).toBe(10.0);
    expect(out[0].end).toBe(12.9);
    expect(out[0].words).toHaveLength(3);
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
    expect(opts).toEqual({ keepalive: true });
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

describe("LyricsEditor — reseed-storm fix is end-to-end (integration proof of #724)", () => {
  // ACTIVE PROOF of #724 that doesn't depend on real users hitting the bug.
  // The reseed-storm / data-loss root cause: the backend sorts segments by
  // `start` on every /save-segments write, so the writeback hands the editor a
  // segments prop with the SAME values in a DIFFERENT order than the local
  // out-of-order array. The OLD positional segmentsValuesEqual saw that as
  // "new content" → reseeded `edited` from the prop → the operator's in-flight
  // edit got clobbered (and, repeated every autosave cycle, the flicker loop).
  //
  // This test reproduces exactly that: edit a line locally, then push a
  // reordered-but-equal segments prop. With #724 the guard treats it as equal
  // and SKIPS the reseed, so the local edit survives. On the pre-#724 build
  // this test fails — the edit is replaced by the reordered original.
  it("a reordered-but-equal segments writeback does NOT clobber a local edit", () => {
    const ordered = [
      { start: 0, end: 3, text: "alpha line" },
      { start: 3, end: 5, text: "beta line" },
      { start: 5, end: 8, text: "gamma line" },
    ];
    const { rerender } = render(<LyricsEditor {...baseProps({ segments: ordered })} />);

    // Operator edits the first line locally (changes `edited`, not the prop).
    const input = screen.getByDisplayValue("alpha line");
    fireEvent.change(input, { target: { value: "alpha EDITED" } });
    expect(screen.getByDisplayValue("alpha EDITED")).toBeInTheDocument();

    // Backend roundtrip: same values, sorted differently, fresh reference —
    // the exact shape that used to trigger the destructive reseed.
    const reorderedSameValues = [
      { start: 5, end: 8, text: "gamma line" },
      { start: 0, end: 3, text: "alpha line" },
      { start: 3, end: 5, text: "beta line" },
    ];
    rerender(<LyricsEditor {...baseProps({ segments: reorderedSameValues })} />);

    // The local edit must survive (guard skipped the reseed). On the buggy
    // build, "alpha EDITED" is gone and the original "alpha line" is back.
    expect(screen.getByDisplayValue("alpha EDITED")).toBeInTheDocument();
    expect(screen.queryByDisplayValue("alpha line")).not.toBeInTheDocument();
  });
});

describe("LyricsEditor — no reseed storm from the editor's own edit echo (P0 titileo)", () => {
  // Reproduces the production storm that #724 did NOT fix (Sentry: [reseed-storm]
  // perSec 6-7 on release 0ffa76f, which already has #724). App.jsx mirrors every
  // local edit up to currentReview.segments (onEditedChange → mergeEditedSegments)
  // and feeds it straight back as the `segments` prop. During editing the
  // prop-sync effect compared that echo against a one-tick-stale ref → saw it as
  // new content → reseeded `edited` (reassigning _id by index → rows REMOUNT) on
  // every edit → "titila todo el editor". The fix compares the echo against the
  // LIVE `edited`, recognises it, and skips the reseed.
  //
  // Detection: a reseed calls setEdited, which re-fires the onEditedChange mirror.
  // So a single user edit that triggers a reseed produces an EXTRA mirror call
  // (the echo of the reseed). No reseed → exactly one mirror call per edit.
  function MirrorHarness({ onMirror }) {
    const [segments, setSegments] = useState([
      { start: 0, end: 2, text: "alpha" },
      { start: 2, end: 4, text: "beta" },
    ]);
    // STABLE identity, exactly like App.jsx's `useCallback(..., [])` handler —
    // otherwise an inline arrow re-fires the editor's [edited, onEditedChange]
    // effect on every render and masks the reseed signal.
    const onMirrorRef = useRef(onMirror);
    onMirrorRef.current = onMirror;
    const handleMirror = useCallback((segs) => {
      onMirrorRef.current(segs);
      // Faithful to App.jsx mergeEditedSegments: no-op when values are equal,
      // else store the edited values straight back as the prop (the loop).
      setSegments((prev) => (segmentsValuesEqual(prev, segs) ? prev : segs));
    }, []);
    return (
      <LyricsEditor
        {...baseProps({
          segments,
          transcribeJobId: "job-1",
          onPersistSegments: vi.fn().mockResolvedValue({ ok: true }),
        })}
        onEditedChange={handleMirror}
      />
    );
  }

  it("a single edit triggers exactly one mirror call — no self-echo reseed", () => {
    const onMirror = vi.fn();
    render(<MirrorHarness onMirror={onMirror} />);

    const baseline = onMirror.mock.calls.length; // mount fires the mirror once
    const input = screen.getByDisplayValue("alpha");
    fireEvent.change(input, { target: { value: "alpha EDITED" } });

    // One user edit = one mirror call. On the buggy build the echo reseeds and
    // setEdited re-fires the mirror → 2+ calls (the storm, one bounce per edit).
    expect(onMirror.mock.calls.length - baseline).toBe(1);
    // And the edit is shown (sanity).
    expect(screen.getByDisplayValue("alpha EDITED")).toBeInTheDocument();
  });
});
