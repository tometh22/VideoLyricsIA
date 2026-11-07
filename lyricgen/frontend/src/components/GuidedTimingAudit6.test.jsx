import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, expect, it, vi } from "vitest";
import GuidedTimingReview from "./GuidedTimingReview";

vi.mock("../i18n", () => ({ useI18n: () => ({ t: () => undefined }) }));
afterEach(cleanup);

function props(duration = 1) {
  return {
    windows: [{ id: "ending", start: 0.5, end: 1, reasons: ["timing"] }],
    segments: [
      { _id: "a", start: 0.6, end: 1, text: "Última frase audible" },
      { _id: "outlier", start: 2, end: 3, text: "Importada fuera del audio" },
    ],
    duration, waveform: { duration: 1, peaks: [] }, audioAvailable: true,
    onPlayWindow: vi.fn().mockResolvedValue(true), onMove: vi.fn(),
  };
}

it.each([1, 0])("blocks a guided nudge past known audio/waveform duration (%s) despite a later outlier", async (duration) => {
  const input = props(duration);
  render(<GuidedTimingReview {...input} />);
  await userEvent.click(screen.getByRole("button", { name: /Escuchar fragmento/ }));
  const later = screen.getByRole("button", { name: /Mover 0,1 segundos después/ });
  expect(later).toBeDisabled();
  await userEvent.click(later);
  expect(input.onMove).not.toHaveBeenCalled();
});

it("does not drag a valid ending beyond the audio toward an outlier", async () => {
  const input = props();
  render(<GuidedTimingReview {...input} />);
  await userEvent.click(screen.getByRole("button", { name: /Escuchar fragmento/ }));
  const line = screen.getByTestId("guided-segment-a");
  fireEvent.pointerDown(line, { pointerId: 1, button: 0, clientX: 100 });
  fireEvent.pointerMove(line, { pointerId: 1, clientX: 460 });
  fireEvent.pointerUp(line, { pointerId: 1, clientX: 460 });
  expect(input.onMove).not.toHaveBeenCalled();
});
