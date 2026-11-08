import { act, cleanup, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import LyricsEditor from "./LyricsEditor";
import { segmentsStore } from "../state/segmentsStore";

const runtime = vi.hoisted(() => ({ autosave: null, history: null }));
vi.mock("../hooks/useEditorAutosave", () => ({ useEditorAutosave: (options) => {
  runtime.autosave = options;
  return { flush: async () => ({ ok: true }) };
} }));
vi.mock("./VersionHistory", () => ({ default: (options) => { runtime.history = options; return null; } }));
vi.mock("../i18n", () => ({ useI18n: () => ({ t: (_key, fallback) => fallback }) }));
vi.mock("./OnboardingTour", () => ({ EditorTour: () => null }));
vi.mock("./ToastProvider", () => ({ useToast: () => ({ toast: vi.fn(), dismiss: vi.fn() }) }));
const job = "persist-audit";
const rows = [
  { segment_id: "first", start: 0, end: 2, text: "Primera original" },
  { segment_id: "second", start: 3, end: 5, text: "Segunda original" },
];
function open() {
  render(<LyricsEditor segments={rows} transcribeJobId={job} filename="song.wav"
    requireLineReview disableAutosave disableBeforeUnload onApprove={vi.fn()} onBack={vi.fn()} />);
}
afterEach(() => { cleanup(); segmentsStore._clearAll(); localStorage.clear(); });

describe("third audit: undo after durable reconciliation", () => {
  it("does not overwrite another editor's reconciled line when undoing a local change", () => {
    open();
    fireEvent.change(screen.getByLabelText("Letra de esta línea"), { target: { value: "Primera local" } });
    const local = segmentsStore.get(job);
    act(() => runtime.autosave.onMerged(local.map(row => row.segment_id === "second"
      ? { ...row, text: "Segunda corregida por Agus" } : row)));
    fireEvent.click(screen.getByRole("button", { name: "Deshacer", exact: true }));
    const current = segmentsStore.get(job);
    expect(current.find(row => row.segment_id === "second").text).toBe("Segunda corregida por Agus");
    expect(current.find(row => row.segment_id === "first").text).toBe("Primera original");
  });
  it("keeps the selected durable identity when a remote correction changes its local row id", () => {
    open();
    fireEvent.click(screen.getByTestId("review-line-2"));
    act(() => runtime.autosave.onMerged(segmentsStore.get(job).map(row => row.segment_id === "second"
      ? { ...row, text: "Segunda corregida remota" } : row)));
    expect(screen.getByLabelText("Letra de esta línea")).toHaveValue("Segunda corregida remota");
  });
  it("does not delete a remotely added line when undoing a local change", () => {
    open();
    fireEvent.change(screen.getByLabelText("Letra de esta línea"), { target: { value: "Primera local" } });
    act(() => runtime.autosave.onMerged([...segmentsStore.get(job),
      { segment_id: "remote", start: 6, end: 8, text: "Estribillo recuperado por Agus" }]));
    fireEvent.click(screen.getByRole("button", { name: "Deshacer", exact: true }));
    expect(segmentsStore.get(job).find(row => row.segment_id === "remote")).toMatchObject({ text: "Estribillo recuperado por Agus" });
  });
});


describe("third audit: recoverable intentional empty draft", () => {
  it("offers recovery when the operator deleted all rows before a failed/offline save", async () => {
    const key = `genly_editor_draft:audit-team:42:${job}`;
    localStorage.setItem(key, JSON.stringify({ segments: [], base_segments: rows, base_revision: 1, updated_at: "2026-11-06T12:00:00Z" }));
    const request = vi.fn(async (path) => {
      const body = path.endsWith("/lock/heartbeat") ? { acquired: true }
        : { job_id: job, revision: 1, segments: rows, original_segments: rows, lock: { active: false } };
      return { ok: true, status: 200, clone: () => ({ json: async () => body }) };
    });
    render(<LyricsEditor segments={rows} transcribeJobId={job} filename="song.wav" requireLineReview
      disableBeforeUnload user={{ id: 42, tenant_id: "audit-team", features: { editor_v2: true } }}
      editorRequest={request} onApprove={vi.fn()} onBack={vi.fn()} />);
    fireEvent.click(await screen.findByRole("button", { name: "Recuperar cambios", exact: true }));
    expect(segmentsStore.get(job)).toEqual([]);
    expect(localStorage.getItem(`${key}:incompatible`)).toBeNull();
  });
});


describe("third audit: authoritative version restoration", () => {
  it("discards stale local undo and repeat prompts after restoring a chosen version", async () => {
    const repeated = [rows[0], { ...rows[1], text: rows[0].text }];
    const restored = [{ ...rows[0], text: "Versión elegida del historial" }];
    const request = vi.fn(async path => {
      const body = path.endsWith("/restore") ? { job_id: job, revision: 3, segments: restored }
        : path.endsWith("/lock/heartbeat") ? { acquired: true }
        : { job_id: job, revision: 1, segments: repeated, original_segments: repeated, lock: { active: false } };
      return { ok: true, status: 200, clone: () => ({ json: async () => body }) };
    });
    render(<LyricsEditor segments={repeated} transcribeJobId={job} filename="song.wav" requireLineReview
      disableBeforeUnload user={{ id: 42, tenant_id: "audit-team", features: { editor_v2: true } }}
      editorRequest={request} onApprove={vi.fn()} onBack={vi.fn()} />);
    const input = await screen.findByLabelText("Letra de esta línea");
    // Await durable hydration, which intentionally replaces the initial seed.
    await act(async () => {});
    fireEvent.focus(input);
    fireEvent.change(input, { target: { value: "Corrección anterior a restaurar" } });
    fireEvent.blur(input);
    expect(screen.getByRole("button", { name: "Deshacer", exact: true })).toBeEnabled();
    expect(screen.getByRole("button", { name: /Aplicar a todas/ })).toBeInTheDocument();
    await act(async () => { await runtime.history.onRestore("chosen-version"); });
    expect(screen.getByLabelText("Letra de esta línea")).toHaveValue("Versión elegida del historial");
    expect(screen.getByRole("button", { name: "Deshacer", exact: true })).toBeDisabled();
    expect(screen.queryByRole("button", { name: /Aplicar a todas/ })).toBeNull();
  });
});
