import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import LyricReviewPanel from "./LyricReviewPanel";

const occurrence = (before, after, fix) => ({ line_segment_id: "b", start: 45.4, end: 46, before, after, fix });
const missing = {
  id: "m", kind: "missing", group: "text", required: true, title: "Falta texto", why: "Coinciden la máquina y el testigo",
  action: "Agregar", dismiss: "No se canta", keys: ["m"], start: 45.4, end: 46, sources: ["machine", "witness"],
  alternatives: [], occurrences: [occurrence("Tu garantía de reloco se fundió", "Tu garantía de reloco se fundió dormite ya",
    { type: "insert", text: "dormite ya" })],
};
const suggestion = {
  id: "s", kind: "heard_different", group: "text", required: false, title: "Se escucha distinto", why: "Lo indica la letra oficial",
  action: "Corregir", dismiss: "Está bien así", keys: ["s"], start: 78, end: 79, sources: ["official"],
  alternatives: [{ label: "la mar", replace: "la mar", why: "Lo indica el testigo" }],
  occurrences: [occurrence("Vos sos Román", "Vos sos lo más", { type: "replace", find: "Román", replace: "lo más" }),
    occurrence("Vos sos Román", "Vos sos lo más", { type: "replace", find: "Román", replace: "lo más" })],
};
const review = (items, extra = {}) => ({
  mode: "enforce", items, sources: { witness: true, gemini: true, official: false }, risk: { level: "normal", reasons: [] }, ...extra,
});

function renderPanel(items, extra = {}, props = {}) {
  const all = { review: review(items, extra), items, failedIds: new Set(), playingId: null,
    onPlay: vi.fn(), onApply: vi.fn(), onDismiss: vi.fn(), onEdit: vi.fn(), onUndo: vi.fn(),
    onActivate: vi.fn(), onOpenOfficial: vi.fn(), ...props };
  render(<LyricReviewPanel {...all} />);
  return all;
}

describe("LyricReviewPanel", () => {
  it("shows one active card with the change, and hides suggestions until asked", () => {
    renderPanel([missing, suggestion]);
    expect(screen.getByTestId("lyric-review-heading").textContent).toBe("Faltan 1 para aprobar");
    expect(screen.getByText("dormite ya").tagName).toBe("INS");
    expect(screen.queryByText(/Vos sos/)).toBeNull();
    fireEvent.click(screen.getByText("Ver 1 sugerencia (no bloquean)"));
    expect(screen.getByText("Vos sos lo más")).toBeTruthy();
  });

  it("decides from the keyboard without leaking keys to the editor", () => {
    const props = renderPanel([missing, { ...missing, id: "m2", keys: ["m2"] }], {}, { canUndo: true });
    const panel = screen.getByTestId("lyric-review-panel");
    let now = 0;
    const clock = vi.spyOn(Date, "now").mockImplementation(() => (now += 1000));
    const outer = vi.fn();
    window.addEventListener("keydown", outer);
    fireEvent.keyDown(panel, { key: "z" });
    expect(props.onUndo).toHaveBeenCalled();
    fireEvent.keyDown(panel, { key: "e" });
    expect(props.onPlay).toHaveBeenCalledWith(missing);
    fireEvent.keyDown(panel, { key: "Enter" });
    expect(props.onApply.mock.calls[0][0].id).toBe("m");
    fireEvent.keyDown(panel, { key: "j" });
    expect(props.onActivate).toHaveBeenCalled();
    fireEvent.keyDown(panel, { key: "m" });
    expect(props.onEdit.mock.calls[0][0].id).toBe("m2");
    expect(outer).not.toHaveBeenCalled();
    window.removeEventListener("keydown", outer);
    clock.mockRestore();
  });

  it("ignores a held key so nothing is applied without being seen", () => {
    const props = renderPanel([missing]);
    const panel = screen.getByTestId("lyric-review-panel");
    fireEvent.keyDown(panel, { key: "Enter", repeat: true });
    fireEvent.keyDown(panel, { key: "Backspace", repeat: true });
    expect(props.onApply).not.toHaveBeenCalled();
    expect(props.onDismiss).not.toHaveBeenCalled();
  });

  it("picks another option with its number", () => {
    const props = renderPanel([suggestion]);
    expect(screen.getByTestId("lyric-review-heading").textContent).toBe("Todo listo para aprobar");
    expect(screen.queryByTestId("lyric-review-active")).toBeNull();
    fireEvent.click(screen.getByText("Ver 1 sugerencia (opcional)"));
    fireEvent.keyDown(screen.getByTestId("lyric-review-panel"), { key: "1" });
    expect(props.onApply).toHaveBeenCalledWith(suggestion, suggestion.alternatives[0]);
  });

  it("marks only the punctuation when that is all that changes", () => {
    const question = { ...missing, id: "q", kind: "question_marks", group: "style", title: "Signos de pregunta",
      occurrences: [occurrence("¿Cuando vuelva?", "Cuando vuelva", { type: "punctuation", mode: "remove_question" })] };
    renderPanel([question]);
    const removed = [...document.querySelectorAll("del")].map((node) => node.textContent);
    expect(removed).toEqual(["¿", "?"]);
  });

  it("asks to listen when only one ear disagrees, never applying its text", () => {
    const listen = { ...suggestion, id: "l", listen: true, title: "Escuchá este tramo", alternatives: [],
      why: "Sólo el testigo oyó «pido»", occurrences: [occurrence("Lo más cierto es que no pito",
        "Lo más cierto es que no pido", { type: "replace", find: "pito", replace: "pido" })] };
    const props = renderPanel([missing, listen]);
    const panel = screen.getByTestId("lyric-review-panel");
    fireEvent.click(screen.getByText("Ver 1 sugerencia (no bloquean)"));
    fireEvent.click(screen.getByText("Lo más cierto es que no pito"));
    expect(screen.getByTestId("lyric-review-active").textContent).not.toMatch(/pido\s*$/);
    expect(screen.queryByText("pido")).toBeNull();
    let now = 0;
    const clock = vi.spyOn(Date, "now").mockImplementation(() => (now += 1000));
    fireEvent.keyDown(panel, { key: "Enter" });
    expect(props.onPlay).toHaveBeenCalledWith(expect.objectContaining({ id: "l" }));
    expect(props.onApply).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole("button", { name: /^Escuchar\s*Enter$/ }));
    expect(props.onApply).not.toHaveBeenCalled();
    clock.mockRestore();
  });

  it("keeps a failed fix on screen with a way out", () => {
    renderPanel([missing], {}, { failedIds: new Set(["m"]) });
    expect(screen.getByRole("alert").textContent).toMatch(/la línea cambió/);
  });

  it("shows the all-set state, the difficult-song warning and survives partial data", () => {
    renderPanel([], { risk: { level: "high" } });
    expect(screen.getByTestId("lyric-review-heading").textContent).toBe("Todo listo para aprobar");
    expect(screen.queryByTestId("lyric-review-risk")).toBeNull();
  });

  it("does not claim approval is blocked in observe mode", () => {
    renderPanel([missing], { mode: "observe" });
    expect(screen.getByTestId("lyric-review-heading").textContent).toBe("1 para revisar");
  });

  it("opens the shared official-lyrics dialog and keeps the guide behind help", () => {
    const props = renderPanel([]);
    expect(screen.getByText("Sin puntos para revisar en esta letra.")).toBeTruthy();
    fireEvent.click(screen.getByText("Comparar con letra oficial"));
    expect(props.onOpenOfficial).toHaveBeenCalled();
    expect(screen.queryByTestId("lyric-review-help")).toBeNull();
    fireEvent.keyDown(screen.getByTestId("lyric-review-panel"), { key: "?" });
    expect(screen.getByTestId("lyric-review-help").textContent).toMatch(/deshacer/);
    expect(screen.getByTestId("lyric-review-help").textContent).toMatch(/Gemini · testigo/);
    fireEvent.click(screen.getByText("Guía UMG"));
    expect(screen.getByTestId("lyric-review-guide").textContent).toMatch(/una sola pantalla/);
  });

  it("toggles auto-listen as a pressed button", () => {
    const onToggleAutoPlay = vi.fn();
    renderPanel([missing], {}, { autoPlay: true, onToggleAutoPlay });
    const toggle = screen.getByRole("button", { name: /Reproducir al avanzar/ });
    expect(toggle.getAttribute("aria-pressed")).toBe("true");
    fireEvent.click(toggle);
    expect(onToggleAutoPlay).toHaveBeenCalled();
  });
});
