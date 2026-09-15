import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";
import CampaignDeliveryProgress from "./CampaignDeliveryProgress";

afterEach(cleanup);

it("explains a partial delivery and selects only failed videos for retry", async () => {
  const select = vi.fn();
  const request = vi.fn().mockResolvedValue({ status: "partial", sent_count: 1, total_count: 2, destination_portal: "chile", items: [
    { job_id: "sent", status: "sent" }, { job_id: "failed", status: "failed", error_code: "stale_approval" },
  ] });
  render(<CampaignDeliveryProgress operationId="operation-1" request={request} onSelectFailed={select} />);
  expect(await screen.findByText(/1 de 2 enviados · 1 con error · Requiere atención/)).toBeInTheDocument();
  expect(screen.getByText(/La aprobación o el video cambió/)).toBeInTheDocument();
  fireEvent.click(screen.getByRole("button", { name: "Seleccionar sólo los fallidos" }));
  expect(select).toHaveBeenCalledWith(["failed"]);
  expect(screen.getByRole("link", { name: "Abrir portal Chile" })).toHaveAttribute("href", "https://umgchile.genly.pro");
});

it("retries a failed status lookup without creating another delivery", async () => {
  const request = vi.fn().mockRejectedValueOnce(new Error("Sin conexión")).mockResolvedValue({ status: "completed", sent_count: 2, total_count: 2, items: [] });
  render(<CampaignDeliveryProgress operationId="operation-2" request={request} onSelectFailed={vi.fn()} />);
  await screen.findByRole("alert");
  fireEvent.click(screen.getByRole("button", { name: "Reintentar consulta" }));
  await screen.findByText(/Envío completado/);
  expect(request.mock.calls.map(([path]) => path)).toEqual(["/batch/delivery-operations/operation-2", "/batch/delivery-operations/operation-2"]);
  expect(request.mock.calls.every(([, options]) => !options.method)).toBe(true);
});
