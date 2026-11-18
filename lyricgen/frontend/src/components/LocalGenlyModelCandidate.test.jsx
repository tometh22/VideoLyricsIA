import { cleanup, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, expect, it, vi } from "vitest";
import LocalGenlyModelCandidate from "./LocalGenlyModelCandidate";

afterEach(cleanup);
const baseline = [{ text: "Una canción", start: 1, end: 3 }];
const response = (body, status = 200) => Promise.resolve({ ok: status < 400, status, json: async () => body });
const props = { jobId: "song", revision: 0, segments: baseline, enabled: true };

it("espera la revisión del editor sin fallar cuando todavía no hay candidata", async () => {
  const request = vi.fn(path => response(path.endsWith("status")
    ? { worker_available: true, request: null } : { candidate: null }));
  render(<LocalGenlyModelCandidate {...props} revision={undefined} request={request} />);
  expect(await screen.findByRole("button", { name: "Analizar canción completa" })).toBeDisabled();
});

it("oculta el piloto cuando no está habilitado en el servidor", async () => {
  const request = vi.fn(() => response({}, 404));
  const { container } = render(<LocalGenlyModelCandidate {...props} request={request} />);
  await waitFor(() => expect(request).toHaveBeenCalledOnce());
  expect(container.innerHTML).toBe("");
});

it("muestra procesador desconectado sin prometer que está analizando", async () => {
  const request = vi.fn(path => response(path.endsWith("status")
    ? { worker_available: false, request: null } : { candidate: null }));
  render(<LocalGenlyModelCandidate {...props} request={request} />);
  expect(await screen.findByText(/procesador local está desconectado/)).toBeInTheDocument();
  expect(screen.getByRole("button", { name: "Analizar canción completa" })).toBeDisabled();
});

it("envía solicitud explícita con la revisión actual", async () => {
  let started = false;
  const request = vi.fn((path, options) => {
    if (options?.method === "POST") {
      started = true;
      return response({ worker_available: true, request: { status: "queued" } });
    }
    return response(path.endsWith("status") ? { worker_available: true,
      request: started ? { status: "queued" } : null } : { candidate: null });
  });
  render(<LocalGenlyModelCandidate {...props} request={request} />);
  await userEvent.click(await screen.findByRole("button", { name: "Analizar canción completa" }));
  expect(request).toHaveBeenCalledWith("/editor/song/genly-model/request", expect.objectContaining({
    method: "POST", body: JSON.stringify({ base_revision: 0 }),
  }));
  expect(await screen.findByText("En espera")).toBeInTheDocument();
});

it("muestra toda la candidata sin cambios y permite escuchar", async () => {
  const onSeek = vi.fn();
  const candidate = { source: { segments_revision: 0 }, baseline, segments: baseline, changes: [],
    status: "complete", candidate_sha256: "hash", model_id: "model-v1" };
  const request = vi.fn(path => response(path.endsWith("status")
    ? { worker_available: true, request: null } : { candidate }));
  render(<LocalGenlyModelCandidate {...props} request={request} onSeek={onSeek} />);
  expect(await screen.findByText(/Se recorrió el audio completo/)).toBeInTheDocument();
  await userEvent.click(screen.getByText(/Ver letra y tiempos de toda la candidata/));
  await userEvent.click(screen.getByRole("button", { name: "Escuchar candidata línea 1" }));
  expect(onSeek).toHaveBeenCalledWith(0);
  expect(screen.queryByRole("button", { name: /aplicar|aprobar/i })).toBeNull();
});

it("no muestra propuesta de otra versión como aplicable", async () => {
  const candidate = { source: { segments_revision: 9 }, baseline, segments: baseline, changes: [],
    status: "complete", candidate_sha256: "hash", model_id: "model-v1" };
  const request = vi.fn(path => response(path.endsWith("status")
    ? { worker_available: true, request: null } : { candidate }));
  const onProposalReady = vi.fn();
  render(<LocalGenlyModelCandidate {...props} request={request} onProposalReady={onProposalReady} />);
  expect(await screen.findByText(/corresponde a otra versión/)).toBeInTheDocument();
  expect(onProposalReady).not.toHaveBeenCalled();
});
