import { act, cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, expect, it, vi } from "vitest";
import { I18nProvider } from "../../i18n";
import { MemoryRouter } from "react-router-dom";
import BatchDesign, { projectBatchBalance } from "./BatchMixDesigner";
import { campaignPost, campaignRequest } from "../../lib/campaignApi";
vi.mock("../../lib/campaignApi", () => ({ campaignPost: vi.fn(), campaignRequest: vi.fn() }));

export const mixCreative = {
  plan: { revision: 4 }, portal_id: "argentina", can_view_account_balance: true, can_manage: true,
  mix_catalog: { looks: ["pincel", "cosmico", "cine"].map((id) => ({ id, compatible: true })).concat([{ id: "cinetico", compatible: false, reason: "Puede reemplazar el fondo" }]),
    effects: ["bokeh", "embers", "rain"], editable_statuses: ["lyrics_approved", "transcribed_pending"] },
  items: Array.from({ length: 50 }, (_, i) => ({ id: `i${i}`, status: "lyrics_approved", assignment: {} })),
};
const response = () => ({ preview_id: "frozen", skipped: [], ready_item_ids: mixCreative.items.map((i) => i.id),
  mix: { total: 50, photo_effect: 25, veo: 25, looks: { pincel: 17, cosmico: 17, cine: 16 }, effects: { bokeh: 9, embers: 8, rain: 8 } },
  changes: mixCreative.items.map((item, i) => ({ item_id: item.id, artist: "Artista", title: `Tema ${i}`, after: {
    lyric_look: ["pincel", "cosmico", "cine"][i % 3], effect: ["bokeh", "embers", "rain"][i % 3], movement_style: i < 25 ? "foto-parallax" : "estandar",
  } })),
});
const balance = () => ({ name: "Universal Argentina", projected: { photo_effect: 10, veo: 40, unknown: 50 }, warnings: [],
  items: mixCreative.items.map((item) => ({ item_id: item.id, campaign_id: "c1", stage: "pending", type: "unknown" })),
});
function setup(props = {}) {
  localStorage.setItem("genly_lang", "es");
  campaignRequest.mockResolvedValue(balance());
  campaignPost.mockImplementation(async (path) => path.endsWith("mix-preview") ? response() : { revision: 5 });
  return render(<I18nProvider><MemoryRouter><BatchDesign campaignId="c1" creative={mixCreative} itemIds={mixCreative.items.map((i) => i.id)} {...props} /></MemoryRouter></I18nProvider>);
}
const reviewBatch = () => {
  fireEvent.click(screen.getByRole("button", { name: "2. Looks" }));
  fireEvent.click(screen.getByRole("button", { name: /Revisar reparto de/ }));
};
afterEach(() => { cleanup(); vi.clearAllMocks(); });

it("offers visual multi-selects and 25/25 before any mutation, excludes unsafe looks", () => {
  setup();
  expect(screen.getByLabelText("25 fotos y 25 videos")).toBeInTheDocument();
  fireEvent.click(screen.getByRole("button", { name: "2. Looks" }));
  expect(screen.getByRole("button", { name: "Look Cinético" })).toBeDisabled();
  fireEvent.click(screen.getByRole("button", { name: "Look Pincel" }));
  expect(screen.getByRole("button", { name: "Look Pincel" })).toHaveAttribute("aria-pressed", "false");
  fireEvent.click(screen.getByRole("button", { name: "1. Fondos y efectos" }));
  fireEvent.click(screen.getByRole("button", { name: "Todas con video" }));
  expect(screen.queryByRole("region", { name: "Efectos para las fotos" })).not.toBeInTheDocument();
  expect(campaignPost).not.toHaveBeenCalled();
});

it("shows exact review, account impact and generates only after an explicit confirmation", async () => {
  const prepare = vi.fn(), saved = vi.fn(); setup({ onSaved: saved, onPrepareGeneration: prepare });
  fireEvent.click(screen.getByRole("button", { name: "Lluvia" }));
  reviewBatch();
  const review = await screen.findByRole("region", { name: "Revisar lote" });
  expect(within(review).getByText(/50 canciones/)).toBeInTheDocument();
  expect(campaignPost.mock.calls[0][1]).toMatchObject({ photo_percent: 50, looks: ["pincel", "cosmico", "cine"], effects: ["bokeh", "embers", "rain"], revision: 4 });
  expect(prepare).not.toHaveBeenCalled();
  await screen.findByText(/no corrige necesariamente el saldo acumulado/);
  const row = screen.getByRole("row", { name: "Con este lote 35 65 0" }); expect(row).toBeInTheDocument();
  fireEvent.click(screen.getByRole("button", { name: "Generar 50 videos" }));
  await waitFor(() => expect(prepare).toHaveBeenCalledWith({ startImmediately: true, itemIds: response().ready_item_ids, expectedRevision: 5 }));
  expect(saved).toHaveBeenCalledOnce();
  expect(campaignPost.mock.calls[1]).toEqual(["/batch/campaigns/c1/creative/apply", { preview_id: "frozen" }]);
});

it("invalidates a reviewed preview when choices or selection change", async () => {
  const { rerender } = setup({ onPrepareGeneration: vi.fn() });
  reviewBatch();
  await screen.findByRole("button", { name: "Generar 50 videos" });
  fireEvent.click(screen.getByRole("button", { name: "2. Looks" }));
  fireEvent.click(screen.getByRole("button", { name: "Look Cine" }));
  expect(screen.queryByRole("button", { name: "Generar 50 videos" })).not.toBeInTheDocument();
  reviewBatch();
  await screen.findByRole("region", { name: "Revisar lote" });
  rerender(<I18nProvider><MemoryRouter><BatchDesign campaignId="c1" creative={mixCreative} itemIds={["i0"]} /></MemoryRouter></I18nProvider>);
  expect(screen.queryByRole("region", { name: "Revisar lote" })).not.toBeInTheDocument();
});

it("reshuffles with a new seed and requires a new review", async () => {
  setup();
  reviewBatch();
  fireEvent.click(await screen.findByRole("button", { name: "Mezclar de nuevo" }));
  expect(screen.queryByRole("button", { name: "Guardar reparto" })).not.toBeInTheDocument();
  reviewBatch();
  await screen.findByRole("button", { name: "Guardar reparto" });
  expect(campaignPost.mock.calls[0][1].seed).not.toBe(campaignPost.mock.calls[1][1].seed);
  expect(campaignPost.mock.calls[1][1].photo_percent).toBe(50);
});

it("makes pins and already rendered exclusions visible; zero look/effect blocks review", () => {
  setup({ creative: { ...mixCreative, items: [{ id: "i0", status: "done" }, { id: "i1", status: "lyrics_approved", assignment: { pinned: true } }] }, itemIds: ["i0", "i1"] });
  expect(screen.getByText(/1 seleccionadas no admiten/)).toBeInTheDocument();
  expect(screen.getByText(/1 diseños fijados/)).toBeInTheDocument();
  expect(screen.getByRole("button", { name: "Elegir looks →" })).toBeDisabled();
  fireEvent.click(screen.getByRole("checkbox", { name: "También cambiar los diseños fijados" }));
  expect(screen.getByLabelText("1 fotos y 0 videos")).toBeInTheDocument();
  fireEvent.click(screen.getByRole("button", { name: "2. Looks" }));
  fireEvent.click(screen.getByRole("button", { name: "Quitar todos" }));
  expect(screen.getByRole("button", { name: "Revisar reparto de 1 canciones" })).toBeDisabled();
});

it("does not start generation when apply fails and shows unavailable account data honestly", async () => {
  const prepare = vi.fn(); setup({ onPrepareGeneration: prepare });
  campaignRequest.mockRejectedValue(new Error("unavailable"));
  campaignPost.mockImplementation(async (path) => { if (path.endsWith("apply")) throw new Error("Cambió la campaña"); return response(); });
  reviewBatch();
  await screen.findByText(/No pudimos verificar el impacto/);
  fireEvent.click(screen.getByRole("button", { name: "Generar 50 videos" }));
  await screen.findByText("Cambió la campaña");
  expect(prepare).not.toHaveBeenCalled();
});

it("does not include pinned or unready songs in the confirmed generation", async () => {
  const prepare = vi.fn(); setup({ onPrepareGeneration: prepare });
  campaignPost.mockImplementation(async (path) => path.endsWith("mix-preview") ? { ...response(), skipped: ["i49"], ready_item_ids: ["i0", "i1"], mix: { ...response().mix, total: 49 } } : { revision: 5 });
  reviewBatch();
  await screen.findByText(/2 listas para generar/);
  fireEvent.click(screen.getByRole("button", { name: "Generar 2 videos" }));
  await waitFor(() => expect(prepare).toHaveBeenCalledWith({ startImmediately: true, itemIds: ["i0", "i1"], expectedRevision: 5 }));
});

it("projects by replacing existing pending assignments, never double counts other campaigns", () => {
  const report = balance();
  report.items[0].type = "veo"; report.projected.unknown--; report.projected.veo++;
  expect(projectBatchBalance(report, "c1", response().changes)).toEqual({ photo_effect: 35, veo: 65, unknown: 0 });
  report.items[0].campaign_id = 'another';
  expect(projectBatchBalance(report, "c1", response().changes)).toBeNull();
});
