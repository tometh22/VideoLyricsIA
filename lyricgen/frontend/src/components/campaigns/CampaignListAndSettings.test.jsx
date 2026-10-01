import { act, cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { MemoryRouter, Route, Routes, useLocation } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";
import { clearCampaignResourceCache } from "../../hooks/useCampaignResource";
import CampaignsPage from "../CampaignsPage";
import ReviewQueuePage, { reviewQueueTarget } from "../ReviewQueuePage";
import ContractReport from "./ContractReport";
import StyleAssignment from "./StyleAssignment";
import CampaignAudioUploader from "./CampaignAudioUploader";
import { createCampaignApi, json, makeSong } from "./campaignTestApi";

vi.mock("../../i18n", () => ({ useI18n: () => ({ t: () => "" }) }));
vi.mock("../WizardLivePreview", () => ({ default: () => <div>Vista previa visual</div> }));
vi.mock("../../mediaUrl", () => ({ useLazyMediaUrl: () => ({ ref: () => {}, url: "" }), useMediaUrl: () => "" }));
const upload = vi.hoisted(() => ({ inspect: null, run: null }));
vi.mock("../../lib/campaignUpload", async (original) => ({
  ...(await original()),
  inspectAudioFiles: (...args) => upload.inspect(...args),
  uploadCampaignAudios: (...args) => upload.run(...args),
}));

function Location() { const location = useLocation(); return <output data-testid="location">{location.pathname}{location.search}</output>; }
const deferred = () => { let resolve; const promise = new Promise((done) => { resolve = done; }); return { promise, resolve }; };

afterEach(() => { cleanup(); vi.unstubAllGlobals(); clearCampaignResourceCache(); localStorage.clear(); });

describe("campaign list", () => {
  it("never shows a false empty state and summarizes each campaign with its next step", async () => {
    const request = deferred();
    vi.stubGlobal("fetch", vi.fn(() => request.promise));
    render(<MemoryRouter initialEntries={["/campaigns"]}><Location /><Routes><Route path="/campaigns" element={<CampaignsPage />} /><Route path="/campaigns/:campaignId" element={<p>Detalle</p>} /></Routes></MemoryRouter>);
    expect(screen.getByText("Cargando campañas…")).toHaveAttribute("role", "status");
    expect(screen.queryByText("Todavía no hay campañas")).not.toBeInTheDocument();
    await act(async () => request.resolve(json({ items: [
      { id: "a", name: "UMG Agosto", status: "active", kind: "lyric_video", registered_count: 10, pipeline: { counts: { lyrics: 4, qc: 1, approved: 2, delivered: 3 } } },
      { id: "b", name: "Chile Art Tracks", status: "paused", kind: "art_track", registered_count: 2, counters: { final_review: 2 } },
    ] })));
    const card = await screen.findByRole("button", { name: /UMG Agosto/ });
    expect(card).toHaveTextContent("50%");
    expect(card).toHaveTextContent("5 de 10 terminadas");
    expect(card).toHaveTextContent("Revisar 4 letras");
    expect(screen.getByRole("button", { name: /Chile Art Tracks/ })).toHaveTextContent("Revisar 2 videos");
    fireEvent.click(screen.getByRole("radio", { name: "Pausadas" }));
    expect(screen.queryByRole("button", { name: /UMG Agosto/ })).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: /Chile Art Tracks/ }));
    expect(screen.getByTestId("location")).toHaveTextContent("/campaigns/b");
  });

  it("flags open client change requests on the campaign card with the age of the oldest", async () => {
    const oldest = new Date(Date.now() - 2 * 24 * 3600 * 1000).toISOString();
    vi.stubGlobal("fetch", vi.fn(async () => json({ items: [
      { id: "a", name: "UMG Agosto", status: "active", kind: "lyric_video", registered_count: 10,
        pipeline: { counts: { delivered: 3 }, flags: { change_requests: 2, change_requests_open: 3, oldest_change_request_at: oldest } } },
      { id: "b", name: "Sin pedidos", status: "active", kind: "lyric_video", registered_count: 4,
        pipeline: { counts: { delivered: 1 }, flags: { change_requests_open: 0 } } },
    ] })));
    render(<MemoryRouter initialEntries={["/campaigns"]}><Routes><Route path="/campaigns" element={<CampaignsPage />} /></Routes></MemoryRouter>);
    const card = await screen.findByRole("button", { name: /UMG Agosto/ });
    expect(card).toHaveTextContent("3 cambios pedidos por el cliente · el más antiguo: hace 2 días");
    expect(screen.getByRole("button", { name: /Sin pedidos/ })).not.toHaveTextContent("cambio");
  });

  it("creates a campaign, saves a visual base style and offers the browser uploader", async () => {
    const calls = [];
    vi.stubGlobal("fetch", vi.fn(async (input, options = {}) => {
      const path = new URL(String(input), "http://test").pathname;
      const body = options.body ? JSON.parse(options.body) : null;
      calls.push({ path, method: options.method || "GET", body });
      if (path === "/batch/campaigns" && options.method === "POST") return json({ id: "new1", name: body.name, kind: body.kind, default_render_params: body.default_render_params });
      if (path === "/batch/campaigns") return json({ items: [] });
      if (path === "/batch/campaigns/new1/creative") return json({ fields: { font: { label: "Tipografía", group: "Letra", kind: "select", options: ["", "anton"] }, delivery_profile: { label: "Entrega", group: "Salida", kind: "select", options: ["youtube"] } }, items: [], plan: { revision: 0 } });
      if (path === "/batch/campaigns/new1" && options.method === "PATCH") return json({ ok: true });
      throw new Error(`unexpected ${path}`);
    }));
    render(<MemoryRouter initialEntries={["/campaigns"]}><Location /><Routes><Route path="/campaigns" element={<CampaignsPage />} /><Route path="/campaigns/:campaignId" element={<p>Detalle</p>} /></Routes></MemoryRouter>);
    fireEvent.click(await screen.findByRole("button", { name: "Crear la primera campaña" }));
    const wizard = await screen.findByRole("dialog", { name: "Nueva campaña" });
    fireEvent.change(within(wizard).getByLabelText("Nombre de la campaña"), { target: { value: "UMG Octubre" } });
    fireEvent.click(within(wizard).getByRole("button", { name: "Crear y seguir" }));
    await within(wizard).findByText("Cambiar Tipografía");
    expect(calls.find((call) => call.method === "POST").body).toMatchObject({ name: "UMG Octubre", kind: "lyric_video", destination_portal: null });
    expect(within(wizard).queryByText("Cambiar Entrega")).not.toBeInTheDocument();
    fireEvent.click(within(wizard).getByText("Cambiar Tipografía"));
    fireEvent.change(within(wizard).getByLabelText("Estilo base: Tipografía"), { target: { value: "anton" } });
    fireEvent.click(within(wizard).getByRole("radio", { name: /Ambos/ }));
    fireEvent.click(within(wizard).getByRole("button", { name: "Guardar y seguir" }));
    await within(wizard).findByText("Arrastrá la carpeta con los audios");
    expect(calls.find((call) => call.method === "PATCH").body.default_render_params).toEqual({ background_mode: "ai", delivery_profile: "both", font: "anton" });
    fireEvent.click(within(wizard).getByRole("button", { name: "Subir después" }));
    expect(screen.getByTestId("location")).toHaveTextContent("/campaigns/new1");
  });

  it("requires a portal for art tracks", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => json({ items: [] })));
    render(<MemoryRouter initialEntries={["/campaigns?new=1"]}><Routes><Route path="/campaigns" element={<CampaignsPage />} /></Routes></MemoryRouter>);
    const wizard = await screen.findByRole("dialog", { name: "Nueva campaña" });
    fireEvent.change(within(wizard).getByLabelText("Nombre de la campaña"), { target: { value: "Art" } });
    fireEvent.click(within(wizard).getByRole("radio", { name: /Art tracks/ }));
    expect(within(wizard).getByRole("button", { name: "Crear y seguir" })).toBeDisabled();
    fireEvent.change(within(wizard).getByLabelText("Portal de destino"), { target: { value: "files" } });
    expect(within(wizard).getByRole("button", { name: "Crear y seguir" })).toBeEnabled();
    expect(within(wizard).getByRole("radio", { name: /Portada \+ onda/ })).toHaveAttribute("aria-checked", "true");
    fireEvent.click(within(wizard).getByRole("radio", { name: /Portada fija/ }));
    expect(within(wizard).getByRole("radio", { name: /Portada fija/ })).toHaveAttribute("aria-checked", "true");
  });
});

describe("/admin/cola", () => {
  const campaigns = [
    { id: "art", status: "active", kind: "art_track" },
    { id: "old", status: "completed", kind: "lyric_video" },
    { id: "live", status: "active", kind: "lyric_video" },
    { id: "other", status: "active", kind: "lyric_video" },
  ];
  it("forwards to the requested, remembered or first active lyric campaign with its context", () => {
    expect(reviewQueueTarget(campaigns, new URLSearchParams("campaign=other&q=garcia&approved=j9"), null)).toBe("/campaigns/other?view=lyrics&q=garcia&approved=j9");
    expect(reviewQueueTarget(campaigns, new URLSearchParams(""), "other")).toBe("/campaigns/other?view=lyrics");
    expect(reviewQueueTarget(campaigns, new URLSearchParams(""), "old")).toBe("/campaigns/live?view=lyrics");
    expect(reviewQueueTarget(campaigns, new URLSearchParams("scope=drafts"), null)).toBe("/campaigns/live?view=lyrics&drafts=1");
    expect(reviewQueueTarget(campaigns, new URLSearchParams("scope=approved"), null)).toBe("/campaigns/live?view=all");
    expect(reviewQueueTarget([{ id: "art", kind: "art_track", status: "active" }], new URLSearchParams(""), null)).toBeNull();
  });
  it("redirects in place", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => json({ items: campaigns })));
    render(<MemoryRouter initialEntries={["/admin/cola?q=x"]}><Location /><Routes><Route path="/admin/cola" element={<ReviewQueuePage />} /><Route path="/campaigns/:campaignId" element={<p>Campaña</p>} /></Routes></MemoryRouter>);
    await screen.findByText("Campaña");
    expect(decodeURIComponent(screen.getByTestId("location").textContent)).toBe("/campaigns/live?view=lyrics&q=x");
  });
});

describe("style assignment", () => {
  const creative = (plan = { revision: 0 }) => ({ plan, can_manage: true, operations: [], fields: {
    font: { label: "Tipografía", group: "Letra", kind: "select", options: ["", "anton"] },
  }, items: [] });
  function setup(onRequest) {
    const calls = [];
    vi.stubGlobal("fetch", vi.fn(async (input, options = {}) => {
      const path = new URL(String(input), "http://test").pathname;
      const body = options.body && typeof options.body === "string" ? JSON.parse(options.body) : null;
      calls.push({ path, body });
      const custom = onRequest?.(path, body);
      if (custom) return custom;
      if (path === "/backgrounds") return json([]);
      if (path.endsWith("/creative/preview")) return json({ preview_id: "p1", counts: [body.item_ids.length], rounded: false, skipped: [], changes: body.item_ids.map((id) => ({ item_id: id, artist: "A", title: id, group: body.groups[0].name, before: {}, after: body.groups[0].settings })) });
      if (path.endsWith("/creative/apply")) return json({ revision: 1 });
      throw new Error(path);
    }));
    return calls;
  }
  it("previews a change for the chosen songs, invalidates it on edit and saves only on confirmation", async () => {
    const calls = setup();
    const onSaved = vi.fn();
    render(<StyleAssignment campaignId="c1" creative={creative()} itemIds={["i1", "i2"]} onSaved={onSaved} />);
    fireEvent.click(screen.getByText("Cambiar Tipografía"));
    fireEvent.change(screen.getByLabelText("Estilo 1: Tipografía"), { target: { value: "anton" } });
    fireEvent.change(screen.getByLabelText("Motivo del cambio"), { target: { value: "Tipografía acordada" } });
    fireEvent.click(screen.getByRole("button", { name: "Ver reparto antes de guardar" }));
    await screen.findByRole("button", { name: "Guardar esta asignación" });
    expect(calls.find((call) => call.path.endsWith("/preview")).body).toMatchObject({ item_ids: ["i1", "i2"], groups: [{ settings: { font: "anton" } }] });
    expect(calls.some((call) => call.path.endsWith("/apply"))).toBe(false);
    fireEvent.change(screen.getByLabelText("Cantidad del grupo 1"), { target: { value: "90" } });
    expect(screen.queryByRole("button", { name: "Guardar esta asignación" })).not.toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Ver reparto antes de guardar" })).toBeDisabled();
    fireEvent.change(screen.getByLabelText("Cantidad del grupo 1"), { target: { value: "100" } });
    fireEvent.click(screen.getByRole("button", { name: "Ver reparto antes de guardar" }));
    fireEvent.click(await screen.findByRole("button", { name: "Guardar esta asignación" }));
    await screen.findByText("Asignación guardada. No se generaron videos.");
    expect(onSaved).toHaveBeenCalled();
  });
  it("always uses Veo Lite, even for groups saved with a faster model", async () => {
    const calls = setup();
    render(<StyleAssignment campaignId="c1" itemIds={["i1"]} creative={creative({ revision: 2, groups: [{ id: "legacy", name: "Legacy", weight: 100, requirement: "veo", model: "veo-3.1-fast-generate-001", settings: { movement_style: "estandar" } }] })} />);
    expect(screen.getByText(/Modelo de fondo: Veo Lite/)).toBeInTheDocument();
    fireEvent.change(screen.getByLabelText("Motivo del cambio"), { target: { value: "Lite obligatorio" } });
    fireEvent.click(screen.getByRole("button", { name: "Ver reparto antes de guardar" }));
    await screen.findByRole("button", { name: "Guardar esta asignación" });
    expect(calls.find((call) => call.path.endsWith("/preview")).body.groups[0].model).toBe("veo-3.1-lite-generate-001");
  });
});

describe("contract report", () => {
  it("offers to register an agreement instead of an empty table", () => {
    const onRegister = vi.fn();
    render(<ContractReport campaignId="c1" report={{ name: "X", at: "2026-09-01", contract: {}, groups: [], videos: [], history: [] }} onRegister={onRegister} />);
    fireEvent.click(screen.getByRole("button", { name: "Registrar un reparto" }));
    expect(onRegister).toHaveBeenCalled();
    expect(screen.queryByRole("table")).not.toBeInTheDocument();
  });
  it("shows totals against the contract universe", () => {
    render(<ContractReport campaignId="c1" report={{ name: "Chile", at: "2026-09-01", contract: { revision: 1, agreement: "Mitad y mitad", mode: "percent", item_ids: ["a", "b", "c", "d"] },
      groups: [{ id: "g1", name: "Foto", target: 2, target_weight: 50, assigned: 2, generated: 2, approved: 1, verified: 1, delivered: 0 }, { id: "g2", name: "Veo", target: 2, target_weight: 50, assigned: 2, generated: 1, approved: 1, verified: 0, delivered: 0 }],
      videos: [], history: [] }} />);
    expect(screen.getByText("Mitad y mitad")).toBeInTheDocument();
    expect(screen.getByRole("progressbar", { name: "Generadas" })).toHaveAttribute("aria-valuenow", "75");
    expect(screen.getAllByRole("row")).toHaveLength(3);
  });
});

describe("browser uploader", () => {
  it("lets the operator fix incomplete metadata before registering", async () => {
    upload.inspect = vi.fn(async () => [
      { file: {}, filename: "tema.wav", relative_path: "x/tema.wav", title: "Tema", artist: "", technical_code: "", size_bytes: 1000, duration_seconds: 200, sha256: "a".repeat(64), client_id: "a".repeat(64) },
      { file: {}, filename: "Otro_ArtistaARF2.wav", relative_path: "x/Otro_ArtistaARF2.wav", title: "Otro", artist: "Artista", technical_code: "ARF2", size_bytes: 1000, duration_seconds: 200, sha256: "b".repeat(64), client_id: "b".repeat(64) },
    ]);
    upload.run = vi.fn(async (campaignId, entries, { onProgress }) => {
      onProgress({ phase: "uploading", registered: 2, uploaded: 1, failed: 0, bytesDone: 1000, bytesTotal: 2000, current: "tema.wav", errors: [] });
      return { phase: "done", registered: 2, uploaded: 2, duplicates: 0, skipped: 0, failed: 0, errors: [] };
    });
    const onUploaded = vi.fn();
    render(<CampaignAudioUploader campaignId="c1" onUploaded={onUploaded} />);
    const audio = new File(["x"], "tema.wav", { type: "audio/wav" });
    fireEvent.change(screen.getByLabelText("Elegir archivos de audio"), { target: { files: [audio] } });
    await screen.findByText("1 con datos incompletos");
    fireEvent.change(screen.getByLabelText("Artista de tema.wav"), { target: { value: "Charly" } });
    fireEvent.change(screen.getByLabelText("Código de tema.wav"), { target: { value: "arf1" } });
    expect(screen.queryByText("1 con datos incompletos")).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Subir 2 audios" }));
    await screen.findByText("2 audios subidos");
    expect(upload.run.mock.calls[0][1][0]).toMatchObject({ artist: "Charly", technical_code: "ARF1" });
    expect(onUploaded).toHaveBeenCalledWith(expect.objectContaining({ uploaded: 2 }));
  });
});

describe("settings from the workspace", () => {
  it("pauses the campaign and asks before cancelling", async () => {
    const api = createCampaignApi({ songs: [makeSong(1, "lyrics")] });
    vi.stubGlobal("fetch", api.fetchMock);
    render(<MemoryRouter initialEntries={["/campaigns/c1?view=config"]}><Routes><Route path="/campaigns/:campaignId" element={<CampaignsPage />} /></Routes></MemoryRouter>);
    fireEvent.click(await screen.findByRole("radio", { name: "Pausada" }));
    await waitFor(() => expect(api.state.calls.find((call) => call.method === "PATCH")?.body).toEqual({ status: "paused" }));
    fireEvent.click(screen.getByRole("button", { name: "Cancelar campaña" }));
    const dialog = await screen.findByRole("dialog", { name: "Cancelar campaña" });
    fireEvent.click(within(dialog).getByRole("button", { name: "Volver" }));
    expect(api.state.calls.filter((call) => call.method === "PATCH")).toHaveLength(1);
  });
});
