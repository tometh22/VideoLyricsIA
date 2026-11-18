import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";
import PublishedHumanReview from "./PublishedHumanReview";

afterEach(cleanup);
const item = { id: 7, comment: "0:14 Cambiar una palabra // 0:30 Ajustar el estribillo",
  published_review: { review_token: "a".repeat(64), published_revision: 6 },
  delivery: { published_video_url: "https://example.test/published-6.mp4", video_url: "https://example.test/candidate.mp4" } };

it("reviews the portal video and requires every point, a reason and acknowledgement", () => {
  const confirm = vi.fn();
  render(<PublishedHumanReview item={item} onConfirm={confirm} onCancel={() => {}} />);
  expect(screen.getByLabelText("Versión publicada para revisión humana")).toHaveAttribute("src", item.delivery.published_video_url);
  const button = screen.getByRole("button", { name: "Confirmar pedido resuelto" });
  expect(button).toBeDisabled();
  const points = screen.getAllByRole("checkbox", { name: /Verificado en la publicación/ });
  fireEvent.click(points[0]);
  fireEvent.change(screen.getByLabelText("Motivo del cierre tras revisión humana"), { target: { value: "Revisé la corrección en el portal." } });
  fireEvent.click(screen.getByRole("checkbox", { name: /Revisé esta versión/ }));
  expect(button).toBeDisabled();
  fireEvent.click(points[1]);
  expect(button).toBeEnabled();
  expect(confirm).not.toHaveBeenCalled();
  fireEvent.click(button);
  expect(confirm).toHaveBeenCalledWith(2, { publicationToken: item.published_review.review_token, reason: "Revisé la corrección en el portal." });
});
