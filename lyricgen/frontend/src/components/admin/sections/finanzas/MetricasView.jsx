// Métricas clave de negocio y de operación, del mismo período que la planilla.
import KpiCard from "../../primitives/KpiCard";
import { monthLabel } from "./finanzasModel";

const money = (v) => `US$ ${(Number(v) || 0).toLocaleString("es-AR", { maximumFractionDigits: 0 })}`;
const money2 = (v) => (v === null || !Number.isFinite(v) ? "—" : `US$ ${v.toLocaleString("es-AR", { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`);
const pct = (a, b) => (b ? `${Math.round((a / b) * 100)}%` : "—");

function Table({ title, hint, cols, rows }) {
  return (
    <section className="glass rounded-card p-4 overflow-x-auto">
      <p className="text-section uppercase tracking-[0.08em] text-gray-400 font-semibold">{title}</p>
      {hint && <p className="text-label text-gray-600 mb-2">{hint}</p>}
      <table className="w-full text-caption mt-1">
        <thead>
          <tr className="text-label uppercase tracking-[0.08em] text-gray-500">
            {cols.map((c, i) => <th key={c} className={`py-1 font-medium ${i ? "text-right" : "text-left"}`}>{c}</th>)}
          </tr>
        </thead>
        <tbody>
          {rows.length === 0 && (
            <tr><td colSpan={cols.length} className="py-2 text-gray-600">Sin datos en el período</td></tr>
          )}
          {rows.map((r, i) => (
            <tr key={i} className="border-t border-white/[0.05] text-gray-300">
              {r.map((v, j) => <td key={j} className={`py-1.5 tabular-nums ${j ? "text-right" : ""}`}>{v}</td>)}
            </tr>
          ))}
        </tbody>
      </table>
    </section>
  );
}

export default function MetricasView({ f, pnl, effective = [] }) {
  const { ops, months } = f;
  const t = pnl.total;
  const piezas = (t.piezas_vl || 0) + (t.piezas_at || 0);
  // Horas de operación cargadas en los renglones de operadores (payload.horas).
  const horas = effective.filter((e) => e.section === "mano_obra" && months.includes(e.period)).reduce((a, e) => a + (Number(e.payload?.horas) || 0), 0);
  // ops trae datos desde el inicio del contrato; las métricas son del rango visible.
  const inRange = (r) => months.includes(r.month);
  const entregas = (ops.deliveries || []).filter(inRange).reduce((a, d) => a + d.entregas, 0);
  const crRecibidos = (ops.change_requests || []).filter(inRange).reduce((a, c) => a + c.recibidos, 0);

  const byMonth = (rows, pick) => months.map((m) => rows.filter((r) => r.month === m).reduce((a, r) => a + pick(r), 0));
  const deliveredVl = byMonth(ops.deliveries || [], (r) => (r.type === "lyric_video" ? r.canciones : 0));
  const deliveredAt = byMonth(ops.deliveries || [], (r) => (r.type === "art_track" ? r.canciones : 0));
  const receivedRows = byMonth(ops.received || [], (r) => r.recibidos);
  const discardedRows = byMonth(ops.received || [], (r) => r.descartados);
  const crIn = byMonth(ops.change_requests || [], (r) => r.recibidos);
  const crOut = byMonth(ops.change_requests || [], (r) => r.resueltos);
  const lead = Object.fromEntries((ops.lead_time || []).map((r) => [r.month, r]));
  const reviewers = [...new Set((ops.reviewers || []).map((r) => r.reviewer))];

  return (
    <div className="space-y-6">
      <div>
        <p className="text-section uppercase tracking-[0.08em] text-gray-400 font-semibold mb-2">Negocio</p>
        <div className="grid gap-3 md:grid-cols-4">
          <KpiCard label="Ingreso devengado" value={money(t.ingreso)} hint={`${t.piezas_vl || 0} VL + ${t.piezas_at || 0} AT entregados`} spark={months.map((m) => pnl.perMonth[m].ingreso)} />
          <KpiCard label="Margen bruto" value={pct(t.margenBruto, t.ingreso)} hint={money(t.margenBruto)} tone="accent" spark={months.map((m) => pnl.perMonth[m].margenBruto)} />
          <KpiCard label="Resultado operativo" value={money(t.operativo)} hint={`${pct(t.operativo, t.ingreso)} del ingreso · antes del trabajo`} />
          <KpiCard label="Concentración de clientes" value="100%" hint="Universal es el único cliente que paga" tone="warn" />
          <KpiCard label="Costo directo por pieza" value={money2(piezas ? ((t.directos || 0) + (t.manoObra || 0)) / piezas : null)} hint="infra + IA + operadores pagados" />
          <KpiCard label="Costo total por pieza" value={money2(piezas ? ((t.directos || 0) + (t.manoObra || 0) + (t.estructura || 0)) / piezas : null)} hint="incluye estructura" />
          <KpiCard label="Resultado por hora de operación" value={money2(horas ? t.operativo / horas : null)} hint={`${Math.round(horas)} h de operadores`} tone="brand" />
          <KpiCard label="Cobrado vs devengado" value={money((t.cobrado || 0) - (t.ingreso || 0))} hint={(t.cobrado || 0) >= (t.ingreso || 0) ? "anticipo: cobrado antes de entregar" : "saldo a cobrar"} />
        </div>
      </div>

      <div>
        <p className="text-section uppercase tracking-[0.08em] text-gray-400 font-semibold mb-2">Operación</p>
        <div className="grid gap-3 md:grid-cols-4 mb-4">
          <KpiCard label="Piezas entregadas" value={String(entregas)} hint="primera entrega vigente de cada job" spark={months.map((m, i) => deliveredVl[i] + deliveredAt[i])} />
          <KpiCard label="Material recibido" value={String(receivedRows.reduce((a, b) => a + b, 0))} hint={`${discardedRows.reduce((a, b) => a + b, 0)} descartados (instrumental o repetido)`} />
          <KpiCard label="Pedidos de cambio" value={String(crRecibidos)} hint={`${pct(crRecibidos, entregas)} de las entregas · ${ops.change_requests_open ?? 0} abiertos hoy`} tone={crRecibidos ? "warn" : "default"} />
          <KpiCard label="Horas de operación" value={String(Math.round(horas))} hint="cargadas en los renglones de operadores" />
        </div>
        <div className="grid gap-4 lg:grid-cols-2">
          <Table
            title="Producción por mes"
            hint="canciones distintas entregadas; tiempo = de la subida del audio a la primera entrega"
            cols={["Mes", "VL", "AT", "Recibidos", "Descartados", "Mediana días", "p90 días"]}
            rows={months.map((m, i) => [monthLabel(m), deliveredVl[i], deliveredAt[i], receivedRows[i], discardedRows[i], lead[m]?.mediana_dias ?? "—", lead[m]?.p90_dias ?? "—"])}
          />
          <Table
            title="Pedidos de cambio del cliente"
            cols={["Mes", "Recibidos", "Resueltos", "Por 100 entregas"]}
            rows={months.map((m, i) => [monthLabel(m), crIn[i], crOut[i], deliveredVl[i] + deliveredAt[i] ? Math.round((crIn[i] / (deliveredVl[i] + deliveredAt[i])) * 100) : "—"])}
          />
          <Table
            title="Letras aprobadas por persona"
            hint="canciones distintas con letra aprobada en el mes (una canción puede aprobarse en dos meses)"
            cols={["Persona", ...months.map(monthLabel)]}
            rows={reviewers.map((name) => [name, ...months.map((m) => (ops.reviewers.find((r) => r.reviewer === name && r.month === m)?.canciones ?? "·"))])}
          />
          <Table
            title="Economía por pieza (precio de lista vs costo)"
            cols={["", "Lyric video", "Art track"]}
            rows={f.contracts.map((c) => [c.nombre, `US$ ${c.precio_vl}`, `US$ ${c.precio_at}`]).concat([
              ["Costo total por pieza (promedio)", money2(piezas ? ((t.directos || 0) + (t.manoObra || 0) + (t.estructura || 0)) / piezas : null), "mismo promedio"],
            ])}
          />
        </div>
      </div>
    </div>
  );
}
