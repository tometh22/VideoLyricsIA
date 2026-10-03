import { useState } from "react";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import DeliveryQCPanel from "./DeliveryQCPanel";

afterEach(() => vi.restoreAllMocks());

const job = {
  job_id: "abc123", segments_revision: 2,
  delivery_qc: {
    status: "COMPLETE", mode: "observe", decision: "REVIEW", report_id: "report-abc",
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

function StatefulQCPanel({ initialJob }) {
  const [currentJob, setCurrentJob] = useState(initialJob);
  return <DeliveryQCPanel job={currentJob} onJobUpdate={setCurrentJob} />;
}

describe("DeliveryQCPanel", () => {
  it("permite iniciar el preflight UMG aunque todavía no haya reporte", async () => {
    const onJobUpdate = vi.fn();
    const report = { status: "COMPLETE", mode: "enforce", issues: [] };
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue({
      ok: true,
      json: async () => ({ ok: true, delivery_qc: report }),
    }));
    render(
      <DeliveryQCPanel
        job={{ job_id: "abc123", delivery_qc: null }}
        forUmgDelivery
        onJobUpdate={onJobUpdate}
      />,
    );

    fireEvent.click(screen.getByRole("button", { name: "Analizar corte" }));
    await waitFor(() => expect(fetch).toHaveBeenCalledWith(
      expect.stringContaining("/delivery-qc/recheck"),
      expect.objectContaining({
        method: "POST",
        body: JSON.stringify({ for_umg_delivery: true }),
      }),
    ));
    await waitFor(() => expect(onJobUpdate).toHaveBeenCalledWith(
      expect.objectContaining({ delivery_qc: report }),
    ));
  });

  it("muestra los checks que pasaron y distingue una revisión de un fallo", () => {
    const checkedJob = {
      ...job,
      delivery_qc: {
        ...job.delivery_qc,
        checks: [
          { check_id: "media_container", label: "Archivo de video válido", status: "PASS" },
          { check_id: "umg_black_bars", label: "Sin franjas negras", status: "REVIEW" },
          { check_id: "ocr_title", label: "Texto visible del title card", status: "NOT_RUN" },
        ],
        check_summary: { pass: 1 },
      },
    };
    render(<DeliveryQCPanel job={checkedJob} onSeek={vi.fn()} onJobUpdate={vi.fn()} onOpenEditor={vi.fn()} />);
    expect(screen.getByTestId("delivery-qc-checks")).toHaveTextContent("Archivo de video válido");
    expect(screen.getByTestId("delivery-qc-checks")).toHaveTextContent("Pasó");
    expect(screen.getByTestId("delivery-qc-checks")).toHaveTextContent("Revisión");
    expect(screen.getByTestId("delivery-qc-checks")).toHaveTextContent("No ejecutado");
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
    fireEvent.click(screen.getByText("Marcar revisado"));
    await waitFor(() => expect(fetch).toHaveBeenCalledWith(
      expect.stringContaining("/delivery-qc/issues/issue-1/decision"),
      expect.objectContaining({
        method: "POST",
        body: JSON.stringify({
          decision: "acknowledged",
          reason: "reviewer_qc_panel",
          expected_report_id: "report-abc",
        }),
      }),
    ));
    expect(onJobUpdate).toHaveBeenCalled();
  });

  it("no ofrece descartar un fallo automático y explica que hay que corregirlo", () => {
    const failedJob = {
      ...job,
      delivery_qc: {
        ...job.delivery_qc,
        decision: "BLOCK",
        issues: [{
          issue_id: "black-frame", severity: "FAIL", result_status: "FAIL",
          status: "OPEN", summary: "Cuadro negro detectado",
        }],
      },
    };
    render(<DeliveryQCPanel job={failedJob} onJobUpdate={vi.fn()} onOpenEditor={vi.fn()} />);
    expect(screen.queryByText("Marcar revisado")).not.toBeInTheDocument();
    expect(screen.getByText("Requiere corrección")).toBeInTheDocument();
    expect(screen.getByText("Corregí este punto en el video y volvé a analizar el corte.")).toBeInTheDocument();
  });

  it("deshabilita decisiones del reporte desactualizado y ofrece una sola salida", () => {
    const staleJob = {
      ...job,
      delivery_qc: {
        ...job.delivery_qc,
        status: "STALE",
        issues: [{ ...job.delivery_qc.issues[0], manual_verification_required: true }],
      },
    };
    render(<DeliveryQCPanel job={staleJob} forUmgDelivery onJobUpdate={vi.fn()} />);
    expect(screen.getByRole("button", { name: "Actualizar preflight" })).toBeEnabled();
    fireEvent.click(screen.getByText("Revisión final"));
    expect(screen.getByRole("button", { name: "Confirmar revisión: Texto visible distinto" })).toBeDisabled();
  });

  it("expande y enfoca la revisión manual cuando la publicación queda bloqueada", async () => {
    const manualJob = {
      ...job,
      delivery_qc: {
        ...job.delivery_qc,
        issues: [{
          issue_id: "manual-final-review", code: "UMG_FINAL_REVIEW", severity: "WARN", result_status: "REVIEW",
          status: "OPEN", manual_verification_required: true,
          summary: "Revisión final del corte",
        }],
      },
    };
    render(<DeliveryQCPanel job={manualJob} focusRequest={1} focusTarget="manual" onJobUpdate={vi.fn()} />);
    const signButton = screen.getByRole("button", { name: "Confirmar revisión: Revisión final del corte" });
    await waitFor(() => expect(signButton).toHaveFocus());
  });

  it("confirma la revisión del corte con una sola firma accesible", async () => {
    const report = {
      ...job.delivery_qc,
      issues: [{
        issue_id: "manual-final-review", code: "UMG_FINAL_REVIEW",
        status: "OPEN", result_status: "REVIEW", severity: "WARN",
        manual_verification_required: true,
        summary: "Revisión final del corte",
      }],
    };
    const signedIds = new Set();
    vi.stubGlobal("fetch", vi.fn().mockImplementation(async (url) => {
      const issueId = url.split("/issues/")[1]?.split("/")[0];
      signedIds.add(issueId);
      const nextIssues = report.issues.map((issue) => ({
        ...issue,
        status: signedIds.has(issue.issue_id) ? "RESOLVED_MANUAL" : issue.status,
      }));
      return {
        ok: true,
        json: async () => ({ delivery_qc: { ...report, issues: nextIssues } }),
      };
    }));
    render(<StatefulQCPanel initialJob={{ ...job, delivery_qc: report }} />);
    fireEvent.click(screen.getByText("Revisión final"));
    fireEvent.click(screen.getByRole("button", { name: "Confirmar revisión: Revisión final del corte" }));
    await waitFor(() => expect(screen.getByText("completa")).toBeInTheDocument());
    expect(fetch).toHaveBeenCalledTimes(1);
    expect(fetch).toHaveBeenLastCalledWith(
      expect.stringContaining("/issues/manual-final-review/decision"),
      expect.objectContaining({ body: JSON.stringify({
        decision: "resolved_manual",
        reason: "reviewer_qc_panel",
        expected_report_id: "report-abc",
      }) }),
    );
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
