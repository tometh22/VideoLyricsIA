import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import QualityLearningPanel from "./QualityLearningPanel";

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
});

describe("QualityLearningPanel", () => {
  beforeEach(() => {
    vi.spyOn(window, "prompt").mockReturnValue("Validar en benchmark holdout");
    global.fetch = vi.fn((url, options = {}) => {
      const value = String(url);
      let body;
      if (options.method === "POST") {
        body = { id: "proposal-1", status: "validating", version: 2 };
      } else if (value.includes("/summary")) {
        body = {
          observations: {
            total: 24, tiers: { trusted: 12 },
            operator_minutes: { p50: 4.5, p90: 8.2 },
            by_release: { "release-1": { observations: 12, operator_minutes_p50: 4.5, operator_minutes_p90: 8.2 } },
            by_route: { acoustic_dp: { observations: 8, operator_minutes_p50: 3.5, operator_minutes_p90: 7.1 } },
          },
          model_readiness: { eligible: false, trusted_observations: 12 },
          operator_suggestions: {
            songs: 2,
            by_type: {
              timing: { shown: 4, accepted: 2, rejected: 1, manual: 1, decided: 3, acceptance_rate: 2 / 3, sanity_gate_met: false },
              text: { shown: 0, accepted: 0, rejected: 0, manual: 0, decided: 0, acceptance_rate: null, sanity_gate_met: false },
              vocalization: { shown: 0, accepted: 0, rejected: 0, manual: 0, decided: 0, acceptance_rate: null, sanity_gate_met: false },
            },
            severe_timing_resolved: 2,
            severe_timing_accepted: 1,
            severe_timing_manual: 1,
          },
        };
      } else if (value.includes("/patterns")) {
        body = { patterns: [{
          id: "pattern-1", category: "missing_event",
          context_key: "is_live=true", support_jobs: 12, support_tenants: 3,
          support_artists: 4,
          relative_risk: 2.5, impact_seconds: 90, status: "correlated",
        }] };
      } else {
        body = { proposals: [{
          id: "proposal-1", title: "Confirmar mezcla",
          hypothesis: "Asociación; requiere ablation", status: "draft", version: 1,
          candidate_config: { prefer_mix_witness: true },
        }] };
      }
      return Promise.resolve({ ok: true, status: 200, json: () => Promise.resolve(body) });
    });
  });

  it("shows anonymised evidence and sends governed validation", async () => {
    render(<QualityLearningPanel />);
    await waitFor(() => expect(screen.getByText("missing_event")).toBeInTheDocument());
    expect(screen.getByText("24")).toBeInTheDocument();
    expect(screen.getByText("4.5 min")).toBeInTheDocument();
    expect(screen.getByText("8.2 min")).toBeInTheDocument();
    expect(screen.getByText("Trabajo por release")).toBeInTheDocument();
    expect(screen.getByText("release-1")).toBeInTheDocument();
    expect(screen.getByLabelText("Filtrar patrones por estado")).toBeInTheDocument();
    expect(screen.getByText("is_live=true")).toBeInTheDocument();
    expect(screen.getByText(/Ninguna acción modifica/)).toBeInTheDocument();
    expect(screen.getByText(/1 manuales/)).toBeInTheDocument();
    expect(screen.getByText(/Finales graves resueltos: 2/)).toBeInTheDocument();

    fireEvent.click(screen.getByRole("button", { name: "Validar" }));
    await waitFor(() => expect(global.fetch).toHaveBeenCalledWith(
      expect.stringContaining("/proposals/proposal-1/validate"),
      expect.objectContaining({ method: "POST" }),
    ));
    const post = global.fetch.mock.calls.find(([, options]) => options?.method === "POST");
    const payload = JSON.parse(post[1].body);
    expect(payload.expected_version).toBe(1);
    expect(payload.reason).toBe("Validar en benchmark holdout");
    expect(payload.idempotency_key.length).toBeGreaterThanOrEqual(8);
  });

  it("surfaces governed action failures without losing the panel", async () => {
    const original = global.fetch;
    global.fetch = vi.fn((url, options = {}) => {
      if (options.method === "POST") {
        return Promise.resolve({
          ok: false, status: 409,
          json: () => Promise.resolve({ detail: "stale_proposal" }),
        });
      }
      return original(url, options);
    });
    render(<QualityLearningPanel />);
    await waitFor(() => expect(screen.getByRole("button", { name: "Validar" })).toBeInTheDocument());
    fireEvent.click(screen.getByRole("button", { name: "Validar" }));
    await waitFor(() => expect(screen.getByRole("alert")).toBeInTheDocument());
  });

  it("sends governed reject and approve actions", async () => {
    render(<QualityLearningPanel />);
    await waitFor(() => expect(screen.getByRole("button", { name: "Rechazar" })).toBeInTheDocument());
    fireEvent.click(screen.getByRole("button", { name: "Rechazar" }));
    await waitFor(() => expect(global.fetch).toHaveBeenCalledWith(
      expect.stringContaining("/proposals/proposal-1/reject"),
      expect.objectContaining({ method: "POST" }),
    ));

    cleanup();
    global.fetch = vi.fn((url, options = {}) => {
      const value = String(url);
      if (options.method === "POST") {
        return Promise.resolve({ ok: true, status: 200, json: () => Promise.resolve({ status: "approved" }) });
      }
      if (value.includes("/summary")) {
        return Promise.resolve({ ok: true, status: 200, json: () => Promise.resolve({ observations: {}, model_readiness: {} }) });
      }
      if (value.includes("/patterns")) {
        return Promise.resolve({ ok: true, status: 200, json: () => Promise.resolve({ patterns: [] }) });
      }
      return Promise.resolve({ ok: true, status: 200, json: () => Promise.resolve({ proposals: [{
        id: "proposal-ready", title: "Fix validado", hypothesis: "Ablation firmada",
        status: "ready", version: 3, candidate_config: { enable_second_asr: true },
      }] }) });
    });
    render(<QualityLearningPanel />);
    await waitFor(() => expect(screen.getByRole("button", { name: "Aprobar" })).toBeInTheDocument());
    fireEvent.click(screen.getByRole("button", { name: "Aprobar" }));
    await waitFor(() => expect(global.fetch).toHaveBeenCalledWith(
      expect.stringContaining("/proposals/proposal-ready/approve"),
      expect.objectContaining({ method: "POST" }),
    ));
  });
});
