import { act, cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { MemoryRouter, Route, Routes, useLocation, useNavigate } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";
import CampaignsPage from "./CampaignsPage";
import ReviewQueuePage from "./ReviewQueuePage";

const campaign = { id: "campaign-1", name: "Campaña de prueba", status: "active", registered_count: 3, counters: {}, default_render_params: {} };
const ready = { item_id: "i1", job_id: "j1", title: "Lista para revisar", state: "ready" };
const approved = { item_id: "i2", job_id: "j2", title: "Letra ya aprobada", state: "approved" };
const failed = { item_id: "i3", job_id: "j3", title: "Falló procesamiento", state: "failed" };
const response = (body) => new Response(JSON.stringify(body), { status: 200, headers: { "Content-Type": "application/json" } });
const deferred = () => { let resolve; const promise = new Promise((done) => { resolve = done; }); return { promise, resolve }; };

function payload(scope) {
  const items = scope === "approved" ? [approved] : scope === "all" ? [ready, approved, failed] : [ready, failed];
  return { items, pages: 1, page: 1, total: items.length, scope: { key: scope, label: scope, total: items.length },
    campaign_totals: { songs: 3, approved: 1, approved_today: 0 }, counters: { ready: scope === "approved" ? 0 : 1 } };
}
function setupFetch(override) {
  const mock = vi.fn(async (input, options) => {
    const url = new URL(String(input), "http://test");
    if (override) { const result = override(url, options); if (result) return result; }
    if (url.pathname === "/batch/campaigns") return response({ items: [campaign] });
    if (url.pathname.endsWith("/review-queue")) return response(payload(url.searchParams.get("scope")));
    if (url.pathname.endsWith("/items")) return response({ items: [], pages: 1 });
    return response(campaign);
  });
  vi.stubGlobal("fetch", mock); return mock;
}
function Controls() {
  const navigate = useNavigate(); const location = useLocation();
  return <><button onClick={() => navigate(-1)}>Volver navegador</button><button onClick={() => navigate(1)}>Adelante navegador</button><div data-testid="location">{location.pathname}{location.search}</div></>;
}
function mount(path) {
  return render(<MemoryRouter initialEntries={[path]}><Controls /><Routes>
    <Route path="/admin/cola" element={<ReviewQueuePage />} />
    <Route path="/campaigns/:campaignId" element={<CampaignsPage />} />
    <Route path="/campaigns" element={<CampaignsPage />} />
    <Route path="/videos/:jobId" element={<p>Detalle aprobado</p>} />
    <Route path="/review/:jobId" element={<p>Editor</p>} />
  </Routes></MemoryRouter>);
}
afterEach(() => { cleanup(); vi.unstubAllGlobals(); sessionStorage.clear(); localStorage.clear(); });

it("finds saved drafts and preserves the drafts tab when resuming", async () => {
  setupFetch((url) => {
    if (!url.pathname.endsWith("/review-queue")) return null;
    const scope = url.searchParams.get("scope");
    return response({ ...payload(scope), campaign,
      campaign_totals: { songs: 3, approved: 1, drafts: 1 },
      items: scope === "drafts" ? [{ ...ready, is_draft: true, last_reviewed_name: "Agus", last_reviewed_at: "2026-09-10T02:00:00Z" }] : [ready, failed] });
  });
  mount("/campaigns/campaign-1");
  await screen.findByText(ready.title);
  fireEvent.click(screen.getByRole("button", { name: "Con cambios humanos guardados 1" }));
  await screen.findByText("Con cambios humanos guardados");
  expect(screen.getByText(/Guardado por Agus/)).toBeInTheDocument();
  expect(screen.getByRole("combobox", { name: "Orden" })).toBeDisabled();
  expect(screen.queryByText(failed.title)).not.toBeInTheDocument();
  fireEvent.click(screen.getByRole("button", { name: "Continuar revisión" }));
  await screen.findByText("Editor");
  expect(decodeURIComponent(screen.getByTestId("location").textContent)).toContain("tab=drafts");
  fireEvent.click(screen.getByRole("button", { name: "Volver navegador" }));
  await screen.findByText("Con cambios humanos guardados");
  expect(screen.getByRole("button", { name: "Con cambios humanos guardados 1" })).toHaveAttribute("aria-pressed", "true");
});

it("shows the 300-song total without adding the nested saved-changes filter", async () => {
  setupFetch((url) => url.pathname.endsWith("/review-queue") ? response({
    ...payload("pending"), campaign,
    campaign_totals: { songs: 300, approved: 79, discarded: 13, drafts: 12 },
  }) : null);
  mount("/campaigns/campaign-1");
  await screen.findByText(ready.title);
  expect(screen.getByText("300 canciones en total = 208 por revisar + 79 aprobadas + 13 descartadas.")).toBeInTheDocument();
  expect(screen.getAllByRole("tab", { name: /Por revisar|Aprobadas|Todas|Descartadas/ })).toHaveLength(4);
  expect(screen.queryByRole("tab", { name: /Borradores|cambios humanos/ })).not.toBeInTheDocument();
  expect(screen.getByRole("button", { name: "Con cambios humanos guardados 12" })).toHaveAttribute("aria-pressed", "false");
});

it("loads the campaign once and discards/restores with a visible reason", async () => {
  let discarded = false;
  const mock = setupFetch((url, options) => {
    if (url.pathname.endsWith("/discard")) {
      expect(JSON.parse(options.body).reason).toBe("Instrumental · solicitud del cliente");
      discarded = true; return response({ ok: true });
    }
    if (url.pathname.endsWith("/restore")) { discarded = false; return response({ ok: true }); }
    if (url.pathname.endsWith("/review-queue")) {
      const scope = url.searchParams.get("scope");
      return response({ campaign, pages: 1, counters: {}, scope: { total: 1 },
        campaign_totals: { songs: 1, approved: 0, discarded: discarded ? 1 : 0 },
        items: discarded ? scope === "discarded" ? [{ ...ready, state: "discarded", discard: { reason: "Instrumental · solicitud del cliente" } }] : []
          : scope === "discarded" ? [] : [{ ...ready, can_discard: true }],
      });
    }
    return null;
  });
  mount("/campaigns/campaign-1");
  await screen.findByText(ready.title);
  expect(mock).toHaveBeenCalledTimes(1);
  expect(mock.mock.calls[0][0]).toContain("limit=1000");
  fireEvent.click(screen.getByRole("button", { name: "Descartar", exact: true }));
  fireEvent.click(screen.getByRole("button", { name: "Confirmar descarte" }));
  await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());
  fireEvent.click(screen.getByRole("tab", { name: /Descartadas/ }));
  await screen.findByText("Instrumental · solicitud del cliente");
  fireEvent.click(screen.getByRole("button", { name: "Recuperar", exact: true }));
  fireEvent.click(screen.getByRole("button", { name: "Confirmar recuperación" }));
  await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());
  fireEvent.click(screen.getByRole("tab", { name: /Por revisar/ }));
  await screen.findByText(ready.title);
  expect(screen.getByRole("button", { name: "Revisar", exact: true })).toBeEnabled();
});

describe.each(["/admin/cola", "/campaigns/campaign-1"])("review navigation %s", (path) => {
  it("makes cards/tabs work and restores scope with browser back/forward", async () => {
    setupFetch(); mount(path);
    await screen.findByText(ready.title);
    fireEvent.click(screen.getByRole("tab", { name: /Aprobadas/ }));
    await screen.findByText(approved.title);
    expect(screen.queryByText(ready.title)).not.toBeInTheDocument();
    expect(screen.getByRole("tab", { name: /Aprobadas/ })).toHaveAttribute("aria-selected", "true");
    expect(screen.getByRole("tab", { name: /Por revisar/ })).toHaveTextContent("2");
    fireEvent.click(screen.getByRole("button", { name: "Volver navegador" }));
    await screen.findByText(ready.title);
    expect(screen.getByRole("tab", { name: /Por revisar/ })).toHaveAttribute("aria-selected", "true");
    fireEvent.click(screen.getByRole("button", { name: "Adelante navegador" }));
    await screen.findByText(approved.title);
    fireEvent.keyDown(screen.getByRole("tab", { name: /Aprobadas/ }), { key: "ArrowRight" });
    await screen.findByText(ready.title);
    expect(screen.getByRole("tab", { name: /Todas/ })).toHaveAttribute("aria-selected", "true");
    expect(screen.getByText("Fallida")).toBeInTheDocument();
  });
  it("rejects late pending responses after switching to approved", async () => {
    const old = deferred();
    let pendingCalls = 0;
    setupFetch((url) => {
      if (!url.pathname.endsWith("/review-queue") || url.searchParams.get("scope") !== "pending") return null;
      pendingCalls += 1;
      return pendingCalls > 1 ? old.promise : null;
    });
    mount(path);
    await screen.findByText(ready.title);
    await act(async () => {});
    fireEvent(window, new Event("pageshow"));
    await waitFor(() => expect(pendingCalls).toBe(2));
    fireEvent.click(screen.getByRole("tab", { name: /Aprobadas/ }));
    await screen.findByText(approved.title);
    await act(async () => old.resolve(response(payload("pending"))));
    expect(screen.queryByText(ready.title)).not.toBeInTheDocument();
    expect(screen.getByText(approved.title)).toBeInTheDocument();
  });
  it("opens the approved transcript with the selected filter in return_to", async () => {
    setupFetch(); mount(path + (path.startsWith("/admin") ? "?scope=approved" : "?tab=approved"));
    await screen.findByText(approved.title);
    fireEvent.click(screen.getByRole("button", { name: "Editar transcripción" }));
    expect(await screen.findByText("Editor")).toBeInTheDocument();
    expect(screen.getByTestId("location")).toHaveTextContent("/review/j2?return_to=");
    expect(decodeURIComponent(screen.getByTestId("location").textContent)).toContain("approved");
  });
});

it("keeps the final-review approved video accessible in its own stage", async () => {
  setupFetch(); mount("/campaigns/campaign-1?tab=approved&stage=final");
  await screen.findByText(approved.title);
  fireEvent.click(screen.getByRole("button", { name: "Ver video" }));
  expect(await screen.findByText("Detalle aprobado")).toBeInTheDocument();
  expect(screen.getByTestId("location")).toHaveTextContent("/videos/j2?return_to=");
  expect(decodeURIComponent(screen.getByTestId("location").textContent)).toContain("stage=final");
});

it("clears the advanced status when changing campaign tabs, and preserves other filters", async () => {
  const mock = setupFetch(); mount("/campaigns/campaign-1?tab=pending&state=ready&version=studio&artist=Divididos");
  await screen.findByText(ready.title);
  fireEvent.click(screen.getByRole("tab", { name: /Aprobadas/ }));
  await screen.findByText(approved.title);
  const url = new URL(mock.mock.calls.filter(([url]) => String(url).includes("review-queue")).at(-1)[0], "http://test");
  expect(url.searchParams.get("scope")).toBe("approved");
  expect(url.searchParams.has("state")).toBe(false);
  expect(url.searchParams.get("version")).toBe("studio");
  expect(url.searchParams.get("artist")).toBe("Divididos");
});

it("shows the first page without waiting for the rest, and disables partial exports", async () => {
  const rest = deferred();
  setupFetch((url) => {
    if (!url.pathname.endsWith("/review-queue")) return null;
    if (url.searchParams.get("page") === "2") return rest.promise;
    return response({ ...payload("pending"), items: [ready], pages: 2 });
  });
  mount("/admin/cola");
  await screen.findByText(ready.title);
  expect(screen.getByRole("status")).toHaveTextContent("Cargando el resto");
  expect(screen.getByRole("button", { name: "Exportar minutos" })).toBeDisabled();
  await act(async () => rest.resolve(response({ items: [failed] })));
  await screen.findByText(failed.title);
  expect(screen.getByRole("button", { name: "Exportar minutos" })).toBeEnabled();
});

it("does not show a false empty campaign list while loading", async () => {
  const request = deferred(); setupFetch((url) => url.pathname === "/batch/campaigns" ? request.promise : null); mount("/campaigns");
  expect(screen.getByRole("status")).toHaveTextContent("Cargando campañas");
  expect(screen.queryByText("Todavía no hay campañas.")).not.toBeInTheDocument();
  await act(async () => request.resolve(response({ items: [campaign] })));
  fireEvent.click(await screen.findByRole("button", { name: /Campaña de prueba/ }));
  expect(screen.getByTestId("location")).toHaveTextContent("/campaigns/campaign-1");
});

it("keeps next inside the visible matching queue instead of claiming a different song", async () => {
  const mock = setupFetch(url => url.pathname.endsWith("/review-queue") ? response({ ...payload("pending"), campaign, items: [{ ...ready, title: "Resultado encontrado" }] }) : null);
  mount("/campaigns/campaign-1?q=garcia&tab=drafts");
  await screen.findByText("Resultado encontrado");
  fireEvent.click(screen.getByRole("button", { name: "Revisar siguiente canción" }));
  await screen.findByText("Editor");
  expect(screen.getByTestId("location")).toHaveTextContent("/review/j1?");
  expect(decodeURIComponent(screen.getByTestId("location").textContent)).toContain("q=garcia");
  expect(mock.mock.calls.some(([url]) => String(url).includes("/next"))).toBe(false);
});

it("paginates 300 songs without fetching again and preserves the page on return", async () => {
  const mock = setupFetch(url => url.pathname.endsWith("/review-queue") ? response({ ...payload("pending"), campaign, items: Array.from({ length: 300 }, (_, i) => ({ ...ready, item_id: `i${i}`, job_id: `j${i}`, title: `Canción ${i}` })) }) : null);
  mount("/campaigns/campaign-1");
  await screen.findByText("Canción 24");
  expect(screen.queryByText("Canción 25")).not.toBeInTheDocument();
  const requests = mock.mock.calls.length;
  fireEvent.click(within(screen.getByRole("navigation", { name: "Páginas de canciones" })).getByRole("button", { name: "Siguiente" }));
  await screen.findByText("Canción 25");
  expect(mock).toHaveBeenCalledTimes(requests);
  fireEvent.click(screen.getAllByRole("button", { name: "Revisar", exact: true })[0]);
  await screen.findByText("Editor");
  expect(decodeURIComponent(screen.getByTestId("location").textContent)).toContain("rpage=2");
});

it("selects the requested campaign and searches the server without reloading the campaign list", async () => {
  const mock = setupFetch(url => url.pathname === "/batch/campaigns" ? response({ items: [campaign, { ...campaign, id: "second", name: "Segunda campaña" }] }) : null);
  mount("/admin/cola?campaign=second");
  await screen.findByText(ready.title);
  expect(screen.getByRole("combobox", { name: "Campaña de la cola" })).toHaveValue("second");
  fireEvent.change(screen.getByRole("searchbox", { name: "Buscar canción o artista" }), { target: { value: "corazon garcia" } });
  await waitFor(() => expect(mock.mock.calls.some(([url]) => String(url).includes("/second/review-queue") && String(url).includes("corazon%20garcia"))).toBe(true));
  expect(mock.mock.calls.filter(([url]) => String(url) === "/batch/campaigns")).toHaveLength(1);
});

it("requests the art-track delivery preview using the backend POST contract", async () => {
  const mock = setupFetch(url => {
    if (url.pathname.endsWith("/review-queue")) return response({ ...payload("pending"), campaign: { ...campaign, kind: "art_track" } });
    if (url.pathname.endsWith("/delivery-preview")) return response({ eligible_count: 2, hostname: "umgchile.genly.pro" });
    return null;
  });
  mount("/campaigns/campaign-1");
  fireEvent.click(await screen.findByRole("button", { name: "Previsualizar envíos" }));
  await screen.findByText("2 art tracks aprobados para umgchile.genly.pro.");
  expect(mock.mock.calls.find(([url]) => String(url).endsWith("/delivery-preview"))[1].method).toBe("POST");
});
