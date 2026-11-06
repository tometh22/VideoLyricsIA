// Sección "Finanzas" del admin (sólo super-admin): planilla de resultados,
// contrato y reparto, métricas y criterio. Un solo rango de meses para todo.
import { useMemo } from "react";

import SectionHeader from "../../layout/SectionHeader";
import ContratoView from "./ContratoView";
import CriterioView from "./CriterioView";
import LiquidacionView from "./LiquidacionView";
import MetricasView from "./MetricasView";
import MixFondosView from "./MixFondosView";
import MovimientosView from "./MovimientosView";
import ResultadoView from "./ResultadoView";
import { addMonths, buildPnl, currentPeriod, effectiveEntries, monthsBetween } from "./finanzasModel";
import useFinanzas from "./useFinanzas";

const TITLES = {
  liquidacion: ["Liquidación por cuota", "Cuánto entró, cuánto costó y cuánto le toca al socio en cada cuota del contrato."],
  resultado: ["Resultado por mes", "Devengado: ingreso entregado y costo consumido en cada mes. Clic en una celda para cargarla."],
  contrato: ["Contrato y reparto", "Avance de cada contrato y la cuenta del socio."],
  metricas: ["Métricas", "Negocio y operación del mismo período."],
  mix: ["Mix de fondos", "Video Veo vs foto + efecto de lo publicado en los portales. Tildá las campañas que entran en la cuenta del contrato."],
  movimientos: ["Movimientos", "Cada renglón con su base y su evidencia. Importar o exportar CSV."],
  criterio: ["Criterio", "Cómo se calcula y qué mira cada rol."],
};

export default function FinanzasSection({ subTab = "liquidacion" }) {
  const f = useFinanzas();
  // Lo cargado a mano + lo automático (recolector, recurrentes, operadores).
  const effective = useMemo(
    () => effectiveEntries({ entries: f.entries, autoCosts: f.autoCosts, reviewers: f.ops.reviewers || [], params: f.params, months: f.months }),
    [f.entries, f.autoCosts, f.ops, f.params, f.months],
  );
  // Las liquidaciones se calculan SIEMPRE sobre todo el contrato (desde su
  // inicio hasta hoy), sin importar el rango visible: si no, una cuota cambia
  // según el "Desde/Hasta" elegido.
  const contractMonths = useMemo(() => {
    const set = new Set(f.months);
    for (const c of f.contracts) {
      const fin = addMonths(c.inicio, (Number(c.meses) || 1) - 1);
      const hasta = currentPeriod() < fin ? currentPeriod() : fin;
      if (c.inicio <= hasta) monthsBetween(c.inicio, hasta).forEach((m) => set.add(m));
    }
    return [...set].sort();
  }, [f.months, f.contracts]);
  const effectiveAll = useMemo(
    () => effectiveEntries({ entries: f.entries, autoCosts: f.autoCosts, reviewers: f.ops.reviewers || [], params: f.params, months: contractMonths }),
    [f.entries, f.autoCosts, f.ops, f.params, contractMonths],
  );
  const pnl = useMemo(
    () => buildPnl({ entries: effective, contracts: f.contracts, deliveries: f.ops.deliveries || [], params: f.params, months: f.months }),
    [effective, f.contracts, f.ops, f.params, f.months],
  );
  const [title, subtitle] = TITLES[subTab] || TITLES.liquidacion;
  const monthInput = "bg-surface-3/60 ring-1 ring-white/[0.08] rounded px-2 py-1 text-caption text-white focus:outline-none focus:ring-brand/40";

  return (
    <div>
      <SectionHeader
        title={title}
        subtitle={subtitle}
        right={!["criterio", "liquidacion", "mix"].includes(subTab) && (
          <div className="flex items-center gap-2 text-caption text-gray-400">
            <span>Desde</span>
            <input type="month" aria-label="Desde" className={monthInput} value={f.since} onChange={(e) => e.target.value && f.setSince(e.target.value)} />
            <span>hasta</span>
            <input type="month" aria-label="Hasta" className={monthInput} value={f.until} onChange={(e) => e.target.value && f.setUntil(e.target.value)} />
          </div>
        )}
      />
      {f.error && (
        <div className="mb-4 rounded-card bg-red-500/10 ring-1 ring-red-500/20 p-3 text-caption text-red-300 flex justify-between">
          <span>{f.error}</span>
          <button type="button" onClick={() => f.setError(null)} className="text-red-200 hover:text-white">✕</button>
        </div>
      )}
      {subTab === "mix" ? (
        // No depende de la planilla: sus datos salen de los portales.
        <MixFondosView />
      ) : f.loading ? (
        <p className="text-caption text-gray-500">Cargando…</p>
      ) : (
        <>
          {subTab === "liquidacion" && <LiquidacionView f={f} effective={effectiveAll} months={contractMonths} />}
          {subTab === "resultado" && <ResultadoView f={f} pnl={pnl} effective={effective} />}
          {subTab === "contrato" && <ContratoView f={f} effective={effectiveAll} months={contractMonths} />}
          {subTab === "metricas" && <MetricasView f={f} pnl={pnl} effective={effective} />}
          {subTab === "movimientos" && <MovimientosView f={f} />}
          {subTab === "criterio" && <CriterioView />}
        </>
      )}
    </div>
  );
}
