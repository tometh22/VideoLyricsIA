// Component tests for EditRequestPanel. Covers the stale-state bug
// surfaced by the 2026-05-19 post-release QA pass: when an operator
// edits lyrics, autosaves, closes the modal (Esc), and reopens it, the
// editor would surface the pre-edit text and the next autosave would
// overwrite the just-saved version with the stale snapshot.
//
// The fix mirrors the just-persisted segments into local state and
// prefers it over job.segments_json on the next modal open. JobDetail's
// /status polling intentionally omits segments_json (too heavy), so
// without this local mirror, the modal has no way to see what it just
// saved.
import { render, screen, cleanup, fireEvent, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, describe, it, expect, vi } from "vitest";
import EditRequestPanel from "./EditRequestPanel";

vi.mock("../i18n", () => ({
  useI18n: () => ({ t: (_key, fallback) => fallback }),
}));

// EditRequestPanel calls useAlert() (themed error modals). Tests render it
// without the app-root <AlertProvider>, so stub the hook.
vi.mock("./AlertProvider", () => ({
  useAlert: () => ({ alert: () => {} }),
  AlertProvider: ({ children }) => children,
}));

// ContentValidationToggle pulls in extra deps we don't need here; the
// "lyrics" mode path doesn't render it. Stubbing it just in case it
// shows up via the typography path the panel still renders.
vi.mock("./ContentValidationToggle", () => ({
  default: () => null,
  isUmgTenant: () => false,
}));

vi.mock("./BackgroundHintField", () => ({
  default: () => null,
}));

// Stub LyricsEditor so the test can: (a) observe which segments prop
// the panel hands it, (b) drive the onPersistSegments callback on
// demand without booting the real editor's heavy DOM.
let _capturedSegments = null;
let _capturedOnPersist = null;
let _capturedJobId = null;

vi.mock("./LyricsEditor", () => ({
  default: ({ segments, onPersistSegments, transcribeJobId }) => {
    _capturedSegments = segments;
    _capturedOnPersist = onPersistSegments;
    _capturedJobId = transcribeJobId;
    return (
      <div data-testid="mock-editor">
        <span data-testid="seg-text">{segments?.[0]?.text || ""}</span>
      </div>
    );
  },
}));

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
  _capturedSegments = null;
  _capturedOnPersist = null;
  _capturedJobId = null;
  localStorage.clear();
});

function baseJob(overrides = {}) {
  return {
    job_id: "job-123",
    status: "pending_review",
    filename: "song.mp3",
    artist: "Test Artist",
    segments_json: [{ start: 0, end: 1.0, text: "ORIGINAL TEXT" }],
    edit_count: 0,
    edits_remaining: 3,
    render_params: {},
    bg_r2_key_cached: "some/key",
    ...overrides,
  };
}

describe("EditRequestPanel — stale segments on modal reopen", () => {
  // BUG: When the operator edits → autosave succeeds → closes the
  // modal → reopens, the editor was re-mounting against job.segments_json
  // (the parent's stale prop) instead of the post-save snapshot.
  // The next autosave then POSTed the pre-edit text, silently
  // overwriting the operator's correction in the DB.
  //
  // Expected: the second open mounts LyricsEditor with the post-save
  // segments, not the parent's stale prop.
  it("uses last-saved segments (not stale job prop) on modal reopen", async () => {
    const user = userEvent.setup();

    // Mock fetch: /source-audio-url → 200 with a fake URL; /save-segments → 200.
    vi.stubGlobal(
      "fetch",
      vi.fn(async (url) => {
        if (typeof url === "string" && url.includes("/source-audio-url")) {
          return new Response(JSON.stringify({ url: "https://stub/audio.mp3" }), {
            status: 200,
            headers: { "Content-Type": "application/json" },
          });
        }
        if (typeof url === "string" && url.includes("/save-segments")) {
          return new Response("{}", {
            status: 200,
            headers: { "Content-Type": "application/json" },
          });
        }
        return new Response("{}", { status: 200 });
      }),
    );

    const job = baseJob();
    const { rerender } = render(<EditRequestPanel job={job} onEditTriggered={vi.fn()} />);

    // Open the lyrics modal — click the "Editar letras y estilo" button.
    const openBtn = screen.getByRole("button", { name: /Editar letras y estilo/i });
    await user.click(openBtn);

    // First open: editor receives the parent's prop verbatim.
    await waitFor(() => {
      expect(_capturedSegments).toBeTruthy();
    });
    expect(_capturedSegments[0].text).toBe("ORIGINAL TEXT");
    expect(screen.getByTestId("seg-text")).toHaveTextContent("ORIGINAL TEXT");

    // Simulate the operator editing + the 3 s autosave debounce firing.
    // We fire onPersistSegments directly with the updated segments — this
    // is what LyricsEditor's own useEffect would do after the operator
    // typed.
    const updatedSegs = [{ start: 0, end: 1.0, text: "UPDATED BY OPERATOR" }];
    await _capturedOnPersist(_capturedJobId, updatedSegs);

    // Close the modal — Esc key, same path the operator uses.
    fireEvent.keyDown(window, { key: "Escape" });

    // Modal closed (LyricsEditModal returns null when open=false), so the
    // mock-editor unmounts.
    await waitFor(() => {
      expect(screen.queryByTestId("mock-editor")).not.toBeInTheDocument();
    });

    // Parent's job prop has NOT changed — JobDetail's /status polling
    // does not return segments_json, so the array reference still points
    // at the pre-edit version. This is the trap the fix addresses.
    rerender(<EditRequestPanel job={job} onEditTriggered={vi.fn()} />);
    expect(job.segments_json[0].text).toBe("ORIGINAL TEXT");

    // Reopen the modal.
    _capturedSegments = null;
    const openBtn2 = screen.getByRole("button", { name: /Editar letras y estilo/i });
    await user.click(openBtn2);

    // The editor should now mount against the LAST-SAVED snapshot, not
    // against the parent's stale prop. Before the fix, this assertion
    // failed with "ORIGINAL TEXT" — proving the bug.
    await waitFor(() => {
      expect(_capturedSegments).toBeTruthy();
    });
    expect(_capturedSegments[0].text).toBe("UPDATED BY OPERATOR");
    expect(screen.getByTestId("seg-text")).toHaveTextContent("UPDATED BY OPERATOR");
  });

  // BUG: Escape was tearing down the whole modal even when the operator
  // only wanted to exit sync mode. LyricsEditor's own keydown handler
  // calls preventDefault on sync-mode-Escape, so the modal can check
  // event.defaultPrevented and skip closing in that case.
  it("does not close the modal when Escape was preventDefault'd by a child", async () => {
    const user = userEvent.setup();

    vi.stubGlobal(
      "fetch",
      vi.fn(async () =>
        new Response("{}", { status: 200, headers: { "Content-Type": "application/json" } }),
      ),
    );

    const job = baseJob();
    render(<EditRequestPanel job={job} onEditTriggered={vi.fn()} />);
    await user.click(screen.getByRole("button", { name: /Editar letras y estilo/i }));
    expect(screen.getByTestId("mock-editor")).toBeInTheDocument();

    // Simulate a child component (the real LyricsEditor in sync mode)
    // consuming Escape via preventDefault. We use a capture-phase
    // listener so it fires before the modal's bubble-phase listener
    // regardless of registration order — matches the real scenario
    // where LyricsEditor's child useEffect attaches its listener
    // earlier than the modal's parent useEffect.
    const consume = (e) => { if (e.key === "Escape") e.preventDefault(); };
    window.addEventListener("keydown", consume, { capture: true });
    try {
      fireEvent.keyDown(window, { key: "Escape", cancelable: true });
      // Modal must still be open — the editor stays mounted.
      expect(screen.getByTestId("mock-editor")).toBeInTheDocument();
    } finally {
      window.removeEventListener("keydown", consume, { capture: true });
    }

    // Sanity: a plain Escape with no child consumer still closes.
    fireEvent.keyDown(window, { key: "Escape" });
    await waitFor(() => {
      expect(screen.queryByTestId("mock-editor")).not.toBeInTheDocument();
    });
  });

  // Resetting the local mirror when the panel is reused across a
  // different job (SPA navigation within JobDetail) is essential —
  // otherwise opening job B's editor would surface job A's last-saved
  // snapshot, which is worse than the stale-prop bug we're fixing.
  it("resets local mirror when the job prop changes", async () => {
    const user = userEvent.setup();

    vi.stubGlobal(
      "fetch",
      vi.fn(async () =>
        new Response("{}", { status: 200, headers: { "Content-Type": "application/json" } }),
      ),
    );

    const jobA = baseJob({
      job_id: "job-A",
      segments_json: [{ start: 0, end: 1, text: "JOB A ORIGINAL" }],
    });
    const { rerender } = render(<EditRequestPanel job={jobA} onEditTriggered={vi.fn()} />);

    // Open + simulate save on job A.
    await user.click(screen.getByRole("button", { name: /Editar letras y estilo/i }));
    await waitFor(() => expect(_capturedOnPersist).toBeTruthy());
    await _capturedOnPersist("job-A", [{ start: 0, end: 1, text: "JOB A EDITED" }]);
    fireEvent.keyDown(window, { key: "Escape" });

    // Navigate to a different job within the same panel.
    const jobB = baseJob({
      job_id: "job-B",
      segments_json: [{ start: 0, end: 1, text: "JOB B ORIGINAL" }],
    });
    rerender(<EditRequestPanel job={jobB} onEditTriggered={vi.fn()} />);

    _capturedSegments = null;
    await user.click(screen.getByRole("button", { name: /Editar letras y estilo/i }));
    await waitFor(() => expect(_capturedSegments).toBeTruthy());

    // MUST show job B's segments — never job A's saved snapshot.
    expect(_capturedSegments[0].text).toBe("JOB B ORIGINAL");
  });
});
