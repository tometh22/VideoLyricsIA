import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import LyricsEditor from "./LyricsEditor";
import { segmentsStore } from "../state/segmentsStore";

vi.mock("../i18n", () => ({
  useI18n: () => ({ t: (_key, fallback) => fallback }),
}));
vi.mock("./OnboardingTour", () => ({ EditorTour: () => null }));
vi.mock("./ToastProvider", () => ({
  useToast: () => ({ toast: vi.fn(), dismiss: vi.fn() }),
  ToastProvider: ({ children }) => children,
}));

const JOB = "undo-ui-job";
const USER = {
  id: 42, username: "operator", tenant_id: "undo-team",
  features: { editor_v2: true },
};
const REPAIRED = [{ _id: "line-1", start: 0, end: 1, text: "versión automática" }];
const SOURCE = [{ _id: "line-1", start: 0, end: 1, text: "versión anterior" }];

function reply(body, status = 200) {
  return {
    ok: status >= 200 && status < 300,
    status,
    clone: () => ({ json: async () => body }),
  };
}

function makeRequest() {
  let current = {
    job_id: JOB, revision: 0, segments: REPAIRED,
    original_segments: REPAIRED,
    auto_repair_undo_available: true,
    lock: { active: false },
  };
  return vi.fn(async (path, options = {}) => {
    if (path === `/editor/${JOB}` && !options.method) return reply(current);
    if (path.endsWith("/lock/heartbeat")) return reply({ acquired: true, user: USER });
    if (path.endsWith("/lock") && options.method === "DELETE") return reply({ released: true });
    if (path === `/editor/${JOB}/auto-repair/undo`) {
      current = {
        ...current, revision: 1, segments: SOURCE,
        auto_repair_undo_available: false,
      };
      return reply(current);
    }
    return reply({}, 404);
  });
}

function renderEditor(editorRequest, overrides = {}) {
  return render(<LyricsEditor
    segments={REPAIRED}
    filename="song.wav"
    user={USER}
    transcribeJobId={JOB}
    storeKey={`${JOB}-${Math.random()}`}
    editorRequest={editorRequest}
    onPersistSegments={vi.fn()}
    onApprove={vi.fn()}
    onBack={vi.fn()}
    {...overrides}
  />);
}

afterEach(() => {
  cleanup();
  localStorage.clear();
  segmentsStore._clearAll();
});

describe("automatic repair undo in the editor", () => {
  it("restores the prior version and hides undo after success", async () => {
    const editorRequest = makeRequest();
    const onAutoRepairUndone = vi.fn();
    renderEditor(editorRequest, { onAutoRepairUndone });
    const undo = await screen.findByRole("button", { name: "Deshacer mejora automática" });
    expect(undo).toBeEnabled();
    fireEvent.click(undo);
    await waitFor(() => expect(onAutoRepairUndone).toHaveBeenCalledWith(
      expect.objectContaining({ revision: 1, segments: SOURCE }),
    ));
    expect(await screen.findByDisplayValue("versión anterior")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Deshacer mejora automática" })).toBeNull();
    expect(editorRequest.mock.calls.filter(([path]) => path.endsWith("/auto-repair/undo"))).toHaveLength(1);
  });

  it("does not overwrite unsaved local edits", async () => {
    const editorRequest = makeRequest();
    renderEditor(editorRequest);
    const undo = await screen.findByRole("button", { name: "Deshacer mejora automática" });
    fireEvent.change(await screen.findByDisplayValue("versión automática"), {
      target: { value: "edición local sin guardar" },
    });
    expect(undo).toBeDisabled();
    expect(screen.getByText("Guardá o descartá tus cambios actuales primero.")).toBeInTheDocument();
    expect(editorRequest.mock.calls.filter(([path]) => path.endsWith("/auto-repair/undo"))).toHaveLength(0);
  });

  it("does not undo while an unresolved browser draft exists", async () => {
    localStorage.setItem(
      `genly_editor_draft:${USER.tenant_id}:${USER.id}:${JOB}`,
      JSON.stringify({
        segments: [{ ...REPAIRED[0], text: "borrador local" }],
        base_revision: 0, updated_at: "2026-09-29T10:00:00Z",
      }),
    );
    const editorRequest = makeRequest();
    renderEditor(editorRequest);
    await screen.findByRole("dialog", { name: "Encontramos un borrador anterior" });
    expect(screen.getByRole("button", { name: "Deshacer mejora automática" })).toBeDisabled();
    expect(editorRequest.mock.calls.filter(([path]) => path.endsWith("/auto-repair/undo"))).toHaveLength(0);
  });
});
