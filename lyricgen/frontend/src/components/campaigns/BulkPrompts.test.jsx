import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import BulkPrompts, { composePrompt } from "./BulkPrompts";
import BulkActionBar from "./BulkActionBar";
import { campaignPost } from "../../lib/campaignApi";
vi.mock("../../lib/campaignApi", () => ({ campaignPost: vi.fn() }));
const items = [
  { id: "second", ordinal: 2, title: "Vean", artist: "Rescate", status: "lyrics_approved", settings: {} },
  { id: "first", ordinal: 1, title: "Buscando Lio", artist: "Rescate", status: "lyrics_approved", settings: {} },
];
const creative = { plan: { revision: 3 }, items };
beforeEach(() => {
  vi.clearAllMocks();
  campaignPost.mockImplementation(async (path, body) => path.endsWith("prompts-preview") ? {
    preview_id: "frozen-prompts", skipped: [], changes: body.songs.map(song => ({ item_id: song.item_id,
      title: items.find(item => item.id === song.item_id).title, after: { background_hint: composePrompt(body.common, song.scene) } })),
  } : { revision: 4 });
});
afterEach(cleanup);
const show = (props = {}) => render(<BulkPrompts campaignId="c1" creative={creative} itemIds={["second", "first"]} {...props} />);
const distribute = () => {
  fireEvent.change(screen.getByLabelText("Dirección visual común"), { target: { value: "Rock, luz y ciudad" } });
  fireEvent.change(screen.getByLabelText("Lista de fondos"), { target: { value: "Avenida nocturna\nTerraza al amanecer" } });
  fireEvent.click(screen.getByRole("button", { name: "Repartir fondos" }));
};
describe("bulk prompt editing", () => {
  it("maps scenes in calendar order and only applies the frozen preview after confirmation", async () => {
    const saved = vi.fn(); show({ onSaved: saved });
    expect(screen.getByRole("button", { name: "Revisar 2 prompts" })).toBeDisabled();
    distribute();
    expect(screen.getByLabelText("Fondo para Buscando Lio")).toHaveValue("Avenida nocturna");
    expect(screen.getByLabelText("Fondo para Vean")).toHaveValue("Terraza al amanecer");
    expect(campaignPost).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole("button", { name: "Revisar 2 prompts" }));
    await screen.findByRole("button", { name: "Guardar 2 prompts" });
    expect(campaignPost.mock.calls[0][1]).toMatchObject({ revision: 3, source: "prompt_improved", common: "Rock, luz y ciudad", songs: [
      { item_id: "first", scene: "Avenida nocturna" }, { item_id: "second", scene: "Terraza al amanecer" },
    ] });
    expect(saved).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole("button", { name: "Guardar 2 prompts" }));
    await waitFor(() => expect(saved).toHaveBeenCalledOnce());
    expect(campaignPost.mock.calls[1]).toEqual(["/batch/campaigns/c1/creative/apply", { preview_id: "frozen-prompts" }]);
    expect(campaignPost.mock.calls).toHaveLength(2);
  });
  it("invalidates a reviewed preview on editing and warns about repeated prompts", async () => {
    show(); distribute();
    fireEvent.click(screen.getByRole("button", { name: "Revisar 2 prompts" }));
    await screen.findByRole("button", { name: "Guardar 2 prompts" });
    fireEvent.change(screen.getByLabelText("Fondo para Vean"), { target: { value: "Avenida nocturna" } });
    expect(screen.queryByRole("button", { name: "Guardar 2 prompts" })).not.toBeInTheDocument();
    expect(screen.getByText(/Hay 1 prompt repetido/)).toBeInTheDocument();
  });
  it("requires an exact scene count and retains pinned designs unless explicitly included", () => {
    show({ creative: { ...creative, items: items.map(item => ({ ...item, assignment: { pinned: item.id === "first" } })) } });
    expect(screen.queryByLabelText("Fondo para Buscando Lio")).not.toBeInTheDocument();
    fireEvent.change(screen.getByLabelText("Lista de fondos"), { target: { value: "Una escena\nOtra escena" } });
    expect(screen.getByRole("button", { name: "Repartir fondos" })).toBeDisabled();
    fireEvent.click(screen.getByRole("checkbox", { name: /También cambiar/ }));
    expect(screen.getByLabelText("Fondo para Buscando Lio")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Repartir fondos" })).toBeEnabled();
  });
  it("does not claim a save when the server rejects a stale preview", async () => {
    const saved = vi.fn(); show({ onSaved: saved }); distribute();
    fireEvent.click(screen.getByRole("button", { name: "Revisar 2 prompts" }));
    await screen.findByRole("button", { name: "Guardar 2 prompts" });
    campaignPost.mockRejectedValueOnce(new Error("Cambió una canción"));
    fireEvent.click(screen.getByRole("button", { name: "Guardar 2 prompts" }));
    await screen.findByText("Cambió una canción");
    expect(saved).not.toHaveBeenCalled();
    expect(screen.queryByText(/Prompts guardados/)).not.toBeInTheDocument();
  });
  it("exposes the bulk action only for editable visible songs with manager access and API support", () => {
    const songs = [{ id: "ready", stage: "ready" }, { id: "rendered", stage: "delivered" }];
    const action = vi.fn();
    const { rerender } = render(<BulkActionBar songs={songs} kind="lyric_video" canManage canEditPrompts onAction={action} />);
    fireEvent.click(screen.getByRole("button", { name: "Editar prompts (1)" }));
    expect(action).toHaveBeenCalledWith("prompts", [songs[0]]);
    rerender(<BulkActionBar songs={songs} kind="lyric_video" canManage onAction={action} />);
    expect(screen.queryByRole("button", { name: /Editar prompts/ })).not.toBeInTheDocument();
    rerender(<BulkActionBar songs={songs} kind="lyric_video" canManage={false} canEditPrompts onAction={action} />);
    expect(screen.queryByRole("button", { name: /Editar prompts/ })).not.toBeInTheDocument();
  });
});
