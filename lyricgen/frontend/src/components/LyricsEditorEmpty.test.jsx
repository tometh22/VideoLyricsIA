import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import LyricsEditor from "./LyricsEditor";

vi.mock("../i18n", () => ({ useI18n: () => ({ t: (_key, fallback) => fallback }) }));
vi.mock("./OnboardingTour", () => ({ EditorTour: () => null }));
vi.mock("./ToastProvider", () => ({ useToast: () => ({ toast: vi.fn(), dismiss: vi.fn() }) }));

function editor(segments, extra = {}) {
  const onApprove = vi.fn();
  render(<LyricsEditor segments={segments} filename="song.mp3" audioFile={null}
    referenceLyrics="" onApprove={onApprove} onBack={vi.fn()} submitLabel="Aprobar letra y timing" {...extra} />);
  return onApprove;
}

describe("empty lyrics recovery", () => {
  it("restores an empty durable draft directly from the review tab", async () => {
    const reply = (body) => ({ ok: true, status: 200, clone: () => ({ json: async () => body }) });
    const restored = [{ _id: "line-1", start: 1, end: 2, text: "Letra recuperada" }];
    const request = vi.fn(async (path, options = {}) => {
      if (path.endsWith("/versions?limit=50")) return reply({ versions: [{ id: "v1", revision: 1, reason: "manual" }] });
      if (path.endsWith("/restore")) return reply({ revision: 17, segments: restored, original_segments: restored });
      if (path.endsWith("/lock/heartbeat")) return reply({ acquired: true });
      if (path.endsWith("/lock")) return reply({ released: true });
      return reply({ job_id: "empty-history", revision: 16, segments: [], original_segments: restored, lock: { active: false } });
    });
    editor([], { transcribeJobId: "empty-history", editorRequest: request,
      user: { id: 42, tenant_id: "team-a", features: { editor_v2: true } } });
    fireEvent.click(await screen.findByRole("button", { name: "Recuperar del historial" }));
    fireEvent.click(await screen.findByRole("button", { name: "Restaurar", exact: true }));
    expect(await screen.findByDisplayValue("Letra recuperada")).toBeInTheDocument();
    await waitFor(() => expect(screen.queryByRole("status", { name: "Letra vacía" })).toBeNull());
    expect(request).toHaveBeenCalledWith("/editor/empty-history/restore", expect.objectContaining({
      method: "POST", body: JSON.stringify({ version_id: "v1", base_revision: 16 }),
    }));
  });

  it.each([[], [{ start: 1, end: 2, text: "   " }]])("blocks approval when no lyric text exists", (segments) => {
    const onApprove = editor(segments, { requireLineReview: true });
    const approve = screen.getByRole("button", { name: "Aprobar letra y timing" });
    expect(approve).toBeDisabled();
    expect(screen.getByRole("status", { name: "Letra vacía" })).toHaveTextContent("recuperá una versión anterior");
    fireEvent.click(approve);
    expect(onApprove).not.toHaveBeenCalled();
  });

  it("updates the empty state while editing, and recovers when text returns", () => {
    editor([{ start: 1, end: 2, text: "Texto existente" }]);
    const field = screen.getByDisplayValue("Texto existente");
    const approve = screen.getByRole("button", { name: "Aprobar letra y timing" });
    expect(approve).toBeEnabled();
    fireEvent.change(field, { target: { value: "" } });
    expect(approve).toBeDisabled();
    expect(screen.getByRole("status", { name: "Letra vacía" })).toBeInTheDocument();
    fireEvent.change(field, { target: { value: "Letra corregida" } });
    expect(approve).toBeEnabled();
    expect(screen.queryByRole("status", { name: "Letra vacía" })).toBeNull();
  });
});
