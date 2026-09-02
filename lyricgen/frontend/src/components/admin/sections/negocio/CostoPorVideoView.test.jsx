/**
 * Tests del bloque "Costo por video".
 *
 * Lo único que protegen: **el número grande sale del denominador honesto**.
 * Medido en ago-2026, dividir por `videos_delivered` en vez de por
 * `delivered_client_songs` da la mitad del costo real, y esa mitad es
 * exactamente el número que alguien copia para cotizarle a un sello. Un
 * error de UI acá se paga en dinero, no en incomodidad.
 */
import { render, screen, cleanup } from "@testing-library/react";
import { afterEach, describe, expect, it } from "vitest";

import CostoPorVideoView from "./CostoPorVideoView";

afterEach(cleanup);

// Números reales de julio-2026 redondeados: $309 de factura, 163 jobs
// entregados, 77 de cliente, 27 canciones.
const BASE = {
  period: "2026-07",
  real_cost_usd: 309.0,
  cost_complete: true,
  missing_sources: [],
  videos_created: 470,
  videos_delivered: 163,
  delivered_client_jobs: 77,
  delivered_client_songs: 27,
  counted_environments: 2,
  cost_per_delivered: 1.8957,
  cost_per_created_MISLEADING: 0.6574,
  cost_per_client_song: 11.4444,
  price_per_video_usd: 13.5,
  cost_per_delivered_is_floor: false,
};

const montar = (o = {}) =>
  render(<CostoPorVideoView data={{ ...BASE, ...o }} loading={false} />);

describe("el número grande", () => {
  it("divide por canciones de cliente, no por jobs entregados", () => {
    montar();
    // $309 / 27 = $11.44. Dividir por los 163 entregados daría $1.90:
    // seis veces más barato, y con margen positivo donde no lo hay.
    expect(screen.getByText("$11.44")).toBeTruthy();
    expect(screen.queryByText("$1.90")).toBeNull();
    expect(screen.getByText(/\$309\.00 ÷ 27 canciones/)).toBeTruthy();
  });

  it("el margen se calcula contra ese mismo costo", () => {
    montar();
    // 13.50 − 11.44 = 2.06. Contra el denominador crudo daría $11.60 y
    // el negocio parecería cinco veces más rentable de lo que es.
    expect(screen.getByText("$2.06")).toBeTruthy();
  });

  it("marca el margen en rojo cuando el costo se come el precio", () => {
    const { container } = montar({ cost_per_client_song: 20.0 });
    expect(screen.getByText("$-6.50")).toBeTruthy();
    expect(container.querySelector(".text-red-400")).toBeTruthy();
  });
});

describe("los cuatro denominadores", () => {
  it("muestra los cuatro y cuánto infla cada uno", () => {
    montar();
    expect(screen.getByText("470")).toBeTruthy();
    expect(screen.getByText("163")).toBeTruthy();
    expect(screen.getByText("77")).toBeTruthy();
    expect(screen.getByText("27")).toBeTruthy();
    // 163/27 = 6.0x — el multiplicador es el argumento de toda la vista.
    expect(screen.getByText("×6.0")).toBeTruthy();
    expect(screen.getByText("×17.4")).toBeTruthy();
  });

  it("señala cuál es el correcto sin esconder los otros", () => {
    montar();
    expect(screen.getByText(/el correcto/i)).toBeTruthy();
    expect(screen.getByText(/La unidad que se factura/)).toBeTruthy();
  });
});

describe("avisos que cambian cómo se lee el número", () => {
  it("dice PISO en el KPI, no sólo en una nota al pie", () => {
    montar({ cost_per_delivered_is_floor: true, cost_complete: false,
             missing_sources: ["gcp"] });
    expect(screen.getByText(/costo \/ canción \(piso\)/i)).toBeTruthy();
    expect(screen.getByText(/faltan: gcp/)).toBeTruthy();
  });

  it("avisa que un solo entorno sobre-estima el costo", () => {
    // La factura de Railway/GCP cubre prod Y staging. Contar los videos de
    // uno solo pone el numerador entero sobre la mitad del denominador.
    montar({ counted_environments: 1 });
    expect(screen.getByText(/sobre-estimado/)).toBeTruthy();
  });

  it("no inventa un costo cuando no hay snapshot", () => {
    montar({ cost_per_client_song: null, real_cost_usd: 0 });
    // Un "$0.00" acá se lee como margen del 100%.
    expect(screen.getAllByText("—").length).toBeGreaterThanOrEqual(1);
  });
});

describe("estados", () => {
  it("muestra el vacío en vez de una división por cero", () => {
    render(<CostoPorVideoView data={null} loading={false} />);
    expect(screen.getByText(/sin datos de costo por video/i)).toBeTruthy();
  });
});
