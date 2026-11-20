import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import CampaignAccessModal from "./CampaignAccessModal";
const row = { id: "campaign", name: "HitsDeSiempre — Lyrics", tenant_id: "UMG", status: "active", kind: "lyric_video", granted: false, inherited: null };
let state;
beforeEach(() => {
  HTMLDialogElement.prototype.showModal = vi.fn(function () { this.setAttribute("open", ""); });
  state = { items: [{ ...row }], total: 1, user_active: true, campaigns_enabled: true };
  global.fetch = vi.fn(async (_url, opts) => {
    if (opts.method === "PUT") state.items[0].granted = JSON.parse(opts.body).enabled;
    return { ok: true, json: async () => structuredClone(state) };
  });
});
afterEach(() => { cleanup(); vi.restoreAllMocks(); });
function open() { render(<CampaignAccessModal user={{ id: 79, email: "danniivanna04@gmail.com" }} onClose={vi.fn()} />); }
it("grants an existing user access and requires confirmation to revoke", async () => {
  open();
  fireEvent.click(await screen.findByRole("button", { name: "Dar acceso" }));
  fireEvent.click(await screen.findByRole("button", { name: "Quitar acceso" }));
  expect(fetch.mock.calls.filter(([, o]) => o.method === "PUT")).toHaveLength(1);
  fireEvent.click(screen.getByRole("button", { name: "Confirmar revocación" }));
  await screen.findByRole("button", { name: "Dar acceso" });
  const writes = fetch.mock.calls.filter(([, o]) => o.method === "PUT");
  expect(writes[0][0]).toContain("/admin/users/79/campaign-access/campaign");
  expect(JSON.parse(writes[0][1].body)).toEqual({ enabled: true, expected_granted: false });
  expect(JSON.parse(writes[1][1].body)).toEqual({ enabled: false, expected_granted: true });
});
it("shows server rejection without claiming the grant succeeded", async () => {
  const normal = fetch.getMockImplementation();
  fetch.mockImplementation((url, opts) => opts.method === "PUT" ? Promise.resolve({ ok: false, status: 409, json: async () => ({ detail: "El permiso cambió" }) }) : normal(url, opts));
  open(); fireEvent.click(await screen.findByRole("button", { name: "Dar acceso" }));
  expect(await screen.findByRole("alert")).toHaveTextContent("El permiso cambió");
  expect(screen.queryByText(/Acceso habilitado:/)).toBeNull();
  await screen.findByRole("button", { name: "Dar acceso" });
});
it("distinguishes inherited access and disables new grants when campaigns are unavailable", async () => {
  state.items.push({ ...row, id: "own", name: "Equipo propio", inherited: "team" });
  state.campaigns_enabled = false;
  open();
  expect(await screen.findByRole("button", { name: "Dar acceso" })).toBeDisabled();
  expect(screen.getByText("Acceso por pertenecer al equipo")).toBeInTheDocument();
  expect(screen.queryByRole("button", { name: "Quitar acceso" })).toBeNull();
});
it("sends searches and filters to the server and paginates", async () => {
  state.total = 45;
  open(); fireEvent.click(await screen.findByRole("button", { name: "Siguiente" }));
  await waitFor(() => expect(fetch.mock.calls.at(-1)[0]).toContain("offset=30"));
  await screen.findByRole("button", { name: "Anterior" });
  fireEvent.change(screen.getByRole("searchbox"), { target: { value: "Septiembre" } });
  fireEvent.click(screen.getByRole("checkbox"));
  await waitFor(() => {
    expect(fetch.mock.calls.at(-1)[0]).toContain("search=Septiembre");
    expect(fetch.mock.calls.at(-1)[0]).toContain("granted_only=true");
    expect(fetch.mock.calls.at(-1)[0]).toContain("offset=0");
  });
});
