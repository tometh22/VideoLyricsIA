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

  // Contrato nuevo (20-ago-2026): en Genly no hay dos personas editando la
  // misma canción — es la MISMA persona con varias pestañas abiertas. Un
  // borrador de revisión vieja es su propio trabajo, así que se recupera solo,
  // sin cartel de conflicto y sin pedirle que arbitre nada.
  it("recupera solo un borrador de revisión obsoleta, sin preguntar nada", async () => {
    const onPersistSegments = vi.fn().mockResolvedValue({ ok: true, revision: 9 });
    localStorage.setItem(DRAFT_KEY, JSON.stringify({
      segments: local,
      base_revision: 7,
    }));

    renderEditor({ onPersistSegments });

    // Lo que el operador había tipeado vuelve a pantalla…
    expect(screen.getByDisplayValue("Borrador viejo")).toBeInTheDocument();
    // …y nunca ve el cartel muerto que antes lo dejaba sin salida.
    expect(screen.queryByText("Cambio en conflicto")).toBeNull();

    // El respaldo sigue pasando por el check de revisión del backend: se
    // guarda contra la revisión ACTUAL del servidor (8), no contra la vieja.
    await act(async () => { await vi.advanceTimersByTimeAsync(4_000); });
    expect(onPersistSegments).toHaveBeenCalled();
    expect(onPersistSegments.mock.calls[0][2]).toMatchObject({ baseRevision: 8 });
  });

  it("mergea con la otra pestaña cuando el borrador conoce su base", async () => {
    const onPersistSegments = vi.fn().mockResolvedValue({ ok: true, revision: 9 });
    localStorage.setItem(DRAFT_KEY, JSON.stringify({
      segments: [{ start: 1, end: 2, text: "Borrador viejo" }],
      base_segments: [{ start: 1, end: 2, text: "Original" }],
      base_revision: 7,
    }));

    renderEditor({ onPersistSegments });

    // Ambas ediciones tocaron la misma línea → gana la de esta pestaña, que
    // es la que el operador está mirando.
    expect(screen.getByDisplayValue("Borrador viejo")).toBeInTheDocument();
    expect(screen.queryByText("Cambio en conflicto")).toBeNull();
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
