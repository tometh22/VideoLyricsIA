/**
 * La solapa Finanzas carga renglones + métricas y arma la planilla.
 * Protege que el ingreso salga de las entregas del contrato (no se carga a
 * mano) y que una celda vacía se pueda cargar con un clic.
 */
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import * as adminApi from "../../adminApi";
import FinanzasSection from "./FinanzasSection";

const ENTRIES = [
  // Números ficticios: los reales no viven en el repo.
  { id: 1, period: "2026-08", section: "contrato", contract: "Contrato X", concept: "Contrato X", basis: "acuerdo",
    payload: { inicio: "2026-08", meses: 2, cupo_vl_mes: 300, cupo_at_mes: 200, precio_vl: 10, precio_at: 3, portales: ["argentina"] } },
  { id: 2, period: "2026-09", section: "costo_directo", category: "Infraestructura", provider: "Railway", amount_usd: 100, basis: "api", evidence: "uso del mes" },
];
const OPS = {
  deliveries: [{ month: "2026-09", portal: "argentina", type: "lyric_video", entregas: 10, canciones: 10 }],
  received: [], reviewers: [], change_requests: [], lead_time: [], change_requests_open: 0,
};

let calls;
beforeEach(() => {
  calls = [];
  vi.spyOn(adminApi, "fetchJson").mockImplementation((url, opts = {}) => {
    calls.push({ url, opts });
    if (url.includes("/admin/finance/entries") && !opts.method) return Promise.resolve({ entries: ENTRIES });
    if (url.includes("/admin/finance/operations")) return Promise.resolve(OPS);
    return Promise.resolve({ id: 99 });
  });
});
afterEach(() => { cleanup(); vi.restoreAllMocks(); });

describe("FinanzasSection", () => {
  it("el ingreso sale de las entregas × precio del contrato", async () => {
    render(<FinanzasSection subTab="resultado" />);
    await waitFor(() => expect(screen.getByText("Entregas del contrato")).toBeTruthy());
    // 10 lyric videos × US$ 10 en septiembre.
    expect(screen.getAllByText("100,00").length).toBeGreaterThan(0);
    expect(screen.getByText("Railway")).toBeTruthy();
    expect(calls.some((c) => c.url.includes("operations?since=2026-08"))).toBe(true);
  });

  it("una celda vacía se carga con un clic y crea el renglón", async () => {
    render(<FinanzasSection subTab="resultado" />);
    await waitFor(() => expect(screen.getByText("Railway")).toBeTruthy());
    const row = screen.getByText("Railway").closest("tr");
    const cells = row.querySelectorAll("td");
    fireEvent.click(cells[2]); // agosto, vacío
    const input = screen.getByLabelText("Monto en USD");
    fireEvent.change(input, { target: { value: "55,5" } });
    fireEvent.blur(input);
    await waitFor(() => expect(calls.some((c) => c.opts.method === "POST")).toBe(true));
    const post = calls.find((c) => c.opts.method === "POST");
    expect(JSON.parse(post.opts.body)).toMatchObject({ section: "costo_directo", period: "2026-08", provider: "Railway", amount_usd: 55.5 });
  });

  it("muestra el criterio sin pedir datos extra", async () => {
    render(<FinanzasSection subTab="criterio" />);
    await waitFor(() => expect(screen.getByText("El criterio de esta planilla")).toBeTruthy());
    expect(screen.getByText("CFO")).toBeTruthy();
  });
});
