// Contrato y reparto: avance de cada contrato (cupo → material recibido →
// entregado → cobrado) y la cuenta del socio con sus parámetros editables.
import { useEffect, useState } from "react";

import KpiCard from "../../primitives/KpiCard";
import { contractStatus, contractForCuota, liquidacion, monthLabel } from "./finanzasModel";

const money = (v) => `US$ ${(Number(v) || 0).toLocaleString("es-AR", { maximumFractionDigits: 0 })}`;
const pesos = (v) => `$ ${(Number(v) || 0).toLocaleString("es-AR", { maximumFractionDigits: 0 })}`;
const field = "bg-surface-3/60 ring-1 ring-white/[0.08] rounded px-2 py-1 text-caption text-white focus:outline-none focus:ring-brand/40";

function Bar({ label, used, cap, capAlDia, received }) {
  const p = cap ? Math.min(100, (used / cap) * 100) : 0;
  const r = cap ? Math.min(100, (received / cap) * 100) : 0;
  return (
    <div>
      <div className="flex justify-between text-label text-gray-400 mb-1">
        <span>{label}</span>
        <span className="tabular-nums">{used} entregadas · {received} recibidas · cupo a la fecha {capAlDia} de {cap}</span>
      </div>
      <div className="relative h-2.5 rounded-full bg-white/[0.06] overflow-hidden" title={`${Math.round(p)}% entregado`}>
        <div className="absolute inset-y-0 left-0 bg-sky-500/30" style={{ width: `${r}%` }} />
        <div className="absolute inset-y-0 left-0 bg-accent" style={{ width: `${p}%` }} />
      </div>
    </div>
  );
}

// Sin valores por defecto: los términos comerciales no viven en el código.
const EMPTY_CONTRACT = {
  nombre: "", inicio: "", meses: "", cupo_vl_mes: "", cupo_at_mes: "",
  precio_vl: "", precio_at: "", fee_mensual: "", portales: "argentina",
};

function ContractForm({ initial, onSave, onCancel }) {
  const [c, setC] = useState({ ...EMPTY_CONTRACT, ...initial, portales: (initial?.portales || ["argentina"]).join(",") });
  const set = (k) => (e) => setC({ ...c, [k]: e.target.value });
  const submit = () => {
    if (!c.nombre || !/^\d{4}-\d{2}$/.test(c.inicio)) return;
    const payload = {
      inicio: c.inicio, meses: Number(c.meses), cupo_vl_mes: Number(c.cupo_vl_mes), cupo_at_mes: Number(c.cupo_at_mes),
      precio_vl: Number(c.precio_vl), precio_at: Number(c.precio_at), fee_mensual: Number(c.fee_mensual),
      portales: String(c.portales).split(",").map((s) => s.trim()).filter(Boolean), cliente: c.cliente || null,
      acuerdo_socio: c.acuerdo_socio || null,
    };
    onSave({ nombre: c.nombre, payload });
  };
  const input = (k, label, w = "w-20") => (
    <label className="flex flex-col gap-1 text-label text-gray-500">
      {label}<input className={`${field} ${w}`} value={c[k]} onChange={set(k)} />
    </label>
  );
  return (
    <div className="glass rounded-card p-4 flex flex-wrap items-end gap-3">
      {input("nombre", "Nombre", "w-44")}
      {input("inicio", "Inicio (YYYY-MM)", "w-24")}
      {input("meses", "Meses")}
      {input("cupo_vl_mes", "Cupo VL/mes")}
      {input("cupo_at_mes", "Cupo AT/mes")}
      {input("precio_vl", "US$ por VL")}
      {input("precio_at", "US$ por AT")}
      {input("fee_mensual", "Fee mensual")}
      {input("portales", "Portales", "w-36")}
      <label className="flex flex-col gap-1 text-label text-gray-500 w-full">
        Acuerdo con el socio
        <textarea className={`${field} w-full min-h-[72px]`} value={c.acuerdo_socio || ""} onChange={set("acuerdo_socio")} />
      </label>
      <button type="button" onClick={submit} className="text-caption px-3 py-1 rounded bg-brand/30 hover:bg-brand/50 text-white">Guardar contrato</button>
      {onCancel && <button type="button" onClick={onCancel} className="text-caption text-gray-500 hover:text-white">Cancelar</button>}
    </div>
  );
}

function ParamsForm({ params, onSave }) {
  const [p, setP] = useState(params);
  useEffect(() => setP(params), [params]);
  const set = (k) => (e) => setP({ ...p, [k]: Number(String(e.target.value).replace(",", ".")) });
  const row = (label, key, hint) => (
    <label className="flex items-center justify-between gap-3 text-caption text-gray-300">
      <span>{label}{hint && <span className="block text-label text-gray-600">{hint}</span>}</span>
      <input className={`${field} w-24 text-right`} value={p[key] ?? ""} onChange={set(key)} />
    </label>
  );
  return (
    <div className="glass rounded-card p-4 space-y-3">
      <p className="text-section uppercase tracking-[0.08em] text-gray-400 font-semibold">Parámetros</p>
      {row("Parte del socio", "pct_socio", "0,5 = 50% de la ganancia")}
      {row("Tipo de cambio por defecto", "tipo_cambio", "ARS por US$, si la cuota no tiene el suyo")}
      {row("Reserva (meses de costos)", "reserva_meses")}
      <button
        type="button"
        onClick={() => { const { entryId: _ignored, ...payload } = p; onSave(payload); }}
        className="w-full mt-1 text-caption px-3 py-1.5 rounded bg-brand text-white hover:opacity-90"
      >
        Guardar parámetros
      </button>
    </div>
  );
}

export default function ContratoView({ f, effective }) {
  const [editing, setEditing] = useState(null);
  const { contracts, params, entries, ops, months } = f;
  // El reparto sale de las liquidaciones por cuota: es el mismo número que
  // se le manda al socio, no un recálculo por mes calendario.
  const cuotas = months.filter((m) => contractForCuota(contracts, m));
  const liqs = cuotas.map((m) => liquidacion({ entries: effective || entries, contracts, params, cuota: m }));
  // Solo cuotas cobradas: las demás son proyección y no se le deben todavía.
  const cobradas = liqs.filter((l) => l.cobrada);
  const socio = cobradas.reduce((a, l) => a + l.socio, 0);
  const pagado = liqs.reduce((a, l) => a + l.pagado, 0);
  const socioArs = cobradas.reduce((a, l) => a + (l.socioArs || 0), 0);
  const mensualCostos = liqs.length ? liqs.reduce((a, l) => a + l.costos, 0) / liqs.length : 0;
  const reserva = mensualCostos * (Number(params.reserva_meses) || 0);
  // Historial del socio: todos los pagos cargados, sin importar el rango.
  const pagos = entries.filter((e) => e.section === "pago_socio").sort((a, b) => a.period.localeCompare(b.period));

  const saveContract = async ({ nombre, payload }) => {
    if (editing?.entry) await f.updateEntry(editing.entry.id, { contract: nombre, concept: nombre, payload });
    else await f.createEntry({ period: payload.inicio, section: "contrato", contract: nombre, concept: nombre, basis: "acuerdo", payload });
    setEditing(null);
  };

  return (
    <div className="space-y-6">
      {contracts.length === 0 && !editing && (
        <div className="glass rounded-card p-4 text-caption text-gray-400">
          No hay contratos cargados. Sin contrato no se puede calcular el ingreso devengado.
          <button type="button" onClick={() => setEditing({})} className="ml-2 text-brand-light hover:text-white">Cargar el primero</button>
        </div>
      )}
      {editing && <ContractForm initial={editing.entry ? { nombre: editing.nombre, ...editing.entry.payload } : {}} onSave={saveContract} onCancel={() => setEditing(null)} />}

      {contracts.map((c) => {
        const st = contractStatus(c, ops, entries, contracts);
        const fin = c.inicio ? monthLabel(`${c.inicio}`) : "—";
        return (
          <section key={c.id} className="glass rounded-card p-5 space-y-4">
            <div className="flex items-start justify-between gap-3">
              <div>
                <h3 className="text-base font-semibold text-white">{c.nombre}</h3>
                <p className="text-caption text-gray-500">
                  Desde {fin} · {c.meses} meses · {c.cupo_vl_mes} VL + {c.cupo_at_mes} AT por mes · US$ {c.precio_vl}/VL, US$ {c.precio_at}/AT · {(c.portales || []).join(", ")}
                </p>
              </div>
              <button type="button" onClick={() => setEditing({ entry: c.entry, nombre: c.nombre })} className="text-label text-brand-light hover:text-white">Editar</button>
            </div>
            <div className="grid gap-3 md:grid-cols-4">
              <KpiCard label="Valor del contrato" value={money(st.valor)} hint={`${st.cap.lyric_video} VL + ${st.cap.art_track} AT`} />
              <KpiCard label="Devengado (entregado)" value={money(st.devengado)} hint={`${st.valor ? Math.round((st.devengado / st.valor) * 100) : 0}% del contrato`} tone="accent" />
              <KpiCard label="Cobrado" value={money(st.cobrado)} hint="renglones de Cobros" />
              <KpiCard
                label={st.anticipo >= 0 ? "Anticipo (cobrado sin entregar)" : "Saldo a cobrar"}
                value={money(Math.abs(st.anticipo))}
                tone={st.anticipo >= 0 ? "warn" : "default"}
              />
            </div>
            <div className="space-y-3">
              <Bar label="Lyric videos" used={st.used.lyric_video} cap={st.cap.lyric_video} capAlDia={st.capAlDia.lyric_video} received={st.recibidoValido.lyric_video} />
              <Bar label="Art tracks" used={st.used.art_track} cap={st.cap.art_track} capAlDia={st.capAlDia.art_track} received={st.recibidoValido.art_track} />
            </div>
            <div className="grid gap-3 md:grid-cols-2 text-caption">
              <div className="rounded-lg bg-surface-3/30 p-3">
                <p className="text-gray-400">Falta que el cliente mande</p>
                <p className="text-white tabular-nums">{st.pendienteMaterial.lyric_video} VL · {st.pendienteMaterial.art_track} AT</p>
                <p className="text-label text-gray-600">cupo de {st.mesesIniciados} meses iniciados − recibido válido ({st.discarded.lyric_video} descartados)</p>
              </div>
              <div className="rounded-lg bg-surface-3/30 p-3">
                <p className="text-gray-400">Pendiente nuestro</p>
                <p className="text-white tabular-nums">{st.pendienteNuestro.lyric_video} VL · {st.pendienteNuestro.art_track} AT</p>
                <p className="text-label text-gray-600">recibido válido − entregado</p>
              </div>
            </div>
          </section>
        );
      })}
      {contracts.length > 0 && !editing && (
        <button type="button" onClick={() => setEditing({})} className="text-label text-brand-light hover:text-white">+ agregar contrato</button>
      )}

      <div className="grid gap-4 lg:grid-cols-[1fr_320px]">
        <section className="glass rounded-card p-5 space-y-4">
          <div>
            <h3 className="text-base font-semibold text-white">Reparto con el socio</h3>
            <p className="text-caption text-gray-500">Suma de las liquidaciones por cuota de {monthLabel(months[0])} a {monthLabel(months[months.length - 1])}.</p>
          </div>
          <div className="grid gap-3 md:grid-cols-3">
            <KpiCard label="Le corresponde" value={money(socio)} tone="warn" hint={`${socioArs ? pesos(socioArs) + " · " : ""}${cobradas.length} cuota${cobradas.length === 1 ? "" : "s"} cobrada${cobradas.length === 1 ? "" : "s"}`} />
            <KpiCard label="Ya pagado" value={money(pagado)} hint="renglones de Pagos al socio" />
            <KpiCard label="Saldo a pagarle" value={money(socio - pagado)} tone="brand" />
          </div>
          <table className="w-full text-caption">
            <thead>
              <tr className="text-label uppercase tracking-[0.08em] text-gray-500">
                <th className="text-left py-1 font-medium">Cuota</th>
                <th className="text-right font-medium">Cobrado</th>
                <th className="text-right font-medium">Costos</th>
                <th className="text-right font-medium">Ganancia</th>
                <th className="text-right font-medium">Socio</th>
                <th className="text-right font-medium">Pagado</th>
              </tr>
            </thead>
            <tbody>
              {liqs.map((l) => (
                <tr key={l.cuota} className={`border-t border-white/[0.05] ${l.cobrada ? "text-gray-300" : "text-gray-600"}`}>
                  <td className="py-1.5">{monthLabel(l.cuota)}{!l.cobrada && <span className="ml-1.5 text-label">· sin cobrar (proyección)</span>}</td>
                  <td className="text-right tabular-nums">{l.cobrado ? money(l.cobrado) : "—"}</td>
                  <td className="text-right tabular-nums">{money(l.costos)}</td>
                  <td className="text-right tabular-nums">{money(l.ganancia)}</td>
                  <td className={`text-right tabular-nums ${l.cobrada ? "text-amber-300" : ""}`}>{money(l.socio)}{l.cobrada && l.socioArs ? ` · ${pesos(l.socioArs)}` : ""}</td>
                  <td className="text-right tabular-nums">{money(l.pagado)}</td>
                </tr>
              ))}
            </tbody>
          </table>
          <p className="text-label text-gray-500">
            Reserva sugerida: {money(reserva)} ({params.reserva_meses || 0} meses de costos). Las cuotas en curso todavía no tienen todos sus costos.
          </p>
          {pagos.length > 0 && (
            <div className="pt-3 border-t border-white/[0.06]">
              <p className="text-section uppercase tracking-[0.08em] text-gray-400 font-semibold mb-1.5">Historial de pagos al socio</p>
              <table className="w-full text-caption">
                <tbody>
                  {pagos.map((e) => (
                    <tr key={e.id} className="border-t border-white/[0.05] text-gray-300">
                      <td className="py-1">{monthLabel(e.period)}</td>
                      <td className="text-gray-500">{e.concept || e.note || ""}</td>
                      <td className="text-right tabular-nums">{e.payload?.ars ? pesos(e.payload.ars) : "—"}</td>
                      <td className="text-right tabular-nums">{money(e.amount_usd)}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </section>
        <ParamsForm params={params} onSave={f.saveParams} />
      </div>
    </div>
  );
}
