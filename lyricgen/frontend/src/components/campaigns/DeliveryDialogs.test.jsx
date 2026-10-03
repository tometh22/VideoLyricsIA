import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { SendToPortalDialog } from "./DeliveryDialogs";
import { json } from "./campaignTestApi";

afterEach(() => { cleanup(); vi.unstubAllGlobals(); });

const videos = [{ item_id: "song1", job_id: "job1", title: "Canción 1", artist: "Artista" }, { item_id: "song2", job_id: "job2", title: "Canción 2", artist: "Artista" }];
const request = (id, patch = {}) => ({ id, song_id: "song1", song: "Canción 1", portal_id: "chile", comment: `Pedido ${id}`, closable_on_publish: true, ...patch });

function mount({ items, sendResponse, canClose = true }) {
  const calls = [];
  vi.stubGlobal("fetch", vi.fn(async (input, options = {}) => {
    const url = String(input);
    calls.push({ url, method: options.method || "GET", body: options.body ? JSON.parse(options.body) : null });
    if (url.includes("/change-requests")) return json({ items });
    return sendResponse ? sendResponse() : json({ operation_id: "op-1", total_count: 2, scheduled: true }, 202);
  }));
  const onStarted = vi.fn();
  render(<SendToPortalDialog campaignId="c1" videos={videos} lockedPortal="chile" canClose={canClose} onClose={vi.fn()} onStarted={onStarted} />);
  return { calls, onStarted };
}
const sendBody = (calls) => calls.find((call) => call.method === "POST").body;

describe("send to portal: closing client requests", () => {
  it("lists only closable requests of the selected songs in this portal, none ticked", async () => {
    mount({ items: [request(1), request(2, { closable_on_publish: false }), request(3, { song_id: "other" }), request(4, { portal_id: "argentina" })] });
    expect(await screen.findByLabelText("Resolver el pedido de Canción 1")).not.toBeChecked();
    expect(screen.getAllByRole("checkbox")).toHaveLength(1);
    expect(screen.queryByLabelText("Qué cambió")).toBeNull();
  });

  it("a plain send carries no resolve_requests", async () => {
    const { calls } = mount({ items: [request(1)] });
    await screen.findByLabelText("Resolver el pedido de Canción 1");
    fireEvent.click(screen.getByRole("button", { name: "Confirmar envío" }));
    await waitFor(() => expect(calls.some((call) => call.method === "POST")).toBe(true));
    expect(sendBody(calls).resolve_requests).toBeUndefined();
    expect(sendBody(calls).resolution_note).toBeUndefined();
  });

  it("sends only the ticked requests, keyed by the job that will be published, with the note", async () => {
    const { calls, onStarted } = mount({ items: [request(1), request(2, { song_id: "song2", song: "Canción 2" })] });
    fireEvent.click(await screen.findByLabelText("Resolver el pedido de Canción 1"));
    fireEvent.change(await screen.findByLabelText("Qué cambió"), { target: { value: "  Corregimos la palabra.  " } });
    fireEvent.click(screen.getByRole("button", { name: "Confirmar envío" }));
    await waitFor(() => expect(onStarted).toHaveBeenCalled());
    expect(sendBody(calls)).toMatchObject({ resolve_requests: { job1: [1] }, resolution_note: "Corregimos la palabra." });
  });

  it("ticks every closable request in one click, still explicitly", async () => {
    const { calls, onStarted } = mount({ items: [request(1), request(2, { song_id: "song2", song: "Canción 2" })] });
    fireEvent.click(await screen.findByRole("button", { name: "Marcar todos (2)" }));
    expect(screen.getByLabelText("Resolver el pedido de Canción 2")).toBeChecked();
    expect(screen.queryByRole("button", { name: /Marcar todos/ })).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "Confirmar envío" }));
    await waitFor(() => expect(onStarted).toHaveBeenCalled());
    expect(sendBody(calls)).toMatchObject({ resolve_requests: { job1: [1], job2: [2] } });
  });

  it("offers nothing when closing is not enabled or the list cannot load", async () => {
    const off = mount({ items: [request(1)], canClose: false });
    await screen.findByText(/Enviar 2 videos aprobados/);
    expect(off.calls.some((call) => call.url.includes("/change-requests"))).toBe(false);
    expect(screen.queryByRole("checkbox")).toBeNull();
  });

  it("explains a rejected tick in plain words and keeps the dialog open", async () => {
    const { onStarted } = mount({ items: [request(1)], sendResponse: () => json({ detail: { code: "change_request_not_closable" } }, 409) });
    fireEvent.click(await screen.findByLabelText("Resolver el pedido de Canción 1"));
    fireEvent.click(screen.getByRole("button", { name: "Confirmar envío" }));
    expect(await screen.findByText(/ya no se puede cerrar con este envío/)).toBeInTheDocument();
    expect(onStarted).not.toHaveBeenCalled();
  });

  it("forgets the idempotency key once the send is confirmed, but keeps it after a failure", async () => {
    const keys = new Map();
    let fail = true;
    vi.stubGlobal("fetch", vi.fn(async () => (fail ? json({ detail: "boom" }, 500) : json({ operation_id: "op-1", total_count: 2, scheduled: true }, 202))));
    const onStarted = vi.fn();
    render(<SendToPortalDialog campaignId="c1" videos={videos} lockedPortal="chile" idempotencyKeys={keys} onClose={vi.fn()} onStarted={onStarted} />);
    fireEvent.click(screen.getByRole("button", { name: "Confirmar envío" }));
    await screen.findByText(/boom|Error 500/);
    expect(keys.size).toBe(1);             // a lost/failed response can be replayed with the same key
    fail = false;
    fireEvent.click(screen.getByRole("button", { name: "Confirmar envío" }));
    await waitFor(() => expect(onStarted).toHaveBeenCalled());
    expect(keys.size).toBe(0);             // a confirmed send is over: the next one is a new operation
  });
});
