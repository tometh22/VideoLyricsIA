import { beforeEach, afterEach, describe, it, expect, vi } from "vitest";
import { render, screen, fireEvent, waitFor, cleanup } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import CampaignCreative from "./CampaignCreative";
vi.mock("../i18n", () => ({ useI18n: () => ({ t: () => "" }) }));
vi.mock("./WizardLivePreview", () => ({ default: () => <div>Vista previa visual</div> }));
vi.mock("../mediaUrl", () => ({
  useLazyMediaUrl: () => ({ ref: () => {}, url: "/preview/thumbnail.jpg" }),
  useMediaUrl: () => "/preview/video.mp4",
}));
let head, report, calls, generateGate, generateFailures, generateCapacity;
const response = data => ({ ok: true, json: async () => data });
beforeEach(() => {
  calls = [];
  generateGate = null;
  generateFailures = new Set();
  generateCapacity = null;
  head = { plan: { revision: 0 }, can_manage: true, veo_model: "veo-3.1-fast-generate-001", fields: {
    font: { label: "Tipografía", group: "Letra", kind: "select", options: ["", "anton"] },
    effect: { label: "Efecto", group: "Movimiento y efectos", kind: "select", options: ["", "bokeh", "rain"] },
  }, operations: [], items: Array.from({ length: 39 }, (_, i) => ({ id: `i${i}`, job_id: `j${i}`, title: `Tema ${i}`, artist: "Artista", status: i < 2 ? "lyrics_approved" : i === 2 ? "done" : "transcribed_pending", settings: {} })) };
  report = { campaign_id: "c1", name: "Chile", at: new Date().toISOString(), contract: {}, groups: [], videos: [], history: [] };
  vi.stubGlobal("fetch", vi.fn(async (url, options = {}) => {
    calls.push([url, options]);
    if (url.endsWith("/creative")) return response(head);
    if (url.endsWith("/report")) return response(report);
    if (url.endsWith("/backgrounds")) return response([]);
    if (url.endsWith("/preview")) return response({ preview_id: "p1", counts: [39], rounded: false, skipped: [], changes: head.items.map(i => ({ item_id: i.id, artist: i.artist, title: i.title, group: "Estilo 1", before: {}, after: { font: "anton" } })) });
    if (url.endsWith("/apply")) { head = { ...head, plan: { revision: 1 } }; return response({ revision: 1 }); }
    if (url.endsWith("/deliveries")) return response({ operation_id: "op1", total_count: 2, status: "queued" });
    if (url.includes("/status/")) return response({ artist: "Artista", song_title: "Tema", segments_json: [], segments_revision: 2 });
    if (url.includes("/approve/")) {
      const jobId = url.split("/").pop();
      report = { ...report, videos: report.videos.map(video => video.job_id === jobId ? { ...video, status: "done", approved_at: new Date().toISOString() } : video) };
      return response({ ok: true, status: "done", job_id: jobId });
    }
    if (url.endsWith("/generate")) {
      if (generateGate) await generateGate;
      const jobId = options.body.get("job_id");
      if (generateCapacity && calls.filter(([path]) => path.endsWith("/generate")).length > generateCapacity.accepted) {
        return { ok: false, status: 429, json: async () => ({ detail: { code: generateCapacity.code, limit: 10 } }) };
      }
      return generateFailures.has(jobId) ? { ok: false, status: 503, json: async () => ({ detail: "No disponible" }) } : response({ ok: true });
    }
    throw new Error(`Unexpected request: ${url}`);
  }));
});
afterEach(() => { cleanup(); vi.unstubAllGlobals(); });

const mount = view => render(<MemoryRouter><CampaignCreative campaignId="c1" view={view} /></MemoryRouter>);
describe("campaign bulk design", () => {
  it("selects all pages, previews a partial change and only saves on confirmation", async () => {
    mount("creative");
    fireEvent.click(await screen.findByRole("button", { name: /Seleccionar resultados/ }));
    expect(screen.getByLabelText(/39 seleccionadas/)).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: /Configurar estilos y reparto/ }));
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
    fireEvent.click(await screen.findByRole("button", { name: /Seleccionar resultados/ }));
    fireEvent.click(screen.getByRole("button", { name: /Configurar estilos y reparto/ }));
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
  it("selects every approved history video and sends the snapshot to the chosen portal", async () => {
    report = { ...report, videos: [
      { job_id: "j1", title: "A", artist: "Artist", status: "done", approved_at: new Date().toISOString(), evidence: { video_sha256: "a".repeat(64) }, assignment: {}, created_at: new Date().toISOString(), video_url: "/download/j1/video" },
      { job_id: "j2", title: "B", artist: "Artist", status: "done", approved_at: new Date().toISOString(), evidence: { video_sha256: "b".repeat(64) }, assignment: {}, created_at: new Date().toISOString(), video_url: "/download/j2/video" },
      { job_id: "j3", title: "Pending", artist: "Artist", status: "pending_review", approved_at: null, evidence: {}, assignment: {}, created_at: new Date().toISOString(), video_url: "/download/j3/video" },
    ] };
    mount("history");
    fireEvent.click(await screen.findByRole("button", { name: "Seleccionar todos los aprobados (2)" }));
    expect(screen.getByLabelText("2 videos aprobados seleccionados")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Enviar seleccionados a un portal" }));
    fireEvent.change(screen.getByLabelText("Portal de destino"), { target: { value: "chile" } });
    fireEvent.click(screen.getByRole("button", { name: "Confirmar envío" }));
    await waitFor(() => expect(screen.getByText(/Envío iniciado para 2 videos a Chile/)).toBeInTheDocument());
    const deliveryCall = calls.find(([url]) => url.endsWith("/deliveries"));
    expect(JSON.parse(deliveryCall[1].body)).toEqual(expect.objectContaining({ job_ids: ["j1", "j2"], destination_portal: "chile" }));
  });
  it("uses a compact video list and only loads the medium player on demand", async () => {
    report = { ...report, videos: [{
      job_id: "video-1", title: "Tema para revisar", artist: "Artista Uno",
      status: "pending_review", created_at: "2026-09-11T12:00:00Z",
      video_url: "/download/video-1/video", open_path: "/videos/video-1",
      assignment: { group_name: "Inspirado en letra" }, evidence: {},
    }] };
    const view = mount("history");
    await screen.findByRole("list", { name: "Lista de videos de la campaña" });
    expect(screen.getByText("Tema para revisar")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Editar" })).toBeEnabled();
    expect(view.container.querySelector("video")).toBeNull();
    fireEvent.click(screen.getByRole("button", { name: "Reproducir" }));
    expect(await screen.findByRole("dialog", { name: "Reproducir Tema para revisar" })).toBeInTheDocument();
    expect(view.container.querySelector('video[src="/preview/video.mp4"]')).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Cerrar" }));
    expect(screen.queryByRole("dialog", { name: "Reproducir Tema para revisar" })).not.toBeInTheDocument();
  });
  it("approves a pending campaign video from the list after explicit confirmation", async () => {
    report = { ...report, videos: [{
      job_id: "video-2", title: "Tema final", artist: "Artista Dos",
      status: "pending_review", created_at: "2026-09-11T12:00:00Z",
      video_url: "/download/video-2/video", open_path: "/videos/video-2",
      assignment: {}, evidence: {},
    }] };
    mount("history");
    fireEvent.click(await screen.findByRole("button", { name: "Aprobar" }));
    expect(screen.getByRole("dialog", { name: "Aprobar Tema final" })).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Confirmar aprobación" }));
    await screen.findByText("Tema final quedó aprobado.");
    expect(screen.getByText("Aprobado")).toBeInTheDocument();
    const approvalCall = calls.find(([url]) => url.endsWith("/approve/video-2"));
    expect(JSON.parse(approvalCall[1].body)).toEqual({ notes: "Aprobado desde el historial de campaña" });
  });
  it("filters and paginates a large campaign video list", async () => {
    report = { ...report, videos: Array.from({ length: 23 }, (_, index) => ({
      job_id: `video-${index}`, title: `Video ${index}`, artist: "Artista",
      status: index < 2 ? "pending_review" : "done",
      created_at: "2026-09-11T12:00:00Z", video_url: `/download/video-${index}/video`,
      open_path: `/videos/video-${index}`, assignment: {}, evidence: {},
    })) };
    mount("history");
    await screen.findByText("Video 19");
    expect(screen.queryByText("Video 20")).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Siguiente" }));
    expect(await screen.findByText("Video 20")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: /^Pendientes 2$/ }));
    expect(await screen.findByText("Video 0")).toBeInTheDocument();
    expect(screen.getByText("Video 1")).toBeInTheDocument();
    expect(screen.queryByText("Video 2")).not.toBeInTheDocument();
  });
  it("never offers generation to selections that are not approved", async () => {
    head = { ...head, items: head.items.map(item => ({ ...item, status: "transcribed_pending" })) };
    mount("creative");
    fireEvent.click(await screen.findByRole("button", { name: /Seleccionar resultados/ }));
    await waitFor(() => expect(screen.getByRole("button", { name: /Preparar generación de 0/ })).toBeDisabled());
  });
  it("filters and selects only songs that are ready to generate", async () => {
    mount("creative");
    fireEvent.click(await screen.findByRole("button", { name: /^Listas para generar 2$/ }));
    expect(screen.getByText("Tema 0")).toBeInTheDocument();
    expect(screen.getByText("Tema 1")).toBeInTheDocument();
    expect(screen.queryByText("Tema 2")).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Seleccionar listas para generar (2)" }));
    expect(screen.getByLabelText(/2 seleccionadas/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Preparar generación de 2 aprobadas seleccionadas" })).toBeEnabled();
    fireEvent.click(screen.getByRole("button", { name: /^Videos aprobados 1$/ }));
    expect(screen.getByText("Tema 2")).toBeInTheDocument();
    expect(screen.queryByText("Tema 0")).not.toBeInTheDocument();
  });
  it("shows immediate progress while the selected videos are being submitted", async () => {
    let releaseGenerate;
    generateGate = new Promise(resolve => { releaseGenerate = resolve; });
    mount("creative");
    fireEvent.click(await screen.findByRole("button", { name: "Seleccionar listas para generar (2)" }));
    fireEvent.click(screen.getByRole("button", { name: "Preparar generación de 2 aprobadas seleccionadas" }));
    fireEvent.click(screen.getByRole("button", { name: "Confirmar generación" }));
    expect(await screen.findByRole("status", { name: "" })).toHaveTextContent("Enviando 1 de 2");
    expect(screen.getByText(/No vuelvas a confirmar/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /Enviando 1 de 2/ })).toBeDisabled();
    releaseGenerate();
    await screen.findByText("2 trabajos enviados; consultá el historial de esta campaña.");
    expect(calls.filter(([url]) => url.endsWith("/generate"))).toHaveLength(2);
  });
  it("continues submitting the remaining videos when one item fails", async () => {
    generateFailures.add("j0");
    mount("creative");
    fireEvent.click(await screen.findByRole("button", { name: "Seleccionar listas para generar (2)" }));
    fireEvent.click(screen.getByRole("button", { name: "Preparar generación de 2 aprobadas seleccionadas" }));
    fireEvent.click(screen.getByRole("button", { name: "Confirmar generación" }));
    await screen.findByText("1 trabajo enviado · 1 no se enviaron; consultá el historial de esta campaña.");
    expect(screen.getByRole("alert")).toHaveTextContent("No se pudieron enviar 1 video: Tema 0: No disponible");
    expect(calls.filter(([url]) => url.endsWith("/generate"))).toHaveLength(2);
  });
  it("leaves eight of eighteen songs selected when the render window fills and retries only those songs", async () => {
    head = { ...head, items: head.items.slice(0, 18).map(item => ({ ...item, status: "lyrics_approved" })) };
    generateCapacity = { accepted: 10, code: "batch_render_window_full" };
    mount("creative");
    fireEvent.click(await screen.findByRole("button", { name: "Seleccionar listas para generar (18)" }));
    fireEvent.click(screen.getByRole("button", { name: "Preparar generación de 18 aprobadas seleccionadas" }));
    fireEvent.click(screen.getByRole("button", { name: "Confirmar generación" }));
    await screen.findByText(/8 videos quedaron sin enviar y siguen seleccionados/);
    expect(screen.getByText(/La cola de generación de tu equipo está completa/)).toBeInTheDocument();
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
    expect(calls.filter(([url]) => url.endsWith("/generate"))).toHaveLength(11);
    expect(screen.getByLabelText(/8 seleccionadas/)).toBeInTheDocument();
    // Even if the refresh still reports the accepted songs as ready, their
    // selection is removed so a second submission never includes them.
    generateCapacity = null;
    fireEvent.click(screen.getByRole("button", { name: "Preparar generación de 8 aprobadas seleccionadas" }));
    fireEvent.click(screen.getByRole("button", { name: "Confirmar generación" }));
    await screen.findByText("8 trabajos enviados; consultá el historial de esta campaña.");
    expect(calls.filter(([url]) => url.endsWith("/generate")).slice(11).map(([, options]) => options.body.get("job_id")))
      .toEqual(Array.from({ length: 8 }, (_, i) => `j${i + 10}`));
    expect(screen.queryByText(/quedaron sin enviar y siguen seleccionados/)).not.toBeInTheDocument();
  });
  it("explains that the final review buffer needs approval instead of reporting failed videos", async () => {
    generateCapacity = { accepted: 0, code: "batch_final_review_full" };
    mount("creative");
    fireEvent.click(await screen.findByRole("button", { name: "Seleccionar listas para generar (2)" }));
    fireEvent.click(screen.getByRole("button", { name: "Preparar generación de 2 aprobadas seleccionadas" }));
    fireEvent.click(screen.getByRole("button", { name: "Confirmar generación" }));
    await screen.findByText(/Revisá y aprobá o rechazá algunos/);
    expect(screen.getByText(/2 videos quedaron sin enviar y siguen seleccionados/)).toBeInTheDocument();
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
    expect(calls.filter(([url]) => url.endsWith("/generate"))).toHaveLength(1);
  });
});
