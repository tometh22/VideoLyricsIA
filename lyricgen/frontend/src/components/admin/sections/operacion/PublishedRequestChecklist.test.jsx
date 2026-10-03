import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import PublishedRequestChecklist from "./PublishedRequestChecklist";

const mocks = vi.hoisted(() => ({ fetchJson: vi.fn() }));
vi.mock("../../adminApi", () => ({ API: "", fetchJson: mocks.fetchJson }));
afterEach(cleanup);
beforeEach(() => {
  mocks.fetchJson.mockReset();
  mocks.fetchJson.mockResolvedValue({ segments: [
    { start: 38.6, text: "Lustrador armoniquero con su labio de charol" },
    { start: 43, text: "Anda a lavártelos" },
  ] });
});

it("muestra lo que dice el video en cada punto y sólo habilita al marcar todos", async () => {
  const onReadyChange = vi.fn();
  const onSeek = vi.fn();
  render(<PublishedRequestChecklist requestId={129} comment="0:39 LUSTRADOR con sus labioS // 0:19 sacar los signos"
    onReadyChange={onReadyChange} onSeek={onSeek} />);
  expect(mocks.fetchJson).toHaveBeenCalledWith("/admin/change-requests/129/review");
  expect(await screen.findByText("Lustrador armoniquero con su labio de charol")).toBeInTheDocument();
  expect(screen.getByText("El video no tiene letra en 0:19.")).toBeInTheDocument();
  expect(onReadyChange).toHaveBeenLastCalledWith(false, 0);
  fireEvent.click(screen.getByLabelText("Está en el video: 0:39 LUSTRADOR con sus labioS"));
  expect(onReadyChange).toHaveBeenLastCalledWith(false, 1);
  fireEvent.click(screen.getByLabelText("Está en el video: 0:19 sacar los signos"));
  expect(onReadyChange).toHaveBeenLastCalledWith(true, 2);
  expect(screen.getByText("2 de 2")).toBeInTheDocument();
  fireEvent.click(screen.getByRole("button", { name: "0:38" }));
  expect(onSeek).toHaveBeenCalledWith(38.6);
});

it("si la letra no carga, igual permite revisar contra el reproductor", async () => {
  mocks.fetchJson.mockRejectedValue(new Error("boom"));
  const onReadyChange = vi.fn();
  render(<PublishedRequestChecklist requestId={1} comment="" onReadyChange={onReadyChange} />);
  expect(await screen.findByText(/No pude cargar la letra del video/)).toBeInTheDocument();
  fireEvent.click(screen.getByLabelText("Está en el video: Revisé el pedido completo en el video"));
  await waitFor(() => expect(onReadyChange).toHaveBeenLastCalledWith(true, 1));
});
