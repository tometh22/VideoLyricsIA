// Component tests for LyricsEditor. Covers the bugs surfaced by the
// agus.cafisi / Una Vez Más audit (2026-05-18). Each test reproduces
// one bug behaviorally; the test file MUST fail on bug code and pass
// once the fix lands.
import { render, screen, cleanup, fireEvent, within } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, it, expect, vi } from "vitest";
import LyricsEditor from "./LyricsEditor";

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
  it("anchors the target segment to currentTime even when it would re-order the array", async () => {
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

    // Refactor 2026-05-23: el UI per-fila de "Activar Sync" se compactó
    // a un botón global (LyricsEditor.jsx:1881). El único entry point a
    // un syncCursor != 0 es secuencial — Space anchora la línea actual
    // y avanza al siguiente _id. Reescribimos el test para walk-through
    // los rows en orden, mirroring el uso real del operador.

    // audioUrl se setea via useEffect (createObjectURL del audioFile).
    // El Ctrl+K handler bail-ea si !audioUrl, así que esperamos a que
    // el effect haya corrido + el <audio> esté en el DOM antes de
    // disparar el keydown. Sin esto, en suite full el test corre
    // bajo timing diferente y Ctrl+K no entra a sync mode.
    await new Promise((res) => setTimeout(res, 0));
    if (!container.querySelector("audio")) {
      throw new Error("audio element not mounted after first tick — useEffect didn't run");
    }

    // Enter sync mode con Cmd/Ctrl+K (no depende de visibilidad del
    // botón en jsdom: la entry tiene class `hidden md:inline-flex` y
    // queda display:none por defecto).
    fireEvent.keyDown(window, { key: "k", ctrlKey: true });

    // Walk cursor de alpha (idx 0) → beta → gamma → delta anchorando
    // cada una en su propio start (no-op delta). Cada Space avanza el
    // syncCursor al siguiente _id post-sort vía queueMicrotask, así que
    // damos un tick antes del siguiente.
    for (const t of [10.0, 20.0, 30.0]) {
      _setAudioCurrentTime(container, t);
      await new Promise((res) => setTimeout(res, 0));
      fireEvent.keyDown(window, { code: "Space" });
    }
    await new Promise((res) => setTimeout(res, 0));

    // Cursor ahora apunta a delta. Audio a 5 s, antes de los otros 3.
    _setAudioCurrentTime(container, 5.0);
    fireEvent.keyDown(window, { code: "Space" });
    await new Promise((res) => setTimeout(res, 0));

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
