import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import LyricsEditor from "./LyricsEditor";

vi.mock("../i18n", () => ({ useI18n: () => ({ t: (_key, fallback) => fallback }) }));
vi.mock("./OnboardingTour", () => ({ EditorTour: () => null }));
vi.mock("./ToastProvider", () => ({
  useToast: () => ({ toast: vi.fn(), dismiss: vi.fn() }),
  ToastProvider: ({ children }) => children,
}));

const JOB = "job-v2-test";
const USER = {
  id: 42,
  username: "operator",
  tenant_id: "team-a",
  features: { editor_v2: true },
};
const SERVER = [{ _id: "line-1", start: 0, end: 1, text: "versión equipo" }];
const LOCAL = [{ _id: "line-1", start: 0, end: 1, text: "versión local" }];

function reply(body, status = 200) {
  return {
    ok: status >= 200 && status < 300,
    status,
    clone: () => ({ json: async () => body }),
  };
}

function makeRequest() {
  return vi.fn(async (path, options = {}) => {
    if (path === `/editor/${JOB}` && !options.method) {
      return reply({
        job_id: JOB, revision: 5, segments: SERVER, original_segments: SERVER,
        updated_by: { id: 7, username: "teammate" }, updated_at: "2026-08-06T10:00:00Z",
        lock: { active: false },
      });
    }
    if (path.endsWith("/lock/heartbeat")) {
      return reply({ acquired: true, user: USER, expires_at: "2026-08-06T10:01:00Z" });
    }
    if (path.endsWith("/lock") && options.method === "DELETE") return reply({ released: true });
    if (path === `/editor/${JOB}` && options.method === "PATCH") {
      return reply({ revision: 6, version_id: "version-6", saved_at: "2026-08-06T10:02:00Z", applied: true });
    }
    if (path.endsWith("/conflicts/resolve")) {
      const body = JSON.parse(options.body);
      const localWins = body.strategy === "save_local_as_new";
      return reply({
        job_id: JOB,
        revision: localWins ? 6 : 5,
        segments: localWins ? LOCAL : SERVER,
        original_segments: SERVER,
        updated_by: USER,
        updated_at: "2026-08-06T10:02:00Z",
        lock: { active: true, user: USER },
      });
    }
    return reply({}, 404);
  });
}

function renderEditor(editorRequest, props = {}) {
  return render(<LyricsEditor
    segments={SERVER}
    filename="song.wav"
    user={USER}
    transcribeJobId={JOB}
    storeKey={`${JOB}-${Math.random()}`}
    editorRequest={editorRequest}
    onPersistSegments={vi.fn()}
    onApprove={props.onApprove || vi.fn()}
    onBack={vi.fn()}
  />);
}

beforeEach(() => {
  localStorage.clear();
  localStorage.setItem(
    `genly_editor_draft:team-a:42:${JOB}`,
    JSON.stringify({ segments: LOCAL, base_revision: 4, updated_at: "2026-08-06T09:59:00Z" }),
  );
});

afterEach(() => {
  cleanup();
  localStorage.clear();
});

describe("Editor 2.0 stale draft recovery", () => {
  it("explains a failed durable load and unblocks only after an explicit retry succeeds", async () => {
    localStorage.clear();
    let loadAttempts = 0;
    const request = vi.fn(async (path, options = {}) => {
      if (path === `/editor/${JOB}` && !options.method) {
        loadAttempts += 1;
        if (loadAttempts === 1) return reply({ detail: "Job not found." }, 404);
        return reply({
          job_id: JOB, revision: 5, segments: SERVER, original_segments: SERVER,
          updated_by: null, updated_at: "2026-08-06T10:00:00Z",
          lock: { active: false },
        });
      }
      if (path.endsWith("/lock/heartbeat")) return reply({ acquired: true, user: USER });
      if (path.endsWith("/lock") && options.method === "DELETE") return reply({ released: true });
      if (path === "/analytics/events") return reply({ accepted: 1, rejected: 0 });
      return reply({}, 404);
    });
    renderEditor(request);

    const loadError = await screen.findByRole("alertdialog", {
      name: "No pudimos abrir la versión editable",
    });
    expect(screen.getByRole("button", { name: /Aprobar y generar/i })).toBeDisabled();
    expect(request.mock.calls.some(([path]) => path.endsWith("/lock/heartbeat"))).toBe(false);

    fireEvent.click(screen.getByRole("button", { name: "Reintentar" }));
    await waitFor(() => expect(loadError).not.toBeInTheDocument());
    expect(await screen.findByDisplayValue("versión equipo")).toBeInTheDocument();
    await waitFor(() => expect(screen.getByRole("button", { name: /Aprobar y generar/i })).toBeEnabled());
    expect(request.mock.calls.some(([path]) => path.endsWith("/lock/heartbeat"))).toBe(true);
  });

  it("never autosaves a stale draft and can explicitly use the team version", async () => {
    const request = makeRequest();
    renderEditor(request);

    expect(await screen.findByRole("dialog", { name: /Hay una versión más nueva/i })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: /Aprobar y generar/i })).toBeDisabled();
    expect(screen.getByDisplayValue("versión local")).toBeInTheDocument();
    expect(request.mock.calls.filter(([path, options]) => path === `/editor/${JOB}` && options?.method === "PATCH")).toHaveLength(0);

    fireEvent.click(screen.getByRole("button", { name: "Usar versión del equipo" }));
    await waitFor(() => expect(screen.queryByRole("dialog", { name: /Hay una versión más nueva/i })).not.toBeInTheDocument());
    expect(screen.getByDisplayValue("versión equipo")).toBeInTheDocument();
    expect(localStorage.getItem(`genly_editor_draft:team-a:42:${JOB}`)).toBeNull();
  });

  it("saves the local copy only through the explicit conflict strategy", async () => {
    const request = makeRequest();
    renderEditor(request);
    await screen.findByRole("dialog", { name: /Hay una versión más nueva/i });

    fireEvent.click(screen.getByRole("button", { name: "Guardar mi versión como nueva revisión" }));
    await waitFor(() => expect(screen.queryByRole("dialog", { name: /Hay una versión más nueva/i })).not.toBeInTheDocument());
    expect(screen.getByDisplayValue("versión local")).toBeInTheDocument();
    const resolution = request.mock.calls.find(([path]) => path.endsWith("/conflicts/resolve"));
    expect(JSON.parse(resolution[1].body)).toEqual({
      strategy: "save_local_as_new",
      server_revision: 5,
      segments: LOCAL,
    });
  });

  it("reopens conflict resolution when a teammate saves during approval", async () => {
    localStorage.clear();
    const request = makeRequest();
    const onApprove = vi.fn().mockResolvedValue({
      ok: false,
      reason: "conflict",
      conflict: {
        server_revision: 7,
        server_segments: [{ ...SERVER[0], text: "cambio remoto final" }],
        updated_by: { id: 7, username: "teammate" },
      },
    });
    renderEditor(request, { onApprove });
    await screen.findByDisplayValue("versión equipo");
    await waitFor(() => expect(screen.getByRole("button", { name: /Aprobar y generar/i })).toBeEnabled());

    fireEvent.click(screen.getByRole("button", { name: /Aprobar y generar/i }));
    expect(await screen.findByRole("dialog", { name: /Hay una versión más nueva/i })).toBeInTheDocument();
    expect(screen.getByText(/revisión del equipo es la 7/i)).toBeInTheDocument();
    expect(onApprove).toHaveBeenCalledWith(
      expect.any(Array),
      expect.objectContaining({ editorRevision: 6, editorVersionId: "version-6" }),
    );
  });
});
