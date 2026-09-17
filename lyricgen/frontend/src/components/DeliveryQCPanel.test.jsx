import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import DeliveryQCPanel from "./DeliveryQCPanel";

afterEach(() => vi.restoreAllMocks());

const job = {
  job_id: "abc123", segments_revision: 2,
  delivery_qc: {
    status: "COMPLETE", mode: "observe", decision: "REVIEW",
    summary: { fail_count: 0, warn_count: 1, open_count: 1 },
    issues: [{
      issue_id: "issue-1", severity: "WARN", status: "OPEN",
      summary: "Texto visible distinto", description: "Revisar cuadro",
      seconds: [12.5], timecodes: ["00:00:12:15"],
    }],
    repairs: { actions: [], candidate_segments: [] },
    approval: { blocked: false },
  },
};

describe("DeliveryQCPanel", () => {
  it("muestra los checks que pasaron y distingue una revisión de un fallo", () => {
    const checkedJob = {
      ...job,
      delivery_qc: {
        ...job.delivery_qc,
        checks: [
          { check_id: "media_container", label: "Archivo de video válido", status: "PASS" },
          { check_id: "umg_black_bars", label: "Sin franjas negras", status: "REVIEW" },
          { check_id: "ocr_title", label: "Texto visible del title card", status: "NOT_RUN" },
          { check_id: "umg_title_metadata", label: "Título coincide con metadata", status: "REVIEW", detector: "mandatory_signed_reviewer_checklist" },
        ],
        check_summary: { pass: 1 },
      },
    };
    render(<DeliveryQCPanel job={checkedJob} onSeek={vi.fn()} onJobUpdate={vi.fn()} onOpenEditor={vi.fn()} />);
    expect(screen.getByTestId("delivery-qc-checks")).toHaveTextContent("Archivo de video válido");
    expect(screen.getByTestId("delivery-qc-checks")).toHaveTextContent("Pasó");
    expect(screen.getByTestId("delivery-qc-checks")).toHaveTextContent("Revisión");
    expect(screen.getByTestId("delivery-qc-checks")).toHaveTextContent("No ejecutado");
    expect(screen.getByTestId("delivery-qc-checks")).not.toHaveTextContent("Título coincide con metadata");
  });

  it("mantiene la revisión informativa y deja editar aunque un reporte legacy diga BLOCK", () => {
    const onOpenEditor = vi.fn();
    const legacyBlockedJob = {
      ...job,
      delivery_qc: { ...job.delivery_qc, decision: "BLOCK", mode: "observe" },
    };
    render(<DeliveryQCPanel job={legacyBlockedJob} onSeek={vi.fn()} onJobUpdate={vi.fn()} onOpenEditor={onOpenEditor} />);
    expect(screen.getByText("Revisión informativa")).toBeTruthy();
    expect(screen.getByText("Estos checks son informativos por ahora y no bloquean la edición ni el avance del video.")).toBeTruthy();
    fireEvent.click(screen.getByText("Editar cambios"));
    expect(onOpenEditor).toHaveBeenCalledTimes(1);
    expect(screen.queryByText("Bloqueado")).toBeNull();
  });

  it("permite actualizar un reporte desactualizado desde el video renderizado", async () => {
    const onJobUpdate = vi.fn();
    const staleJob = {
      ...job,
      delivery_qc: { ...job.delivery_qc, status: "STALE", issues: [] },
    };
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue({
      ok: true,
      json: async () => ({ delivery_qc: { ...staleJob.delivery_qc, status: "COMPLETE" } }),
    }));
    render(<DeliveryQCPanel job={staleJob} onSeek={vi.fn()} onJobUpdate={onJobUpdate} onOpenEditor={vi.fn()} />);
    fireEvent.click(screen.getByText("Actualizar preflight"));
    await waitFor(() => expect(fetch).toHaveBeenCalledWith(
      expect.stringContaining("/delivery-qc/recheck"),
      expect.objectContaining({ method: "POST" }),
    ));
    expect(onJobUpdate).toHaveBeenCalledWith(expect.objectContaining({
      delivery_qc: expect.objectContaining({ status: "COMPLETE" }),
    }));
  });

  it("seeks to findings and persists a reviewer decision", async () => {
    const onSeek = vi.fn();
    const onJobUpdate = vi.fn();
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue({
      ok: true,
      json: async () => ({ delivery_qc: { ...job.delivery_qc, summary: { open_count: 0 } } }),
    }));
    render(<DeliveryQCPanel job={job} onSeek={onSeek} onJobUpdate={onJobUpdate} onOpenEditor={vi.fn()} />);
    fireEvent.click(screen.getByText("00:00:12:15"));
    expect(onSeek).toHaveBeenCalledWith(12.5);
    fireEvent.click(screen.getByText("Revisado"));
    await waitFor(() => expect(fetch).toHaveBeenCalledWith(
      expect.stringContaining("/delivery-qc/issues/issue-1/decision"),
      expect.objectContaining({ method: "POST" }),
    ));
    expect(onJobUpdate).toHaveBeenCalled();
  });

  it("applies only server-certified text/timing actions with one click", async () => {
    const onJobUpdate = vi.fn();
    const actionableJob = {
      ...job,
      delivery_qc: {
        ...job.delivery_qc,
        repairs: {
          candidate_segments: [{ start: 1, end: 2, text: "Letra corregida" }],
          actions: [
            { action_id: "safe-timing", domain: "timing", status: "APPLIED" },
            { action_id: "unsafe-text", domain: "text", status: "PROPOSED" },
          ],
        },
      },
    };
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue({
      ok: true,
      json: async () => ({ ok: true }),
    }));

    render(<DeliveryQCPanel job={actionableJob} onSeek={vi.fn()} onJobUpdate={onJobUpdate} onOpenEditor={vi.fn()} />);
    fireEvent.click(screen.getByText("Corregir texto/timing seguro"));

    await waitFor(() => expect(fetch).toHaveBeenCalledTimes(1));
    const [, request] = fetch.mock.calls[0];
    const body = JSON.parse(request.body);
    expect(body.delivery_qc_action_ids).toEqual(["safe-timing"]);
    expect(body.delivery_qc_action_ids).not.toContain("unsafe-text");
    expect(body.segments).toEqual(actionableJob.delivery_qc.repairs.candidate_segments);
    expect(onJobUpdate).toHaveBeenCalledWith(expect.objectContaining({
      status: "editing",
      delivery_qc: expect.objectContaining({ status: "STALE" }),
    }));
  });
});
