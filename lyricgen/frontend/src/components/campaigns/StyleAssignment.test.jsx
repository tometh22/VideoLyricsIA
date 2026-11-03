import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import StyleAssignment from "./StyleAssignment";
import { campaignPost, campaignRequest } from "../../lib/campaignApi";
vi.mock("../../i18n", () => ({ useI18n: () => ({ t: () => "" }) }));
vi.mock("../../lib/campaignApi", () => ({ campaignPost: vi.fn(), campaignRequest: vi.fn() }));
vi.mock("./StyleControls", () => ({ default: () => <div>Ajustes avanzados</div>, StylePreview: () => null,
  SOURCE_LABELS: { lyrics: "Inspirado en la letra", auto: "Automático", prompt_literal: "Prompt exacto", prompt_improved: "Prompt mejorado con IA" } }));
const items = Array.from({ length: 10 }, (_, i) => ({ id: `i${i}`, status: "lyrics_approved" }));
const creative = { plan: { revision: 3 }, fields: {}, operations: [], items };
const ids = items.map(item => item.id);
beforeEach(() => {
  vi.clearAllMocks();
  campaignRequest.mockResolvedValue([]);
  campaignPost.mockImplementation(async (path, body) => path.endsWith("/preview") ? {
    preview_id: "frozen", counts: body.groups.length === 2 ? [5, 5] : [10], rounded: false, skipped: [],
    changes: ids.map((id, n) => ({ item_id: id, title: id, group: body.groups[n % body.groups.length].name, before: {}, after: {} })),
  } : { revision: 4 });
});
afterEach(cleanup);
describe("photo assignment", () => {
  it("previews a lyrics-inspired 50/50 photo split with no note, then saves before preparing generation", async () => {
    const sequence = [];
    const saved = vi.fn(async () => { sequence.push("saved"); });
    const prepare = vi.fn(async () => { sequence.push("prepare"); });
    render(<StyleAssignment campaignId="c1" creative={creative} itemIds={ids} onSaved={saved} onPrepareGeneration={prepare} />);
    fireEvent.click(screen.getByRole("button", { name: /Foto fija/ }));
    fireEvent.click(screen.getByRole("button", { name: "Mitad y mitad" }));
    expect(screen.getByLabelText("Efecto de foto del grupo 1")).not.toBeVisible();
    expect(screen.getByLabelText("Efecto de foto del grupo 1")).toHaveValue("bokeh");
    expect(screen.getByLabelText("Efecto de foto del grupo 2")).toHaveValue("embers");
    expect(screen.getByLabelText("Inspiración de foto del grupo 1")).toHaveValue("lyrics");
    fireEvent.click(screen.getByRole("button", { name: "Continuar" }));
    await screen.findByRole("button", { name: "Generar 10 videos" });
    const body = campaignPost.mock.calls[0][1];
    expect(body.reason).toBe("Configuración de estilos y reparto desde la campaña");
    expect(body.groups.map(group => group.weight)).toEqual([50, 50]);
    expect(body.groups.every(group => group.settings.background_id === null && group.settings.animate_image === false && group.settings.enable_scenes === false)).toBe(true);
    expect(prepare).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole("button", { name: "Generar 10 videos" }));
    await waitFor(() => expect(prepare).toHaveBeenCalledOnce());
    expect(campaignPost.mock.calls[1]).toEqual(["/batch/campaigns/c1/creative/apply", { preview_id: "frozen" }]);
    expect(sequence).toEqual(["saved", "prepare"]);
    expect(prepare).toHaveBeenCalledWith({ startImmediately: true });
    expect(campaignPost.mock.calls.some(([path]) => path === "/generate")).toBe(false);
  });
  it("shows why preview is blocked and exposes custom photo descriptions", async () => {
    render(<StyleAssignment campaignId="c1" creative={creative} itemIds={ids} />);
    fireEvent.click(screen.getByRole("button", { name: /Foto fija/ }));
    fireEvent.click(screen.getByRole("button", { name: "Chispas" }));
    fireEvent.click(screen.getByText("Opciones avanzadas"));
    fireEvent.change(screen.getByLabelText("Cantidad del grupo 1"), { target: { value: "50" } });
    expect(screen.getByText("Los porcentajes deben sumar 100%")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Continuar" })).toBeDisabled();
    fireEvent.change(screen.getByLabelText("Cantidad del grupo 1"), { target: { value: "100" } });
    fireEvent.change(screen.getByLabelText("Motivo del cambio"), { target: { value: "a" } });
    expect(screen.getByText(/Escribí al menos 3 caracteres/)).toBeInTheDocument();
    fireEvent.change(screen.getByLabelText("Motivo del cambio"), { target: { value: "" } });
    fireEvent.change(screen.getByLabelText("Inspiración de foto del grupo 1"), { target: { value: "prompt_literal" } });
    expect(screen.getByText(/Escribí la descripción del fondo/)).toBeInTheDocument();
    fireEvent.change(screen.getByLabelText("Descripción de foto del grupo 1"), { target: { value: "Un jardín al atardecer" } });
    expect(screen.getByRole("button", { name: "Continuar" })).toBeEnabled();
  });
  it("never prepares generation when the assignment fails to save", async () => {
    const prepare = vi.fn();
    campaignPost.mockImplementation(async path => {
      if (path.endsWith("/apply")) throw new Error("No se pudo guardar el diseño");
      return { preview_id: "frozen", counts: [10], rounded: false, changes: [], skipped: [] };
    });
    render(<StyleAssignment campaignId="c1" creative={creative} itemIds={ids} onPrepareGeneration={prepare} />);
    fireEvent.click(screen.getByRole("button", { name: /Foto fija/ }));
    fireEvent.click(screen.getByRole("button", { name: "Continuar" }));
    fireEvent.click(await screen.findByRole("button", { name: "Generar 10 videos" }));
    await screen.findByText("No se pudo guardar el diseño");
    expect(prepare).not.toHaveBeenCalled();
  });
  it("uses Veo Lite when the simple video choice is selected", async () => {
    render(<StyleAssignment campaignId="c1" creative={creative} itemIds={ids} />);
    fireEvent.click(screen.getByRole("button", { name: /^Video/ }));
    expect(screen.queryByRole("group", { name: "Efecto de la foto" })).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Continuar" }));
    await screen.findByRole("button", { name: "Guardar diseño" });
    expect(campaignPost.mock.calls[0][1].groups[0]).toMatchObject({ requirement: "veo", model: "veo-3.1-lite-generate-001", weight: 100 });
  });
  it("keeps the final confirmation when some selected songs retain pinned designs", async () => {
    const prepare = vi.fn();
    campaignPost.mockImplementation(async path => path.endsWith("/preview") ? {
      preview_id: "frozen", counts: [9], rounded: false, skipped: ["i9"], changes: [],
    } : { revision: 4 });
    render(<StyleAssignment campaignId="c1" creative={creative} itemIds={ids} onPrepareGeneration={prepare} />);
    fireEvent.click(screen.getByRole("button", { name: /Foto fija/ }));
    fireEvent.click(screen.getByRole("button", { name: "Continuar" }));
    fireEvent.click(await screen.findByRole("button", { name: "Revisar 10 videos" }));
    await waitFor(() => expect(prepare).toHaveBeenCalledWith({ startImmediately: false }));
  });

});
