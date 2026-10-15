// Planilla de resultados por mes (devengado). Cada escalón responde una
// pregunta: ¿cuánto entregamos? → ¿cuánto deja después de producirlo? →
// ¿y después de mantener la empresa? → ¿y después de pagar el trabajo? →
// ¿cuánto le toca al socio?
//
// Las celdas de renglones cargados a mano se editan con un clic; las que
// salen del sistema (entregas, trabajo imputado) no.
import { useMemo, useState } from "react";

import { BASIS_LABELS, groupRows, monthChecklist, monthLabel } from "./finanzasModel";

const usd = (v) => {
  const n = Number(v) || 0;
  const s = Math.abs(n).toLocaleString("es-AR", { minimumFractionDigits: 2, maximumFractionDigits: 2 });
  return n < 0 ? `(${s})` : s;
};
const pct = (part, whole) => (whole ? `${Math.round((part / whole) * 100)}%` : "—");

const BASIS_TONE = {
  factura: "bg-accent/15 text-accent",
  api: "bg-accent/15 text-accent",
  captura: "bg-sky-500/15 text-sky-300",
  sistema: "bg-sky-500/15 text-sky-300",
  acuerdo: "bg-brand/20 text-brand-light",
  estimado: "bg-amber-500/15 text-amber-300",
  imputado: "bg-amber-500/15 text-amber-300",
};

export function BasisBadge({ basis }) {
  if (!basis) return null;
  return (
    <span className={`px-1.5 py-0.5 rounded text-[10px] font-semibold uppercase tracking-wide ${BASIS_TONE[basis] || "bg-white/10 text-gray-300"}`}>
      {BASIS_LABELS[basis] || basis}
    </span>
  );
}

function EditableCell({ value, entries, onSave, onCreate }) {
  const [editing, setEditing] = useState(false);
  const [draft, setDraft] = useState("");
  const real = (entries || []).filter((e) => !e.synthetic);
  const synthetic = (entries || []).find((e) => e.synthetic);
  const many = real.length > 1;
  const evidence = (entries || []).map((e) => [e.concept, e.evidence, e.note].filter(Boolean).join(" · ")).filter(Boolean).join("\n");
  const incomplete = synthetic?.coverage && synthetic.coverage.days < synthetic.coverage.expected;
  if (editing) {
    const commit = async () => {
      setEditing(false);
      const n = draft.trim() === "" ? null : Number(draft.replace(",", "."));
      if (n !== null && !Number.isFinite(n)) return;
      // Editar un valor automático lo convierte en uno cargado a mano.
      if (real.length === 1) await onSave(real[0], n);
      else if (n !== null) await onCreate(n);
    };
    return (
      <td className="px-2 py-1 text-right">
        <input
          autoFocus
          aria-label="Monto en USD"
          value={draft}
          onChange={(e) => setDraft(e.target.value)}
          onBlur={commit}
          onKeyDown={(e) => {
            if (e.key === "Enter") e.currentTarget.blur();
            if (e.key === "Escape") setEditing(false);
          }}
          className="w-24 bg-surface-3/60 ring-1 ring-brand/40 rounded px-1.5 py-0.5 text-right text-caption text-white tabular-nums focus:outline-none"
        />
      </td>
    );
  }
  return (
    <td
      className={`px-2 py-1.5 text-right tabular-nums ${many ? "text-gray-300 cursor-help" : "cursor-pointer hover:bg-white/[0.04]"} ${value ? (synthetic && !real.length ? "text-sky-200/90 italic" : "text-gray-200") : "text-gray-600"}`}
      title={many ? `${real.length} renglones: editalos en Movimientos\n${evidence}`
        : synthetic && !real.length ? `${evidence}\nAutomático. Clic para reemplazarlo por un valor cargado a mano.`
          : evidence || "Clic para cargar"}
      onClick={() => {
        if (many) return;
        setDraft(value ? String(Math.round(value * 100) / 100) : "");
        setEditing(true);
      }}
    >
      {value ? usd(value) : "·"}
      {incomplete && <span className="ml-1 text-amber-300" title="Mes incompleto en el recolector">•</span>}
    </td>
  );
}

function SectionTitle({ label, months, hint }) {
  return (
    <tr className="border-t border-white/[0.06]">
      <td colSpan={months.length + 3} className="pt-4 pb-1.5 px-2">
        <span className="text-section uppercase tracking-[0.08em] text-gray-400 font-semibold">{label}</span>
        {hint && <span className="ml-2 text-label text-gray-600 normal-case">{hint}</span>}
      </td>
    </tr>
  );
}

function ValueRow({ label, values, months, total, tone = "", sub, badge, indent = true }) {
  return (
    <tr className={tone}>
      <td className={`py-1.5 pr-2 ${indent ? "pl-5" : "pl-2"} text-caption`}>
        <span>{label}</span>
        {sub && <span className="ml-1.5 text-label text-gray-600">{sub}</span>}
      </td>
      <td className="px-2">{badge}</td>
      {months.map((m) => (
        <td key={m} className="px-2 py-1.5 text-right tabular-nums text-caption">{values[m] ? usd(values[m]) : "·"}</td>
      ))}
      <td className="px-2 py-1.5 text-right tabular-nums text-caption font-semibold">{usd(total)}</td>
    </tr>
  );
}

function TotalRow({ label, pnl, field, months, tone = "text-white", ratioOf }) {
  return (
    <>
      <tr className={`border-t border-white/[0.08] ${tone}`}>
        <td className="py-2 pl-2 pr-2 text-caption font-semibold" colSpan={2}>{label}</td>
        {months.map((m) => (
          <td key={m} className="px-2 py-2 text-right tabular-nums text-caption font-semibold">{usd(pnl.perMonth[m][field])}</td>
        ))}
        <td className="px-2 py-2 text-right tabular-nums text-caption font-bold">{usd(pnl.total[field])}</td>
      </tr>
      {ratioOf && (
        <tr className="text-gray-500">
          <td className="pl-5 pb-1.5 text-label" colSpan={2}>% del ingreso</td>
          {months.map((m) => (
            <td key={m} className="px-2 pb-1.5 text-right tabular-nums text-label">{pct(pnl.perMonth[m][field], pnl.perMonth[m][ratioOf])}</td>
          ))}
          <td className="px-2 pb-1.5 text-right tabular-nums text-label">{pct(pnl.total[field], pnl.total[ratioOf])}</td>
        </tr>
      )}
    </>
  );
}

function AddRow({ section, months, onCreate }) {
  const [open, setOpen] = useState(false);
  const [form, setForm] = useState({ category: "", provider: "", period: months[months.length - 1], amount: "", basis: "factura" });
  if (!open) {
    return (
      <tr>
        <td colSpan={months.length + 3} className="pl-5 py-1">
          <button type="button" onClick={() => setOpen(true)} className="text-label text-brand-light hover:text-white">+ agregar renglón</button>
        </td>
      </tr>
    );
  }
  const field = "bg-surface-3/60 ring-1 ring-white/[0.08] rounded px-1.5 py-0.5 text-caption text-white focus:outline-none focus:ring-brand/40";
  const submit = async () => {
    if (!form.provider.trim()) return;
    await onCreate({
      section, period: form.period, category: form.category || null, provider: form.provider,
      amount_usd: form.amount === "" ? null : Number(form.amount), basis: form.basis,
    });
    setOpen(false);
  };
  return (
    <tr>
      <td colSpan={months.length + 3} className="pl-5 py-1.5">
        <div className="flex flex-wrap items-center gap-2">
          <input className={`${field} w-28`} placeholder="Categoría" value={form.category} onChange={(e) => setForm({ ...form, category: e.target.value })} />
          <input className={`${field} w-36`} placeholder="Proveedor / concepto" value={form.provider} onChange={(e) => setForm({ ...form, provider: e.target.value })} />
          <select className={field} value={form.period} onChange={(e) => setForm({ ...form, period: e.target.value })}>
            {months.map((m) => <option key={m} value={m}>{monthLabel(m)}</option>)}
          </select>
          <input className={`${field} w-24 text-right`} placeholder="USD" value={form.amount} onChange={(e) => setForm({ ...form, amount: e.target.value })} />
          <select className={field} value={form.basis} onChange={(e) => setForm({ ...form, basis: e.target.value })}>
            {Object.entries(BASIS_LABELS).map(([k, v]) => <option key={k} value={k}>{v}</option>)}
          </select>
          <button type="button" onClick={submit} className="text-caption px-2.5 py-0.5 rounded bg-brand/30 hover:bg-brand/50 text-white">Guardar</button>
          <button type="button" onClick={() => setOpen(false)} className="text-caption text-gray-500 hover:text-white">Cancelar</button>
        </div>
      </td>
    </tr>
  );
}

function GroupRows({ section, entries, months, onSave, onCreate }) {
  const rows = useMemo(() => groupRows(entries, section, months), [entries, section, months]);
  let lastCategory = null;
  return (
    <>
      {rows.map((r) => {
        const showCategory = r.category !== lastCategory;
        lastCategory = r.category;
        return (
          <tr key={r.key} className="text-gray-300">
            <td className="py-1 pl-5 pr-2 text-caption">
              {showCategory && <span className="block text-label text-gray-500">{r.category}</span>}
              {r.provider}
            </td>
            <td className="px-2"><div className="flex gap-1">{r.bases.map((b) => <BasisBadge key={b} basis={b} />)}</div></td>
            {months.map((m) => (
              <EditableCell
                key={m}
                value={r.byMonth[m]}
                entries={r.entries[m]}
                onSave={(entry, amount) => onSave(entry.id, { amount_usd: amount })}
                onCreate={(amount) => onCreate({ section, period: m, category: r.category === "Sin categoría" ? null : r.category, provider: r.provider, amount_usd: amount, basis: r.bases[0] || "estimado" })}
              />
            ))}
            <td className="px-2 py-1 text-right tabular-nums text-caption text-gray-200">{usd(r.total)}</td>
          </tr>
        );
      })}
      <AddRow section={section} months={months} onCreate={onCreate} />
    </>
  );
}

function Checklist({ month, f, effective }) {
  const items = monthChecklist({ month, entries: f.entries, effective, autoCosts: f.autoCosts });
  const pending = items.filter((i) => !i.ok).length;
  return (
    <div className="glass rounded-card p-3 mb-4">
      <p className="text-caption text-gray-300 mb-2">
        Cierre de <span className="text-white font-semibold">{monthLabel(month)}</span>
        {pending ? <span className="ml-2 text-amber-300">{pending} pendiente{pending > 1 ? "s" : ""}</span> : <span className="ml-2 text-accent">completo</span>}
      </p>
      <div className="flex flex-wrap gap-2">
        {items.map((i) => (
          <span key={i.label} title={i.detail} className={`px-2 py-0.5 rounded-md text-label ring-1 ${i.ok ? "ring-accent/30 text-accent" : "ring-amber-400/30 text-amber-300"}`}>
            {i.ok ? "✓" : "!"} {i.label}
            <span className="ml-1 text-gray-500">{i.detail}</span>
          </span>
        ))}
      </div>
    </div>
  );
}

export default function ResultadoView({ f, pnl, effective }) {
  const { months } = f;
  const entries = effective || f.entries;
  const save = (id, body) => f.updateEntry(id, body);
  const create = (body) => f.createEntry(body);
  const piezas = Object.fromEntries(months.map((m) => [m, pnl.perMonth[m].ingresoEntregas]));

  return (
    <div>
    <Checklist month={months[months.length - 1]} f={f} effective={entries} />
    <div className="glass rounded-card p-4 overflow-x-auto">
      <table className="w-full min-w-[720px] text-gray-300">
        <thead>
          <tr className="text-label uppercase tracking-[0.08em] text-gray-500">
            <th className="text-left pl-2 py-1.5 font-medium">Concepto</th>
            <th className="text-left px-2 font-medium">Base</th>
            {months.map((m) => <th key={m} className="text-right px-2 font-medium">{monthLabel(m)}</th>)}
            <th className="text-right px-2 font-medium">Total</th>
          </tr>
        </thead>
        <tbody>
          <SectionTitle label="Ingresos (devengado)" months={months} hint="lo entregado en el mes × precio del contrato" />
          <ValueRow
            label="Entregas del contrato"
            sub={months.map((m) => pnl.perMonth[m]).reduce((a, p) => a + p.piezas_vl, 0) + " VL · " + months.map((m) => pnl.perMonth[m]).reduce((a, p) => a + p.piezas_at, 0) + " AT"}
            values={piezas} months={months} total={pnl.total.ingresoEntregas}
            badge={<BasisBadge basis="sistema" />}
          />
          <GroupRows section="ingreso" entries={entries} months={months} onSave={save} onCreate={create} />
          <TotalRow label="Ingreso total" pnl={pnl} field="ingreso" months={months} />

          <SectionTitle label="Costos directos" months={months} hint="lo que cuesta producir: infraestructura, IA y operadores" />
          <GroupRows section="costo_directo" entries={entries} months={months} onSave={save} onCreate={create} />
          <SectionTitle label="Mano de obra pagada" months={months} />
          <GroupRows section="mano_obra" entries={entries} months={months} onSave={save} onCreate={create} />
          <TotalRow label="Margen bruto" pnl={pnl} field="margenBruto" months={months} tone="text-accent" ratioOf="ingreso" />

          <SectionTitle label="Gastos de estructura" months={months} hint="lo que se paga aunque no haya clientes" />
          <GroupRows section="estructura" entries={entries} months={months} onSave={save} onCreate={create} />
          <TotalRow label="Resultado operativo (antes del trabajo del equipo)" pnl={pnl} field="operativo" months={months} ratioOf="ingreso" />

          <tr>
            <td colSpan={months.length + 3} className="px-2 pt-3 pb-1 text-label text-gray-500">
              Esta vista es por mes calendario. El reparto con el socio se calcula por cuota, en "Liquidación por cuota".
            </td>
          </tr>

          <SectionTitle label="Caja" months={months} hint="lo cobrado y pagado efectivamente" />
          <GroupRows section="cobro" entries={entries} months={months} onSave={save} onCreate={create} />
          <GroupRows section="pago_socio" entries={entries} months={months} onSave={save} onCreate={create} />
        </tbody>
      </table>
      <p className="mt-3 text-label text-gray-600">
        <span className="text-sky-200/90 italic">Celeste</span> = automático (recolector diario, gasto recurrente o letras aprobadas por operador). Lo cargado a mano lo reemplaza.
      </p>
    </div>
    </div>
  );
}
