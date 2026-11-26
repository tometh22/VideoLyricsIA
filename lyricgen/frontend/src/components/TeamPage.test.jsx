import {
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor,
  within,
} from "@testing-library/react";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { MemoryRouter } from "react-router-dom";
import TeamPage from "./TeamPage";

const task = {
  id: "item-1",
  title: "Canción de prueba",
  artist: "Artista",
  campaign_id: "campaign-1",
  campaign: "Lanzamiento",
  status: "pending",
  phase: "lyrics_ready",
  job_id: "job-1",
  assignee_id: 2,
  version: 3,
  priority: "normal",
  note: "",
  blocked_reason: "",
};
const summary = {
  can_manage: true,
  counts: { pending: 1 },
  unassigned: 0,
  campaigns: [
    { id: "campaign-1", name: "Lanzamiento", counts: { pending: 1 } },
  ],
  members: [{ id: 2, name: "Agus", active: true, counts: { pending: 1 } }],
  activity: [],
};
let request;
beforeEach(() => {
  HTMLDialogElement.prototype.showModal = function () {
    this.setAttribute("open", "");
  };
  HTMLDialogElement.prototype.close = function () {
    this.removeAttribute("open");
  };
  request = vi.fn(async (url) => ({
    ok: true,
    json: async () =>
      url.includes("/access")
        ? { can_manage: true, tenant_id: "team", tenants: ["team"] }
        : url.includes("/dashboard")
          ? summary
          : { items: [task], total: 1 },
  }));
  vi.stubGlobal("fetch", request);
});
afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});
function mount(path = "/team") {
  render(
    <MemoryRouter initialEntries={[path]}>
      <TeamPage user={{ id: 2 }} />
    </MemoryRouter>,
  );
}

it("sends a version-bound assignment and shows the server result", async () => {
  request.mockImplementation(async (url, options) => ({
    ok: true,
    json: async () =>
      options.method === "POST"
        ? { assigned: [task.id], skipped: [] }
        : url.includes("/access")
          ? { can_manage: true, tenant_id: "team" }
          : url.includes("/dashboard")
            ? summary
            : { items: [task], total: 1 },
  }));
  mount();
  fireEvent.click(
    await screen.findByRole("checkbox", {
      name: "Seleccionar Canción de prueba",
    }),
  );
  fireEvent.click(screen.getByRole("button", { name: "Asignar selección" }));
  const dialog = screen.getByRole("dialog", { name: "Asignar videos" });
  fireEvent.change(
    within(dialog).getByRole("combobox", { name: "Responsable" }),
    { target: { value: "2" } },
  );
  fireEvent.click(
    within(dialog).getByRole("button", { name: "Confirmar asignación" }),
  );
  await screen.findByText("1 videos actualizados.");
  const call = request.mock.calls.find(
    ([, options]) => options.method === "POST",
  );
  expect(JSON.parse(call[1].body)).toEqual({
    items: [{ id: "item-1", version: 3 }],
    assignee_id: 2,
    priority: "normal",
    due_date: null,
  });
});

it("keeps a conflicting assignment open with the actionable server error", async () => {
  const original = request.getMockImplementation();
  request.mockImplementation((url, options) =>
    options.method === "POST"
      ? Promise.resolve({
          ok: false,
          status: 409,
          json: async () => ({
            detail: "La asignación cambió. Actualizá la lista.",
          }),
        })
      : original(url, options),
  );
  mount();
  fireEvent.click(
    await screen.findByRole("checkbox", {
      name: "Seleccionar Canción de prueba",
    }),
  );
  fireEvent.click(screen.getByRole("button", { name: "Asignar selección" }));
  fireEvent.click(screen.getByRole("button", { name: "Confirmar asignación" }));
  expect(
    await screen.findByText("La asignación cambió. Actualizá la lista."),
  ).toBeInTheDocument();
  expect(screen.getByRole("dialog", { name: "Asignar videos" })).toBeVisible();
});

it("opens the existing editor, and never offers synthetic approval", async () => {
  mount("/my-tasks");
  fireEvent.click(await screen.findByRole("button", { name: task.title }));
  const dialog = screen.getByRole("dialog", { name: "Detalle de tarea" });
  expect(
    within(dialog).getByRole("link", { name: /Abrir revisión de letra/ }),
  ).toHaveAttribute(
    "href",
    "/review/job-1?return_to=%2Fcampaigns%2Fcampaign-1",
  );
  expect(
    within(dialog).queryByRole("button", { name: /Aprobar/ }),
  ).not.toBeInTheDocument();
  expect(
    within(dialog).getByRole("button", { name: "Informar bloqueo" }),
  ).toBeDisabled();
  expect(
    screen.queryByRole("button", { name: "Asignar campaña" }),
  ).not.toBeInTheDocument();
  await waitFor(() =>
    expect(request.mock.calls.some(([url]) => url.includes("mine=true"))).toBe(
      true,
    ),
  );
});

it("shows a failed read without fabricating empty successful data", async () => {
  request.mockImplementation(async (url) => ({
    ok: url.includes("/access"),
    status: 503,
    json: async () =>
      url.includes("/access")
        ? { can_manage: false, tenant_id: "team" }
        : { detail: "Servicio no disponible" },
  }));
  mount();
  expect(await screen.findByRole("alert")).toHaveTextContent(
    "Servicio no disponible",
  );
  expect(
    screen.queryByText("No hay videos en esta vista"),
  ).not.toBeInTheDocument();
});
