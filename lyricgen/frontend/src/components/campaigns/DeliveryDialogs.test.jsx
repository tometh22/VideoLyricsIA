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

describe("send to portal: client requests newer than the approved lyrics", () => {
  const blockedSong = { job_id: "job1", title: "Barricada", artist: "2 minutos", version: { source: "editor_version", revision: 17 },
    requests: [{ id: 141, submitted_at: "2026-10-02T17:57:14Z", comment: "0:19 Barricada policial, hay que enfrentar" }] };

  function mountGuard({ blocked = [blockedSong], sendResponse } = {}) {
    const calls = [];
    vi.stubGlobal("fetch", vi.fn(async (input, options = {}) => {
      const url = String(input);
      calls.push({ url, method: options.method || "GET", body: options.body ? JSON.parse(options.body) : null });
      if (url.includes("/change-request-guard")) return json({ enabled: true, available: true, blocked });
      if (url.includes("/change-requests")) return json({ items: [] });
      return sendResponse ? sendResponse() : json({ operation_id: "op-1", total_count: 1, scheduled: true }, 202);
    }));
    const onStarted = vi.fn();
    render(<SendToPortalDialog campaignId="c1" videos={videos} lockedPortal="chile" onClose={vi.fn()} onStarted={onStarted} />);
    return { calls, onStarted };
  }

  it("asks about the selected jobs and shows the blocked song with its open request", async () => {
    const { calls } = mountGuard();
    expect(await screen.findByText(/Pedido #141/)).toBeInTheDocument();
    expect(screen.getByText(/más nuevos que la letra aprobada/)).toBeInTheDocument();
    expect(screen.getByText(/Se envía la otra canción/)).toBeInTheDocument();
    expect(calls.find((call) => call.url.includes("/change-request-guard")).url).toContain("job_ids=job1%2Cjob2");
  });

  it("a plain send leaves the blocked song to the backend and carries no override", async () => {
    const { calls, onStarted } = mountGuard();
    await screen.findByText(/Pedido #141/);
    fireEvent.click(screen.getByRole("button", { name: "Confirmar envío" }));
    await waitFor(() => expect(onStarted).toHaveBeenCalled());
    expect(sendBody(calls).publish_anyway).toBeUndefined();
  });

  it("«Publicar igual» needs a reason and sends it with the blocked jobs", async () => {
    const { calls, onStarted } = mountGuard();
    fireEvent.click(await screen.findByLabelText("Publicar igual"));
    expect(screen.getByRole("button", { name: "Confirmar envío" })).toBeDisabled();
    fireEvent.change(screen.getByLabelText("Motivo para publicar igual"), { target: { value: "  UMG pidió reenviar.  " } });
    fireEvent.click(screen.getByRole("button", { name: "Confirmar envío" }));
    await waitFor(() => expect(onStarted).toHaveBeenCalled());
    expect(sendBody(calls)).toMatchObject({ publish_anyway: true, override_reason: "UMG pidió reenviar.", override_job_ids: ["job1"] });
  });

  it("cannot send when every selected song is blocked and nobody overrides", async () => {
    mountGuard({ blocked: [blockedSong, { ...blockedSong, job_id: "job2", title: "Otra" }] });
    expect(await screen.findByText(/No queda ninguna canción para enviar/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Confirmar envío" })).toBeDisabled();
  });

  it("a 409 for a request that arrived meanwhile shows the songs and keeps the dialog open", async () => {
    const { onStarted } = mountGuard({
      blocked: [],
      sendResponse: () => json({ detail: { code: "change_request_newer_than_version", blocked: [blockedSong] } }, 409),
    });
    await screen.findByText(/Enviar 2 videos aprobados/);
    fireEvent.click(screen.getByRole("button", { name: "Confirmar envío" }));
    expect(await screen.findByText(/Pedido #141/)).toBeInTheDocument();
    expect(screen.getByText(/marcá «Publicar igual»/)).toBeInTheDocument();
    expect(onStarted).not.toHaveBeenCalled();
  });
});

describe("send to portal: summary before publishing", () => {
  const summary = (jobId, patch = {}) => ({
    job_id: jobId, title: jobId === "job1" ? "Canción 1" : "Canción 2", artist: "Artista",
    listening_status: "measured", lines_total: 20, lines_unheard: 2,
    unheard: [{ line_index: 4, start: 61, end: 64, text: "Una línea que nadie escuchó" }, { line_index: 9, start: 95, end: 98, text: "Otra más" }],
    warnings_count: 1, warnings_by_category: { official: 1, two_ears: 0, style: 0 },
    warnings: [{ id: "w1", category: "official", title: "Se escucha distinto", count: 1, occurrences: [{ start: 30, before: "dice mal", after: "dice bien" }] }],
    needs_attention: true, token: `${jobId}-token`.padEnd(64, "0"), ...patch,
  });

  function mountReview({ preview, sendResponses }) {
    const calls = [];
    const queue = [...sendResponses];
    vi.stubGlobal("fetch", vi.fn(async (input, options = {}) => {
      const url = String(input);
      calls.push({ url, method: options.method || "GET", body: options.body ? JSON.parse(options.body) : null });
      if (url.includes("/publish-review")) return json(preview);
      if (url.includes("/change-request-guard")) return json({ enabled: false, blocked: [] });
      if (url.includes("/change-requests")) return json({ items: [] });
      return queue.shift()();
    }));
    const onStarted = vi.fn();
    render(<SendToPortalDialog campaignId="c1" videos={videos} lockedPortal="chile" onClose={vi.fn()} onStarted={onStarted} />);
    return { calls, onStarted };
  }

  it("shows one summary with the total per song and sends the tokens it showed", async () => {
    const { calls, onStarted } = mountReview({
      preview: { mode: "prompt", available: true, summaries: [summary("job1")], totals: {} },
      sendResponses: [() => json({ operation_id: "op-1", total_count: 2, scheduled: true }, 202)],
    });
    expect(await screen.findByText("2 líneas sin escuchar y 1 aviso sin mirar.")).toBeInTheDocument();
    expect(screen.getByText("2 de 20 líneas sin escuchar · 1 aviso sin mirar")).toBeInTheDocument();
    expect(screen.getByText("Una línea que nadie escuchó")).toBeInTheDocument();
    expect(calls.find((call) => call.url.includes("/publish-review")).url).toContain("job_ids=job1%2Cjob2");
    fireEvent.click(screen.getByRole("button", { name: "Publicar igual" }));
    await waitFor(() => expect(onStarted).toHaveBeenCalled());
    expect(sendBody(calls).publish_review_tokens).toEqual({ job1: summary("job1").token });
  });

  it("observe mode shows nothing and sends no tokens", async () => {
    const { calls, onStarted } = mountReview({
      preview: { mode: "observe", available: true, summaries: [], totals: {} },
      sendResponses: [() => json({ operation_id: "op-1", total_count: 2, scheduled: true }, 202)],
    });
    fireEvent.click(await screen.findByRole("button", { name: "Confirmar envío" }));
    await waitFor(() => expect(onStarted).toHaveBeenCalled());
    expect(screen.queryByLabelText("Resumen antes de publicar")).toBeNull();
    expect(sendBody(calls).publish_review_tokens).toBeUndefined();
  });

  it("a changed summary comes back from the send and needs one more confirmation", async () => {
    const fresh = summary("job2", { lines_unheard: 5, token: "fresh".padEnd(64, "1") });
    const { calls, onStarted } = mountReview({
      preview: { mode: "prompt", available: true, summaries: [], totals: {} },
      sendResponses: [
        () => json({ detail: { code: "publish_review_required", summaries: [fresh], totals: { songs: 1, songs_needing_attention: 1, lines_unheard: 5, lines_total: 20, warnings: 1 } } }, 409),
        () => json({ operation_id: "op-2", total_count: 2, scheduled: true }, 202),
      ],
    });
    fireEvent.click(await screen.findByRole("button", { name: "Confirmar envío" }));
    expect(await screen.findByText(/Antes de enviar, mirá el resumen/)).toBeInTheDocument();
    expect(onStarted).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole("button", { name: "Publicar igual" }));
    await waitFor(() => expect(onStarted).toHaveBeenCalled());
    const posts = calls.filter((call) => call.method === "POST");
    expect(posts[1].body.publish_review_tokens).toEqual({ job2: fresh.token });
    expect(posts[1].body.idempotency_key).toBe(posts[0].body.idempotency_key);
  });
});
