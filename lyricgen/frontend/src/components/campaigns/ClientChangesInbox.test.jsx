import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import ClientChangesInbox from "./ClientChangesInbox";

afterEach(cleanup);

const hoursAgo = (hours) => new Date(Date.now() - hours * 3600 * 1000).toISOString();
const item = (id, patch = {}) => ({
  id, delivery_id: id * 10, portal_id: "chile", job_id: `job${id}`, song_id: `song${id}`, artist: "Artista", song: `Canción ${id}`,
  comment: `Pedido ${id}`, submitted_at: hoursAgo(1), updated_at: hoursAgo(1), resolved_at: null, resolution_note: null,
  resolution_source: null, published_revision: 2, client_approval: "pending", step: { key: "correct", tone: "idle", label: "Sin atender" }, ...patch,
});
const page = (items, extra = {}) => ({ campaign_id: "c1", available: true, counts: { open: items.length, resolved: 1, oldest_open_at: null }, items, next_cursor: null, ...extra });

describe("client changes inbox", () => {
  it("shows each request with portal, step and client verdict, and links to Admin only for admins", async () => {
    const request = vi.fn().mockResolvedValue(page([item(1), item(2, { portal_id: "argentina", step: { key: "publish", tone: "attention", label: "Corregido: falta publicar" }, client_approval: "approved" })]));
    const { rerender } = render(<ClientChangesInbox campaignId="c1" isAdmin request={request} />);
    expect(await screen.findByText("Pedido 1")).toBeInTheDocument();
    expect(screen.getByText("Chile")).toBeInTheDocument();
    expect(screen.getByText("Argentina")).toBeInTheDocument();
    expect(screen.getByText("Corregido: falta publicar")).toBeInTheDocument();
    expect(screen.getByText("Cliente aprobó · v2")).toBeInTheDocument();
    expect(screen.getAllByRole("link", { name: "Resolver en Admin →" })[0]).toHaveAttribute("href", "/admin?section=cambios&change_request_id=1");
    expect(request.mock.calls[0][0]).toBe("/batch/campaigns/c1/change-requests?status=open&limit=50");
    rerender(<ClientChangesInbox campaignId="c1" isAdmin={false} request={request} />);
    expect(screen.queryByRole("link", { name: "Resolver en Admin →" })).toBeNull();
    expect(screen.getAllByText("Lo resuelve un admin")).toHaveLength(2);
  });

  it("opens the song of a request", async () => {
    const onOpenSong = vi.fn();
    render(<ClientChangesInbox campaignId="c1" request={vi.fn().mockResolvedValue(page([item(3)]))} onOpenSong={onOpenSong} />);
    fireEvent.click(await screen.findByRole("button", { name: "Ver canción" }));
    expect(onOpenSong).toHaveBeenCalledWith("song3");
  });

  it("colors the wait by business hours against the 48 h goal", async () => {
    const request = vi.fn().mockResolvedValue(page([item(1, { submitted_at: hoursAgo(0.2) })]));
    render(<ClientChangesInbox campaignId="c1" request={request} />);
    const chip = await screen.findByTitle(/horas hábiles sin resolver \(objetivo 48\)/);
    expect(chip.className).toContain("text-ink-secondary");
  });

  it("switches the filter and refetches with it", async () => {
    const request = vi.fn().mockResolvedValue(page([item(1)]));
    render(<ClientChangesInbox campaignId="c1" request={request} />);
    await screen.findByText("Pedido 1");
    fireEvent.click(screen.getByRole("radio", { name: /Resueltos/ }));
    await waitFor(() => expect(request.mock.calls.at(-1)[0]).toContain("status=resolved"));
  });

  it("loads the next page with the cursor and appends it", async () => {
    const request = vi.fn()
      .mockResolvedValueOnce(page([item(1)], { next_cursor: "2026-09-30T10:00:00+00:00|1" }))
      .mockResolvedValueOnce(page([item(2)]));
    render(<ClientChangesInbox campaignId="c1" request={request} />);
    await screen.findByText("Pedido 1");
    fireEvent.click(screen.getByRole("button", { name: "Ver más" }));
    expect(await screen.findByText("Pedido 2")).toBeInTheDocument();
    expect(screen.getByText("Pedido 1")).toBeInTheDocument();
    expect(request.mock.calls[1][0]).toContain("cursor=2026-09-30T10%3A00%3A00%2B00%3A00%7C1");
  });

  it("never reads an unreachable portal as an empty inbox", async () => {
    const request = vi.fn().mockResolvedValue(page([], { available: false, counts: { open: null, resolved: null, oldest_open_at: null } }));
    render(<ClientChangesInbox campaignId="c1" request={request} />);
    expect(await screen.findByText(/No pudimos consultar el portal del cliente/)).toBeInTheDocument();
    expect(screen.queryByText("No hay pedidos del cliente")).toBeNull();
  });

  it("explains an empty inbox and reports request errors with a retry", async () => {
    const request = vi.fn().mockRejectedValueOnce(new Error("Error 500")).mockResolvedValue(page([]));
    render(<ClientChangesInbox campaignId="c1" request={request} />);
    expect(await screen.findByText("Error 500")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Reintentar" }));
    expect(await screen.findByText("No hay pedidos del cliente")).toBeInTheDocument();
  });
});
