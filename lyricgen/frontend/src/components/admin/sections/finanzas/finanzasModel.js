// Cálculos de la solapa Finanzas. Puro: sin fetch ni React, para testearlo.
//
// Criterio contable (ver CriterioView):
//   - Base: DEVENGADO por mes. El ingreso del mes es lo que se ENTREGÓ ese mes
//     × el precio del contrato; el costo del mes es lo que se CONSUMIÓ ese mes.
//     Una factura de Railway del 20-ago al 20-sep se reparte en sus meses.
//   - El contrato agrupa los meses que lo produjeron (el de agosto se produjo
//     entre agosto y octubre porque el material llegó tarde).
//   - Caja: lo cobrado contra lo devengado. La diferencia es anticipo (cobramos
//     antes de entregar) o saldo a cobrar.

export const SECTION_LABELS = {
  ingreso: "Otros ingresos",
  costo_directo: "Costos directos",
  mano_obra: "Mano de obra pagada",
  estructura: "Gastos de estructura",
  cobro: "Cobros",
  pago_socio: "Pagos al socio",
  contrato: "Contrato",
  parametros: "Parámetros",
};

export const BASIS_LABELS = {
  factura: "Factura",
  api: "API",
  captura: "Captura",
  estimado: "Estimado",
  imputado: "Imputado",
  acuerdo: "Acuerdo",
  sistema: "Sistema",
};

// Neutros a propósito: los valores reales se cargan en la UI (el repo es
// público).
export const DEFAULT_PARAMS = {
  tarifa_hora: 0,
  horas_dia_agus: 0,
  horas_dia_tomi: 0,
  // Parte de las horas de Tomi que es OPERACIÓN del cliente (entregas,
  // atención, mantener la plataforma andando). El resto es desarrollo de
  // producto: un activo de Genly que no se le cobra al reparto.
  pct_operacion_tomi: 0.5,
  pct_socio: 0,
  // Qué trabajo del equipo se resta antes de calcular la parte del socio:
  // "ninguno" | "agus" | "agus_y_tomi"
  resta_trabajo: "ninguno",
  solo_dias_habiles: true,
  reserva_meses: 0,
  // Pesos por dólar para mostrar la parte del socio en ARS.
  tipo_cambio: 0,
  // Operadores pagados por canción: [{ usuario, nombre, tarifa_cancion }].
  // Con esto la mano de obra del mes sale sola de las letras aprobadas.
  operadores: [],
  comision_operadores: 0,
};

const MONTH_NAMES = ["ene", "feb", "mar", "abr", "may", "jun", "jul", "ago", "sep", "oct", "nov", "dic"];

export function monthLabel(period) {
  const [y, m] = String(period).split("-").map(Number);
  return `${MONTH_NAMES[m - 1]} ${String(y).slice(2)}`;
}

export function addMonths(period, n) {
  const [y, m] = period.split("-").map(Number);
  const d = new Date(Date.UTC(y, m - 1 + n, 1));
  return `${d.getUTCFullYear()}-${String(d.getUTCMonth() + 1).padStart(2, "0")}`;
}

export function monthsBetween(since, until) {
  const out = [];
  let cur = since;
  while (cur <= until && out.length < 60) {
    out.push(cur);
    cur = addMonths(cur, 1);
  }
  return out;
}

export function currentPeriod(today = new Date()) {
  return `${today.getFullYear()}-${String(today.getMonth() + 1).padStart(2, "0")}`;
}

/** Días trabajables del mes; el mes en curso cuenta hasta hoy inclusive. */
export function workDays(period, { soloHabiles = true, today = new Date(), desde = null } = {}) {
  const [y, m] = period.split("-").map(Number);
  const last = new Date(y, m, 0).getDate();
  const isCurrent = currentPeriod(today) === period;
  const isFuture = period > currentPeriod(today);
  if (isFuture) return 0;
  const end = isCurrent ? today.getDate() : last;
  // `desde` (YYYY-MM-DD): no contar días anteriores (lo previo ya está cargado a mano).
  let start = 1;
  if (desde) {
    if (desde.slice(0, 7) > period) return 0;
    if (desde.slice(0, 7) === period) start = Number(desde.slice(8, 10)) || 1;
  }
  let days = 0;
  for (let d = start; d <= end; d += 1) {
    const wd = new Date(y, m - 1, d).getDay();
    if (!soloHabiles || (wd !== 0 && wd !== 6)) days += 1;
  }
  return days;
}

const num = (v) => (Number.isFinite(Number(v)) ? Number(v) : 0);

/** Número escrito a mano en es-AR: "1.535" → 1535, "9.517.000" → 9517000, "7,78" → 7.78. */
export function parseNumberAr(text) {
  let t = String(text ?? "").trim().replace(/\s/g, "").replace(/^US\$|^\$|^ARS/i, "");
  if (t === "") return null;
  if (/^-?\d{1,3}(\.\d{3})+(,\d+)?$/.test(t)) t = t.replace(/\./g, "").replace(",", ".");
  else if (/^-?\d+,\d+$/.test(t)) t = t.replace(",", ".");
  const n = Number(t);
  return Number.isFinite(n) ? n : null;
}
const round2 = (v) => Math.round(v * 100) / 100;

export function contractsFrom(entries) {
  return entries
    .filter((e) => e.section === "contrato" && e.payload)
    .map((e) => ({ id: e.id, nombre: e.contract || e.concept || "Contrato", ...e.payload, entry: e }))
    .sort((a, b) => String(a.inicio).localeCompare(String(b.inicio)));
}

export function paramsFrom(entries) {
  const row = entries.find((e) => e.section === "parametros");
  return { ...DEFAULT_PARAMS, ...(row?.payload || {}), entryId: row?.id ?? null };
}

/**
 * Reparte las entregas entre contratos, en orden de inicio: cada pieza cuenta
 * para un solo contrato, y lo que excede el cupo total de uno pasa al
 * siguiente (o queda sin devengar si no hay siguiente).
 */
export function allocateContracts(contracts, deliveries) {
  const pool = deliveries.map((d) => ({ ...d, libres: num(d.canciones) }))
    .sort((a, b) => a.month.localeCompare(b.month));
  const result = {};
  for (const c of [...contracts].sort((a, b) => String(a.inicio).localeCompare(String(b.inicio)))) {
    const portales = c.portales?.length ? c.portales : null;
    const meses = num(c.meses) || 1;
    const cap = { lyric_video: num(c.cupo_vl_mes) * meses, art_track: num(c.cupo_at_mes) * meses };
    const used = { lyric_video: 0, art_track: 0 };
    const byMonth = {};
    for (const d of pool) {
      if (d.month < c.inicio || (portales && !portales.includes(d.portal)) || !d.libres) continue;
      const t = d.type === "art_track" ? "art_track" : "lyric_video";
      const n = Math.min(Math.max(0, cap[t] - used[t]), d.libres);
      if (!n) continue;
      used[t] += n;
      d.libres -= n;
      byMonth[d.month] ??= { lyric_video: 0, art_track: 0 };
      byMonth[d.month][t] += n;
    }
    result[c.nombre] = { byMonth, used, cap };
  }
  return result;
}

/** Ingreso devengado por mes (todos los contratos). */
export function revenueByMonth(contracts, deliveries) {
  const alloc = allocateContracts(contracts, deliveries);
  const out = {};
  for (const c of contracts) {
    for (const [month, p] of Object.entries(alloc[c.nombre]?.byMonth || {})) {
      out[month] ??= { lyric_video: 0, art_track: 0, usd: 0, detalle: [] };
      out[month].lyric_video += p.lyric_video;
      out[month].art_track += p.art_track;
      const usd = p.lyric_video * num(c.precio_vl) + p.art_track * num(c.precio_at);
      out[month].usd += usd;
      out[month].detalle.push({ contrato: c.nombre, ...p, usd });
    }
  }
  return out;
}

/** Horas y costo imputado del equipo por mes. */
export function teamLabor(months, params, today = new Date()) {
  const rate = num(params.tarifa_hora);
  const out = {};
  for (const m of months) {
    const days = workDays(m, { soloHabiles: params.solo_dias_habiles, today });
    const agusH = days * num(params.horas_dia_agus);
    const tomiH = days * num(params.horas_dia_tomi);
    const tomiOpH = tomiH * num(params.pct_operacion_tomi);
    out[m] = {
      days,
      agus_h: agusH,
      tomi_h: tomiH,
      tomi_op_h: tomiOpH,
      agus_usd: round2(agusH * rate),
      tomi_op_usd: round2(tomiOpH * rate),
      tomi_dev_usd: round2((tomiH - tomiOpH) * rate),
    };
  }
  return out;
}

const providerKey = (p) => String(p || "").toLowerCase().split(/[—(\-]/)[0].trim();

// Fuente de un costo, para decidir si lo manual pisa a lo automático. Usa
// payload.source si está, y si no reconoce el proveedor por palabra clave
// (no por prefijo: "R2" y "Cloudflare R2" son lo mismo).
const SOURCE_PATTERNS = [
  ["railway", /railway/i],
  ["r2", /\br2\b|cloudflare r2/i],
  ["gcp", /google cloud|\bgcp\b|vertex|gemini|\bveo\b/i],
  ["replicate", /replicate/i],
  ["openai", /openai|whisper/i],
];
export function sourceOf(e) {
  if (e?.payload?.source) return e.payload.source;
  const text = `${e?.provider || ""}`;
  return SOURCE_PATTERNS.find(([, re]) => re.test(text))?.[0] || null;
}

// Secciones que no se repiten nunca: son hechos de un momento.
const NO_RECURRENTE = new Set(["cobro", "pago_socio", "contrato", "parametros"]);

/**
 * Renglones efectivos de la planilla: los cargados a mano + los que salen
 * solos. Lo cargado a mano SIEMPRE pisa a lo automático del mismo proveedor y
 * mes (una factura verificada vale más que el recolector).
 *
 *   - "api": costos del recolector diario (Railway, Google Cloud, R2, Replicate).
 *   - "recurrente": un renglón con payload.recurrente se repite cada mes
 *     siguiente (hasta payload.hasta o el mes actual).
 *   - "operadores": letras aprobadas por cada operador × su tarifa por
 *     canción, más la comisión.
 */
export function effectiveEntries({ entries, autoCosts = [], reviewers = [], params, months, today = new Date() }) {
  const manual = entries.filter((e) => e.section !== "contrato" && e.section !== "parametros");
  const has = (section, month, key) => manual.some(
    (e) => e.section === section && e.period === month && providerKey(e.provider).startsWith(key),
  );
  const out = [...entries];
  const manualSource = (month, source) => manual.some(
    (e) => e.period === month && e.section === "costo_directo" && sourceOf(e) === source,
  );
  for (const c of autoCosts) {
    if (!months.includes(c.month) || manualSource(c.month, c.source)) continue;
    out.push({
      id: null, synthetic: "api", period: c.month, section: "costo_directo", category: c.category,
      provider: c.provider, amount_usd: c.amount_usd, basis: "api", payload: { source: c.source },
      coverage: { days: c.days, expected: c.expected_days },
      evidence: `recolector diario: ${c.days} de ${c.expected_days} días`,
    });
  }
  const last = currentPeriod(today);
  const sameProvider = (a, b) => String(a || "").trim().toLowerCase() === String(b || "").trim().toLowerCase();
  // Un recurrente se repite cada mes hasta que hay un renglón del MISMO
  // proveedor ese mes, o hasta que otro recurrente más nuevo del mismo
  // proveedor lo reemplaza (cambio de precio). La copia no arrastra la cuota
  // ni datos de cobro: es un gasto nuevo de su mes.
  const recurrentes = manual.filter((e) => e.payload?.recurrente && !NO_RECURRENTE.has(e.section) && !e.payload?.cuota);
  for (const e of recurrentes) {
    const hasta = e.payload.hasta && e.payload.hasta < last ? e.payload.hasta : last;
    for (const m of months) {
      if (m <= e.period || m > hasta) continue;
      if (manual.some((x) => x.period === m && x.section === e.section && sameProvider(x.provider, e.provider))) continue;
      const newer = recurrentes.some((x) => x !== e && sameProvider(x.provider, e.provider) && x.section === e.section
        && x.period > e.period && x.period <= m);
      if (newer) continue;
      const { cuota: _c, ars: _a, tipo_cambio: _t, ...rest } = e.payload || {};
      out.push({ ...e, id: null, synthetic: "recurrente", period: m, payload: rest, evidence: `se repite desde ${monthLabel(e.period)}` });
    }
  }
  // Un mes con operadores externos cargados a mano no se recalcula: lo manual
  // pisa a lo automático. Se reconoce por el nombre ("Upwork", "operadores
  // externos") sin depender de cómo se cortó el proveedor.
  const isExternal = (e) => e.section === "mano_obra" && /upwork|operadores externos/i.test(`${e.provider || ""} ${e.concept || ""}`);
  // Un renglón manual genérico ("Operadores externos (Upwork)") cubre a todos
  // los operadores del mes; uno con nombre ("Operadores externos — Ana") solo
  // reemplaza a esa persona.
  const manualExternal = (m, nombre) => manual.some((e) => {
    if (e.period !== m || !isExternal(e)) return false;
    const named = /—/.test(e.provider || "");
    return !named || (nombre && (e.provider || "").toLowerCase().includes(String(nombre).toLowerCase()));
  });
  const ops = (params.operadores || []).filter((o) => o.usuario && Number(o.tarifa_cancion) > 0);
  const opsDesde = params.operadores_desde || "";
  for (const m of months) {
    if (!ops.length || (opsDesde && m < opsDesde) || manualExternal(m, null)) continue;
    let subtotal = 0;
    for (const o of ops) {
      if (manualExternal(m, o.nombre || o.usuario)) continue;
      const n = reviewers.filter((r) => r.month === m && r.reviewer === o.usuario).reduce((a, r) => a + num(r.canciones), 0);
      if (!n) continue;
      const amount = round2(n * num(o.tarifa_cancion));
      subtotal += amount;
      out.push({
        id: null, synthetic: "operadores", period: m, section: "mano_obra", category: "Operadores",
        provider: `Operadores externos — ${o.nombre || o.usuario}`, amount_usd: amount, basis: "sistema",
        evidence: `${n} letras aprobadas × US$ ${o.tarifa_cancion}`,
        // ~8,5 min por canción: la mediana medida de revisión.
        payload: { personas: 1, horas: Math.round((n * 8.5) / 60) },
      });
    }
    if (subtotal && num(params.comision_operadores)) {
      out.push({
        id: null, synthetic: "operadores", period: m, section: "mano_obra", category: "Operadores",
        provider: "Operadores externos — comisiones", amount_usd: round2(subtotal * num(params.comision_operadores)), basis: "sistema",
        evidence: `${Math.round(num(params.comision_operadores) * 1000) / 10}% sobre ${subtotal.toFixed(2)}`,
      });
    }
  }
  const pi = params.produccion_interna || {};
  // Sin fecha de inicio no se calcula: lo anterior ya está cargado a mano y
  // contarlo de nuevo duplicaría horas.
  if (num(pi.horas_dia) > 0 && num(pi.tarifa_hora) > 0 && /^\d{4}-\d{2}-\d{2}$/.test(pi.desde || "")) {
    for (const m of months) {
      if (manual.some((e) => e.period === m && e.section === "mano_obra" && /producci[oó]n interna/i.test(e.provider || ""))) continue;
      const days = workDays(m, { soloHabiles: true, today, desde: pi.desde || null });
      if (!days) continue;
      const horas = round2(days * num(pi.horas_dia));
      out.push({
        id: null, synthetic: "produccion", period: m, section: "mano_obra", category: "Operadores",
        provider: "Producción interna", amount_usd: round2(horas * num(pi.tarifa_hora)), basis: "imputado",
        evidence: `${days} días hábiles × ${pi.horas_dia} h × US$ ${pi.tarifa_hora}/h`,
        payload: { personas: num(pi.personas) || 1, horas },
      });
    }
  }
  return out;
}

/** Qué falta para cerrar un mes. */
export function monthChecklist({ month, entries, effective, autoCosts = [] }) {
  const items = [];
  const manualIn = (key, section = "costo_directo") => entries.some(
    (e) => e.section === section && e.period === month && providerKey(e.provider).startsWith(key),
  );
  for (const [key, label] of [["railway", "Railway"], ["google cloud", "Google Cloud"], ["cloudflare r2", "Cloudflare R2"], ["replicate", "Replicate"]]) {
    const auto = autoCosts.find((c) => c.month === month && providerKey(c.provider).startsWith(key));
    if (manualIn(key)) items.push({ label, ok: true, detail: "cargado a mano" });
    else if (auto) {
      const full = auto.days >= auto.expected_days;
      items.push({ label, ok: full, detail: `automático, ${auto.days} de ${auto.expected_days} días` });
    } else items.push({ label, ok: false, detail: "sin datos: falta el recolector o cargarlo a mano" });
  }
  items.push(manualIn("openai")
    ? { label: "OpenAI", ok: true, detail: "cargado" }
    : { label: "OpenAI", ok: false, detail: "cargalo desde platform.openai.com → Usage (sin API para la cuenta de Genly)" });
  const labor = effective.filter((e) => e.section === "mano_obra" && e.period === month);
  items.push({ label: "Operadores", ok: true, detail: labor.length ? `${labor.length} renglones${labor.some((e) => e.synthetic) ? " (automático)" : ""}` : "sin operadores pagos este mes" });
  const estructura = effective.filter((e) => e.section === "estructura" && e.period === month);
  items.push({ label: "Estructura", ok: estructura.length > 0, detail: estructura.length ? `${estructura.length} renglones` : "sin gastos de estructura" });
  return items;
}

function sumBy(entries, section, month) {
  return entries
    .filter((e) => e.section === section && e.period === month)
    .reduce((acc, e) => acc + num(e.amount_usd), 0);
}

/** Agrupa renglones de una sección en filas (categoría → proveedor) × mes. */
export function groupRows(entries, section, months) {
  const rows = new Map();
  for (const e of entries) {
    if (e.section !== section || !months.includes(e.period)) continue;
    const key = `${e.category || "Sin categoría"}::${e.provider || e.concept || "—"}`;
    if (!rows.has(key)) {
      rows.set(key, {
        key,
        category: e.category || "Sin categoría",
        provider: e.provider || e.concept || "—",
        byMonth: {},
        entries: {},
        bases: new Set(),
      });
    }
    const row = rows.get(key);
    row.byMonth[e.period] = (row.byMonth[e.period] || 0) + num(e.amount_usd);
    (row.entries[e.period] ??= []).push(e);
    if (e.basis) row.bases.add(e.basis);
  }
  return [...rows.values()]
    .map((r) => ({ ...r, total: months.reduce((a, m) => a + (r.byMonth[m] || 0), 0), bases: [...r.bases] }))
    .sort((a, b) => a.category.localeCompare(b.category) || b.total - a.total);
}

/**
 * Estado de resultados por mes. Devuelve, por mes y total, cada escalón:
 * ingreso → margen bruto → resultado operativo → resultado después del
 * trabajo del equipo → reparto.
 */
export function buildPnl({ entries, contracts, deliveries, params, months, today = new Date() }) {
  const revenue = revenueByMonth(contracts, deliveries);
  const labor = teamLabor(months, params, today);
  const perMonth = {};
  for (const m of months) {
    const ingresoEntregas = revenue[m]?.usd || 0;
    const otrosIngresos = sumBy(entries, "ingreso", m);
    const ingreso = ingresoEntregas + otrosIngresos;
    const directos = sumBy(entries, "costo_directo", m);
    const manoObra = sumBy(entries, "mano_obra", m);
    const margenBruto = ingreso - directos - manoObra;
    const estructura = sumBy(entries, "estructura", m);
    const operativo = margenBruto - estructura;
    const l = labor[m];
    const trabajoRestado = params.resta_trabajo === "ninguno" ? 0
      : params.resta_trabajo === "agus_y_tomi" ? l.agus_usd + l.tomi_op_usd
        : l.agus_usd;
    const baseReparto = operativo - trabajoRestado;
    const socio = Math.max(0, baseReparto) * num(params.pct_socio);
    perMonth[m] = {
      piezas_vl: revenue[m]?.lyric_video || 0,
      piezas_at: revenue[m]?.art_track || 0,
      ingresoEntregas, otrosIngresos, ingreso, directos, manoObra, margenBruto,
      estructura, operativo,
      trabajo_agus: l.agus_usd, trabajo_tomi_op: l.tomi_op_usd, trabajo_tomi_dev: l.tomi_dev_usd,
      horas_equipo: l.agus_h + l.tomi_h,
      trabajoRestado, baseReparto, socio,
      genly: operativo - socio,
      cobrado: sumBy(entries, "cobro", m),
      pagadoSocio: sumBy(entries, "pago_socio", m),
    };
  }
  const total = {};
  for (const m of months) {
    for (const [k, v] of Object.entries(perMonth[m])) total[k] = (total[k] || 0) + v;
  }
  // El reparto se calcula sobre el acumulado, no mes a mes: un mes en pérdida
  // (agosto, mucho costo y poca entrega) compensa uno con ganancia.
  total.socio = Math.max(0, total.baseReparto || 0) * num(params.pct_socio);
  total.genly = (total.operativo || 0) - total.socio;
  return { perMonth, total, revenue, labor };
}

/**
 * Avance de cada contrato: cupo, recibido, entregado, cobrado.
 *
 * "Cupo a la fecha" son los meses ya iniciados del contrato: es lo que el
 * cliente debería haber mandado hasta hoy. Lo recibido se toma como el máximo
 * entre lo que entró por campañas y lo entregado, porque parte del material
 * (los 66 de agosto) se trabajó fuera de una campaña.
 */
export function contractStatus(contract, ops, entries, contracts = [contract], today = new Date()) {
  const { used, cap } = allocateContracts(contracts, ops.deliveries || [])[contract.nombre];
  const portales = contract.portales?.length ? contract.portales : null;
  const meses = num(contract.meses) || 1;
  const fin = addMonths(contract.inicio, meses - 1);
  const hasta = currentPeriod(today) < fin ? currentPeriod(today) : fin;
  const mesesIniciados = hasta < contract.inicio ? 0 : monthsBetween(contract.inicio, hasta).length;
  const capAlDia = {
    lyric_video: num(contract.cupo_vl_mes) * mesesIniciados,
    art_track: num(contract.cupo_at_mes) * mesesIniciados,
  };
  const received = { lyric_video: 0, art_track: 0 };
  const discarded = { lyric_video: 0, art_track: 0 };
  for (const r of ops.received || []) {
    if (r.month < contract.inicio || (portales && !portales.includes(r.portal))) continue;
    const t = r.type === "art_track" ? "art_track" : "lyric_video";
    received[t] += num(r.recibidos);
    discarded[t] += num(r.descartados);
  }
  const valido = (t) => Math.max(received[t] - discarded[t], used[t]);
  const valor = cap.lyric_video * num(contract.precio_vl) + cap.art_track * num(contract.precio_at);
  const devengado = used.lyric_video * num(contract.precio_vl) + used.art_track * num(contract.precio_at);
  const cobrado = entries
    .filter((e) => e.section === "cobro" && (!e.contract || e.contract === contract.nombre))
    .reduce((a, e) => a + num(e.amount_usd), 0);
  return {
    cap, capAlDia, mesesIniciados, used, received, discarded, valor, devengado, cobrado,
    recibidoValido: { lyric_video: valido("lyric_video"), art_track: valido("art_track") },
    anticipo: cobrado - devengado,
    pendienteMaterial: {
      lyric_video: Math.max(0, capAlDia.lyric_video - valido("lyric_video")),
      art_track: Math.max(0, capAlDia.art_track - valido("art_track")),
    },
    pendienteNuestro: {
      lyric_video: Math.max(0, valido("lyric_video") - used.lyric_video),
      art_track: Math.max(0, valido("art_track") - used.art_track),
    },
  };
}

/** Escenarios de reparto con distintas tarifas por hora. */
export function partnerScenarios(pnl, params, rates = [0, 5, 8, 10]) {
  const t = pnl.total;
  const months = Object.values(pnl.labor);
  const agusH = months.reduce((a, l) => a + l.agus_h, 0);
  const tomiOpH = months.reduce((a, l) => a + l.tomi_op_h, 0);
  return rates.map((rate) => {
    const horas = params.resta_trabajo === "ninguno" ? 0
      : params.resta_trabajo === "agus_y_tomi" ? agusH + tomiOpH : agusH;
    const trabajo = horas * rate;
    const base = (t.operativo || 0) - trabajo;
    const socio = Math.max(0, base) * num(params.pct_socio);
    return { rate, horas, trabajo, base, socio, equipo: trabajo + (base - socio) };
  });
}

export function entriesToCsv(entries) {
  const cols = ["period", "section", "category", "provider", "concept", "amount_usd", "basis", "contract", "evidence", "note", "payload"];
  const esc = (v) => {
    const s = v === null || v === undefined ? "" : String(v);
    return /[",\n]/.test(s) ? `"${s.replace(/"/g, '""')}"` : s;
  };
  const cell = (e, c) => (c === "payload" ? (e.payload ? JSON.stringify(e.payload) : "") : e[c]);
  return [cols.join(","), ...entries.map((e) => cols.map((c) => esc(cell(e, c))).join(","))].join("\n");
}

/** CSV → renglones para /entries/import. Acepta `payload` como JSON. */
export function csvToEntries(text) {
  const rows = [];
  let field = "";
  let row = [];
  let quoted = false;
  const src = String(text).replace(/\r\n/g, "\n");
  for (let i = 0; i < src.length; i += 1) {
    const ch = src[i];
    if (quoted) {
      if (ch === '"' && src[i + 1] === '"') { field += '"'; i += 1; }
      else if (ch === '"') quoted = false;
      else field += ch;
    } else if (ch === '"') quoted = true;
    else if (ch === ",") { row.push(field); field = ""; }
    else if (ch === "\n") { row.push(field); rows.push(row); row = []; field = ""; }
    else field += ch;
  }
  if (field !== "" || row.length) { row.push(field); rows.push(row); }
  const [header, ...body] = rows.filter((r) => r.some((c) => c.trim() !== ""));
  if (!header) return [];
  const idx = Object.fromEntries(header.map((h, i) => [h.trim(), i]));
  return body.map((r) => {
    const get = (k) => (idx[k] === undefined ? "" : (r[idx[k]] ?? "").trim());
    const amount = get("amount_usd");
    const payload = get("payload");
    return {
      period: get("period"),
      section: get("section"),
      category: get("category") || null,
      provider: get("provider") || null,
      concept: get("concept") || null,
      amount_usd: amount === "" ? null : Number(amount),
      basis: get("basis") || null,
      contract: get("contract") || null,
      evidence: get("evidence") || null,
      note: get("note") || null,
      payload: payload ? JSON.parse(payload) : null,
    };
  });
}

/**
 * Liquidación de una cuota mensual del contrato (lo que se le manda al socio).
 *
 * Criterio acordado: la cuota del mes es el ingreso; los costos de producción
 * van a la cuota de las piezas que produjeron (payload.cuota), aunque se hayan
 * gastado en otro mes; servidores y herramientas van al mes en que se usaron.
 * Un renglón sin payload.cuota se asigna a la cuota de su propio mes.
 */
export function cuotaDe(entry) {
  return entry?.payload?.cuota || entry?.period;
}

export function contractForCuota(contracts, cuota) {
  return contracts.find((c) => {
    const fin = addMonths(c.inicio, (num(c.meses) || 1) - 1);
    return cuota >= c.inicio && cuota <= fin;
  });
}

/**
 * A qué cuota va la producción de cada mes, sin marcar nada a mano.
 *
 * Las piezas entregadas llenan las cuotas en orden (FIFO): primero el cupo de
 * la cuota 1, después el de la 2, etc. La producción de un mes se reparte
 * entre las cuotas de las piezas que se entregaron ese mes (ponderadas por
 * precio: un art track pesa menos que un video). Un mes sin entregas produce
 * para la cuota más vieja que todavía no se completó.
 *
 * Devuelve { [mes]: { [cuota]: fracción } }.
 */
export function productionShares(contract, deliveries, months, contracts = [contract]) {
  if (!contract) return {};
  const meses = num(contract.meses) || 1;
  const cuotas = monthsBetween(contract.inicio, addMonths(contract.inicio, meses - 1));
  const left = cuotas.map(() => ({ lyric_video: num(contract.cupo_vl_mes), art_track: num(contract.cupo_at_mes) }));
  const price = { lyric_video: num(contract.precio_vl) || 1, art_track: num(contract.precio_at) || 1 };
  // Las piezas de ESTE contrato: cada entrega cuenta para un solo contrato.
  const byMonth = allocateContracts(contracts, deliveries)[contract.nombre]?.byMonth || {};
  const allMonths = [...new Set([...Object.keys(byMonth), ...months])].sort();
  const out = {};
  for (const m of allMonths) {
    const weights = {};
    for (const t of ["lyric_video", "art_track"]) {
      let n = byMonth[m]?.[t] || 0;
      for (let i = 0; i < cuotas.length && n > 0; i += 1) {
        if (cuotas[i] > m) break;
        const take = Math.min(n, left[i][t]);
        if (take <= 0) continue;
        left[i][t] -= take;
        n -= take;
        weights[cuotas[i]] = (weights[cuotas[i]] || 0) + take * price[t];
      }
    }
    const total = Object.values(weights).reduce((a, v) => a + v, 0);
    if (total > 0) {
      out[m] = Object.fromEntries(Object.entries(weights).map(([c, w]) => [c, w / total]));
    } else {
      const open = cuotas.find((c, i) => c <= m && (left[i].lyric_video > 0 || left[i].art_track > 0));
      if (open) out[m] = { [open]: 1 };
    }
  }
  return out;
}

/** Peso de un contrato en los costos de un mes: su cuota sobre la suma de las cuotas activas. */
export function contractWeight(contract, contracts, month) {
  const active = contracts.filter((c) => month >= c.inicio && month <= addMonths(c.inicio, (num(c.meses) || 1) - 1));
  if (!active.length || !active.some((c) => c.nombre === contract?.nombre)) return active.length ? 0 : 1;
  const total = active.reduce((a, c) => a + num(c.fee_mensual), 0);
  return total > 0 ? num(contract.fee_mensual) / total : 1 / active.length;
}

/** Clave estable de un renglón, para saber si ya estaba en una liquidación pagada. */
export function entryKey(e) {
  return e.id != null ? `id:${e.id}` : `${e.synthetic || "x"}:${e.period}:${e.provider || ""}`;
}

// Producción = operadores, o IA (por categoría o por proveedor). Servidores,
// almacenamiento y herramientas son fijos del mes aunque estén mal rotulados.
const FIXED_SOURCES = new Set(["railway", "r2"]);
export const isProduction = (e) => {
  if (e.section === "mano_obra") return true;
  if (e.section !== "costo_directo") return false;
  const src = sourceOf(e);
  if (FIXED_SOURCES.has(src)) return false;
  return /^ia$/i.test(e.category || "") || ["gcp", "openai", "replicate"].includes(src);
};

/**
 * Renglones imputados a una cuota: los fijos por su mes; los de producción por
 * su payload.cuota si lo tienen (cargado a mano), y si no, repartidos según
 * las entregas (productionShares).
 */
export function entriesForCuota(entries, cuota, shares, frozen = {}, contract = null, contracts = []) {
  const cuotasDelContrato = contract ? monthsBetween(contract.inicio, addMonths(contract.inicio, (num(contract.meses) || 1) - 1)) : [];
  // Una cuota pagada está congelada: lo que no estaba en su liquidación pasa a
  // la próxima cuota abierta (desde su propio mes), así nunca se pierde y la
  // liquidación pagada no cambia.
  const reroute = (target, e) => {
    if (!frozen[target] || frozen[target].keys.has(entryKey(e))) return target;
    const from = e.period > target ? e.period : target;
    return cuotasDelContrato.find((c) => c >= from && !frozen[c]) || from;
  };
  const out = [];
  const push = (e, share) => {
    if (share <= 0) return;
    if (share >= 0.9999) { out.push(e); return; }
    out.push({
      ...e, amount_usd: num(e.amount_usd) * share, shared: share,
      payload: e.payload ? { ...e.payload, horas: e.payload.horas ? e.payload.horas * share : e.payload.horas } : e.payload,
    });
  };
  for (const e of entries) {
    if (e.section === "contrato" || e.section === "parametros") continue;
    if (e.section === "cobro" || e.section === "pago_socio") {
      if (contract && e.contract && e.contract !== contract.nombre) continue;
      if (cuotaDe(e) === cuota) out.push(e);
      continue;
    }
    // Con contratos superpuestos, cada costo se reparte por el peso de la cuota.
    const w = contracts.length > 1 ? contractWeight(contract, contracts, e.period) : 1;
    if (w <= 0) continue;
    if (e.payload?.cuota || !isProduction(e) || !shares) {
      if (reroute(cuotaDe(e), e) === cuota) push(e, w);
      continue;
    }
    const byTarget = {};
    for (const [c, frac] of Object.entries(shares[e.period] || { [e.period]: 1 })) {
      const t = reroute(c, e);
      byTarget[t] = (byTarget[t] || 0) + frac;
    }
    push(e, (byTarget[cuota] || 0) * w);
  }
  return out;
}

export function liquidacion({ entries, contracts = [], params, cuota, deliveries = null, months = [], contract: forced = null }) {
  const contract = forced || contractForCuota(contracts, cuota);
  const fee = num(contract?.fee_mensual);
  // Todos los meses con renglones, no solo los del rango visible: un costo de
  // producción en un mes sin entregas igual tiene que caer en alguna cuota.
  const allMonths = [...new Set([...months, ...entries.map((e) => e.period).filter(Boolean)])];
  const shares = deliveries ? productionShares(contract, deliveries, allMonths, contracts.length ? contracts : [contract]) : null;
  // Cuotas pagadas: su liquidación queda congelada en el snapshot del pago.
  const frozen = {};
  for (const e of entries) {
    if (e.section !== "pago_socio" || !e.payload?.cuota || !e.payload?.snapshot) continue;
    if (contract && e.contract && e.contract !== contract.nombre) continue;
    frozen[e.payload.cuota] = { snapshot: e.payload.snapshot, keys: new Set(e.payload.snapshot.keys || []) };
  }
  const mine = entriesForCuota(entries, cuota, shares, frozen, contract, contracts);
  const pick = (pred) => mine.filter(pred);
  const sum = (rows) => round2(rows.reduce((a, e) => a + num(e.amount_usd), 0));
  const ia = pick((e) => e.section === "costo_directo" && isProduction(e));
  const servidores = pick((e) => e.section === "costo_directo" && !isProduction(e));
  const herramientas = pick((e) => e.section === "estructura");
  const operadores = pick((e) => e.section === "mano_obra");
  const horas = operadores.reduce((a, e) => a + num(e.payload?.horas), 0);
  const personas = operadores.filter((e) => !/comisi/i.test(e.provider || "") && !e.shared).reduce((a, e) => a + num(e.payload?.personas), 0);
  const iaProv = [...new Set(ia.map((e) => (e.provider || "").split(" (")[0]))].filter(Boolean).join(", ");
  const opSub = [personas ? `${personas} personas` : "", horas ? `${Math.round(horas)} h` : ""].filter(Boolean).join(" · ");
  const lines = [
    { key: "ia", label: `IA (${iaProv || "—"})`, short: "IA", sub: iaProv, rows: ia, usd: sum(ia) },
    { key: "servidores", label: `Servidores y almacenamiento (${monthLabel(cuota)})`, short: "Servidores", sub: `y almacenamiento · ${monthLabel(cuota)}`, rows: servidores, usd: sum(servidores) },
    { key: "herramientas", label: "Herramientas", short: "Herramientas", sub: "", rows: herramientas, usd: sum(herramientas) },
    { key: "operadores", label: `Operadores${personas ? ` (${personas})` : ""}${horas ? ` · ${Math.round(horas)} h` : ""}`, short: "Operadores", sub: opSub, rows: operadores, usd: sum(operadores) },
  ];
  const costos = round2(lines.reduce((a, l) => a + l.usd, 0));
  const cobros = pick((e) => e.section === "cobro");
  const cobrado = sum(cobros);
  const tc = num(cobros.find((e) => e.payload?.tipo_cambio)?.payload?.tipo_cambio) || num(params.tipo_cambio);
  // Una cuota sin cobro registrado es una proyección: se muestra, pero no
  // suma al reparto hasta que entra la plata. Cobrada, el ingreso es lo que
  // ENTRÓ (una retención o un cobro parcial bajan la ganancia), no la cuota.
  const cobrada = cobrado > 0;
  const ingreso = cobrada ? cobrado : fee;
  const ganancia = round2(ingreso - costos);
  const pct = num(params.pct_socio);
  const socio = round2(Math.max(0, ganancia) * pct);
  const pagos = pick((e) => e.section === "pago_socio");
  const pagado = sum(pagos);
  const keys = mine.filter((e) => e.section !== "cobro" && e.section !== "pago_socio").map(entryKey);
  const live = {
    contract, cuota, fee, ingreso, cobrado, cobrada, cobroParcial: cobrada && fee > 0 && cobrado < fee - 0.01,
    tc, cobros, lines, costos, ganancia, pct,
    socio, socioArs: tc ? Math.round(socio * tc) : null, pagos, pagado,
    saldo: round2(socio - pagado), keys, frozen: false,
  };
  const snap = frozen[cuota]?.snapshot;
  if (!snap) return live;
  // Pagada: se muestra lo que se liquidó, y si los renglones cambiaron
  // después, cuánto (para cargar el ajuste en la cuota abierta).
  return {
    ...live, frozen: true, lines: snap.lines, costos: snap.costos, ganancia: snap.ganancia, ingreso: snap.ingreso,
    socio: snap.socio, socioArs: snap.socioArs ?? live.socioArs, tc: snap.tc ?? live.tc,
    saldo: round2(snap.socio - pagado), drift: round2(live.costos - snap.costos),
  };
}

/** Texto plano de la liquidación, listo para pegar en un mensaje. */
export function liquidacionTexto(l) {
  const f = (v) => v.toLocaleString("es-AR", { minimumFractionDigits: 2, maximumFractionDigits: 2 });
  const out = [
    ...(l.cobrada ? [] : ["PROYECCIÓN — la cuota todavía no se cobró"]),
    `Liquidación ${l.contract?.nombre || ""} — ${monthLabel(l.cuota)}`,
    `Cobrado: US$ ${f(l.ingreso)}${l.tc ? ` (BNA ${l.tc})` : ""}`,
    ...l.lines.filter((x) => x.usd).map((x) => `${x.label}: US$ ${f(x.usd)}`),
    `Total costos: US$ ${f(l.costos)}`,
    `Ganancia: US$ ${f(l.ganancia)}`,
    `${Math.round(l.pct * 100)}% de ganancia: US$ ${f(l.socio)}${l.socioArs ? ` = ARS ${l.socioArs.toLocaleString("es-AR")}` : ""}`,
  ];
  return out.join("\n");
}

/** Lo que se guarda al registrar el pago: la liquidación tal como se pagó. */
export function liquidacionSnapshot(l) {
  return {
    ingreso: l.ingreso, costos: l.costos, ganancia: l.ganancia, socio: l.socio, socioArs: l.socioArs, tc: l.tc,
    keys: l.keys,
    lines: l.lines.map((x) => ({
      key: x.key, label: x.label, short: x.short, sub: x.sub, usd: x.usd,
      rows: x.rows.map((r) => ({ period: r.period, provider: r.provider, concept: r.concept, basis: r.basis, amount_usd: Math.round(num(r.amount_usd) * 100) / 100 })),
    })),
  };
}
