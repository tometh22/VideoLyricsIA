import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import LyricReviewPanel from "./LyricReviewPanel";

const occurrence = (before, after, fix) => ({ line_segment_id: "b", start: 45.4, end: 46, before, after, fix });
const missing = {
  id: "m", kind: "missing", required: true, title: "Falta texto", why: "Coinciden la máquina y el testigo",
  action: "Agregar", dismiss: "No se canta", keys: ["m"], start: 45.4, end: 46, sources: ["machine", "witness"],
  alternatives: [], occurrences: [occurrence("Tu garantía de reloco se fundió", "Tu garantía de reloco se fundió dormite ya",
    { type: "insert", text: "dormite ya" })],
};
const suggestion = {
  id: "s", kind: "heard_different", required: false, title: "Se escucha distinto", why: "Lo indica la letra oficial",
  action: "Corregir", dismiss: "Está bien así", keys: ["s"], start: 78, end: 79, sources: ["official"],
  alternatives: [{ replace: "la mar", why: "Lo indica el testigo" }],
  occurrences: [occurrence("Vos sos Román", "Vos sos lo más", { type: "replace", find: "Román", replace: "lo más" }),
    occurrence("Vos sos Román", "Vos sos lo más", { type: "replace", find: "Román", replace: "lo más" })],
};
const review = (items, extra = {}) => ({
  mode: "enforce", items, sources: { witness: true, gemini: true, official: false }, risk: { level: "normal", reasons: [] }, ...extra,
});

function renderPanel(items, extra) {
  const props = { review: review(items, extra), items, playingId: null,
    onPlay: vi.fn(), onApply: vi.fn(), onDismiss: vi.fn(), onPasteOfficial: vi.fn(async () => true) };
  render(<LyricReviewPanel {...props} />);
  return props;
}

describe("LyricReviewPanel", () => {
  it("says how many points block approval and hides suggestions until asked", () => {
    renderPanel([missing, suggestion]);
    expect(screen.getByText(/1 para decidir antes de aprobar/)).toBeTruthy();
    expect(screen.getByText("dormite ya").tagName).toBe("MARK");
    expect(screen.queryByText("Vos sos")).toBeNull();
    fireEvent.click(screen.getByText("Ver 1 sugerencia (no bloquean)"));
    expect(screen.getByText(/\+1 repetición, se corrigen juntas/)).toBeTruthy();
  });

  it("applies, dismisses, listens and moves from the keyboard", () => {
    const props = renderPanel([missing, { ...missing, id: "m2", keys: ["m2"] }]);
    const panel = screen.getByTestId("lyric-review-panel");
    fireEvent.keyDown(panel, { key: "e" });
    expect(props.onPlay).toHaveBeenCalledWith(missing);
    fireEvent.keyDown(panel, { key: "j" });
    fireEvent.keyDown(panel, { key: "a" });
    expect(props.onApply.mock.calls[0][0].id).toBe("m2");
    fireEvent.keyDown(panel, { key: "k" });
    fireEvent.keyDown(panel, { key: "n" });
    expect(props.onDismiss).toHaveBeenCalledWith(missing);
  });

  it("lets the reviewer pick another option", () => {
    const props = renderPanel([suggestion]);
    fireEvent.click(screen.getByRole("button", { name: "la mar" }));
    expect(props.onApply).toHaveBeenCalledWith(suggestion, suggestion.alternatives[0]);
  });

  it("shows a clear all-set state and the difficult-song warning", () => {
    renderPanel([], { risk: { level: "high", reasons: ["El testigo automático no pudo transcribir bien esta canción"] } });
    expect(screen.getByText(/todo listo para aprobar/)).toBeTruthy();
    expect(screen.getByTestId("lyric-review-risk").textContent).toMatch(/Canción difícil/);
  });

  it("pastes official lyrics only for comparison", async () => {
    const props = renderPanel([]);
    fireEvent.click(screen.getByText("Pegar letra oficial"));
    fireEvent.change(screen.getByLabelText("Letra oficial"), { target: { value: "Letra" } });
    fireEvent.click(screen.getByText("Comparar con esta letra"));
    expect(props.onPasteOfficial).toHaveBeenCalledWith("Letra");
  });

  it("shows the UMG guide on demand", () => {
    renderPanel([]);
    fireEvent.click(screen.getByText("Guía UMG"));
    expect(screen.getByTestId("lyric-review-guide").textContent).toMatch(/una sola pantalla/);
  });
});
