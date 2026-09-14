import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import DeliveryQCPanel from "./DeliveryQCPanel";

afterEach(() => { cleanup(); vi.unstubAllGlobals(); });

function reportJob(mode = "observe", detected = false) {
  return {
    job_id: "trial-scenes", render_params: { enable_scenes: true },
    delivery_qc: {
      status: "COMPLETE", decision: "BLOCK", mode,
      approval: { blocked: mode === "enforce" },
      summary: { fail_count: detected ? 3 : 2, warn_count: 0, open_count: detected ? 3 : 2 },
      issues: [
        { issue_id: "scene-check", code: "UMG_SCENE_CHANGE", severity: "FAIL", status: "OPEN", manual_verification_required: true, summary: "Sin cambios de escena" },
        { issue_id: "contrast-check", code: "UMG_MOBILE_CONTRAST", severity: "FAIL", status: "OPEN", manual_verification_required: true, summary: "Contraste legible en mobile" },
        ...(detected ? [{ issue_id: "media-failure", code: "MEDIA_DECODE", severity: "FAIL", status: "OPEN", summary: "El video no se puede decodificar" }] : []),
      ],
    },
  };
}

describe("trial Delivery QC semantics", () => {
  it("shows unsigned manual checks as review and explains the inapplicable no-cut criterion", () => {
    render(<DeliveryQCPanel job={reportJob()} />);
    expect(screen.getByText("Revisar · no bloquea")).toBeInTheDocument();
    expect(screen.queryByText("Bloqueado")).not.toBeInTheDocument();
    expect(screen.queryByText("FAIL")).not.toBeInTheDocument();
    expect(screen.queryByText("Sin cambios de escena")).not.toBeInTheDocument();
    expect(screen.getByText("Cambios de escena previstos")).toBeInTheDocument();
    expect(screen.getByText("fallos detectados").parentElement).toHaveTextContent("0");
    expect(screen.getByText("checks por revisar").parentElement).toHaveTextContent("1");
    expect(screen.getAllByRole("button", { name: "Firmar check" })).toHaveLength(1);
  });

  it("keeps actual failures visible even though observe mode does not block", () => {
    render(<DeliveryQCPanel job={reportJob("observe", true)} />);
    expect(screen.getByText("FAIL")).toBeInTheDocument();
    expect(screen.getByText("El video no se puede decodificar")).toBeInTheDocument();
    expect(screen.getByText("fallos detectados").parentElement).toHaveTextContent("1");
  });

  it("retains enforced delivery checks and persists the original manual decision", async () => {
    const job = reportJob("enforce");
    vi.stubGlobal("fetch", vi.fn().mockResolvedValue({ ok: true, json: async () => ({ delivery_qc: job.delivery_qc }) }));
    render(<DeliveryQCPanel job={job} />);
    expect(screen.getByText("Bloqueado")).toBeInTheDocument();
    const card = screen.getByText("Sin cambios de escena").closest(".rounded-xl");
    fireEvent.click(within(card).getByRole("button", { name: "Firmar check" }));
    await waitFor(() => expect(fetch).toHaveBeenCalledWith(expect.stringContaining("/issues/scene-check/decision"), expect.objectContaining({ body: JSON.stringify({ decision: "resolved_manual", reason: "reviewer_qc_panel" }) })));
  });

  it("does not call a failed analysis clean", () => {
    const job = reportJob();
    job.delivery_qc.status = "FAILED";
    job.delivery_qc.decision = "PASS";
    render(<DeliveryQCPanel job={job} />);
    expect(screen.getByText("Falló el análisis")).toBeInTheDocument();
    expect(screen.queryByText("Sin hallazgos")).not.toBeInTheDocument();
  });
});
