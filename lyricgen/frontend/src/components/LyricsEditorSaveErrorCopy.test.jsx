/**
 * Copy honesto del fallo de respaldo, por CAUSA real (fast-follow del
 * incidente UMG 21-jul-2026). Antes el banner + el confirm de "Aprobar"
 * decían "problema de red" para CUALQUIER fallo — engañoso cuando la causa
 * era otra (una sesión vencida no se arregla con el auto-retry). Ahora el
 * texto refleja result.reason/status de persistSegments.
 */
import { render, screen, cleanup, fireEvent, act } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import LyricsEditor from "./LyricsEditor";

vi.mock("../i18n", () => ({ useI18n: () => ({ t: (_k, fb) => fb }) }));
vi.mock("./OnboardingTour", () => ({ EditorTour: () => null }));
vi.mock("./ToastProvider", () => ({
  useToast: () => ({ toast: () => {}, dismiss: () => {} }),
  ToastProvider: ({ children }) => children,
}));

function baseProps(overrides = {}) {
  return {
    segments: [{ start: 1.0, end: 2.0, text: "alpha line" }],
    filename: "song.mp3",
    audioFile: null,
    referenceLyrics: "",
    onApprove: vi.fn(),
    onBack: vi.fn(),
    transcribeJobId: "job-copy-test",
    ...overrides,
  };
}

afterEach(() => cleanup());

// Dispara el autosave debounced y deja resolver la promesa del persist.
async function editAndFlush() {
  const input = screen.getByDisplayValue("alpha line");
  fireEvent.change(input, { target: { value: "alpha line editada" } });
  await act(async () => {
    vi.advanceTimersByTime(3100);
  });
  // dejar que el microtask del await resuelva y React re-renderice
  await act(async () => { await Promise.resolve(); });
}

describe("banner de fallo de respaldo — copy por causa", () => {
  beforeEach(() => vi.useFakeTimers());
  afterEach(() => vi.useRealTimers());

  it("un 401 muestra 'tu sesión venció', NO 'problema de red'", async () => {
    const onPersistSegments = vi
      .fn()
      .mockResolvedValue({ ok: false, reason: "http-401", status: 401 });
    render(<LyricsEditor {...baseProps({ onPersistSegments })} />);
    await editAndFlush();

    expect(screen.getByText(/tu sesión venció/i)).toBeTruthy();
    expect(screen.queryByText(/problema de red/i)).toBeNull();
  });

  it("un 404 (job-gone) muestra 'ya no está en el servidor'", async () => {
    const onPersistSegments = vi
      .fn()
      .mockResolvedValue({ ok: false, reason: "job-gone", status: 404 });
    render(<LyricsEditor {...baseProps({ onPersistSegments })} />);
    await editAndFlush();

    expect(screen.getByText(/ya no está en el servidor/i)).toBeTruthy();
    expect(screen.queryByText(/problema de red/i)).toBeNull();
  });

  it("un fallo de red real SÍ dice 'problema de red'", async () => {
    const onPersistSegments = vi
      .fn()
      .mockResolvedValue({ ok: false, reason: "network" });
    render(<LyricsEditor {...baseProps({ onPersistSegments })} />);
    await editAndFlush();

    expect(screen.getByText(/problema de red/i)).toBeTruthy();
  });

  it("un motivo desconocido (5xx) cae al copy genérico, sin mentir 'red'", async () => {
    const onPersistSegments = vi
      .fn()
      .mockResolvedValue({ ok: false, reason: "http-500", status: 500 });
    render(<LyricsEditor {...baseProps({ onPersistSegments })} />);
    await editAndFlush();

    expect(
      screen.getByText(/No pudimos respaldar tu última edición en el servidor/i),
    ).toBeTruthy();
    expect(screen.queryByText(/problema de red/i)).toBeNull();
  });
});
