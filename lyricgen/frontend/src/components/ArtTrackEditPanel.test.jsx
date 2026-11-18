import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import ArtTrackEditPanel from "./ArtTrackEditPanel";

vi.mock("../i18n", () => ({ useI18n: () => ({ t: () => "" }) }));

const job = { job_id: "art-track", artist: "Enrique Iglesias", song_title: "¿Dónde Están Corazón?", render_params: { art_track: true } };
let post;

beforeEach(() => {
  post = vi.fn();
  vi.stubGlobal("fetch", vi.fn((url, options) => options?.method === "POST"
    ? post(url, options)
    : Promise.resolve({ ok: true, json: async () => ({ url: "https://example.test/cover.jpg" }) })));
  vi.stubGlobal("URL", Object.assign(URL, { createObjectURL: vi.fn(() => "blob:new-cover"), revokeObjectURL: vi.fn() }));
});

afterEach(() => { cleanup(); vi.unstubAllGlobals(); vi.restoreAllMocks(); });

it("retains the complete draft after a structured 503 and submits only an explicit retry", async () => {
  const onEdited = vi.fn();
  post.mockResolvedValueOnce({ ok: false, status: 503, json: async () => ({ detail: {
    code: "art_track_save_unconfirmed", message: "No pudimos confirmar el guardado. Revisá el estado del video antes de reintentar.",
  } }) }).mockResolvedValueOnce({ ok: true, json: async () => ({ status: "editing" }) });
  const { container } = render(<ArtTrackEditPanel job={job} onEdited={onEdited} />);
  fireEvent.change(screen.getByPlaceholderText("Artista"), { target: { value: "Coti Ft. Enrique Iglesias" } });
  fireEvent.change(screen.getByPlaceholderText("Título"), { target: { value: "Título corregido" } });
  fireEvent.change(container.querySelector('input[type="file"]'), { target: { files: [new File(["cover"], "new-cover.jpg", { type: "image/jpeg" })] } });
  fireEvent.click(screen.getByRole("button", { name: "Guardar y re-renderizar" }));
  expect(await screen.findByText(/No pudimos confirmar el guardado/)).toBeTruthy();
  expect(screen.getByPlaceholderText("Artista").value).toBe("Coti Ft. Enrique Iglesias");
  expect(screen.getByPlaceholderText("Título").value).toBe("Título corregido");
  expect(screen.getByText("new-cover.jpg")).toBeTruthy();
  expect(post).toHaveBeenCalledTimes(1);
  expect(onEdited).not.toHaveBeenCalled();
  fireEvent.click(screen.getByRole("button", { name: "Guardar y re-renderizar" }));
  await waitFor(() => expect(onEdited).toHaveBeenCalledWith({ status: "editing", edit_type: "art_track" }));
  const draft = post.mock.calls[1][1].body;
  expect(draft.get("artist")).toBe("Coti Ft. Enrique Iglesias");
  expect(draft.get("song_title")).toBe("Título corregido");
  expect(draft.get("background_file").name).toBe("new-cover.jpg");
});

it("explains an uncertain network failure without automatically resubmitting", async () => {
  post.mockRejectedValue(new TypeError("Failed to fetch"));
  const onEdited = vi.fn();
  render(<ArtTrackEditPanel job={job} onEdited={onEdited} />);
  fireEvent.change(screen.getByPlaceholderText("Artista"), { target: { value: "Coti" } });
  fireEvent.click(screen.getByRole("button", { name: "Guardar y re-renderizar" }));
  expect(await screen.findByText(/revisá el estado del video antes de reintentar/)).toBeTruthy();
  expect(screen.getByPlaceholderText("Artista").value).toBe("Coti");
  expect(post).toHaveBeenCalledTimes(1);
  expect(onEdited).not.toHaveBeenCalled();
});

it("sends a single render when save is clicked twice before React updates", async () => {
  let finish;
  post.mockImplementation(() => new Promise(resolve => { finish = resolve; }));
  const onEdited = vi.fn();
  render(<ArtTrackEditPanel job={job} onEdited={onEdited} />);
  const save = screen.getByRole("button", { name: "Guardar y re-renderizar" });
  act(() => { save.click(); save.click(); });
  expect(post).toHaveBeenCalledTimes(1);
  await act(async () => finish({ ok: true, json: async () => ({ status: "editing" }) }));
  expect(onEdited).toHaveBeenCalledTimes(1);
});
