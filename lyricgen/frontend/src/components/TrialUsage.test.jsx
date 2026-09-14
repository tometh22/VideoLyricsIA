import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { I18nProvider } from "../i18n";
import UsageBadge from "./UsageBadge";
import TrialUsageSummary from "./TrialUsageSummary";

const user = { id: 15, username: "trial-qa", plan: "free" };
const trial = { state: "active", credits: 9, reserved: 3, available: 6, starts_at: "2026-09-14T16:00:00Z", expires_at: "2026-09-15T16:00:00Z", grant_id: 2 };
const show = (component) => render(<I18nProvider>{component}</I18nProvider>);
beforeEach(() => { localStorage.clear(); localStorage.setItem("genly_token", "test-token"); });
afterEach(() => { cleanup(); localStorage.clear(); vi.unstubAllGlobals(); });

describe("trial usage lifecycle", () => {
  it("explains pending admin activation without presenting an expiry date", () => {
    show(<TrialUsageSummary trial={{ ...trial, state: "pending", reserved: 0, available: 9, starts_at: null, expires_at: null }} />);
    expect(screen.getByText("Trial pendiente de activación")).toBeInTheDocument();
    expect(screen.getByText(/cuando el administrador activa/)).toBeInTheDocument();
    expect(screen.queryByText(/Vencimiento:/)).not.toBeInTheDocument();
  });

  it("shows reservation accounting instead of a monthly free-plan allowance", async () => {
    localStorage.setItem("cache:usage:15", JSON.stringify({ ts: Date.now(), payload: { plan: "free", limit: 5, used: 0, total_available: 5 } }));
    let release;
    vi.stubGlobal("fetch", vi.fn(() => new Promise(resolve => { release = resolve; })));
    show(<UsageBadge user={user} />);
    expect(screen.queryByText(/5|este mes/)).not.toBeInTheDocument();
    release({ ok: true, json: async () => ({ plan: "trial", limit: 9, used: 3, total_available: 6, trial }) });
    await screen.findByText("3 de 9 créditos reservados · 6 disponibles");
    expect(screen.getByText(/reserva 3 créditos al iniciar/)).toBeInTheDocument();
    expect(screen.getByText(/Aprobarlo no vuelve a descontarlos/)).toBeInTheDocument();
    expect(screen.getByText(/Vencimiento:/)).toBeInTheDocument();
    expect(screen.queryByText(/este mes/)).not.toBeInTheDocument();
    expect(localStorage.getItem("cache:usage:15")).toBeNull();
  });

  it("refreshes reservations after generation and allows finishing reserved videos at zero balance", async () => {
    const fetchMock = vi.fn()
      .mockResolvedValueOnce({ ok: true, json: async () => ({ plan: "trial", trial }) })
      .mockResolvedValueOnce({ ok: true, json: async () => ({ plan: "trial", trial: { ...trial, state: "exhausted", reserved: 9, available: 0 } }) });
    vi.stubGlobal("fetch", fetchMock);
    show(<UsageBadge user={user} />);
    await screen.findByText("Trial activo");
    fireEvent(window, new Event("genly:usage-changed"));
    await screen.findByText("9 de 9 créditos reservados · 0 disponibles");
    expect(screen.getByText(/Podés terminar y aprobar los videos ya reservados/)).toBeInTheDocument();
    expect(screen.queryByText(/Mejorar plan/)).not.toBeInTheDocument();
    await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(2));
  });

  it("does not promise finishing after expiry", () => {
    show(<TrialUsageSummary trial={{ ...trial, state: "expired", available: 0 }} />);
    expect(screen.getByText("Trial vencido")).toBeInTheDocument();
    expect(screen.queryByText(/Podés terminar/)).not.toBeInTheDocument();
  });
});
