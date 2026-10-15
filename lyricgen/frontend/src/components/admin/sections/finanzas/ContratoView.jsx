// Contrato y reparto: avance de cada contrato (cupo → material recibido →
// entregado → cobrado) y la cuenta del socio con sus parámetros editables.
import { useEffect, useState } from "react";

import KpiCard from "../../primitives/KpiCard";
import { contractStatus, monthLabel, partnerScenarios } from "./finanzasModel";

const money = (v) => `US$ ${(Number(v) || 0).toLocaleString("es-AR", { maximumFractionDigits: 0 })}`;
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
      <button type="button" onClick={submit} className="text-caption px-3 py-1 rounded bg-brand/30 hover:bg-brand/50 text-white">Guardar contrato</button>
      {onCancel && <button type="button" onClick={onCancel} className="text-caption text-gray-500 hover:text-white">Cancelar</button>}
    </div>
  );
}

function ParamsForm({ params, onSave }) {
  const [p, setP] = useState(params);
  useEffect(() => setP(params), [params]);
  const set = (k, cast = Number) => (e) => setP({ ...p, [k]: cast(e.target.value) });
  const row = (label, children, hint) => (
    <label className="flex items-center justify-between gap-3 text-caption text-gray-300">
      <span>{label}{hint && <span className="block text-label text-gray-600">{hint}</span>}</span>
      {children}
    </label>
  );
  return (
    <div className="glass rounded-card p-4 space-y-2.5">
      <p className="text-section uppercase tracking-[0.08em] text-gray-400 font-semibold">Parámetros del reparto</p>
      {row("Tarifa por hora (US$)", <input className={`${field} w-20 text-right`} value={p.tarifa_hora} onChange={set("tarifa_hora")} />)}
      {row("Horas por día · Agus", <input className={`${field} w-20 text-right`} value={p.horas_dia_agus} onChange={set("horas_dia_agus")} />)}
      {row("Horas por día · Tomi", <input className={`${field} w-20 text-right`} value={p.horas_dia_tomi} onChange={set("horas_dia_tomi")} />)}
      {row("Parte operativa de Tomi", <input className={`${field} w-20 text-right`} value={p.pct_operacion_tomi} onChange={set("pct_operacion_tomi")} />, "0 a 1; el resto es desarrollo")}
      {row("Parte del socio", <input className={`${field} w-20 text-right`} value={p.pct_socio} onChange={set("pct_socio")} />, "0,5 = 50% de la ganancia")}
      {row("Trabajo que se resta", (
        <select className={field} value={p.resta_trabajo} onChange={set("resta_trabajo", String)}>
          <option value="ninguno">Ninguno</option>
          <option value="agus">Solo Agus</option>
          <option value="agus_y_tomi">Agus + operación de Tomi</option>
        </select>
      ))}
      {row("Contar solo días hábiles", <input type="checkbox" checked={!!p.solo_dias_habiles} onChange={(e) => setP({ ...p, solo_dias_habiles: e.target.checked })} />)}
      {row("Reserva (meses de costos)", <input className={`${field} w-20 text-right`} value={p.reserva_meses} onChange={set("reserva_meses")} />)}
      <button
        type="button"
        onClick={() => { const { entryId: _ignored, ...payload } = p; onSave(payload); }}
        className="w-full mt-1 text-caption px-3 py-1.5 rounded bg-brand/30 hover:bg-brand/50 text-white"
      >
        Guardar parámetros
      </button>
    </div>
  );
}

export default function ContratoView({ f, pnl }) {
  const [editing, setEditing] = useState(null);
  const { contracts, params, entries, ops, months } = f;
  const scenarios = partnerScenarios(pnl, params);
  const pagado = pnl.total.pagadoSocio || 0;
  const mensualCostos = months.length
    ? ((pnl.total.directos || 0) + (pnl.total.manoObra || 0) + (pnl.total.estructura || 0)) / months.length : 0;
  const reserva = mensualCostos * (Number(params.reserva_meses) || 0);

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
            <p className="text-caption text-gray-500">Período {monthLabel(months[0])} → {monthLabel(months[months.length - 1])}. Se calcula sobre el acumulado.</p>
          </div>
          <div className="grid gap-3 md:grid-cols-3">
            <KpiCard label="Le corresponde" value={money(pnl.total.socio)} tone="warn" hint={`${Math.round(params.pct_socio * 100)}% de ${money(pnl.total.baseReparto)}`} />
            <KpiCard label="Ya pagado" value={money(pagado)} hint="renglones de Pagos al socio" />
            <KpiCard label="Saldo a pagarle" value={money(pnl.total.socio - pagado)} tone="brand" />
          </div>
          <table className="w-full text-caption">
            <thead>
              <tr className="text-label uppercase tracking-[0.08em] text-gray-500">
                <th className="text-left py-1 font-medium">Tarifa por hora</th>
                <th className="text-right font-medium">Horas restadas</th>
                <th className="text-right font-medium">Trabajo</th>
                <th className="text-right font-medium">Base</th>
                <th className="text-right font-medium">Socio</th>
                <th className="text-right font-medium">Equipo (trabajo + su parte)</th>
              </tr>
            </thead>
            <tbody>
              {scenarios.map((s) => (
                <tr key={s.rate} className={`border-t border-white/[0.05] ${s.rate === Number(params.tarifa_hora) ? "text-white" : "text-gray-400"}`}>
                  <td className="py-1.5">US$ {s.rate}</td>
                  <td className="text-right tabular-nums">{Math.round(s.horas)}</td>
                  <td className="text-right tabular-nums">{money(s.trabajo)}</td>
                  <td className="text-right tabular-nums">{money(s.base)}</td>
                  <td className="text-right tabular-nums text-amber-300">{money(s.socio)}</td>
                  <td className="text-right tabular-nums">{money(s.equipo)}</td>
                </tr>
              ))}
            </tbody>
          </table>
          <p className="text-label text-gray-500">
            Reserva sugerida antes de repartir: {money(reserva)} ({params.reserva_meses} meses de costos directos + estructura).
          </p>
        </section>
        <ParamsForm params={params} onSave={f.saveParams} />
      </div>
    </div>
  );
}
