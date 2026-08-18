import { act, cleanup, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import LyricsEditor from "./LyricsEditor";
import { segmentsStore } from "../state/segmentsStore";

vi.mock("../i18n", () => ({ useI18n: () => ({ t: () => undefined }) }));
vi.mock("./OnboardingTour", () => ({ EditorTour: () => null }));
vi.mock("./ToastProvider", () => ({
  useToast: () => ({ toast: vi.fn(), dismiss: vi.fn() }),
  ToastProvider: ({ children }) => children,
}));

const JOB_ID = "legacy-draft-job";
const DRAFT_KEY = `genly_segments_draft:tenant-1:user-1:${JOB_ID}`;
const remote = [{ start: 1, end: 2, text: "Cambio de otra pestaña" }];
const local = [{ start: 1, end: 2, text: "Borrador viejo" }];

function renderEditor(overrides = {}) {
  return render(<LyricsEditor
    segments={remote}
    segmentsRevision={8}
    filename="song.mp3"
    audioFile={null}
    referenceLyrics=""
    transcribeJobId={JOB_ID}
    user={{ id: "user-1", tenant_id: "tenant-1" }}
    onPersistSegments={vi.fn().mockResolvedValue({ ok: true, revision: 9 })}
    onApprove={vi.fn()}
    onBack={vi.fn()}
    {...overrides}
  />);
}

describe("LyricsEditor legacy draft concurrency", () => {
  beforeEach(() => {
    vi.useFakeTimers();
    localStorage.clear();
    segmentsStore._clearAll();
  });
  afterEach(() => {
    cleanup();
    segmentsStore._clearAll();
    localStorage.clear();
    vi.useRealTimers();
  });

  it("preserva pero no restaura ni autosavea un borrador de revisión obsoleta", async () => {
    const onPersistSegments = vi.fn().mockResolvedValue({ ok: true, revision: 9 });
    localStorage.setItem(DRAFT_KEY, JSON.stringify({
      segments: local,
      base_revision: 7,
    }));

    renderEditor({ onPersistSegments });

    expect(screen.getByDisplayValue("Cambio de otra pestaña")).toBeInTheDocument();
    expect(screen.queryByDisplayValue("Borrador viejo")).toBeNull();
    expect(screen.getByText("Cambio en conflicto")).toBeInTheDocument();
    await act(async () => { await vi.advanceTimersByTimeAsync(4_000); });
    expect(onPersistSegments).not.toHaveBeenCalled();
    expect(JSON.parse(localStorage.getItem(DRAFT_KEY)).segments).toEqual(local);
    window.dispatchEvent(new Event("pagehide"));
    window.dispatchEvent(new Event("beforeunload"));
    expect(onPersistSegments).not.toHaveBeenCalled();
    expect(JSON.parse(localStorage.getItem(DRAFT_KEY)).segments).toEqual(local);
  });

  it("restaura y guarda un borrador que parte de la revisión actual", async () => {
    const onPersistSegments = vi.fn().mockResolvedValue({ ok: true, revision: 9 });
    localStorage.setItem(DRAFT_KEY, JSON.stringify({
      segments: local,
      base_revision: 8,
    }));

    renderEditor({ onPersistSegments });

    expect(screen.getByDisplayValue("Borrador viejo")).toBeInTheDocument();
    await act(async () => { await vi.advanceTimersByTimeAsync(3_100); });
    expect(onPersistSegments).toHaveBeenCalledTimes(1);
    expect(onPersistSegments.mock.calls[0][1]).toEqual(expect.arrayContaining([
      expect.objectContaining({ text: "Borrador viejo" }),
    ]));
    expect(onPersistSegments.mock.calls[0][2]).toMatchObject({ baseRevision: 8 });
  });
});
