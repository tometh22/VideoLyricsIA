/**
 * Tests de las vistas de métricas de decisión (Fases 1+2, 2026-06-11):
 * salud por tenant, funnel operativo y margen por tenant — renderizadas
 * contra fixtures con la forma EXACTA de los endpoints /admin/metrics/*.
 */
import { render, screen, cleanup } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";

import TenantHealthCards from "./operacion/TenantHealthCards";
import FunnelCard from "./operacion/FunnelCard";
import MargenTenantsView from "./negocio/MargenTenantsView";

afterEach(() => cleanup());

describe("TenantHealthCards", () => {
  const HEALTH = {
    tenants: [
      { tenant_id: "universal_argentina", score: 82, status: "verde",
        jobs_7d: 12, jobs_prev_7d: 10, usage_delta_wow: 0.2,
        first_pass_rate: 0.75, rework_rate: 0.25, error_rate: 0.0 },
      { tenant_id: "tenant_riesgo", score: 30, status: "rojo",
        jobs_7d: 1, jobs_prev_7d: 8, usage_delta_wow: -0.88,
        first_pass_rate: 0.2, rework_rate: 0.6, error_rate: 0.3 },
    ],
  };

  it("muestra score, delta WoW y componentes por tenant", () => {
    render(<TenantHealthCards health={HEALTH} />);
    expect(screen.getByText("universal_argentina")).toBeInTheDocument();
    expect(screen.getByText("82")).toBeInTheDocument();
    expect(screen.getByText(/↗ 20% WoW/)).toBeInTheDocument();
    expect(screen.getByText("30")).toBeInTheDocument();
    expect(screen.getByText(/↘ 88% WoW/)).toBeInTheDocument();
  });

  it("sin tenants no renderiza nada", () => {
    const { container } = render(<TenantHealthCards health={{ tenants: [] }} />);
    expect(container.firstChild).toBe(null);
  });
});

describe("FunnelCard", () => {
  const FUNNEL = {
    days: 7, total_jobs: 10, failed: 1,
    stages: [
      { stage: "hasta_review", reached: 10, p50_s: 120, p95_s: 600 },
      { stage: "review", reached: 8, p50_s: 900, p95_s: 3600 },
      { stage: "render", reached: 7, p50_s: 300, p95_s: 700 },
      { stage: "aprobacion", reached: 5, p50_s: 60, p95_s: 120 },
      { stage: "descarga", reached: 3, p50_s: 30, p95_s: 90 },
    ],
  };

  it("muestra las 5 etapas con conversión y tiempos", () => {
    render(<FunnelCard funnel={FUNNEL} />);
    expect(screen.getByText(/Funnel · últimos 7 días · 10 jobs/)).toBeInTheDocument();
    expect(screen.getByText("Review de letra")).toBeInTheDocument();
    expect(screen.getByText(/1 fallidos/)).toBeInTheDocument();
    // p50 de review = 900s → fmtDuration lo muestra en minutos
    expect(screen.getByText(/p50 15m/)).toBeInTheDocument();
  });

  it("sin jobs no renderiza nada", () => {
    const { container } = render(<FunnelCard funnel={{ days: 7, total_jobs: 0, stages: [] }} />);
    expect(container.firstChild).toBe(null);
  });
});

describe("MargenTenantsView", () => {
  const ECON = {
    days: 28,
    tenants: [
      { tenant_id: "universal_argentina", approved_videos: 20,
        ai_cost_usd: 61.5, price_per_video: 8, revenue_usd: 160.0,
        margin_usd: 98.5, cost_per_approved_usd: 3.08 },
    ],
  };

  it("muestra revenue, costo y margen por tenant", () => {
    render(<MargenTenantsView economics={ECON} loading={false} forbidden={false} />);
    expect(screen.getByText("Margen por tenant")).toBeInTheDocument();
    expect(screen.getByText("$160.00")).toBeInTheDocument();
    expect(screen.getByText("$61.50")).toBeInTheDocument();
    expect(screen.getByText("$98.50")).toBeInTheDocument();
  });

  it("forbidden (no super-admin) oculta el bloque ENTERO en silencio", () => {
    const { container } = render(
      <MargenTenantsView economics={null} loading={false} forbidden={true} />,
    );
    expect(container.firstChild).toBe(null);
  });
});
