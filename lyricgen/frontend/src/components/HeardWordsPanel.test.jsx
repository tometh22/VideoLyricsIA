import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import HeardWordsPanel from "./HeardWordsPanel";

const segments = [
  { segment_id: "b", start: 43.5, end: 47.8, text: "Tu garantía de reloco se fundió" },
];
const alert = {
  id: "a1", key: "dormite ya@45", text: "dormite ya", start: 45.4, end: 46.0,
  sources: "both", action: "insert", line_segment_id: "b", anchor_before: "fundio",
};

function renderPanel(overrides = {}) {
  const props = {
    alerts: [alert], segments, playingId: null,
    onPlay: vi.fn(), onAdd: vi.fn(), onDismiss: vi.fn(), ...overrides,
  };
  render(<HeardWordsPanel {...props} />);
  return props;
}

describe("HeardWordsPanel", () => {
  it("renders nothing when there is nothing to decide", () => {
    const { container } = render(
      <HeardWordsPanel alerts={[]} segments={segments} onPlay={vi.fn()} onAdd={vi.fn()} onDismiss={vi.fn()} />,
    );
    expect(container.firstChild).toBeNull();
  });

  it("shows the line as it would read after adding the missing words", () => {
    renderPanel();
    expect(screen.getByText(/1 por decidir/)).toBeTruthy();
    expect(screen.getByTitle("Tu garantía de reloco se fundió dormite ya")).toBeTruthy();
    expect(screen.getByText("dormite ya").tagName).toBe("MARK");
  });

  it("resolves with one click", () => {
    const props = renderPanel();
    fireEvent.click(screen.getByText("Agregar"));
    fireEvent.click(screen.getByText("No se canta"));
    fireEvent.click(screen.getByLabelText("Escuchar 0:45"));
    expect(props.onAdd).toHaveBeenCalledWith(alert);
    expect(props.onDismiss).toHaveBeenCalledWith(alert);
    expect(props.onPlay).toHaveBeenCalledWith(alert);
  });

  it("resolves the first pending alert from the keyboard", () => {
    const props = renderPanel();
    const panel = screen.getByTestId("heard-words-panel");
    fireEvent.keyDown(panel, { key: "e" });
    fireEvent.keyDown(panel, { key: "a" });
    fireEvent.keyDown(panel, { key: "N" });
    expect(props.onPlay).toHaveBeenCalledWith(alert);
    expect(props.onAdd).toHaveBeenCalledWith(alert);
    expect(props.onDismiss).toHaveBeenCalledWith(alert);
  });
});
