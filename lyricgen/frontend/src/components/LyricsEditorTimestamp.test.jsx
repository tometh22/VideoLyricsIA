import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import LyricsEditor from "./LyricsEditor";
import { segmentsStore } from "../state/segmentsStore";

vi.mock("../i18n", () => ({ useI18n: () => ({ t: (_key, fallback) => fallback }) }));
vi.mock("./OnboardingTour", () => ({ EditorTour: () => null }));
vi.mock("./ToastProvider", () => ({ useToast: () => ({ toast: () => {}, dismiss: () => {} }) }));

afterEach(() => {
  cleanup();
  segmentsStore._clearAll();
});

function mount(start, end = start + 1) {
  const onApprove = vi.fn();
  render(<LyricsEditor segments={[{ start, end, text: "Una línea de prueba" }]}
    filename="trial-timing.mp3" audioFile={null} referenceLyrics=""
    onApprove={onApprove} onBack={vi.fn()} />);
  return onApprove;
}

function openTime() {
  fireEvent.doubleClick(screen.getByRole("button", {
    name: /Doble click para editar el tiempo de la línea 1$/,
  }));
  return screen.getByRole("textbox", { name: "Tiempo de inicio de la línea 1" });
}

describe("trial timestamp display and editing", () => {
  it.each([
    [14.1, "0:14.1"], [14.2, "0:14.2"], [35.9, "0:35.9"],
    [59.9, "0:59.9"], [59.96, "1:00.0"], [64.1, "1:04.1"],
    [119.96, "2:00.0"],
  ])("displays %s without losing a tenth or breaking minute carry", (start, label) => {
    mount(start);
    expect(screen.getByRole("button", {
      name: `Reproducir desde ${label}. Doble click para editar el tiempo de la línea 1`,
    })).toBeInTheDocument();
  });

  it.each([
    [14.1, "0:14.1"], [14.083, "0:14.083"], [59.96, "0:59.96"],
    [64.125, "1:04.125"], [14.0834, "0:14.083"], [59.9999, "1:00.0"],
  ])("opening and blurring %s keeps the original start and end", async (start, input) => {
    const end = start + 1.2345;
    const onApprove = mount(start, end);
    const field = openTime();
    expect(field).toHaveValue(input);
    fireEvent.blur(field);
    fireEvent.click(screen.getByRole("button", { name: "Aprobar y generar" }));
    await waitFor(() => expect(onApprove).toHaveBeenCalledTimes(1));
    expect(onApprove.mock.calls[0][0][0]).toMatchObject({ start, end });
  });

  it("editing 14.0 to 14.1 survives Enter, reopening and approval", async () => {
    const onApprove = mount(14);
    const field = openTime();
    fireEvent.change(field, { target: { value: "0:14.1" } });
    act(() => {
      fireEvent.keyDown(field, { key: "Enter" });
      fireEvent.blur(field);
    });
    expect(screen.getByRole("button", {
      name: "Reproducir desde 0:14.1. Doble click para editar el tiempo de la línea 1",
    })).toBeInTheDocument();
    const reopened = openTime();
    expect(reopened).toHaveValue("0:14.1");
    fireEvent.blur(reopened);
    fireEvent.click(screen.getByRole("button", { name: "Aprobar y generar" }));
    await waitFor(() => expect(onApprove).toHaveBeenCalledTimes(1));
    expect(onApprove.mock.calls[0][0][0].start).toBe(14.1);
  });

  it.each(["unchanged", "equivalent", "escape"])("%s input leaves a short precise line untouched", async (kind) => {
    const start = 14.0834, end = 14.2084;
    const onApprove = mount(start, end);
    const field = openTime();
    if (kind === "equivalent") fireEvent.change(field, { target: { value: String(start) } });
    if (kind === "escape") {
      fireEvent.change(field, { target: { value: "0:14.3" } });
      act(() => {
        fireEvent.keyDown(field, { key: "Escape" });
        fireEvent.blur(field);
      });
    } else fireEvent.blur(field);
    fireEvent.click(screen.getByRole("button", { name: "Aprobar y generar" }));
    await waitFor(() => expect(onApprove).toHaveBeenCalledTimes(1));
    expect(onApprove.mock.calls[0][0][0]).toMatchObject({ start, end });
  });
});
