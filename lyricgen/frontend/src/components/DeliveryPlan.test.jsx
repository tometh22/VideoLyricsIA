import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import DeliveryPlan from "./DeliveryPlan";

let report, calls, applyError;
const empty = () => ({ veo: 0, photo_effect: 0, unknown: 0 });
const response = (data, status = 200) => ({ ok: status < 400, status, json: async () => data });
beforeEach(() => {
  calls = []; applyError = false;
  report = {
    scope: "portal:argentina", name: "Universal Argentina", portal_id: "argentina", period: "", fingerprint: "v1",
    buckets: { delivered: { veo: 312, photo_effect: 112, unknown: 0 }, generated: empty(), in_progress: empty(), pending: { veo: 0, photo_effect: 0, unknown: 2 } },
    projected: { veo: 312, photo_effect: 112, unknown: 2 }, suggestion: { veo: 0, photo_effect: 2, total: 2, projected_veo: 312, projected_photo_effect: 114, remaining_gap: 198, odd_total: false },
    excluded_art_tracks: 254, blockers: [], warnings: [], unmapped_campaigns: [], can_manage: true, can_view_accounts: true,
    campaigns: [{ id: "c1", name: "Campaña septiembre", portal_id: "argentina", pending: 2, ready: 0, writable: true }],
    items: [0, 1].map(i => ({ item_id: `i${i}`, campaign_id: "c1", campaign_name: "Campaña septiembre", title: `Canción ${i}`, artist: "Artista", job_id: `j${i}`, status: i ? "transcribed_pending" : "lyrics_approved", type: "unknown", effect: "", stage: "pending", ready: false, editable: true, revision: 0, settings: {}, assignment: {} })),
  };
  vi.stubGlobal("fetch", vi.fn(async (url, options = {}) => {
    calls.push([url, options]);
    if (url.includes("/delivery-plan?") ) return response(report);
    if (url.endsWith("/delivery-plan/preview")) return response({ preview_id: "preview1", suggestion: report.suggestion, changes: report.items.map(row => ({ ...row, after_type: "photo_effect" })) });
    if (url.endsWith("/delivery-plan/apply")) {
      if (applyError) return response({ detail: "Cambió el saldo. Revisá una nueva propuesta." }, 409);
      report = { ...report, fingerprint: "v2", items: report.items.map(row => ({ ...row, type: "photo_effect", effect: "bokeh", ready: row.status === "lyrics_approved", settings: { movement_style: "foto-parallax", effect: "bokeh" }, assignment: { requirement: "photo_effect", revision: 1 } })) };
      return response({ ok: true });
    }
    if (url.endsWith("/items/i0")) return response({ revision: 1 });
    if (url.includes("/status/")) return response({ status: "lyrics_approved", segments_json: [], segments_revision: 3 });
    if (url.endsWith("/generate")) return response({ ok: true });
    throw new Error(`Unexpected ${url}`);
  }));
});
afterEach(() => { cleanup(); vi.unstubAllGlobals(); });
const mount = (campaignId, route = "/campaigns?view=accounts&account=argentina") => render(<MemoryRouter initialEntries={[route]}><DeliveryPlan campaignId={campaignId} /></MemoryRouter>);

describe("Delivery plan", () => {
  it("uses portal account and period, never the operator tenant", async () => {
    mount();
    await screen.findByText("Saldo y proyección");
    fireEvent.change(screen.getByLabelText("Cuenta cliente"), { target: { value: "chile" } });
    await waitFor(() => expect(calls.some(([url]) => url.includes("portal_id=chile"))).toBe(true));
    fireEvent.change(screen.getByLabelText("Período del saldo"), { target: { value: "2026-10" } });
    await waitFor(() => expect(calls.some(([url]) => url.includes("portal_id=chile&period=2026-10"))).toBe(true));
    expect(calls.every(([url]) => !url.includes("tenant"))).toBe(true);
  });

  it("previews before saving and never generates while balancing", async () => {
    mount(); await screen.findByText("Saldo y proyección");
    fireEvent.click(screen.getByRole("button", { name: "Revisar reparto" }));
    const dialog = await screen.findByRole("dialog", { name: "Revisar reparto de fondos" });
    expect(calls.some(([url]) => url.endsWith("/apply"))).toBe(false);
    expect(within(dialog).getByText(/Guardar sólo asigna/)).toBeInTheDocument();
    fireEvent.click(within(dialog).getByRole("button", { name: "Aplicar asignaciones" }));
    await screen.findByText(/Reparto guardado/);
    expect(calls.filter(([url]) => url.endsWith("/apply"))).toHaveLength(1);
    expect(calls.some(([url]) => url.endsWith("/generate"))).toBe(false);
  });

  it("only generates ready assigned songs after a separate confirmation", async () => {
    mount(); await screen.findByText("Saldo y proyección");
    expect(screen.getByRole("button", { name: "Generar seleccionadas (0)" })).toBeDisabled();
    fireEvent.click(screen.getByRole("button", { name: "Revisar reparto" }));
    fireEvent.click(await screen.findByRole("button", { name: "Aplicar asignaciones" }));
    await screen.findByText(/Reparto guardado/);
    fireEvent.click(screen.getByRole("button", { name: "Seleccionar listas (1)" }));
    fireEvent.click(screen.getByRole("button", { name: "Generar seleccionadas (1)" }));
    const dialog = await screen.findByRole("dialog", { name: "Confirmar generación" });
    expect(calls.some(([url]) => url.endsWith("/generate"))).toBe(false);
    fireEvent.click(within(dialog).getByRole("button", { name: "Confirmar generación" }));
    await screen.findByText(/1 de 1 enviados a generación/);
    const generated = calls.filter(([url]) => url.endsWith("/generate"));
    expect(generated).toHaveLength(1);
    expect(generated[0][1].body.get("job_id")).toBe("j0");
    expect(generated[0][1].body.get("movement_style")).toBe("foto-parallax");
  });

  it("keeps stale preview failures visible without pretending assignments saved", async () => {
    applyError = true;
    mount(); await screen.findByText("Saldo y proyección");
    fireEvent.click(screen.getByRole("button", { name: "Revisar reparto" }));
    fireEvent.click(await screen.findByRole("button", { name: "Aplicar asignaciones" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("Cambió el saldo");
    expect(screen.queryByText(/Reparto guardado/)).not.toBeInTheDocument();
    expect(calls.some(([url]) => url.endsWith("/generate"))).toBe(false);
  });

  it("supports campaign scope and links to its portal account", async () => {
    mount("c1"); await screen.findByText("Saldo y proyección");
    expect(calls[0][0]).toContain("campaign_id=c1");
    expect(screen.getByRole("link", { name: "Ver saldo de la cuenta" })).toHaveAttribute("href", "/campaigns?view=accounts&account=argentina");
    expect(screen.queryByLabelText("Cuenta cliente")).not.toBeInTheDocument();
  });

  it("does not propose an automatic split with unverified generated backgrounds", async () => {
    report.blockers = ["Hay fondos sin evidencia verificable."];
    mount(); await screen.findByText("Saldo y proyección");
    expect(screen.getByRole("button", { name: "Revisar reparto" })).toBeDisabled();
    expect(screen.getByText("Hay fondos sin evidencia verificable.")).toBeInTheDocument();
  });

  it("lets users inspect delivered videos without selecting them for generation", async () => {
    report.items.push({ item_id: "delivery:j2:argentina", campaign_id: "c1", job_id: "j2", title: "Video entregado", type: "veo", stage: "delivered", ready: false, editable: false });
    mount(); await screen.findByText("Saldo y proyección");
    expect(screen.queryByText("Video entregado")).not.toBeInTheDocument();
    fireEvent.change(screen.getByLabelText("Filtrar plan"), { target: { value: "delivered" } });
    expect(screen.getByText("Video entregado")).toBeInTheDocument();
    expect(screen.getByRole("checkbox", { name: "Generar Video entregado" })).toBeDisabled();
    expect(screen.getByRole("link", { name: "Ver entregable" })).toHaveAttribute("href", "/campaigns/c1?view=delivered");
    expect(screen.getByRole("button", { name: "Generar seleccionadas (0)" })).toBeDisabled();
  });
});
