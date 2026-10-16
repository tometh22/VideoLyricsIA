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
  liquidacion, liquidacionSnapshot, liquidacionTexto, monthChecklist, parseNumberAr, productionShares, monthsBetween, partnerScenarios, workDays, DEFAULT_PARAMS,
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

describe("liquidación por cuota", () => {
  const contract = { nombre: "Contrato X", inicio: "2026-08", meses: 6, fee_mensual: 1000 };
  const params = { ...DEFAULT_PARAMS, pct_socio: 0.5 };
  const entries = [
    // IA gastada en octubre pero imputada a la cuota de agosto.
    { section: "costo_directo", category: "IA", provider: "Google Cloud (Vertex)", period: "2026-10", amount_usd: 50, payload: { cuota: "2026-08" } },
    { section: "costo_directo", category: "Infraestructura", provider: "Railway", period: "2026-08", amount_usd: 100 },
    // Servidores de septiembre: van a la cuota de septiembre, no a agosto.
    { section: "costo_directo", category: "Infraestructura", provider: "Railway", period: "2026-09", amount_usd: 999 },
    { section: "estructura", provider: "Vercel", period: "2026-08", amount_usd: 10 },
    { section: "mano_obra", provider: "Operadores", period: "2026-10", amount_usd: 140, payload: { cuota: "2026-08", horas: 20, personas: 3 } },
    { section: "cobro", period: "2026-10", amount_usd: 1000, payload: { cuota: "2026-08", tipo_cambio: 1500 } },
    { section: "pago_socio", period: "2026-10", amount_usd: 300, payload: { cuota: "2026-08" } },
  ];

  it("arma la tabla del socio con la regla acordada", () => {
    const l = liquidacion({ entries, contracts: [contract], params, cuota: "2026-08" });
    expect(l.lines.map((x) => x.usd)).toEqual([50, 100, 10, 140]);
    expect(l.lines[0].label).toBe("IA (Google Cloud)");
    expect(l.lines[3].label).toBe("Operadores (3) · 20 h");
    expect(l.ganancia).toBe(700);
    expect(l.socio).toBe(350);
    expect(l.socioArs).toBe(525000);
    expect(l.saldo).toBe(50);
    expect(liquidacionTexto(l)).toContain("50% de ganancia: US$ 350,00 = ARS 525.000");
  });
});

describe("automático por cuota", () => {
  const contract = { nombre: "C", inicio: "2026-08", meses: 3, cupo_vl_mes: 10, cupo_at_mes: 5, precio_vl: 10, precio_at: 2, fee_mensual: 1000, portales: ["argentina"] };
  const del = (month, type, n) => ({ month, portal: "argentina", type, entregas: n, canciones: n });

  it("la producción va a la cuota de las piezas que entregó (FIFO), aunque se entregue tarde", () => {
    const shares = productionShares(contract, [del("2026-08", "lyric_video", 4), del("2026-10", "lyric_video", 10)], ["2026-09"]);
    expect(shares["2026-08"]).toEqual({ "2026-08": 1 });
    // Septiembre sin entregas: produce para la cuota más vieja sin completar.
    expect(shares["2026-09"]).toEqual({ "2026-08": 1 });
    // Octubre completa la cuota 1 (6 piezas) y empieza la 2 (4 piezas).
    expect(shares["2026-10"]["2026-08"]).toBeCloseTo(0.6);
    expect(shares["2026-10"]["2026-09"]).toBeCloseTo(0.4);
  });

  it("liquidación: IA de un mes se reparte entre cuotas; servidores quedan en su mes", () => {
    const entries = [
      { section: "costo_directo", category: "IA", provider: "Google Cloud", period: "2026-10", amount_usd: 100 },
      { section: "costo_directo", category: "Infraestructura", provider: "Railway", period: "2026-10", amount_usd: 50 },
    ];
    const deliveries = [del("2026-08", "lyric_video", 4), del("2026-10", "lyric_video", 10)];
    const params = { ...DEFAULT_PARAMS, pct_socio: 0.5 };
    const ago = liquidacion({ entries, contracts: [contract], params, cuota: "2026-08", deliveries, months: ["2026-08", "2026-09", "2026-10"] });
    const sep = liquidacion({ entries, contracts: [contract], params, cuota: "2026-09", deliveries, months: ["2026-08", "2026-09", "2026-10"] });
    const oct = liquidacion({ entries, contracts: [contract], params, cuota: "2026-10", deliveries, months: ["2026-08", "2026-09", "2026-10"] });
    expect(ago.lines.find((x) => x.key === "ia").usd).toBe(60);
    expect(sep.lines.find((x) => x.key === "ia").usd).toBe(40);
    expect(oct.lines.find((x) => x.key === "servidores").usd).toBe(50);
    expect(ago.lines.find((x) => x.key === "servidores").usd + sep.lines.find((x) => x.key === "servidores").usd).toBe(0);
  });

  it("operadores externos cargados a mano con otro nombre no se duplican con los automáticos", () => {
    const params = { ...DEFAULT_PARAMS, operadores: [{ usuario: "op1", nombre: "Uno", tarifa_cancion: 1 }] };
    const entries = [{ id: 1, section: "mano_obra", period: "2026-10", provider: "Operadores externos (Upwork)", amount_usd: 7 }];
    const eff = effectiveEntries({ entries, reviewers: [{ month: "2026-10", reviewer: "op1", canciones: 9 }], params, months: ["2026-10"], today: TODAY });
    expect(eff.filter((e) => e.section === "mano_obra").map((e) => e.amount_usd)).toEqual([7]);
  });

  it("producción interna automática cuenta días hábiles desde la fecha indicada", () => {
    const params = { ...DEFAULT_PARAMS, produccion_interna: { horas_dia: 2, tarifa_hora: 10, desde: "2026-10-12" } };
    const eff = effectiveEntries({ entries: [], params, months: ["2026-09", "2026-10"], today: TODAY });
    const pi = eff.filter((e) => e.provider === "Producción interna");
    // 12 al 15 de octubre = 4 días hábiles × 2 h × US$ 10.
    expect(pi.map((e) => [e.period, e.amount_usd])).toEqual([["2026-10", 80]]);
  });
});

describe("errores de la auditoría (no deben volver)", () => {
  const contract = { nombre: "C", inicio: "2026-08", meses: 3, cupo_vl_mes: 10, cupo_at_mes: 0, precio_vl: 10, precio_at: 1, fee_mensual: 1000, portales: ["argentina"] };
  const params = { ...DEFAULT_PARAMS, pct_socio: 0.5, tipo_cambio: 1500 };
  const deliveries = [{ month: "2026-08", portal: "argentina", type: "lyric_video", entregas: 5, canciones: 5 }];
  const months = ["2026-08", "2026-09", "2026-10"];
  const liq = (entries, cuota) => liquidacion({ entries, contracts: [contract], params, cuota, deliveries, months });

  it("un recurrente no arrastra la cuota a sus copias", () => {
    const entries = [{ id: 1, section: "mano_obra", provider: "Producción interna", period: "2026-08", amount_usd: 100, payload: { cuota: "2026-08", recurrente: true } }];
    const eff = effectiveEntries({ entries, params, months, today: TODAY });
    expect(eff.filter((e) => e.provider === "Producción interna")).toHaveLength(1);
  });

  it("un manual 'R2' pisa al automático 'Cloudflare R2' del mismo mes", () => {
    const entries = [{ id: 1, section: "costo_directo", category: "Infraestructura", provider: "R2", period: "2026-08", amount_usd: 47 }];
    const auto = [{ month: "2026-08", source: "r2", category: "Infraestructura", provider: "Cloudflare R2", amount_usd: 46, days: 31, expected_days: 31 }];
    const eff = effectiveEntries({ entries, autoCosts: auto, params, months, today: TODAY });
    expect(eff.filter((e) => /r2/i.test(e.provider)).map((e) => e.amount_usd)).toEqual([47]);
  });

  it("cobrada, el ingreso es lo que entró (cobro parcial)", () => {
    const l = liq([{ section: "cobro", period: "2026-10", amount_usd: 900, payload: { cuota: "2026-08", tipo_cambio: 1500 } }], "2026-08");
    expect(l.ingreso).toBe(900);
    expect(l.cobroParcial).toBe(true);
  });

  it("una cuota pagada queda congelada; lo que llega después va a la próxima cuota abierta", () => {
    const base = [
      { id: 1, section: "cobro", period: "2026-08", amount_usd: 1000, payload: { cuota: "2026-08" } },
      { id: 2, section: "costo_directo", category: "IA", provider: "Google Cloud", period: "2026-08", amount_usd: 100 },
    ];
    const antes = liq(base, "2026-08");
    expect(antes.socio).toBe(450);
    const pago = { id: 3, section: "pago_socio", period: "2026-09", amount_usd: 450, payload: { cuota: "2026-08", snapshot: liquidacionSnapshot(antes) } };
    // Llega IA tardía de agosto y de octubre, después del pago.
    const despues = [...base, pago,
      { id: 4, section: "costo_directo", category: "IA", provider: "Google Cloud", period: "2026-08", amount_usd: 30 },
      { id: 5, section: "costo_directo", category: "IA", provider: "Google Cloud", period: "2026-10", amount_usd: 80 }];
    const ago = liq(despues, "2026-08");
    expect([ago.frozen, ago.socio, ago.saldo]).toEqual([true, 450, 0]);
    // Lo nuevo no cuenta como diferencia del pago: va a otra cuota.
    expect(ago.drift).toBe(0);
    // Editar un renglón que SÍ estaba en el pago se marca como diferencia.
    const editado = despues.map((e) => (e.id === 2 ? { ...e, amount_usd: 120 } : e));
    expect(liq(editado, "2026-08").drift).toBe(20);
    // Lo tardío no se pierde: va a la primera cuota abierta desde su mes.
    expect(liq(despues, "2026-09").lines.find((x) => x.key === "ia").usd).toBe(30);
    expect(liq(despues, "2026-10").lines.find((x) => x.key === "ia").usd).toBe(80);
  });

  it("cobros de otro contrato no se mezclan", () => {
    const entries = [{ section: "cobro", period: "2026-08", contract: "Otro", amount_usd: 999, payload: { cuota: "2026-08" } }];
    expect(liq(entries, "2026-08").cobrada).toBe(false);
  });

  it("la copia para mandar marca la proyección", () => {
    expect(liquidacionTexto(liq([], "2026-09"))).toMatch(/^PROYECCIÓN/);
  });

  it("CSV ida y vuelta conserva el payload (cuota, contrato)", () => {
    const rows = [{ period: "2026-10", section: "cobro", provider: "U", amount_usd: 6200, payload: { cuota: "2026-08", ars: 9517000 } }];
    expect(csvToEntries(entriesToCsv(rows))[0].payload).toEqual({ cuota: "2026-08", ars: 9517000 });
  });

  it("números en es-AR", () => {
    expect(parseNumberAr("1.535")).toBe(1535);
    expect(parseNumberAr("9.517.000")).toBe(9517000);
    expect(parseNumberAr("7,78")).toBe(7.78);
    expect(parseNumberAr("0.0834")).toBe(0.0834);
    expect(parseNumberAr("")).toBe(null);
  });
});

describe("dos contratos superpuestos", () => {
  it("cada entrega cuenta para un solo contrato y los costos se reparten por cuota", () => {
    const a = { nombre: "A", inicio: "2026-08", meses: 6, cupo_vl_mes: 100, cupo_at_mes: 0, precio_vl: 10, precio_at: 1, fee_mensual: 3000, portales: ["argentina"] };
    const b = { nombre: "B", inicio: "2026-10", meses: 6, cupo_vl_mes: 100, cupo_at_mes: 0, precio_vl: 10, precio_at: 1, fee_mensual: 1000, portales: ["argentina"] };
    const entries = [{ id: 1, section: "costo_directo", category: "Infraestructura", provider: "Railway", period: "2026-10", amount_usd: 400 }];
    const params = { ...DEFAULT_PARAMS, pct_socio: 0.5 };
    const la = liquidacion({ entries, contracts: [a, b], params, cuota: "2026-10", deliveries: [], months: ["2026-10"], contract: a });
    const lb = liquidacion({ entries, contracts: [a, b], params, cuota: "2026-10", deliveries: [], months: ["2026-10"], contract: b });
    expect([la.costos, lb.costos]).toEqual([300, 100]);
  });
});

describe("cierre sin pago", () => {
  it("una cuota cerrada con un renglón de US$ 0 queda congelada y sigue pendiente de pago", () => {
    const contract = { nombre: "C", inicio: "2026-08", meses: 3, cupo_vl_mes: 10, cupo_at_mes: 0, precio_vl: 10, precio_at: 1, fee_mensual: 1000, portales: ["argentina"] };
    const params = { ...DEFAULT_PARAMS, pct_socio: 0.5 };
    const base = [{ id: 1, section: "cobro", period: "2026-08", amount_usd: 1000, payload: { cuota: "2026-08" } }];
    const l = liquidacion({ entries: base, contracts: [contract], params, cuota: "2026-08", deliveries: [], months: ["2026-08"] });
    const cierre = { id: 2, section: "pago_socio", period: "2026-10", amount_usd: 0, payload: { cuota: "2026-08", cierre: true, snapshot: liquidacionSnapshot(l) } };
    const tarde = { id: 3, section: "mano_obra", provider: "Producción interna", period: "2026-10", amount_usd: 50 };
    const ago = liquidacion({ entries: [...base, cierre, tarde], contracts: [contract], params, cuota: "2026-08", deliveries: [], months: ["2026-08", "2026-10"] });
    expect([ago.frozen, ago.socio, ago.pagado, ago.saldo]).toEqual([true, 500, 0, 500]);
  });
});
