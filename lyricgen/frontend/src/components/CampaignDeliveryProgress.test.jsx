import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";
import CampaignDeliveryProgress from "./CampaignDeliveryProgress";

afterEach(cleanup);

it('refreshes campaign history once when a delivery finishes', async () => {
  const onSettled = vi.fn();
  const request = vi.fn().mockResolvedValue({ status: 'completed', total_count: 1, sent_count: 1, items: [] });
  const { rerender } = render(<CampaignDeliveryProgress operationId="op-3" request={request} onSettled={onSettled} />);
  await screen.findByText(/Envío completado/);
  expect(onSettled).toHaveBeenCalledOnce();
  rerender(<CampaignDeliveryProgress operationId="op-3" request={request} onSettled={onSettled} />);
  expect(onSettled).toHaveBeenCalledOnce();
});

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

it("names the songs of this delivery so a past operation is not read as the whole campaign", async () => {
  const request = vi.fn().mockResolvedValue({ status: "completed", sent_count: 1, total_count: 1, items: [{ job_id: "j9", status: "sent" }] });
  render(<CampaignDeliveryProgress operationId="op-9" request={request} describe={(jobId) => (jobId === "j9" ? "Influencia · Charly García" : "")} />);
  await screen.findByText(/Envío completado/);
  expect(screen.getByText("Canciones de este envío (1)")).toBeInTheDocument();
  expect(screen.getByText("Influencia · Charly García")).toBeInTheDocument();
  expect(screen.getByText(/no de toda la campaña/)).toBeInTheDocument();
});

it("retries the failed songs of the operation and keeps polling after it is re-queued", async () => {
  const partial = { status: "partial", sent_count: 1, total_count: 2, items: [
    { job_id: "ok", status: "sent" }, { job_id: "bad", status: "failed", error_code: "deliverables_not_ready", retryable: true },
  ] };
  const request = vi.fn(async (path, options = {}) => {
    if (options.method === "POST") return { operation_id: "op-r", status: "queued", scheduled: true, outcome: "queued" };
    return request.mock.calls.filter(([, o]) => o?.method === "POST").length ? { status: "completed", sent_count: 2, total_count: 2, items: [] } : partial;
  });
  render(<CampaignDeliveryProgress operationId="op-r" request={request} onSelectFailed={vi.fn()} />);
  fireEvent.click(await screen.findByRole("button", { name: "Reintentar 1 fallido" }));
  await screen.findByText(/Envío completado/);
  const post = request.mock.calls.find(([, options]) => options?.method === "POST");
  expect(post[0]).toBe("/batch/delivery-operations/op-r/retry");
  expect(screen.queryByRole("button", { name: /Reintentar \d/ })).toBeNull();
});

it("does not offer a retry for failures that need a person and explains them", async () => {
  const request = vi.fn().mockResolvedValue({ status: "partial", sent_count: 0, total_count: 1, items: [
    { job_id: "amb", status: "failed", error_code: "ambiguous_replacement", error_detail: "varios pedidos", retryable: false },
  ] });
  render(<CampaignDeliveryProgress operationId="op-a" request={request} onSelectFailed={vi.fn()} />);
  expect(await screen.findByText(/varios pedidos del cliente vinculados/)).toBeInTheDocument();
  expect(screen.queryByRole("button", { name: /Reintentar/ })).toBeNull();
});

it("flags a stalled operation and offers a retry even without failed songs", async () => {
  const request = vi.fn().mockResolvedValue({ status: "sending", stalled: true, sent_count: 0, total_count: 3, items: [
    { job_id: "p1", status: "pending" },
  ] });
  render(<CampaignDeliveryProgress operationId="op-s" request={request} onSelectFailed={vi.fn()} />);
  expect(await screen.findByText(/parece detenido/)).toBeInTheDocument();
  expect(screen.getByRole("button", { name: "Reintentar envío" })).toBeInTheDocument();
});

it("does not offer a retry while a send is still running normally", async () => {
  const request = vi.fn().mockResolvedValue({ status: "sending", stalled: false, sent_count: 1, total_count: 3, items: [
    { job_id: "a", status: "sent" }, { job_id: "b", status: "failed", error_code: "deliverables_not_ready", retryable: true }, { job_id: "c", status: "pending" },
  ] });
  render(<CampaignDeliveryProgress operationId="op-run" request={request} onSelectFailed={vi.fn()} />);
  await screen.findByText(/En curso/);
  expect(screen.queryByRole("button", { name: /Reintentar/ })).toBeNull();
});

it("explains a refused retry in plain words instead of showing the raw code", async () => {
  const request = vi.fn(async (path, options = {}) => {
    if (options.method === "POST") throw Object.assign(new Error("operation_in_progress"), { code: "operation_in_progress" });
    return { status: "partial", sent_count: 0, total_count: 1, items: [{ job_id: "b", status: "failed", error_code: "deliverables_not_ready", retryable: true }] };
  });
  render(<CampaignDeliveryProgress operationId="op-refused" request={request} onSelectFailed={vi.fn()} />);
  fireEvent.click(await screen.findByRole("button", { name: "Reintentar 1 fallido" }));
  expect(await screen.findByText(/todavía se está procesando/)).toBeInTheDocument();
  expect(screen.queryByText("operation_in_progress")).toBeNull();
});
