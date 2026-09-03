import { cleanup, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, describe, expect, it, vi } from "vitest";

import StatusPage from "./StatusPage";

vi.mock("../i18n", () => ({
  useI18n: () => ({
    lang: "es",
    setLang: vi.fn(),
    t: (key, vars) => {
      const dict = {
        "service_status.title": "Estado del servicio",
        "service_status.headline.operational": "Todos los sistemas operativos",
        "service_status.headline.major_outage": "Hay una caída en curso",
        "service_status.unreachable_headline": "No podemos contactar la API de GenLy",
        "service_status.unreachable_body": "Esta página cargó, pero nuestra API no responde.",
        "service_status.active_incidents": "Incidentes en curso",
        "service_status.past_incidents": "Incidentes resueltos",
        "service_status.no_past_incidents": "Sin incidentes registrados en este período.",
        "service_status.components_title": "Servicios",
        "service_status.no_uptime_data": "Todavía sin datos suficientes",
        "service_status.uptime_value": `${vars?.pct} de disponibilidad`,
        "service_status.low_coverage": `medido sobre ${vars?.pct} del período`,
        "service_status.component.api": "Portal y API",
        "service_status.component.transcription": "Transcripción y sincronía",
        "service_status.state.operational": "Operativo",
        "service_status.state.major_outage": "Caído",
        "service_status.state.no_data": "Sin datos",
        "service_status.incident_status.investigating": "Investigando",
        "service_status.incident_status.identified": "Causa identificada",
        "service_status.impact.critical": "Crítico",
        "service_status.view_all_updates": `Ver las ${vars?.n} actualizaciones`,
      };
      return dict[key] || key;
    },
  }),
}));
vi.mock("./BrandLockup", () => ({ default: () => <span>GenLy</span> }));

function renderPage() {
  return render(<MemoryRouter><StatusPage /></MemoryRouter>);
}

const OK_PAYLOAD = {
  indicator: "operational",
  updated_at: "2026-09-03T15:00:00+00:00",
  history_days: 90,
  components: [
    {
      id: "api", label: "Portal y API", description: "",
      status: "operational", uptime_pct: 99.94, coverage_pct: 98.2,
      days: [{ day: "2026-09-02", status: "operational" },
             { day: "2026-09-03", status: "operational" }],
    },
    {
      id: "transcription", label: "Transcripción y sincronía", description: "",
      status: "operational", uptime_pct: null, coverage_pct: 0,
      days: [{ day: "2026-09-02", status: "no_data" },
             { day: "2026-09-03", status: "no_data" }],
    },
  ],
  active_incidents: [],
  past_incidents: [],
};

afterEach(() => { cleanup(); vi.restoreAllMocks(); });

describe("StatusPage", () => {
  it("muestra el veredicto y el detalle por servicio", async () => {
    global.fetch = vi.fn(() => Promise.resolve({
      ok: true, status: 200, json: () => Promise.resolve(OK_PAYLOAD),
    }));
    renderPage();
    expect(await screen.findByText("Todos los sistemas operativos")).toBeInTheDocument();
    expect(screen.getByText("Portal y API")).toBeInTheDocument();
    expect(screen.getByText(/99.94% de disponibilidad/)).toBeInTheDocument();
  });

  it("un servicio sin datos dice que no hay datos, NO 100%", async () => {
    // El test de honestidad del frontend: `uptime_pct: null` del backend no
    // puede renderizarse como un número. `Number(null) || 0` daría 0.00% y
    // `?? 100` daría un verde inventado — las dos mentiras son fáciles de
    // escribir sin querer.
    global.fetch = vi.fn(() => Promise.resolve({
      ok: true, status: 200, json: () => Promise.resolve(OK_PAYLOAD),
    }));
    renderPage();
    await screen.findByText("Todos los sistemas operativos");
    expect(screen.getByText("Todavía sin datos suficientes")).toBeInTheDocument();
    expect(screen.queryByText(/100.00% de disponibilidad/)).not.toBeInTheDocument();
    expect(screen.queryByText(/0.00% de disponibilidad/)).not.toBeInTheDocument();
  });

  it("publica la cobertura junto al porcentaje cuando es baja", async () => {
    // Un 100% medido sobre el 4% del período no es un 100%. Mostrarlo
    // pelado es la forma más fácil de que la página mienta sin decir una
    // sola cosa falsa.
    global.fetch = vi.fn(() => Promise.resolve({
      ok: true, status: 200, json: () => Promise.resolve({
        ...OK_PAYLOAD,
        components: [{
          ...OK_PAYLOAD.components[0],
          uptime_pct: 100, coverage_pct: 4,
        }],
      }),
    }));
    renderPage();
    await screen.findByText("Todos los sistemas operativos");
    expect(screen.getByText(/medido sobre 4% del período/)).toBeInTheDocument();
  });

  it("si la API no contesta, la página lo dice en rojo en vez de spinnear", async () => {
    // ESTO es lo que justifica que la página viva en Vercel y la API en
    // Railway: una caída de Railway la deja EN PIE dando exactamente la
    // respuesta que el visitante vino a buscar.
    global.fetch = vi.fn(() => Promise.reject(new Error("offline")));
    renderPage();
    expect(await screen.findByText("No podemos contactar la API de GenLy")).toBeInTheDocument();
    expect(screen.getByText(/nuestra API no responde/)).toBeInTheDocument();
  });

  it("un 500 de la API también se reporta como caída y no como página vacía", async () => {
    global.fetch = vi.fn(() => Promise.resolve({
      ok: false, status: 503, json: () => Promise.resolve({ detail: "nope" }),
    }));
    renderPage();
    expect(await screen.findByText("No podemos contactar la API de GenLy")).toBeInTheDocument();
  });

  it("muestra el timeline del incidente abierto, del más nuevo al más viejo", async () => {
    global.fetch = vi.fn(() => Promise.resolve({
      ok: true, status: 200, json: () => Promise.resolve({
        ...OK_PAYLOAD,
        indicator: "major_outage",
        active_incidents: [{
          id: 3, title: "Caída de la cola de trabajos", status: "identified",
          impact: "critical", components: ["transcription"],
          started_at: "2026-09-03T13:00:00+00:00", resolved_at: null,
          updated_at: "2026-09-03T14:00:00+00:00", resolved: false,
          updates: [
            { id: 2, status: "identified", body: "Encontramos la causa.",
              created_at: "2026-09-03T14:00:00+00:00" },
            { id: 1, status: "investigating", body: "Estamos investigando.",
              created_at: "2026-09-03T13:00:00+00:00" },
          ],
        }],
      }),
    }));
    renderPage();
    expect(await screen.findByText("Caída de la cola de trabajos")).toBeInTheDocument();
    expect(screen.getByText("Hay una caída en curso")).toBeInTheDocument();
    // Los incidentes activos vienen abiertos: el visitante no tiene que
    // hacer un click extra para leer qué pasa.
    expect(screen.getByText(/Encontramos la causa/)).toBeInTheDocument();
    expect(screen.getByText(/Estamos investigando/)).toBeInTheDocument();
  });

  it("sin incidentes pasados lo dice explícitamente", async () => {
    global.fetch = vi.fn(() => Promise.resolve({
      ok: true, status: 200, json: () => Promise.resolve(OK_PAYLOAD),
    }));
    renderPage();
    await screen.findByText("Todos los sistemas operativos");
    expect(screen.getByText("Sin incidentes registrados en este período.")).toBeInTheDocument();
  });

  it("no requiere token: no manda header Authorization", async () => {
    // Si el outage es de login, el cliente no tiene token válido — y es
    // exactamente cuando necesita esta página.
    global.fetch = vi.fn(() => Promise.resolve({
      ok: true, status: 200, json: () => Promise.resolve(OK_PAYLOAD),
    }));
    localStorage.setItem("genly_token", "un-token-viejo");
    renderPage();
    await waitFor(() => expect(global.fetch).toHaveBeenCalled());
    const [, opts] = global.fetch.mock.calls[0];
    expect(opts?.headers?.Authorization).toBeUndefined();
    localStorage.clear();
  });
});
