import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import DeliveryQCPanel from "./DeliveryQCPanel";

afterEach(() => vi.restoreAllMocks());

const job = {
  job_id: "abc123", segments_revision: 2,
  delivery_qc: {
    generated_at: "2026-09-18T11:00:00Z",
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
    expect(screen.getByTestId("delivery-qc-checks")).toHaveTextContent("Título coincide con metadata");
  });

  it("mantiene la revisión informativa y deja editar aunque un reporte legacy diga BLOCK", () => {
    const onOpenEditor = vi.fn();
    const legacyBlockedJob = {
      ...job,
      delivery_qc: { ...job.delivery_qc, decision: "BLOCK", mode: "observe" },
    };
    render(<DeliveryQCPanel job={legacyBlockedJob} onSeek={vi.fn()} onJobUpdate={vi.fn()} onOpenEditor={onOpenEditor} />);
    expect(screen.getByText("Hallazgos del informe")).toBeTruthy();
    expect(screen.getByText("Este informe es informativo. Podés editar; la aprobación y publicación validan sus requisitos por separado.")).toBeTruthy();
    fireEvent.click(screen.getByText("Editar cambios"));
    expect(onOpenEditor).toHaveBeenCalledTimes(1);
    expect(screen.queryByText("Bloqueado")).toBeNull();
  });

  it("never hides swapped metadata findings or turns their FAIL into PASS", () => {
    render(<DeliveryQCPanel job={{ ...job, artist: "Artista", song_title: "Título", delivery_qc: {
      mode: "enforce", decision: "BLOCK", approval: { blocked: true },
      checks: [{ check_id: "metadata_title", label: "Título detectado", status: "FAIL", issue_ids: ["swap"] }],
      issues: [{ issue_id: "swap", status: "OPEN", severity: "FAIL", actual: "Artista", expected: "Título", summary: "Título no coincide" }],
    } }} />);
    expect(screen.getByText("Bloqueado")).toBeInTheDocument();
    expect(screen.getByText("Título no coincide")).toBeInTheDocument();
    expect(screen.getByTestId("delivery-qc-checks")).toHaveTextContent("Falló");
    expect(screen.getByTestId("delivery-qc-checks")).not.toHaveTextContent("Pasó");
  });

  it("keeps different checks with the same label and required human issues visible", () => {
    render(<DeliveryQCPanel job={{ ...job, delivery_qc: {
      decision: "BLOCK", mode: "observe", approval: { blocked: true },
      checks: [{ check_id: "a", label: "Título", status: "PASS" },
        { check_id: "b", label: "Título", status: "REVIEW", detector: "mandatory_signed_reviewer_checklist" }],
      issues: [{ issue_id: "human", status: "OPEN", detector: "mandatory_signed_reviewer_checklist", summary: "Confirmar título", manual_verification_required: true }],
    } }} />);
    expect(screen.getAllByText("Título")).toHaveLength(2);
    expect(screen.getByText("Confirmar título")).toBeInTheDocument();
    expect(screen.getByText("Bloqueado")).toBeInTheDocument();
    expect(screen.queryByText(/Este informe es informativo/)).not.toBeInTheDocument();
  });

  it("does not call a report with missing decision a PASS", () => {
    render(<DeliveryQCPanel job={{ ...job, delivery_qc: { checks: [], issues: [] } }} />);
    expect(screen.getByText("Verificación pendiente")).toBeInTheDocument();
    expect(screen.queryByText("Sin hallazgos")).not.toBeInTheDocument();
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
    expect(JSON.parse(fetch.mock.calls[0][1].body).expected_report_id).toBe(job.delivery_qc.generated_at);
  });

  it("binds a human decision to the displayed report and never retries a stale report conflict", async () => {
    const onJobUpdate = vi.fn();
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue({ ok: false, status: 409, json: async () => ({ detail: "report_changed" }) }));
    render(<DeliveryQCPanel job={{ ...job, delivery_qc: { ...job.delivery_qc, report_id: "report-A" } }} onJobUpdate={onJobUpdate} />);
    fireEvent.click(screen.getByText("Revisado"));
    await waitFor(() => expect(screen.getByText(/El informe cambió mientras revisabas/)).toBeInTheDocument());
    expect(fetch).toHaveBeenCalledTimes(1);
    expect(JSON.parse(fetch.mock.calls[0][1].body).expected_report_id).toBe("report-A");
    expect(onJobUpdate).not.toHaveBeenCalled();
  });

  it("requires rechecking an unidentifiable legacy report instead of signing blindly", () => {
    render(<DeliveryQCPanel job={{ ...job, delivery_qc: { ...job.delivery_qc, generated_at: undefined } }} />);
    expect(screen.getByText("Revisado")).toBeDisabled();
    expect(screen.getByText("Actualizar informe para revisar")).toBeEnabled();
  });

  it.each(["STALE", "RUNNING", "FAILED", undefined])("blocks decisions and repairs on %s even with a report token", async status => {
    const onJobUpdate = vi.fn();
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue({ ok: true,
      json: async () => ({ delivery_qc: { ...job.delivery_qc, status: "COMPLETE", report_id: "new-report" } }),
    }));
    render(<DeliveryQCPanel job={{ ...job, delivery_qc: { ...job.delivery_qc, status,
      report_id: "old-report", repairs: { candidate_segments: [], actions: [
        { action_id: "text", domain: "text", status: "APPLIED" },
        { action_id: "meta", domain: "metadata", status: "APPLIED" },
      ] },
    } }} onJobUpdate={onJobUpdate} />);
    for (const label of ["Revisado", "Corregir texto/timing seguro", "Corregir metadata segura"]) {
      expect(screen.getByText(label)).toBeDisabled();
      fireEvent.click(screen.getByText(label));
    }
    expect(fetch).not.toHaveBeenCalled();
    expect(screen.getByText(/El informe no está vigente o completo/)).toBeInTheDocument();
    fireEvent.click(screen.getByText("Actualizar preflight"));
    await waitFor(() => expect(onJobUpdate).toHaveBeenCalled());
    expect(fetch).toHaveBeenCalledTimes(1);
    expect(fetch.mock.calls[0][0]).toContain("/delivery-qc/recheck");
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
    expect(body.expected_delivery_qc_report_id).toBe(job.delivery_qc.generated_at);
    expect(body.delivery_qc_action_ids).not.toContain("unsafe-text");
    expect(body.segments).toEqual(actionableJob.delivery_qc.repairs.candidate_segments);
    expect(onJobUpdate).toHaveBeenCalledWith(expect.objectContaining({
      status: "editing",
      delivery_qc: expect.objectContaining({ status: "STALE" }),
    }));
  });

  it("discards a late QC response after job/revision changes", async () => {
    let resolve;
    const pending = new Promise(done => { resolve = done; });
    vi.stubGlobal("fetch", vi.fn().mockReturnValue(pending));
    const onJobUpdate = vi.fn();
    const stale = { ...job, delivery_qc: { ...job.delivery_qc, status: "STALE" } };
    const { rerender } = render(<DeliveryQCPanel job={stale} onJobUpdate={onJobUpdate} />);
    fireEvent.click(screen.getByText("Actualizar preflight"));
    rerender(<DeliveryQCPanel job={{ ...job, job_id: "other-job", segments_revision: 9 }} onJobUpdate={onJobUpdate} />);
    await act(async () => { resolve({ ok: true, json: async () => ({ delivery_qc: job.delivery_qc }) }); await pending; });
    expect(onJobUpdate).not.toHaveBeenCalled();
  });

  it("merges current unrelated metadata instead of restoring its captured job", async () => {
    let resolve;
    const pending = new Promise(done => { resolve = done; });
    vi.stubGlobal("fetch", vi.fn().mockReturnValue(pending));
    const onJobUpdate = vi.fn();
    const { rerender } = render(<DeliveryQCPanel job={{ ...job, song_title: "Before" }} onJobUpdate={onJobUpdate} />);
    fireEvent.click(screen.getByText("Revisado"));
    rerender(<DeliveryQCPanel job={{ ...job, song_title: "Current title" }} onJobUpdate={onJobUpdate} />);
    await act(async () => { resolve({ ok: true, json: async () => ({ delivery_qc: job.delivery_qc }) }); await pending; });
    expect(onJobUpdate).toHaveBeenCalledWith(expect.objectContaining({ song_title: "Current title" }));
  });

  it("serializes QC mutations instead of racing two signed decisions", async () => {
    let resolve;
    const pending = new Promise(done => { resolve = done; });
    vi.stubGlobal("fetch", vi.fn().mockReturnValue(pending));
    render(<DeliveryQCPanel job={{ ...job, delivery_qc: { ...job.delivery_qc,
      issues: [...job.delivery_qc.issues, { ...job.delivery_qc.issues[0], issue_id: "issue-2" }] } }} />);
    fireEvent.click(screen.getAllByText("Revisado")[0]);
    expect(screen.getAllByText("Revisado")[1]).toBeDisabled();
    fireEvent.click(screen.getAllByText("Revisado")[1]);
    expect(fetch).toHaveBeenCalledTimes(1);
    await act(async () => { resolve({ ok: true, json: async () => ({ delivery_qc: job.delivery_qc }) }); await pending; });
  });

  it("does not replace QC with an incomplete successful HTTP response", async () => {
    const onJobUpdate = vi.fn();
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue({ ok: true, json: async () => ({}) }));
    render(<DeliveryQCPanel job={job} onJobUpdate={onJobUpdate} />);
    fireEvent.click(screen.getByText("Revisado"));
    await waitFor(() => expect(screen.getByText(/no confirmó un informe verificable/)).toBeInTheDocument());
    expect(onJobUpdate).not.toHaveBeenCalled();
  });
});
