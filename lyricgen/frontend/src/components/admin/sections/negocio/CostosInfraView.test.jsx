/**
 * Tests de la vista de costo de infraestructura.
 *
 * El requisito con el que se pidió este panel fue literal: "100% de
 * confianza para no tener que hacer esto todos los meses a mano". De ahí
 * sale lo único que estos tests protegen: **un total incompleto no se
 * puede ver igual que un total completo**. Un día que el proveedor no
 * contestó no tiene filas, así que sin un aviso explícito se dibuja
 * idéntico a un día barato — y el panel muestra una caída que parece una
 * buena noticia.
 */
import { render, screen, cleanup } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

import { AdminProvider } from "../../AdminContext";
import CostosInfraView from "./CostosInfraView";

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
});

const BASE = {
  since: "2026-08-21", until: "2026-08-25",
  granularity: "day", group_by: "source",
  total_usd: 37.96,
  by_group: { railway: 29.86, r2: 7.36, replicate: 0.74 },
  series: [
    { bucket: "2026-08-21", total: 5.21, by: { railway: 3.64, r2: 1.45, replicate: 0.12 } },
    { bucket: "2026-08-22", total: 4.24, by: { railway: 2.75, r2: 1.46, replicate: 0.03 } },
    { bucket: "2026-08-23", total: 3.54, by: { railway: 2.08, r2: 1.45, replicate: 0.01 } },
  ],
  invoiced_usd: 0, estimated_usd: 37.96, estimated_share: 1.0,
  openai_line_item_filter: ["whisper", "gpt-4o-mini"],
  coverage: { expected_cells: 30, collected_cells: 30, complete: true,
              missing: [], missing_total: 0 },
};

function montar(overrides = {}) {
  const props = {
    data: { ...BASE, ...overrides }, loading: false,
    granularity: "day", setGranularity: vi.fn(),
    groupBy: "source", setGroupBy: vi.fn(),
    colectar: vi.fn(), colectando: false,
    since: BASE.since, until: BASE.until,
  };
  return render(<AdminProvider><CostosInfraView {...props} /></AdminProvider>);
}

describe("cobertura incompleta", () => {
  it("rotula el total como PISO y nombra qué fuente falta", () => {
    montar({
      coverage: {
        expected_cells: 30, collected_cells: 25, complete: false,
        missing_total: 5,
        missing: Array.from({ length: 5 }, (_, i) => ({
          day: `2026-08-2${i + 1}`, source: "gcp", status: "error",
        })),
      },
    });

    expect(screen.getByText(/el total es un piso, no el total/i)).toBeTruthy();
    // El KPI grande cambia de rótulo, no sólo el banner: alguien que mira
    // el número sin leer el aviso tiene que ver igual que está incompleto.
    expect(screen.getByText(/total \(piso\)/i)).toBeTruthy();
    // Y dice QUÉ falta, no sólo cuánto.
    expect(screen.getByText(/Google Vertex: 5 días/)).toBeTruthy();
  });

  it("no muestra ningún aviso cuando la cobertura está completa", () => {
    montar();
    expect(screen.queryByText(/el total es un piso/i)).toBeNull();
    expect(screen.getByText(/total del rango/i)).toBeTruthy();
  });
});

describe("factura vs modelo propio", () => {
  it("separa lo facturado de lo que estimamos nosotros", () => {
    montar({ invoiced_usd: 20, estimated_usd: 17.96, estimated_share: 0.473 });
    expect(screen.getByText(/sale de una factura/i)).toBeTruthy();
    expect(screen.getByText("$20.00")).toBeTruthy();
    expect(screen.getByText(/47% del total/)).toBeTruthy();
  });

  it("avisa cuando más de la mitad del total es modelo nuestro", () => {
    // Railway, R2 y Replicate no exponen importe por API: se valorizan con
    // tarifas nuestras. Que eso sea la mayoría del total es información
    // que cambia cuánto se puede confiar en el número.
    const { container } = montar({ estimated_share: 1.0 });
    expect(screen.getByText(/100% del total/)).toBeTruthy();
    expect(container.textContent).toMatch(/métrica valorizada por nosotros/);
  });
});

describe("desglose", () => {
  it("ordena por costo y muestra el share de cada fuente", () => {
    montar();
    const filas = screen.getAllByText(/^\$\d/);
    // "Railway" aparece dos veces a propósito: en la leyenda del gráfico y
    // en la tabla. Las dos son identidad de la misma serie.
    expect(screen.getAllByText("Railway").length).toBeGreaterThanOrEqual(1);
    expect(screen.getByText("78.7%")).toBeTruthy();
    expect(filas.length).toBeGreaterThan(0);
  });

  it("explica fijo/variable/stock cuando se abre por comportamiento", () => {
    const props = {
      data: {
        ...BASE, group_by: "behavior",
        by_group: { fijo: 29.86, stock: 7.35, variable: 0.75 },
        series: BASE.series.map((b) => ({
          ...b, by: { fijo: b.by.railway, stock: b.by.r2, variable: b.by.replicate },
        })),
      },
      loading: false,
      granularity: "day", setGranularity: vi.fn(),
      groupBy: "behavior", setGroupBy: vi.fn(),
      colectar: vi.fn(), colectando: false,
      since: BASE.since, until: BASE.until,
    };
    const { container } = render(
      <AdminProvider><CostosInfraView {...props} /></AdminProvider>);

    expect(screen.getAllByText("Fijo (capacidad)").length).toBeGreaterThanOrEqual(1);
    // La explicación no es decorativa: sin ella, el "$/video" baja al subir
    // el volumen y parece una mejora cuando recorta la ganancia absoluta.
    expect(container.textContent).toMatch(/baja al subir el volumen/);
  });
});

describe("estados de borde", () => {
  it("no intenta dibujar una evolución con un solo bucket", () => {
    montar({ series: [BASE.series[0]] });
    expect(screen.getByText(/al menos 2 buckets/i)).toBeTruthy();
  });

  it("muestra el vacío cuando no hay datos en absoluto", () => {
    render(
      <AdminProvider>
        <CostosInfraView
          data={null} loading={false}
          granularity="day" setGranularity={vi.fn()}
          groupBy="source" setGroupBy={vi.fn()}
          colectar={vi.fn()} colectando={false}
          since="2026-08-01" until="2026-08-05"
        />
      </AdminProvider>);
    expect(screen.getByText(/sin datos de costo/i)).toBeTruthy();
  });
});


describe("límite de series", () => {
  it("pliega el 7º grupo en “Otros” en vez de generar un color nuevo", () => {
    // Ocho hues es el límite del sistema y el ORDEN es el mecanismo de
    // seguridad CVD. Ciclar la paleta haría que dos series distintas
    // compartan color — peor que agrupar.
    const nueve = {
      "gcp:Veo": 40, "gcp:Gemini": 20, "gcp:Imagen": 10, "r2:storage": 7,
      "railway:mem": 5, "railway:cpu": 3, "replicate:demucs": 2,
      "replicate:whisperx": 1, "openai:whisper": 0.5,
    };
    montar({
      group_by: "sku", by_group: nueve, total_usd: 88.5,
      series: [
        { bucket: "2026-08-21", total: 44.25, by: nueve },
        { bucket: "2026-08-22", total: 44.25, by: nueve },
      ],
    });

    expect(screen.getAllByText("Otros").length).toBeGreaterThanOrEqual(1);
    expect(screen.getByText("(4 más)")).toBeTruthy();
    // Los 4 plegados no aparecen sueltos en la tabla.
    expect(screen.queryByText("replicate:whisperx")).toBeNull();
  });

  it("no pliega nada cuando hay 6 grupos o menos", () => {
    montar();
    expect(screen.queryByText("Otros")).toBeNull();
  });
});

describe("una fuente que contesta ok pero devuelve cero", () => {
  it("avisa aunque la cobertura esté completa", () => {
    // El caso real, medido en staging el 1-sep-2026: agosto con las 31
    // celdas de GCP en `ok` y `complete: true`, y 30 de esos 31 días en
    // $0,00 porque el export de facturación cortaba el 1-ago. En verde,
    // eso se lee como un mes barato en vez de un mes sin datos.
    montar({
      stale_sources: [{
        source: "gcp", last_nonzero_day: "2026-08-01", zero_days: 30,
        reported_usd: 3.97,
      }],
    });
    expect(screen.getByText(/contestó bien pero devolvió cero/i)).toBeTruthy();
    // Dice QUÉ fuente y desde cuándo, no sólo que algo pasa.
    expect(screen.getByText(/Google Vertex/)).toBeTruthy();
    expect(screen.getByText("2026-08-01")).toBeTruthy();
    expect(screen.getByText("30")).toBeTruthy();
    // Y el banner de cobertura sigue callado: son dos preguntas distintas.
    expect(screen.queryByText(/el total es un piso, no el total/i)).toBeNull();
  });

  it("no dice nada cuando ninguna fuente está sospechosa", () => {
    montar({ stale_sources: [] });
    expect(screen.queryByText(/contestó bien pero devolvió cero/i)).toBeNull();
  });
});
