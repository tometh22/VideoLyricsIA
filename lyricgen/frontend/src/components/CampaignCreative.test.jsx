import { beforeEach, afterEach, describe, it, expect, vi } from "vitest";
import { render, screen, fireEvent, waitFor, cleanup } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import CampaignCreative from "./CampaignCreative";
vi.mock("../i18n", () => ({ useI18n: () => ({ t: () => "" }) }));
vi.mock("./WizardLivePreview", () => ({ default: () => <div>Vista previa visual</div> }));
vi.mock("../mediaUrl", () => ({ useLazyMediaUrl: () => ({ ref: () => {}, url: "" }) }));
let head, report, calls;
const response = data => ({ ok: true, json: async () => data });
beforeEach(() => {
  calls = [];
  head = { plan: { revision: 0 }, can_manage: true, veo_model: "veo-3.1-fast-generate-001", fields: {
    font: { label: "Tipografía", group: "Letra", kind: "select", options: ["", "anton"] },
    effect: { label: "Efecto", group: "Movimiento y efectos", kind: "select", options: ["", "bokeh", "rain"] },
  }, operations: [], items: Array.from({ length: 39 }, (_, i) => ({ id: `i${i}`, title: `Tema ${i}`, artist: "Artista", status: "transcribed_pending", settings: {} })) };
  report = { campaign_id: "c1", name: "Chile", at: new Date().toISOString(), contract: {}, groups: [], videos: [], history: [] };
  vi.stubGlobal("fetch", vi.fn(async (url, options = {}) => {
    calls.push([url, options]);
    if (url.endsWith("/creative")) return response(head);
    if (url.endsWith("/report")) return response(report);
    if (url.endsWith("/backgrounds")) return response([]);
    if (url.endsWith("/preview")) return response({ preview_id: "p1", counts: [39], rounded: false, skipped: [], changes: head.items.map(i => ({ item_id: i.id, artist: i.artist, title: i.title, group: "Estilo 1", before: {}, after: { font: "anton" } })) });
    if (url.endsWith("/apply")) { head = { ...head, plan: { revision: 1 } }; return response({ revision: 1 }); }
    throw new Error(`Unexpected request: ${url}`);
  }));
});
afterEach(() => { cleanup(); vi.unstubAllGlobals(); });

const mount = view => render(<MemoryRouter><CampaignCreative campaignId="c1" view={view} /></MemoryRouter>);
describe("campaign bulk design", () => {
  it("selects all pages, previews a partial change and only saves on confirmation", async () => {
    mount("creative");
    fireEvent.click(await screen.findByRole("button", { name: /Seleccionar todas las coincidencias/ }));
    expect(screen.getByText("39 seleccionadas")).toBeInTheDocument();
    fireEvent.click(screen.getByText("Cambiar Tipografía"));
    fireEvent.change(screen.getByLabelText("Estilo 1: Tipografía"), { target: { value: "anton" } });
    fireEvent.change(screen.getByLabelText("Motivo del cambio"), { target: { value: "Tipografía acordada" } });
    fireEvent.click(screen.getByRole("button", { name: "Ver reparto antes de guardar" }));
    await screen.findByRole("button", { name: "Guardar esta asignación" });
    const body = JSON.parse(calls.find(([url]) => url.endsWith("/preview"))[1].body);
    expect(body.item_ids).toHaveLength(39);
    expect(body.groups[0].settings).toEqual({ font: "anton" });
    expect(calls.some(([url]) => url.endsWith("/generate") || url.endsWith("/apply"))).toBe(false);
    fireEvent.click(screen.getByRole("button", { name: "Guardar esta asignación" }));
    await screen.findByText("Asignación guardada. No se generaron videos.");
    expect(JSON.parse(calls.find(([url]) => url.endsWith("/apply"))[1].body)).toEqual({ preview_id: "p1" });
    expect(calls.some(([url]) => url.endsWith("/generate"))).toBe(false);
  });
  it("invalidates the preview after changing any assignment input", async () => {
    mount("creative");
    fireEvent.click(await screen.findByRole("button", { name: /Seleccionar todas las coincidencias/ }));
    fireEvent.change(screen.getByLabelText("Motivo del cambio"), { target: { value: "Acordado" } });
    fireEvent.click(screen.getByRole("button", { name: "Ver reparto antes de guardar" }));
    await screen.findByRole("button", { name: "Guardar esta asignación" });
    fireEvent.change(screen.getByLabelText("Cantidad del grupo 1"), { target: { value: "50" } });
    expect(screen.queryByRole("button", { name: "Guardar esta asignación" })).not.toBeInTheDocument();
  });
  it("has a campaign-specific empty video history, never the account's global history", async () => {
    mount("history");
    await screen.findByText("Videos de esta campaña (0)");
    expect(screen.getByText(/Todavía no hay videos generados/)).toBeInTheDocument();
    expect(calls.some(([url]) => url === "/jobs")).toBe(false);
  });
  it("never offers generation to selections that are not approved", async () => {
    mount("creative");
    fireEvent.click(await screen.findByRole("button", { name: /Seleccionar todas las coincidencias/ }));
    await waitFor(() => expect(screen.getByRole("button", { name: /Preparar generación de 0/ })).toBeDisabled());
  });
});
