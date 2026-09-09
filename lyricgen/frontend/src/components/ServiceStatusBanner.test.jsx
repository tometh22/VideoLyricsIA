import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import ServiceStatusBanner from "./ServiceStatusBanner";
import { __resetServiceStatusPollForTests } from "../hooks/useServiceStatusSummary";

vi.mock("../i18n", () => ({
  useI18n: () => ({
    lang: "es",
    setLang: vi.fn(),
    t: (key) => ({
      "service_status.banner_auto_title": "Estamos con problemas en el servicio",
      "service_status.banner_cta": "Ver estado del servicio",
      "service_status.component.transcription": "Transcripción y sincronía",
      "service_status.component.render": "Generación de videos",
      "service_status.incident_status.investigating": "Investigando",
      "common.close": "Cerrar",
    })[key] || key,
  }),
}));

function renderBanner() {
  return render(
    <MemoryRouter>
      <ServiceStatusBanner />
    </MemoryRouter>,
  );
}

function mockSummary(payload, { ok = true, status = 200 } = {}) {
  global.fetch = vi.fn(() => Promise.resolve({
    ok, status, json: () => Promise.resolve(payload),
  }));
}

beforeEach(() => {
  __resetServiceStatusPollForTests();
  localStorage.clear();
});

afterEach(() => {
  cleanup();
  __resetServiceStatusPollForTests();
  vi.restoreAllMocks();
});

describe("ServiceStatusBanner", () => {
  it("no muestra nada cuando todo está operativo", async () => {
    mockSummary({
      indicator: "operational", banner: false, severity: "info",
      incident: null, auto_affected: [], open_incidents: 0,
    });
    renderBanner();
    await waitFor(() => expect(global.fetch).toHaveBeenCalled());
    expect(screen.queryByRole("status")).not.toBeInTheDocument();
  });

  it("muestra el título redactado por el operador, no el genérico", async () => {
    mockSummary({
      indicator: "partial_outage", banner: true, severity: "critical",
      incident: {
        id: 7, title: "Demoras en la transcripción", status: "investigating",
        impact: "major", components: ["transcription"],
        started_at: "2026-09-03T12:00:00+00:00",
        updated_at: "2026-09-03T12:00:00+00:00",
      },
      auto_affected: [], open_incidents: 1,
    });
    renderBanner();
    expect(await screen.findByText("Demoras en la transcripción")).toBeInTheDocument();
    expect(screen.getByText(/Investigando/)).toBeInTheDocument();
    expect(screen.getByText(/Transcripción y sincronía/)).toBeInTheDocument();
    // Un humano ya explicó qué pasa: el copy automático no aparece.
    expect(screen.queryByText("Estamos con problemas en el servicio")).not.toBeInTheDocument();
  });

  it("usa el copy genérico cuando la sonda detectó algo y nadie redactó nada", async () => {
    mockSummary({
      indicator: "major_outage", banner: true, severity: "critical",
      incident: null, auto_affected: ["transcription", "render"],
      open_incidents: 0,
    });
    renderBanner();
    expect(await screen.findByText("Estamos con problemas en el servicio")).toBeInTheDocument();
    expect(screen.getByText(/Transcripción y sincronía, Generación de videos/)).toBeInTheDocument();
  });

  it("un endpoint de status caído NO se convierte en barra roja", async () => {
    // Un bundle viejo contra una API nueva da 404 acá. Anunciar un
    // incidente inexistente gasta la única señal que tenemos para los
    // reales, así que el fallo del propio status es silencioso.
    //
    // El cuerpo del mock es DELIBERADAMENTE un payload que sí mostraría la
    // barra: sin esto el test pasaría igual con `res.ok` ignorado, porque
    // un `{}` no dispara banner por su propia forma y no por la condición.
    mockSummary({
      indicator: "major_outage", banner: true, severity: "critical",
      incident: {
        id: 1, title: "No deberia publicarse", status: "investigating",
        impact: "critical", components: [],
        started_at: "2026-09-03T12:00:00+00:00",
        updated_at: "2026-09-03T12:00:00+00:00",
      },
      auto_affected: [], open_incidents: 1,
    }, { ok: false, status: 404 });
    renderBanner();
    await waitFor(() => expect(global.fetch).toHaveBeenCalled());
    expect(screen.queryByText("No deberia publicarse")).not.toBeInTheDocument();
    expect(screen.queryByRole("status")).not.toBeInTheDocument();
  });

  it("una excepción de red tampoco muestra la barra", async () => {
    global.fetch = vi.fn(() => Promise.reject(new Error("offline")));
    renderBanner();
    await waitFor(() => expect(global.fetch).toHaveBeenCalled());
    expect(screen.queryByRole("status")).not.toBeInTheDocument();
  });

  it("linkea al incidente puntual dentro de /status", async () => {
    mockSummary({
      indicator: "major_outage", banner: true, severity: "critical",
      incident: {
        id: 42, title: "Caída de la cola", status: "identified",
        impact: "critical", components: [],
        started_at: "2026-09-03T12:00:00+00:00",
        updated_at: "2026-09-03T12:30:00+00:00",
      },
      auto_affected: [], open_incidents: 1,
    });
    renderBanner();
    const link = await screen.findByRole("link", { name: "Ver estado del servicio" });
    expect(link).toHaveAttribute("href", "/status?incident=42");
  });

  it("cerrar la barra la oculta y persiste el descarte", async () => {
    const payload = {
      indicator: "major_outage", banner: true, severity: "critical",
      incident: {
        id: 9, title: "Algo se rompió", status: "investigating",
        impact: "critical", components: [],
        started_at: "2026-09-03T12:00:00+00:00",
        updated_at: "2026-09-03T12:00:00+00:00",
      },
      auto_affected: [], open_incidents: 1,
    };
    mockSummary(payload);
    renderBanner();
    await screen.findByText("Algo se rompió");
    fireEvent.click(screen.getByRole("button", { name: "Cerrar" }));
    expect(screen.queryByText("Algo se rompió")).not.toBeInTheDocument();

    // Al remontar sigue descartada.
    cleanup();
    __resetServiceStatusPollForTests();
    mockSummary(payload);
    renderBanner();
    await waitFor(() => expect(global.fetch).toHaveBeenCalled());
    expect(screen.queryByText("Algo se rompió")).not.toBeInTheDocument();
  });

  it("una actualización nueva del MISMO incidente vuelve a mostrarse", async () => {
    // El bug que este test pinea: con la key de descarte basada sólo en el
    // id, quien cierra la barra temprano no vería nunca el aviso de que el
    // incidente empeoró. La key incluye `updated_at`.
    const base = {
      indicator: "major_outage", banner: true, severity: "critical",
      auto_affected: [], open_incidents: 1,
    };
    const inc = {
      id: 9, title: "Algo se rompió", status: "investigating",
      impact: "critical", components: [],
      started_at: "2026-09-03T12:00:00+00:00",
    };
    mockSummary({ ...base, incident: { ...inc, updated_at: "2026-09-03T12:00:00+00:00" } });
    renderBanner();
    await screen.findByText("Algo se rompió");
    fireEvent.click(screen.getByRole("button", { name: "Cerrar" }));

    cleanup();
    __resetServiceStatusPollForTests();
    mockSummary({
      ...base,
      incident: { ...inc, status: "identified", updated_at: "2026-09-03T13:30:00+00:00" },
    });
    renderBanner();
    expect(await screen.findByText("Algo se rompió")).toBeInTheDocument();
  });

  it("hace UN solo request aunque haya dos consumidores montados", async () => {
    // El poll es un singleton de módulo: el rate limit por IP es 120/min y
    // se comparte entre todos los usuarios detrás del mismo NAT.
    mockSummary({
      indicator: "operational", banner: false, severity: "info",
      incident: null, auto_affected: [], open_incidents: 0,
    });
    render(
      <MemoryRouter>
        <ServiceStatusBanner />
        <ServiceStatusBanner />
      </MemoryRouter>,
    );
    await waitFor(() => expect(global.fetch).toHaveBeenCalled());
    expect(global.fetch).toHaveBeenCalledTimes(1);
  });
});
