/**
 * Tests de la sección Insights (panel CEO, 2026-06-10): adopción de
 * features, funnel del wizard, perfil de usuario, gating del sidebar y el
 * drill-down App → Tenant → Usuario con fetch mockeado contra fixtures con
 * la forma EXACTA de /admin/insights/* y /admin/activity/{id}.
 */
import { render, screen, cleanup, fireEvent, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { AdminProvider } from "../AdminContext";
import AdminSidebar from "../layout/AdminSidebar";
import AdoptionPanel from "./insights/AdoptionPanel";
import WizardFunnelPanel from "./insights/WizardFunnelPanel";
import UserProfileView from "./insights/UserProfileView";
import InsightsSection from "./insights/InsightsSection";

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
});

const ADOPTION = {
  days: 30, tenant_id: null, user_id: null,
  total_jobs: 10, jobs_with_params: 8,
  features: {
    lyrics_animation: { karaoke: 5, "(default)": 3 },
    line_transition: { "(default)": 8 },
    effect: {}, movement_style: {}, text_case: { upper: 8 },
    text_contrast: {}, title_template: {},
    style: { oscuro: 7, neon: 3 },
    delivery_profile: { youtube: 9, umg: 1 },
  },
  font: [{ value: "Anton", count: 6 }],
  flags: { custom_colors: 2, lyric_color: 0, lyric_sung_color: 0,
           background_hint: 4, bg_verbatim: 0, concept: 1, title_song_break: 0 },
  background_source: { library_as_is: 3, ai_video: 6, other: 1 },
};

describe("AdoptionPanel", () => {
  it("muestra distribuciones, fuentes top, flags y fuente del fondo", () => {
    render(<AdoptionPanel adoption={ADOPTION} />);
    expect(screen.getByText(/basado en 8\/10 jobs/)).toBeInTheDocument();
    expect(screen.getByText("Animación de letra")).toBeInTheDocument();
    expect(screen.getByText("Karaoke")).toBeInTheDocument();
    expect(screen.getByText("Anton")).toBeInTheDocument();
    expect(screen.getByText(/Colores custom: 2/)).toBeInTheDocument();
    // Flag en cero → "nunca usó"
    expect(screen.getByText(/Color de letra: nunca usó/)).toBeInTheDocument();
    expect(screen.getByText("Biblioteca (tal cual)")).toBeInTheDocument();
  });

  it("sin jobs no renderiza nada", () => {
    const { container } = render(<AdoptionPanel adoption={{ ...ADOPTION, total_jobs: 0 }} />);
    expect(container.firstChild).toBe(null);
  });
});

describe("WizardFunnelPanel", () => {
  it("vacío muestra 'Recolectando datos'", () => {
    render(<WizardFunnelPanel wizard={{ empty: true, telemetry_enabled: true }} />);
    expect(screen.getByText("Recolectando datos")).toBeInTheDocument();
  });

  it("con datos muestra funnel, conversión y abandonos", () => {
    render(<WizardFunnelPanel wizard={{
      empty: false, sessions_total: 4, sessions_generated: 2, conversion: 0.5,
      funnel: [{ step: 1, reached: 4 }, { step: 2, reached: 3 }, { step: 3, reached: 2 },
               { step: 4, reached: 2 }, { step: 5, reached: 2 }, { step: 6, reached: 1 }],
      abandon_by_step: { 2: 2 },
      scene_modes: { library: 3 }, library: { selects: 2, filters: 1, modes: {} },
      p50_to_generate_s: 480, p95_to_generate_s: 900, event_counts: {},
      telemetry_enabled: true,
    }} />);
    expect(screen.getByText(/Funnel del wizard · 4 sesiones/)).toBeInTheDocument();
    expect(screen.getByText(/50% conversión/)).toBeInTheDocument();
    expect(screen.getByText(/2 abandonos/)).toBeInTheDocument();
    expect(screen.getByText(/Fondo: Biblioteca × 3/)).toBeInTheDocument();
  });
});

describe("UserProfileView", () => {
  const DETAIL = {
    user: { username: "ana.m", email: "ana@umg.com", tenant_id: "universal_argentina",
            plan_id: "b2b", role: "user", is_active: true },
    jobs: [
      { job_id: "j1", artist: "Rata Blanca", song_title: "Mujer Amante", status: "done",
        edit_count: 2, parent_job_id: null, created_at: "2026-06-09T10:00:00Z",
        choices: { font: "Anton", lyrics_animation: "karaoke", line_transition: null,
                   effect: null, style: "oscuro", title_template: "auto", text_case: "upper",
                   has_custom_colors: true, background_source: "library_as_is" } },
      { job_id: "j2", artist: "Rata Blanca", song_title: "Otra", status: "error",
        edit_count: 0, parent_job_id: null, created_at: "2026-06-08T10:00:00Z",
        choices: null },
    ],
    downloads: [{ action: "job.download", detail: { job_id: "j1", file_type: "video" },
                  created_at: "2026-06-09T12:00:00Z" }],
    events: [],
    sessions: null,
    logins: [{ ip_address: "1.2.3.4", user_agent: "Chrome", created_at: "2026-06-09T09:00:00Z",
               last_seen_at: null, revoked: false }],
    library_usage: [{ asset_id: 1, name: "Mural", mode: "as_is", job_id: "j1",
                      used_at: "2026-06-09T10:00:00Z" }],
  };

  it("muestra identidad, choices por job y extras", () => {
    render(<UserProfileView detail={DETAIL} summaryRow={{ user_id: 7, jobs: 2, done: 1,
      failed: 1, rework_events: 3, edits_total: 2, retries: 1, corrected_jobs: 0,
      ai_cost_usd: 4.2, online: false }} />);
    expect(screen.getByText("ana.m")).toBeInTheDocument();
    // La línea de choices resume animación · fuente · estilo · colores
    expect(screen.getByText(/anim karaoke · Anton · oscuro · colores custom/)).toBeInTheDocument();
    expect(screen.getByText(/fondo: biblioteca/)).toBeInTheDocument();
    // Job sin render_params no rompe
    expect(screen.getByText("sin parámetros registrados")).toBeInTheDocument();
    // Telemetría apagada
    expect(screen.getByText(/Tracking de sesiones apagado/)).toBeInTheDocument();
    expect(screen.getByText("Mural")).toBeInTheDocument();
    expect(screen.getByText("Retrabajos")).toBeInTheDocument();
  });
});

describe("AdminSidebar gating", () => {
  const props = { section: "operacion", subTab: null, onNavigate: () => {} };

  it("sin showInsights la entrada Insights NO existe", () => {
    render(<AdminSidebar {...props} />);
    expect(screen.queryByText("Insights")).toBe(null);
  });

  it("con showInsights la entrada aparece", () => {
    render(<AdminSidebar {...props} showInsights />);
    expect(screen.getByText("Insights")).toBeInTheDocument();
  });
});

describe("InsightsSection drill-down", () => {
  const OVERVIEW_APP = {
    days: 30, tenant_id: null,
    kpis: { jobs_total: 12, jobs_done: 9, jobs_approved: 8, jobs_failed: 1, in_progress: 1,
            approval_rate: 0.89, active_users: 3, jobs_prev_window: 10, wow_delta: 0.2,
            rework_events: 5, edits_total: 3, retries: 1, corrected_jobs: 1, ai_cost_usd: 30.5 },
    errors_by_category: { render: 1 },
    recent_errors: [{ job_id: "jx", user_id: 7, username: "ana.m", artist: "A", song_title: "S",
                      category: "render", error: "boom", created_at: "2026-06-09T10:00:00Z" }],
    tenants: [
      { tenant_id: "universal_argentina", jobs: 8, done: 7, failed: 1, rework_events: 4,
        active_users: 2, ai_cost_usd: 25.0, last_activity: "2026-06-09T10:00:00Z" },
      { tenant_id: "default", jobs: 4, done: 2, failed: 0, rework_events: 1,
        active_users: 1, ai_cost_usd: 5.5, last_activity: "2026-06-08T10:00:00Z" },
    ],
    users: [
      { user_id: 7, username: "ana.m", tenant_id: "universal_argentina", jobs: 6, done: 5,
        approved: 5, failed: 1, in_progress: 0, variants: 1, edits_total: 2, retries: 1,
        corrected_jobs: 0, rework_events: 4, ai_cost_usd: 20.0,
        last_activity: "2026-06-09T10:00:00Z", online: true },
    ],
    telemetry_enabled: false,
  };
  const DETAIL = {
    user: { username: "ana.m", email: "a@b.c", tenant_id: "universal_argentina",
            plan_id: "b2b", role: "user", is_active: true },
    jobs: [], downloads: [], events: [], sessions: null, logins: [], library_usage: [],
  };

  beforeEach(() => {
    global.fetch = vi.fn((url) => {
      const body =
        String(url).includes("/admin/stats") ? { jobs: {} }
        : String(url).includes("/admin/insights/overview") ? OVERVIEW_APP
        : String(url).includes("/admin/insights/adoption") ? ADOPTION
        : String(url).includes("/admin/insights/wizard") ? { empty: true, telemetry_enabled: false }
        : String(url).includes("/admin/activity/7") ? DETAIL
        : {};
      return Promise.resolve({
        ok: true, status: 200,
        json: () => Promise.resolve(body),
      });
    });
  });

  it("nivel app → click tenant → click usuario → breadcrumb vuelve", async () => {
    render(
      <AdminProvider>
        <InsightsSection />
      </AdminProvider>
    );
    // Nivel app cargado: KPIs + tabla de tenants. El tenant_id aparece en
    // varias filas (tabla de tenants + sub-línea de usuarios) → getAllByText.
    await waitFor(() =>
      expect(screen.getAllByText("universal_argentina").length).toBeGreaterThan(0)
    );
    expect(screen.getByText(/Videos creados \(30d\)/)).toBeInTheDocument();
    expect(screen.getByText("Por tenant")).toBeInTheDocument();

    // Drill a tenant (primera aparición = fila de la tabla de tenants)
    fireEvent.click(screen.getAllByText("universal_argentina")[0]);
    await waitFor(() =>
      expect(screen.getByText(/Usuarios de universal_argentina/)).toBeInTheDocument()
    );

    // Drill a usuario
    fireEvent.click(screen.getByText("ana.m"));
    await waitFor(() => expect(screen.getByText(/a@b\.c/)).toBeInTheDocument());

    // Breadcrumb: volver a toda la app
    fireEvent.click(screen.getByText("Toda la app"));
    await waitFor(() => expect(screen.getByText("Por tenant")).toBeInTheDocument());
  });
});
