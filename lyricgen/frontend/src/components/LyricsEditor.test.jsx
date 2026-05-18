// Component tests for LyricsEditor. Covers the bugs surfaced by the
// agus.cafisi / Una Vez Más audit (2026-05-18). Each test reproduces
// one bug behaviorally; the test file MUST fail on bug code and pass
// once the fix lands.
import { render, screen, cleanup } from "@testing-library/react";
import { afterEach, describe, it, expect, vi } from "vitest";
import LyricsEditor from "./LyricsEditor";

// useI18n + OnboardingTour pull in joyride / locale loading we don't
// need for these unit tests. Mock them to noops so the editor renders
// without booting the whole app shell.
vi.mock("../i18n", () => ({
  useI18n: () => ({ t: (key, fallback) => fallback || key }),
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
