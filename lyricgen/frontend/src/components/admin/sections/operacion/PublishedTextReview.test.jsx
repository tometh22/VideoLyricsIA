import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";
import PublishedTextReview from "./PublishedTextReview";

afterEach(cleanup);
it("requires the checklist, explicit video review and a reason before manual closure", () => {
  const confirm = vi.fn();
  const props = { review: { missing: ["me despirerta por las noches"] }, onConfirm: confirm };
  const { rerender } = render(<PublishedTextReview {...props} checklistReady={false} />);
  const button = screen.getByRole("button", { name: "Resolver con revisión manual" });
  expect(screen.getByText("«me despirerta por las noches»")).toBeInTheDocument();
  fireEvent.change(screen.getByLabelText("Motivo de la revisión manual"), { target: { value: "Verifiqué despierta en el video." } });
  fireEvent.click(screen.getByRole("checkbox"));
  expect(button).toBeDisabled();
  rerender(<PublishedTextReview {...props} checklistReady />);
  expect(button).toBeEnabled();
  fireEvent.change(screen.getByLabelText("Motivo de la revisión manual"), { target: { value: "   " } });
  expect(button).toBeDisabled();
  fireEvent.change(screen.getByLabelText("Motivo de la revisión manual"), { target: { value: " Verifiqué despierta en el video. " } });
  fireEvent.click(button);
  expect(confirm).toHaveBeenCalledWith("Verifiqué despierta en el video.");
  rerender(<PublishedTextReview {...props} checklistReady busy />);
  expect(button).toBeDisabled();
});
