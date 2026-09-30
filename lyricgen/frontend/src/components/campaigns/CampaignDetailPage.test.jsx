import { act, cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { MemoryRouter, Route, Routes, useLocation, useNavigate } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";
import { clearCampaignResourceCache } from "../../hooks/useCampaignResource";
import CampaignsPage from "../CampaignsPage";
import { createCampaignApi, json, makeSong } from "./campaignTestApi";

vi.mock("../../i18n", () => ({ useI18n: () => ({ t: () => "" }) }));
vi.mock("../WizardLivePreview", () => ({ default: () => <div>Vista previa visual</div> }));
vi.mock("../../mediaUrl", () => ({
  useLazyMediaUrl: () => ({ ref: () => {}, url: "" }),
  useMediaUrl: (jobId) => (jobId ? `/preview/${jobId}.mp4` : ""),
}));

function Location() {
  const location = useLocation();
  const navigate = useNavigate();
  return <><output data-testid="location">{location.pathname}{location.search}</output><button onClick={() => navigate(-1)}>Atrás navegador</button></>;
}

function mount(api, path = "/campaigns/c1") {
  vi.stubGlobal("fetch", api.fetchMock);
  return render(<MemoryRouter initialEntries={[path]}><Location /><Routes>
    <Route path="/campaigns" element={<CampaignsPage />} />
    <Route path="/campaigns/:campaignId" element={<CampaignsPage />} />
    <Route path="/review/:jobId" element={<p>Editor de letra</p>} />
    <Route path="/videos/:jobId/*" element={<p>Detalle del video</p>} />
  </Routes></MemoryRouter>);
}
const location = () => decodeURIComponent(screen.getByTestId("location").textContent);
const tab = (name) => screen.getByRole("tab", { name: new RegExp(`^${name}`) });

afterEach(() => { cleanup(); vi.unstubAllGlobals(); clearCampaignResourceCache(); localStorage.clear(); });

describe("campaign workspace", () => {
  it("counts every song once with the same numbers in every tab", async () => {
    const api = createCampaignApi({ songs: [makeSong(1, "lyrics"), makeSong(2, "lyrics"), makeSong(3, "ready"), makeSong(4, "qc"), makeSong(5, "delivered"), makeSong(6, "discarded")] });
    mount(api);
    await screen.findByText("Canción 1");
    expect(tab("Todas")).toHaveTextContent("5");
    expect(tab("Letra")).toHaveTextContent("2");
    expect(tab("QC video")).toHaveTextContent("1");
    expect(tab("Entregada")).toHaveTextContent("1");
    expect(tab("Descartadas")).toHaveTextContent("1");
    expect(screen.getByText("canciones terminadas", { exact: false })).toHaveTextContent("1 de 5 canciones terminadas · 20%");
    expect(screen.queryByText("Canción 6")).not.toBeInTheDocument();
    fireEvent.click(tab("QC video"));
    await waitFor(() => expect(screen.queryByText("Canción 1")).not.toBeInTheDocument());
    expect(screen.getByText("Canción 4")).toBeInTheDocument();
    expect(tab("QC video")).toHaveAttribute("aria-selected", "true");
    fireEvent.click(screen.getByRole("button", { name: "Atrás navegador" }));
    await screen.findByText("Canción 1");
    expect(tab("Todas")).toHaveAttribute("aria-selected", "true");
  });

  it("opens the next lyric inside the filtered list and returns to the same context", async () => {
    const api = createCampaignApi({ songs: [makeSong(1, "lyrics"), makeSong(2, "lyrics"), makeSong(3, "lyrics")] });
    mount(api, "/campaigns/c1?view=lyrics&q=divididos");
    await screen.findByText("Canción 2");
    expect(screen.queryByText("Canción 1")).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: /Revisar 3 letras/ }));
    await screen.findByText("Editor de letra");
    expect(location()).toContain("/review/j2?return_to=/campaigns/c1?view=lyrics&q=divididos&focus=j2");
    expect(api.state.calls.some((call) => call.path.endsWith("/next"))).toBe(false);
  });

  it("filters saved drafts and keeps alerts behind an info tip", async () => {
    const api = createCampaignApi({
      songs: [makeSong(1, "lyrics"), makeSong(2, "lyrics")],
      reviewRows: () => [
        { item_id: "i2", job_id: "j2", state: "ready", is_draft: true, last_reviewed_name: "Agus", review_priority: "timing_targeted", timing_evidence: [{ start: 1, end: 2 }], can_discard: true },
        { item_id: "i1", job_id: "j1", state: "ready", review_priority: "standard", timing_evidence: [], can_discard: true },
      ],
    });
    mount(api, "/campaigns/c1?view=lyrics");
    await screen.findByText("Cambios guardados por Agus");
    const rows = screen.getAllByRole("row").slice(1).map((row) => row.textContent);
    expect(rows[0]).toContain("Canción 2");
    expect(screen.getByText("1 fragmento a comprobar")).toBeInTheDocument();
    expect(screen.getByText(/Las alertas orientan/)).not.toBeVisible();
    fireEvent.click(screen.getByLabelText("Cómo leer las alertas"));
    expect(screen.getByText(/Las alertas orientan/)).toBeVisible();
    fireEvent.click(screen.getByRole("button", { name: /Con cambios guardados/ }));
    await waitFor(() => expect(screen.queryByText("Canción 1")).not.toBeInTheDocument());
    expect(location()).toContain("drafts=1");
    fireEvent.click(screen.getByRole("button", { name: "Continuar revisión · Canción 2" }));
    await screen.findByText("Editor de letra");
    expect(location()).toContain("drafts=1");
  });

  it("discards with a visible reason and restores from Descartadas", async () => {
    const api = createCampaignApi({ songs: [makeSong(1, "lyrics"), makeSong(2, "lyrics")] });
    mount(api, "/campaigns/c1?view=lyrics");
    await screen.findByText("Canción 1");
    fireEvent.click(screen.getByRole("button", { name: "Detalle de Canción 1" }));
    const drawer = await screen.findByRole("dialog", { name: "Canción Canción 1" });
    fireEvent.click(within(drawer).getByRole("button", { name: "Descartar" }));
    const dialog = await screen.findByRole("dialog", { name: "Descartar canción" });
    fireEvent.change(within(dialog).getByLabelText("Motivo"), { target: { value: "Instrumental, sin letra" } });
    fireEvent.click(within(dialog).getByRole("button", { name: "Confirmar descarte" }));
    await waitFor(() => expect(screen.queryByRole("dialog", { name: "Descartar canción" })).not.toBeInTheDocument());
    expect(api.state.calls.find((call) => call.path.endsWith("/i1/discard")).body).toEqual({ reason: "Instrumental, sin letra" });
    await waitFor(() => expect(tab("Descartadas")).toHaveTextContent("1"));
    fireEvent.click(tab("Descartadas"));
    await screen.findByText("Instrumental, sin letra");
    fireEvent.click(screen.getByRole("button", { name: "Recuperar · Canción 1" }));
    fireEvent.click(await screen.findByRole("button", { name: "Confirmar recuperación" }));
    await waitFor(() => expect(api.state.calls.some((call) => call.path.endsWith("/i1/restore"))).toBe(true));
  });

  it("generates only ready songs, keeps deferred ones selected when the render window fills", async () => {
    const api = createCampaignApi({ songs: [makeSong(1, "ready"), makeSong(2, "ready"), makeSong(3, "ready"), makeSong(4, "lyrics")] });
    api.state.generateResponses.push(undefined, json({ detail: { code: "batch_render_window_full" } }, 429));
    mount(api, "/campaigns/c1?view=ready");
    await screen.findByText("Canción 3");
    fireEvent.click(screen.getByRole("checkbox", { name: "Seleccionar 3 canciones" }));
    const bar = screen.getByRole("region", { name: "Acciones sobre la selección" });
    fireEvent.click(within(bar).getByRole("button", { name: "Generar 3 videos" }));
    const dialog = await screen.findByRole("dialog", { name: "Confirmar generación" });
    fireEvent.click(within(dialog).getByRole("button", { name: "Confirmar generación" }));
    await screen.findByText(/1 trabajo enviado/);
    expect(screen.getByText(/La cola de generación de tu equipo está completa/)).toBeInTheDocument();
    expect(screen.getByText(/2 videos quedaron sin enviar y siguen seleccionados/)).toBeInTheDocument();
    const generated = api.state.calls.filter((call) => call.path === "/generate");
    expect(generated.map((call) => call.body.get("job_id"))).toEqual(["j1", "j2"]);
    expect(generated[0].body.get("base_revision")).toBe("3");
    expect(generated[0].body.get("font")).toBe("anton");
    await waitFor(() => expect(within(screen.getByRole("region", { name: "Acciones sobre la selección" })).getByText("2")).toBeInTheDocument());
  });

  it("approves only the video on screen and advances inside the QC list", async () => {
    const api = createCampaignApi({ songs: [makeSong(1, "qc"), makeSong(2, "qc"), makeSong(3, "qc")] });
    mount(api, "/campaigns/c1?view=qc&q=garcia");
    await screen.findByText("Canción 3");
    fireEvent.click(screen.getByRole("button", { name: "Revisar video · Canción 1" }));
    await screen.findByRole("dialog", { name: "Reproducir Canción 1" });
    fireEvent.click(screen.getByRole("button", { name: "Aprobar y siguiente" }));
    await screen.findByRole("dialog", { name: "Reproducir Canción 3" });
    const approvals = api.state.calls.filter((call) => call.path.startsWith("/approve/"));
    expect(approvals.map((call) => call.path)).toEqual(["/approve/j1"]);
    expect(approvals[0].body).toEqual({ notes: "Revisión final desde el reproductor de campaña" });
    fireEvent.keyDown(window, { key: "a" });
    await waitFor(() => expect(screen.queryByRole("dialog", { name: /Reproducir/ })).not.toBeInTheDocument());
    expect(api.state.calls.filter((call) => call.path.startsWith("/approve/")).map((call) => call.path)).toEqual(["/approve/j1", "/approve/j3"]);
    await screen.findByText(/No quedan videos por revisar en esta lista/);
  });

  it("sends approved songs with one idempotency key across a lost response", async () => {
    let attempts = 0;
    const api = createCampaignApi({
      songs: [makeSong(1, "approved"), makeSong(2, "approved"), makeSong(3, "delivered")],
      onRequest: ({ path }) => { if (path === "/batch/campaigns/c1/deliveries" && ++attempts === 1) throw new TypeError("Conexión interrumpida"); return null; },
    });
    mount(api, "/campaigns/c1?view=approved");
    await screen.findByText("Canción 2");
    fireEvent.click(screen.getByRole("button", { name: /Enviar 2 aprobadas/ }));
    const dialog = await screen.findByRole("dialog", { name: "Enviar videos aprobados" });
    fireEvent.change(within(dialog).getByLabelText("Portal de destino"), { target: { value: "chile" } });
    fireEvent.click(within(dialog).getByRole("button", { name: "Confirmar envío" }));
    await within(dialog).findByText("Conexión interrumpida");
    fireEvent.click(within(dialog).getByRole("button", { name: "Confirmar envío" }));
    await screen.findByText(/Envío iniciado para 2 videos a Chile/);
    const sends = api.state.calls.filter((call) => call.path === "/batch/campaigns/c1/deliveries");
    expect(sends).toHaveLength(2);
    expect(sends[0].body.idempotency_key).toBe(sends[1].body.idempotency_key);
    expect(sends[1].body).toMatchObject({ job_ids: ["j1", "j2"], destination_portal: "chile" });
    expect(location()).toContain("delivery_op=op1");
    await screen.findByText(/1 de 1 enviados/);
  });

  it("keeps legacy links working and edits song data from the drawer", async () => {
    const api = createCampaignApi({ songs: [makeSong(1, "audio", { metadata_error: "missing_metadata", technical_code: null }), makeSong(2, "qc")] });
    mount(api, "/campaigns/c1?view=history&video_state=review");
    await screen.findByText("Canción 2");
    expect(tab("QC video")).toHaveAttribute("aria-selected", "true");
    fireEvent.click(tab("Audio"));
    fireEvent.click(await screen.findByRole("button", { name: "Completar datos · Canción 1" }));
    const drawer = await screen.findByRole("dialog", { name: "Canción Canción 1" });
    expect(location()).toContain("song=i1");
    fireEvent.change(within(drawer).getByLabelText("Código ARF / ARUM"), { target: { value: "arf77" } });
    fireEvent.click(within(drawer).getByRole("button", { name: "Guardar datos" }));
    await within(drawer).findByText("Datos guardados.");
    expect(api.state.calls.find((call) => call.method === "PATCH" && call.path === "/batch/campaigns/c1/items/i1").body).toMatchObject({ technical_code: "arf77" });
  });

  it("art-track campaigns have no lyric stages and keep the delivery preview POST", async () => {
    const api = createCampaignApi({ kind: "art_track", songs: [makeSong(1, "qc"), makeSong(2, "rendering")] });
    mount(api, "/campaigns/c1");
    await screen.findByText("Canción 1");
    expect(screen.queryByRole("tab", { name: /^Letra/ })).not.toBeInTheDocument();
    expect(screen.queryByRole("tab", { name: /^Lista/ })).not.toBeInTheDocument();
    expect(api.state.calls.some((call) => call.path.endsWith("/review-queue") || call.path.endsWith("/creative"))).toBe(false);
    fireEvent.click(screen.getByRole("button", { name: "Configuración" }));
    fireEvent.click(await screen.findByRole("button", { name: "Carga de audios" }));
    fireEvent.click(await screen.findByRole("button", { name: "Previsualizar envíos" }));
    await screen.findByText("2 art tracks aprobados para umgchile.genly.pro.");
    expect(api.state.calls.find((call) => call.path.endsWith("/delivery-preview")).method).toBe("POST");
  });

  it("shows a banner with the next song after approving a lyric in the editor", async () => {
    const api = createCampaignApi({ songs: [makeSong(1, "ready"), makeSong(2, "lyrics")] });
    mount(api, "/campaigns/c1?view=lyrics&approved=j1");
    await screen.findByText(/Letra aprobada:/);
    fireEvent.click(screen.getByRole("button", { name: "Revisar siguiente: Canción 2" }));
    await screen.findByText("Editor de letra");
    expect(location()).toContain("/review/j2?return_to=/campaigns/c1?view=lyrics&focus=j2");
  });

  it("opens the discard dialog requested by the editor, or explains why it cannot", async () => {
    const api = createCampaignApi({ songs: [makeSong(1, "lyrics"), makeSong(2, "qc")] });
    mount(api, "/campaigns/c1?tab=all&discard=i1");
    const dialog = await screen.findByRole("dialog", { name: "Descartar canción" });
    expect(within(dialog).getByRole("heading")).toHaveTextContent("Canción 1");
    expect(location()).not.toContain("discard=");
    cleanup(); clearCampaignResourceCache();
    mount(createCampaignApi({ songs: [makeSong(2, "qc")] }), "/campaigns/c1?discard=i2");
    await screen.findByText("La canción cambió de estado y no se puede descartar desde esta revisión.");
    expect(screen.queryByRole("dialog", { name: "Descartar canción" })).not.toBeInTheDocument();
  });

  it("opens the song drawer from a shared link and closes it with Escape", async () => {
    const api = createCampaignApi({ songs: [makeSong(1, "lyrics"), makeSong(2, "qc")] });
    mount(api, "/campaigns/c1?view=lyrics&song=i2");
    const drawer = await screen.findByRole("dialog", { name: "Canción Canción 2" });
    expect(within(drawer).getByText("Video por revisar", { selector: "span" })).toBeInTheDocument();
    fireEvent.keyDown(drawer, { key: "Escape" });
    await waitFor(() => expect(screen.queryByRole("dialog")).not.toBeInTheDocument());
    expect(location()).not.toContain("song=");
  });

  it("moves through songs with the keyboard and acts with Enter", async () => {
    const api = createCampaignApi({ songs: [makeSong(1, "lyrics"), makeSong(2, "lyrics")] });
    mount(api, "/campaigns/c1?view=lyrics");
    await screen.findByText("Canción 2");
    await act(async () => { fireEvent.keyDown(window, { key: "j" }); });
    await act(async () => { fireEvent.keyDown(window, { key: "j" }); });
    const focused = document.activeElement;
    expect(focused.tagName).toBe("TR");
    expect(focused.textContent).toContain("Canción 2");
    fireEvent.keyDown(focused, { key: "x" });
    expect(await screen.findByRole("region", { name: "Acciones sobre la selección" })).toHaveTextContent("1");
    fireEvent.keyDown(focused, { key: "Enter" });
    await screen.findByText("Editor de letra");
    expect(location()).toContain("/review/j2");
  });
});
