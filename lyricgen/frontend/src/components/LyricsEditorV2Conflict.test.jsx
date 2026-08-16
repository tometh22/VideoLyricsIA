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

function makeRequest({ legacyBase = false, approvalRemote = false } = {}) {
  let durableGets = 0;
  let durablePatches = 0;
  return vi.fn(async (path, options = {}) => {
    if (path === `/editor/${JOB}` && !options.method) {
      durableGets += 1;
      const remoteSegments = approvalRemote && durableGets > 1
        ? [...SERVER, { _id: "line-2", start: 1, end: 2, text: "cambio remoto independiente" }]
        : SERVER;
      return reply({
        job_id: JOB, revision: approvalRemote && durableGets > 1 ? 7 : 5,
        segments: legacyBase && durableGets === 1 ? [{ ...SERVER[0], text: "base" }] : remoteSegments,
        original_segments: legacyBase && durableGets === 1 ? [{ ...SERVER[0], text: "base" }] : SERVER,
        updated_by: { id: 7, username: "teammate" }, updated_at: "2026-08-06T10:00:00Z",
        lock: { active: false },
      });
    }
    if (legacyBase && path === `/editor/${JOB}/versions?limit=50`) {
      return reply({ versions: [{ id: "version-4", revision: 4 }] });
    }
    if (legacyBase && path === `/editor/${JOB}/versions/version-4`) {
      return reply({ segments: [{ ...SERVER[0], text: "base" }] });
    }
    if (path.endsWith("/lock/heartbeat")) {
      return reply({ acquired: true, user: USER, expires_at: "2026-08-06T10:01:00Z" });
    }
    if (path.endsWith("/lock") && options.method === "DELETE") return reply({ released: true });
    if (path === `/editor/${JOB}` && options.method === "PATCH") {
      durablePatches += 1;
      return reply({
        revision: approvalRemote && durablePatches > 1 ? 8 : 6,
        version_id: `version-${approvalRemote && durablePatches > 1 ? 8 : 6}`,
        saved_at: "2026-08-06T10:02:00Z",
        applied: true,
      });
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
  it("starts activity telemetry only after the durable lock is acquired", async () => {
    localStorage.clear();
    vi.spyOn(document, "hasFocus").mockReturnValue(true);
    let releaseLock;
    const pendingLock = new Promise((resolve) => { releaseLock = resolve; });
    const request = vi.fn(async (path, options = {}) => {
      if (path === `/editor/${JOB}` && !options.method) {
        return reply({
          job_id: JOB, revision: 0, segments: SERVER, original_segments: SERVER,
          updated_by: null, updated_at: "2026-08-06T10:00:00Z",
          lock: { active: false },
        });
      }
      if (path.endsWith("/lock/heartbeat")) return pendingLock;
      if (path.endsWith("/activity/heartbeat")) return reply({ event_id: 1, revision: 0 });
      if (path === "/analytics/events") return reply({ accepted: 1, rejected: 0 });
      if (path.endsWith("/lock") && options.method === "DELETE") return reply({ released: true });
      return reply({}, 404);
    });

    renderEditor(request);
    await waitFor(() => expect(
      request.mock.calls.some(([path]) => path.endsWith("/lock/heartbeat")),
    ).toBe(true));
    expect(request.mock.calls.some(([path]) => path.endsWith("/activity/heartbeat"))).toBe(false);

    releaseLock(reply({
      acquired: true, user: USER, expires_at: "2026-08-06T10:01:00Z",
    }));
    await waitFor(() => expect(
      request.mock.calls.some(([path]) => path.endsWith("/activity/heartbeat")),
    ).toBe(true));

    const lockCall = request.mock.calls.findIndex(([path]) => path.endsWith("/lock/heartbeat"));
    const activityCall = request.mock.calls.findIndex(([path]) => path.endsWith("/activity/heartbeat"));
    expect(activityCall).toBeGreaterThan(lockCall);
    expect(JSON.parse(request.mock.calls[activityCall][1].body)).toMatchObject({ activity_seq: 1 });
  });

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

  it("rebases a stale draft silently and keeps the local copy", async () => {
    const request = makeRequest();
    renderEditor(request);

    expect(await screen.findByDisplayValue("versión local")).toBeInTheDocument();
    expect(screen.queryByRole("dialog", { name: /Hay una versión más nueva/i })).not.toBeInTheDocument();
    await waitFor(() => expect(screen.getByRole("button", { name: /Aprobar y generar/i })).toBeEnabled());
  });

  it("recovers a legacy draft base from version history without opening a false conflict", async () => {
    localStorage.clear();
    localStorage.setItem(
      `genly_editor_draft:team-a:42:${JOB}`,
      JSON.stringify({ segments: LOCAL, base_revision: 4 }),
    );
    const request = makeRequest({ legacyBase: true });
    renderEditor(request);

    expect(await screen.findByDisplayValue("versión local")).toBeInTheDocument();
    expect(screen.queryByRole("dialog", { name: /Hay una versión más nueva/i })).not.toBeInTheDocument();
    expect(request.mock.calls.some(([path]) => path === `/editor/${JOB}/versions?limit=50`)).toBe(true);
    await waitFor(() => expect(screen.getByRole("button", { name: /Aprobar y generar/i })).toBeEnabled());
  });

  it("hydrates the oldest unversioned draft without a collaboration popup", async () => {
    localStorage.clear();
    localStorage.setItem(
      `genly_editor_draft:team-a:42:${JOB}`,
      JSON.stringify({ segments: LOCAL }),
    );
    const request = makeRequest();
    renderEditor(request);

    expect(await screen.findByDisplayValue("versión local")).toBeInTheDocument();
    expect(screen.queryByRole("dialog", { name: /Hay una versión más nueva/i })).not.toBeInTheDocument();
    await waitFor(() => expect(screen.getByRole("button", { name: /Aprobar y generar/i })).toBeEnabled());
  });

  it("does not expose a conflict resolver for an old local draft", async () => {
    const request = makeRequest();
    renderEditor(request);
    expect(await screen.findByDisplayValue("versión local")).toBeInTheDocument();
    expect(screen.queryByRole("dialog", { name: /Hay una versión más nueva/i })).not.toBeInTheDocument();
    expect(request.mock.calls.some(([path]) => path.endsWith("/conflicts/resolve"))).toBe(false);
  });

  it("retries approval after a revision race without opening a conflict dialog", async () => {
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
    await waitFor(() => expect(onApprove).toHaveBeenCalledTimes(3));
    expect(screen.queryByRole("dialog", { name: /Hay una versión más nueva/i })).not.toBeInTheDocument();
    expect(screen.queryByText(/revisión del equipo es la 7/i)).not.toBeInTheDocument();
  });

  it("preserva cambios remotos independientes al reintentar aprobación", async () => {
    localStorage.clear();
    const request = makeRequest({ approvalRemote: true });
    const onApprove = vi.fn()
      .mockResolvedValueOnce({ ok: false, reason: "conflict" })
      .mockResolvedValueOnce({ ok: true });
    renderEditor(request, { onApprove });
    await screen.findByDisplayValue("versión equipo");
    await waitFor(() => expect(screen.getByRole("button", { name: /Aprobar y generar/i })).toBeEnabled());

    fireEvent.click(screen.getByRole("button", { name: /Aprobar y generar/i }));
    await waitFor(() => expect(onApprove).toHaveBeenCalledTimes(2));
    expect(onApprove.mock.calls[1][0]).toEqual(expect.arrayContaining([
      expect.objectContaining({ text: "versión equipo" }),
      expect.objectContaining({ text: "cambio remoto independiente" }),
    ]));
    expect(screen.queryByRole("dialog", { name: /Hay una versión más nueva/i })).not.toBeInTheDocument();
  });
});
