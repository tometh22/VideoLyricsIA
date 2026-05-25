// Tests for the stale re-render escape in EditRequestPanel. When a lyrics
// re-render is requested but the editor segments match the persisted
// segments_json exactly, the panel used to hard-error ("no hay nada que
// re-renderizar") and trap the operator: the editor looked correct but the
// rendered video was stale (lyrics fixed out-of-band, or a prior render
// failed). Now it offers an explicit "Re-renderizar igual" escape.
// Origin: Babasónicos job 284a63a8b806 — segments_json corrected directly,
// operator could not re-render because the UI saw no diff (2026-05-20).
import { render, screen, cleanup, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, it, expect, vi } from "vitest";
import EditRequestPanel from "./EditRequestPanel";

vi.mock("../i18n", () => ({ useI18n: () => ({ t: (_key, fallback) => fallback }) }));
vi.mock("./AlertProvider", () => ({
  useAlert: () => ({ alert: () => {} }),
  AlertProvider: ({ children }) => children,
}));
vi.mock("./ContentValidationToggle", () => ({ default: () => null, isUmgTenant: () => false }));
vi.mock("./BackgroundHintField", () => ({ default: () => null }));

// Mock LyricsEditor to expose onApprove via two buttons: one re-submits the
// exact segments it received (unchanged), the other submits edited text.
vi.mock("./LyricsEditor", () => ({
  default: ({ segments, onApprove }) => (
    <div data-testid="mock-editor">
      <button data-testid="approve-same" onClick={() => onApprove(segments)}>same</button>
      <button
        data-testid="approve-changed"
        onClick={() => onApprove([{ start: 0, end: 1.0, text: "TEXTO NUEVO" }])}
      >
        changed
      </button>
    </div>
  ),
}));

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
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

function mockFetch() {
  const calls = [];
  vi.stubGlobal(
    "fetch",
    vi.fn(async (url, opts) => {
      calls.push({ url: String(url), opts });
      if (String(url).includes("/source-audio-url")) {
        return new Response(JSON.stringify({ url: "https://stub/audio.mp3" }), {
          status: 200,
          headers: { "Content-Type": "application/json" },
        });
      }
      return new Response("{}", { status: 200, headers: { "Content-Type": "application/json" } });
    }),
  );
  return calls;
}

const editPosted = (calls) =>
  calls.some((c) => c.url.includes("/edit/") && (c.opts?.method || "").toUpperCase() === "POST");

async function openModal(user) {
  await user.click(screen.getByRole("button", { name: /Editar letras y estilo/i }));
  await waitFor(() => expect(screen.getByTestId("mock-editor")).toBeInTheDocument());
}

describe("EditRequestPanel — stale re-render escape", () => {
  it("offers 'Re-renderizar igual' on unchanged segments, then forces the re-render", async () => {
    const user = userEvent.setup();
    const calls = mockFetch();
    render(<EditRequestPanel job={baseJob()} onEditTriggered={vi.fn()} />);
    await openModal(user);

    // Approve without changing anything → no /edit POST, escape appears.
    await user.click(screen.getByTestId("approve-same"));
    expect(editPosted(calls)).toBe(false);
    const forceBtn = await screen.findByRole("button", { name: /Re-renderizar igual/i });
    expect(forceBtn).toBeInTheDocument();

    // Force → the re-render actually fires this time.
    await user.click(forceBtn);
    await waitFor(() => expect(editPosted(calls)).toBe(true));
  });

  it("submits directly (no escape) when the segments actually changed", async () => {
    const user = userEvent.setup();
    const calls = mockFetch();
    render(<EditRequestPanel job={baseJob()} onEditTriggered={vi.fn()} />);
    await openModal(user);

    await user.click(screen.getByTestId("approve-changed"));
    await waitFor(() => expect(editPosted(calls)).toBe(true));
    expect(screen.queryByRole("button", { name: /Re-renderizar igual/i })).not.toBeInTheDocument();
  });
});
