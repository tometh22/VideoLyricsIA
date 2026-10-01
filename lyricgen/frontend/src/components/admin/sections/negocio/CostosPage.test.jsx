/**
 * Tests de la página consolidada de Costos.
 *
 * Lo que protegen es la razón por la que la página existe: antes había tres
 * pantallas de costo con tres controles de tiempo distintos, y abiertas de a
 * una parecían coherentes. **Los tres bloques tienen que mirar el mismo
 * período, y decir cuál de sus números es la factura y cuál una estimación.**
 */
import { render, screen, cleanup, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import * as adminApi from "../../adminApi";
import { AdminProvider } from "../../AdminContext";
import CostosPage from "./CostosPage";

const SERIES = {
  since: "2026-07-01", until: "2026-07-31", total_usd: 309.0,
  by_group: { railway: 91.89, gcp: 142.87, r2: 26.49 },
  series: [
    { bucket: "2026-07-01", total: 10, by: { railway: 3, gcp: 5, r2: 2 } },
    { bucket: "2026-07-02", total: 11, by: { railway: 3, gcp: 6, r2: 2 } },
  ],
  invoiced_usd: 142.87, estimated_usd: 166.13, estimated_share: 0.54,
  coverage: { expected_cells: 186, collected_cells: 186, complete: true,
              missing: [], missing_total: 0 },
};
const UNIDAD = {
  period: "2026-07", real_cost_usd: 309.0, cost_complete: true,
  missing_sources: [], videos_created: 470, videos_delivered: 163,
  delivered_client_jobs: 77, delivered_client_songs: 27,
  counted_environments: 2, cost_per_delivered: 1.8957,
  cost_per_client_song: 11.4444, price_per_video_usd: 13.5,
  cost_per_delivered_is_floor: false, portal_disponible: true,
};
const ATRIBUCION = {
  total_cost: 250.0, total_calls: 900, since_days: 31,
  video_counts: { done: 40, pending_review: 5, rejected: 3, error: 1,
                  deliverable: 45 },
  rejection_rate: 0.0625, cost_per_deliverable: 5.55,
  by_provider: [{ provider: "gcp", calls: 500, cost: 200 }],
  by_tenant: [{ tenant_id: "universal_chile", calls: 500, cost: 200,
                done: 30, pending_review: 3, rejected: 2, deliverable: 33,
                cost_per_deliverable: 6.06, rejection_rate: 0.06 }],
  by_user: [], by_tool: [],
};

function rutear(url) {
  if (url.includes("/admin/costs/series")) return Promise.resolve(SERIES);
  if (url.includes("/admin/cost/unit-economics")) return Promise.resolve(UNIDAD);
  if (url.includes("/admin/margin")) return Promise.resolve(ATRIBUCION);
  return Promise.resolve({});
}

beforeEach(() => {
  vi.spyOn(adminApi, "fetchJson").mockImplementation(rutear);
});
afterEach(() => { cleanup(); vi.restoreAllMocks(); });

const montar = () => render(<AdminProvider><CostosPage /></AdminProvider>);

describe("un solo período para los tres bloques", () => {
  it("las tres consultas piden el MISMO mes", async () => {
    montar();
    // AdminContext también usa fetchJson, así que no se cuentan llamadas:
    // se busca cada una por su URL.
    await waitFor(() => {
      const u = adminApi.fetchJson.mock.calls.map((c) => c[0]).join(" ");
      expect(u).toContain("/admin/costs/series");
      expect(u).toContain("/admin/cost/unit-economics");
      expect(u).toContain("/admin/margin");
    });
    const urls = adminApi.fetchJson.mock.calls.map((c) => c[0]);

    const serie = urls.find((u) => u.includes("/admin/costs/series"));
    const unidad = urls.find((u) => u.includes("/admin/cost/unit-economics"));
    const margen = urls.find((u) => u.includes("/admin/margin"));

    // El bug que la página vino a resolver: /admin/margin contaba
    // `since_days` desde HOY, así que mostraba los últimos 30 días al lado
    // de la factura de julio, bajo el mismo encabezado.
    const mes = serie.match(/since=(\d{4}-\d{2})-01/)[1];
    expect(unidad).toContain(`period=${mes}`);
    expect(margen).toContain(`since=${mes}-01`);
    expect(margen).not.toContain("since_days");
  });

  it("arranca en el mes pasado, el único que puede estar completo", async () => {
    montar();
    await waitFor(() => expect(adminApi.fetchJson).toHaveBeenCalled());
    const hoy = new Date();
    const pasado = new Date(Date.UTC(hoy.getUTCFullYear(),
                                     hoy.getUTCMonth() - 1, 1));
    const esperado = `${pasado.getUTCFullYear()}-`
      + `${String(pasado.getUTCMonth() + 1).padStart(2, "0")}`;
    expect(adminApi.fetchJson.mock.calls.map((c) => c[0]).join(" "))
      .toContain(`period=${esperado}`);
  });
});

describe("cada bloque dice qué clase de número es", () => {
  it("la factura y la estimación no se presentan igual", async () => {
    const { container } = montar();
    await waitFor(() => expect(screen.getByText("$11.44")).toBeTruthy());

    // Bloque 1: la factura.
    expect(container.textContent).toMatch(/Es la factura, no una estimación/);
    // Bloque 3: explícitamente una estimación, y por qué existe igual.
    expect(container.textContent).toMatch(/Esto es una .*estimación/);
    expect(container.textContent)
      .toMatch(/el proveedor cobra por proyecto, no por cliente/);
  });

  it("el costo por video sale del denominador honesto", async () => {
    montar();
    // $309 / 27 canciones. Por los 163 entregados daría $1.90.
    await waitFor(() => expect(screen.getByText("$11.44")).toBeTruthy());
    expect(screen.queryByText("$1.90")).toBeNull();
  });

  it("muestra el desvío entre el modelo y la factura", async () => {
    montar();
    // $250 modelado vs $309 facturado = -19%. Es el único chequeo
    // automático de si la tabla de tarifas envejeció.
    await waitFor(() => expect(screen.getByText("-19%")).toBeTruthy());
  });
});
