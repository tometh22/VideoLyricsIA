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
