/**
 * Consolidación del admin (2026-06-11): 4 secciones por pregunta.
 * Verifica el NAV nuevo (con gating de Insights), el switch de Gestión y
 * los KPIs WoW de Rendimiento contra fixtures con la forma exacta de
 * /admin/metrics/timeseries.
 */
import { render, screen, cleanup, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { AdminProvider } from "../AdminContext";
import AdminSidebar, { defaultSubTab } from "../layout/AdminSidebar";
import RendimientoSection from "./rendimiento/RendimientoSection";

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
});

describe("AdminSidebar consolidado", () => {
  const props = { section: "ahora", subTab: null, onNavigate: () => {} };

  it("muestra las 4 secciones por pregunta (Insights solo super-admin)", () => {
    render(<AdminSidebar {...props} showInsights />);
    for (const label of ["Ahora", "Rendimiento", "Insights", "Gestión"]) {
      expect(screen.getByText(label)).toBeInTheDocument();
    }
    // Las secciones viejas por origen-del-dato no existen más
    for (const gone of ["Operación", "Usuarios", "Contenido", "Negocio"]) {
      expect(screen.queryByText(gone)).toBe(null);
    }
  });

  it("sin super-admin, Insights desaparece y el resto queda", () => {
    render(<AdminSidebar {...props} />);
    expect(screen.queryByText("Insights")).toBe(null);
    expect(screen.getByText("Gestión")).toBeInTheDocument();
  });

  it("Gestión arranca en usuarios; Ahora no tiene sub-tabs", () => {
    expect(defaultSubTab("gestion")).toBe("usuarios");
    expect(defaultSubTab("ahora")).toBe(null);
  });
});

describe("RendimientoSection", () => {
  function dayKey(daysAgo) {
    const d = new Date(Date.now() - daysAgo * 86_400_000);
    return d.toISOString().slice(0, 10);
  }

  beforeEach(() => {
    const series = {
      // Semana actual: 6 creados, 4 aprobados
      [dayKey(1)]: { umg: { created: 4, approved: 3, edit_requests: 2, ai_cost_usd: 10 } },
      [dayKey(3)]: { genly: { created: 2, approved: 1, edit_requests: 0, ai_cost_usd: 5 } },
      // Semana previa: 3 creados
      [dayKey(9)]: { umg: { created: 3, approved: 2, edit_requests: 4, ai_cost_usd: 20 } },
    };
    global.fetch = vi.fn((url) => {
      const u = String(url);
      const body = u.includes("/admin/metrics/timeseries")
        ? { days: 28, series }
        : u.includes("/admin/metrics/health")
          ? { tenants: [{ tenant_id: "umg", score: 80, status: "verde", jobs_7d: 6,
                          usage_delta_wow: 1.0, first_pass_rate: 0.8, rework_rate: 0.1, error_rate: 0 }] }
          : u.includes("/admin/metrics/funnel")
            ? { days: 28, total_jobs: 6, failed: 0,
                stages: [{ stage: "hasta_review", reached: 6, p50_s: 60, p95_s: 120 }] }
            : {};
      return Promise.resolve({ ok: true, status: 200, json: () => Promise.resolve(body) });
    });
  });

  it("KPIs WoW desde timeseries + salud por cuenta + funnel", async () => {
    render(
      <AdminProvider>
        <RendimientoSection />
      </AdminProvider>
    );
    // creados 6v3 y aprobados 4v2: ambos +100% → delta chips "↗ 100%"
    await waitFor(() =>
      expect(screen.getAllByText(/↗ 100%/).length).toBeGreaterThanOrEqual(2)  // +1 del health card del fixture
    );
    expect(screen.getByText("Videos creados")).toBeInTheDocument();
    // Ediciones bajaron 2 vs 4 → ↘ 50%
    expect(screen.getByText(/↘ 50%/)).toBeInTheDocument();
    // Salud por cuenta y funnel presentes
    await waitFor(() => expect(screen.getByText("umg")).toBeInTheDocument());
    expect(screen.getByText(/Funnel · últimos 28 días · 6 jobs/)).toBeInTheDocument();
  });
});
