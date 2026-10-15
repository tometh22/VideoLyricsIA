/**
 * Cálculos de Finanzas. Lo que protegen:
 * - el ingreso devengado sale de lo ENTREGADO × precio, con tope en el cupo;
 * - una pieza cuenta para un solo contrato;
 * - el reparto se calcula sobre el acumulado (un mes en pérdida compensa);
 * - el desarrollo de Tomi nunca se resta del reparto;
 * - el CSV ida y vuelta conserva los renglones.
 */
import { describe, expect, it } from "vitest";

import {
  allocateContracts, buildPnl, contractStatus, csvToEntries, effectiveEntries, entriesToCsv,
  monthChecklist, monthsBetween, partnerScenarios, workDays, DEFAULT_PARAMS,
} from "./finanzasModel";

// Números ficticios: los términos reales no viven en el repo.
const CONTRATO = {
  nombre: "Contrato X", inicio: "2026-08", meses: 2, cupo_vl_mes: 300, cupo_at_mes: 200,
  precio_vl: 10, precio_at: 3, portales: ["argentina", "chile"],
};
const ENTREGAS = [
  { month: "2026-08", portal: "argentina", type: "lyric_video", entregas: 50, canciones: 49 },
  { month: "2026-09", portal: "argentina", type: "lyric_video", entregas: 60, canciones: 60 },
  { month: "2026-09", portal: "chile", type: "lyric_video", entregas: 20, canciones: 20 },
  { month: "2026-10", portal: "argentina", type: "art_track", entregas: 100, canciones: 100 },
  { month: "2026-07", portal: "argentina", type: "lyric_video", entregas: 30, canciones: 30 },
];
const TODAY = new Date(2026, 9, 15);

describe("contratos", () => {
  it("devenga lo entregado desde el inicio, con tope de cupo", () => {
    const small = { ...CONTRATO, meses: 1, cupo_vl_mes: 100 };
    const alloc = allocateContracts([small], ENTREGAS)[small.nombre];
    expect(alloc.used.lyric_video).toBe(100);
    expect(alloc.byMonth["2026-08"].lyric_video).toBe(49);
    expect(alloc.byMonth["2026-09"].lyric_video).toBe(51);
    expect(alloc.byMonth["2026-07"]).toBeUndefined();
  });

  it("una pieza cuenta para un solo contrato", () => {
    const a = { ...CONTRATO, nombre: "A", meses: 1, cupo_vl_mes: 100 };
    const b = { ...CONTRATO, nombre: "B", inicio: "2026-09", meses: 1 };
    const alloc = allocateContracts([a, b], ENTREGAS);
    expect(alloc.A.used.lyric_video + alloc.B.used.lyric_video).toBe(49 + 60 + 20);
  });

  it("estado: anticipo = cobrado − devengado", () => {
    const entries = [{ section: "cobro", amount_usd: 3000, contract: "Contrato X" }];
    const st = contractStatus(CONTRATO, { deliveries: ENTREGAS, received: [] }, entries);
    expect(st.devengado).toBe((49 + 60 + 20) * 10 + 100 * 3);
    expect(st.anticipo).toBe(3000 - st.devengado);
  });
});

describe("resultado y reparto", () => {
  const entries = [
    { section: "costo_directo", period: "2026-08", amount_usd: 900, category: "Infra", provider: "Railway" },
    { section: "costo_directo", period: "2026-09", amount_usd: 100, category: "Infra", provider: "Railway" },
    { section: "estructura", period: "2026-09", amount_usd: 20, provider: "Vercel" },
  ];
  const months = monthsBetween("2026-08", "2026-10");

  it("el reparto total va sobre el acumulado, no sobre la suma de meses positivos", () => {
    const params = { ...DEFAULT_PARAMS, resta_trabajo: "ninguno", pct_socio: 0.5 };
    const pnl = buildPnl({ entries, contracts: [CONTRATO], deliveries: ENTREGAS, params, months, today: TODAY });
    expect(pnl.perMonth["2026-08"].operativo).toBe(49 * 10 - 900);
    const operativo = pnl.total.ingreso - 1000 - 20;
    expect(pnl.total.operativo).toBeCloseTo(operativo);
    expect(pnl.total.socio).toBeCloseTo(operativo * 0.5);
  });

  it("el desarrollo de Tomi no se resta nunca", () => {
    const params = { ...DEFAULT_PARAMS, resta_trabajo: "agus_y_tomi", tarifa_hora: 10, horas_dia_agus: 2, horas_dia_tomi: 2, pct_operacion_tomi: 0.25, pct_socio: 0.5 };
    const pnl = buildPnl({ entries, contracts: [CONTRATO], deliveries: ENTREGAS, params, months, today: TODAY });
    const t = pnl.total;
    expect(t.trabajoRestado).toBeCloseTo(t.trabajo_agus + t.trabajo_tomi_op);
    expect(t.trabajo_tomi_dev).toBeGreaterThan(0);
    expect(t.baseReparto).toBeCloseTo(t.operativo - t.trabajo_agus - t.trabajo_tomi_op);
  });

  it("escenarios: más tarifa, menos para el socio", () => {
    const params = { ...DEFAULT_PARAMS, resta_trabajo: "agus", horas_dia_agus: 2, pct_socio: 0.5 };
    const pnl = buildPnl({ entries, contracts: [CONTRATO], deliveries: ENTREGAS, params, months, today: TODAY });
    const [s0, s5] = partnerScenarios(pnl, params, [0, 5]);
    expect(s0.socio).toBeGreaterThan(s5.socio);
    expect(s5.trabajo).toBeCloseTo(s5.horas * 5);
  });
});

describe("utilidades", () => {
  it("días hábiles: el mes en curso cuenta hasta hoy; el futuro, cero", () => {
    expect(workDays("2026-09", { today: TODAY })).toBe(22);
    expect(workDays("2026-10", { today: TODAY })).toBe(11);
    expect(workDays("2026-11", { today: TODAY })).toBe(0);
    expect(workDays("2026-09", { soloHabiles: false, today: TODAY })).toBe(30);
  });

  it("CSV ida y vuelta, con comas y comillas", () => {
    const rows = [{ period: "2026-09", section: "costo_directo", category: "IA", provider: "OpenAI",
      concept: 'Whisper, "clave de prueba"', amount_usd: 12.34, basis: "captura", contract: null,
      evidence: "https://example.com/usage", note: null }];
    const back = csvToEntries(entriesToCsv(rows));
    expect(back[0]).toMatchObject({ provider: "OpenAI", concept: 'Whisper, "clave de prueba"', amount_usd: 12.34 });
  });
});

describe("renglones automáticos", () => {
  const months = ["2026-09", "2026-10"];
  const auto = [
    { month: "2026-09", source: "railway", category: "Infraestructura", provider: "Railway", amount_usd: 300, days: 30, expected_days: 30 },
    { month: "2026-10", source: "railway", category: "Infraestructura", provider: "Railway", amount_usd: 50, days: 10, expected_days: 14 },
  ];

  it("lo cargado a mano pisa al recolector del mismo proveedor y mes", () => {
    const entries = [{ id: 1, section: "costo_directo", period: "2026-09", provider: "Railway", amount_usd: 310 }];
    const eff = effectiveEntries({ entries, autoCosts: auto, params: DEFAULT_PARAMS, months, today: TODAY });
    const railway = eff.filter((e) => providerIs(e, "Railway"));
    expect(railway.map((e) => [e.period, e.amount_usd, !!e.synthetic])).toEqual([["2026-09", 310, false], ["2026-10", 50, true]]);
    const check = monthChecklist({ month: "2026-10", entries, effective: eff, autoCosts: auto });
    expect(check.find((i) => i.label === "Railway").ok).toBe(false);
    expect(check.find((i) => i.label === "OpenAI").ok).toBe(false);
  });

  it("un gasto recurrente se repite hasta que se carga otro", () => {
    const entries = [{ id: 2, section: "estructura", period: "2026-08", provider: "GitHub Pro", amount_usd: 4, payload: { recurrente: true } },
      { id: 3, section: "estructura", period: "2026-10", provider: "GitHub Pro", amount_usd: 5 }];
    const eff = effectiveEntries({ entries, params: DEFAULT_PARAMS, months, today: TODAY });
    const gh = eff.filter((e) => e.provider === "GitHub Pro" && months.includes(e.period));
    expect(gh.map((e) => [e.period, e.amount_usd])).toEqual([["2026-10", 5], ["2026-09", 4]]);
  });

  it("operadores: letras aprobadas × tarifa + comisión, salvo que el mes ya tenga Upwork cargado", () => {
    const params = { ...DEFAULT_PARAMS, operadores: [{ usuario: "op1", nombre: "Uno", tarifa_cancion: 0.5 }], comision_operadores: 0.1 };
    const reviewers = [{ month: "2026-09", reviewer: "op1", canciones: 20 }, { month: "2026-10", reviewer: "op1", canciones: 8 }];
    const entries = [{ id: 4, section: "mano_obra", period: "2026-10", provider: "Upwork — Uno", amount_usd: 7 }];
    const eff = effectiveEntries({ entries, reviewers, params, months, today: TODAY });
    const sep = eff.filter((e) => e.section === "mano_obra" && e.period === "2026-09");
    expect(sep.map((e) => e.amount_usd)).toEqual([10, 1]);
    expect(eff.filter((e) => e.section === "mano_obra" && e.period === "2026-10").map((e) => e.amount_usd)).toEqual([7]);
  });
});

const providerIs = (e, name) => e.provider === name && e.section === "costo_directo";
