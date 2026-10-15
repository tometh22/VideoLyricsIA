// Liquidación por cuota: la misma tabla que se le manda al socio cada mes,
// con el estado del cobro y del pago, y el acuerdo vigente a la vista.
import { useMemo, useState } from "react";

import { addMonths, currentPeriod, liquidacion, liquidacionTexto, monthLabel, monthsBetween } from "./finanzasModel";
import { BasisBadge } from "./ResultadoView";

const usd = (v) => (Number(v) || 0).toLocaleString("es-AR", { minimumFractionDigits: 2, maximumFractionDigits: 2 });
const ars = (v) => `ARS ${(Number(v) || 0).toLocaleString("es-AR", { maximumFractionDigits: 0 })}`;

function Detail({ rows }) {
  if (!rows.length) return null;
  return (
    <div className="mt-1 space-y-0.5">
      {rows.map((e, i) => (
        <div key={`${e.id ?? "s"}-${i}`} className="flex items-center gap-2 text-label text-gray-500">
          <span className="truncate">{[e.provider, e.concept].filter(Boolean).join(" · ")}</span>
          <BasisBadge basis={e.basis} />
          <span className="ml-auto tabular-nums">{usd(e.amount_usd)}</span>
        </div>
      ))}
    </div>
  );
}

export default function LiquidacionView({ f, effective }) {
  const { contracts, params } = f;
  const cuotas = useMemo(() => {
    const out = [];
    for (const c of contracts) {
      const fin = addMonths(c.inicio, (Number(c.meses) || 1) - 1);
      const hasta = currentPeriod() < fin ? currentPeriod() : fin;
      out.push(...monthsBetween(c.inicio, hasta));
    }
    return [...new Set(out)];
  }, [contracts]);
  const [cuota, setCuota] = useState(null);
  const [copied, setCopied] = useState(false);
  const active = cuota || cuotas[0];

  if (!contracts.length) {
    return <p className="text-caption text-gray-500">Cargá el contrato en "Contrato y reparto" para ver las liquidaciones.</p>;
  }
  const l = liquidacion({ entries: effective, contracts, params, cuota: active });
  const acuerdo = l.contract?.acuerdo_socio;

  const copy = async () => {
    try {
      await navigator.clipboard.writeText(liquidacionTexto(l));
      setCopied(true);
      setTimeout(() => setCopied(false), 2000);
    } catch {
      setCopied(false);
    }
  };

  return (
    <div className="grid gap-4 lg:grid-cols-[1fr_320px]">
      <section className="glass rounded-card p-5 space-y-3">
        <div className="flex flex-wrap items-center gap-2">
          {cuotas.map((m, i) => (
            <button
              key={m}
              type="button"
              onClick={() => setCuota(m)}
              className={`px-3 py-1 rounded-md text-caption ring-1 ${m === active ? "bg-brand/20 ring-brand/40 text-white" : "ring-white/[0.06] text-gray-400 hover:text-white"}`}
            >
              {monthLabel(m)} <span className="text-gray-500">· cuota {i + 1}</span>
            </button>
          ))}
          <button type="button" onClick={copy} className="ml-auto text-caption px-3 py-1 rounded glass hover:bg-white/[0.06]">
            {copied ? "Copiada" : "Copiar para mandar"}
          </button>
        </div>
        <table className="w-full text-caption">
          <thead>
            <tr className="text-label uppercase tracking-[0.08em] text-gray-500">
              <th className="text-left py-1.5 font-medium">Concepto</th>
              <th className="text-right font-medium">US$</th>
            </tr>
          </thead>
          <tbody className="text-gray-300">
            <tr className="border-t border-white/[0.06] text-white font-semibold">
              <td className="py-2">
                Cobrado {monthLabel(active)}
                {l.cobrado > 0 && l.tc ? <span className="ml-1 font-normal text-gray-400">({ars(l.cobrado * l.tc)}, BNA {l.tc})</span> : <span className="ml-1 font-normal text-amber-300">(sin cobro registrado)</span>}
              </td>
              <td className="text-right tabular-nums">{usd(l.ingreso)}</td>
            </tr>
            {l.lines.map((line) => (
              <tr key={line.key} className="border-t border-white/[0.05] align-top">
                <td className="py-2">
                  {line.label}
                  <Detail rows={line.rows} />
                </td>
                <td className="text-right tabular-nums py-2">{usd(line.usd)}</td>
              </tr>
            ))}
            <tr className="border-t border-white/[0.08] text-white font-semibold">
              <td className="py-2">Total costos</td>
              <td className="text-right tabular-nums">{usd(l.costos)}</td>
            </tr>
            <tr className="text-white font-semibold">
              <td className="py-2">Ganancia</td>
              <td className="text-right tabular-nums">{usd(l.ganancia)}</td>
            </tr>
            <tr className="text-amber-300 font-semibold">
              <td className="py-2">{Math.round(l.pct * 100)}% de ganancia</td>
              <td className="text-right tabular-nums">{usd(l.socio)}{l.socioArs ? ` = ${ars(l.socioArs)}` : ""}</td>
            </tr>
          </tbody>
        </table>
        <p className="text-label text-gray-500">
          Los costos de producción van a la cuota de las piezas que produjeron (columna "Cuota" en Movimientos); servidores y herramientas, al mes en que se usaron. Cada costo se cuenta una sola vez.
        </p>
      </section>

      <aside className="space-y-4">
        <section className="glass rounded-card p-4 space-y-2 text-caption">
          <p className="text-section uppercase tracking-[0.08em] text-gray-400 font-semibold">Pago al socio</p>
          <div className="flex justify-between"><span className="text-gray-400">Le corresponde</span><span className="tabular-nums text-white">US$ {usd(l.socio)}</span></div>
          <div className="flex justify-between"><span className="text-gray-400">Pagado</span><span className="tabular-nums">US$ {usd(l.pagado)}</span></div>
          <div className="flex justify-between font-semibold"><span>{l.saldo > 0.01 ? "Pendiente" : "Saldado"}</span><span className={`tabular-nums ${l.saldo > 0.01 ? "text-amber-300" : "text-accent"}`}>US$ {usd(l.saldo)}</span></div>
          {l.pagos.map((p, i) => (
            <p key={i} className="text-label text-gray-500">{[p.concept, p.note, p.payload?.ars ? ars(p.payload.ars) : null].filter(Boolean).join(" · ")}</p>
          ))}
        </section>
        {acuerdo && (
          <section className="glass rounded-card p-4 space-y-1.5">
            <p className="text-section uppercase tracking-[0.08em] text-gray-400 font-semibold">Acuerdo con el socio</p>
            <p className="text-caption text-gray-300 whitespace-pre-line leading-relaxed">{acuerdo}</p>
          </section>
        )}
      </aside>
    </div>
  );
}
