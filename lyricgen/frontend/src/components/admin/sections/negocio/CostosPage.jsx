// Página de Costos — UNA pantalla, tres bloques, un solo período.
//
// Qué reemplaza
// -------------
// Había tres superficies de costo (Gestión → "Costos y márgenes", Gestión →
// "Costo de infra", Insights → "Margen") con cuatro cálculos distintos del
// mismo concepto, y ninguna decía cuál de las otras había que creer. Peor:
// cada una tenía su propio control de tiempo, así que abiertas de a una
// parecían coherentes y puestas juntas mostraban meses distintos.
//
// El orden de los bloques es el argumento, y se lee de arriba a abajo:
//
//   1. Lo que pagamos      — la factura. Un hecho.
//   2. Costo por video     — esa factura dividida. Toda la dificultad está
//                            en el denominador, así que se muestran los cuatro.
//   3. A quién se le fue   — una ESTIMACIÓN. Es la única que sabe repartir
//                            por tenant, y no cuadra con (1) ni debe.
//
// Los tres números no coinciden entre sí. Eso no es un bug: es el motivo
// por el que están juntos y rotulados, en vez de en tres pantallas donde
// cada uno parecía el definitivo.
import { fmtMoney } from "../../adminApi";
import FilterBar from "../../primitives/FilterBar";
import SectionHeader from "../../layout/SectionHeader";
import CostoAtribuidoView from "./CostoAtribuidoView";
import CostoPorVideoView from "./CostoPorVideoView";
import CostosInfraView from "./CostosInfraView";
import useCostos from "./useCostos";

function Bloque({ n, titulo, bajada, children }) {
  return (
    <section className="space-y-3">
      <div className="flex items-baseline gap-3">
        <span className="shrink-0 w-6 h-6 rounded-full bg-brand/20 ring-1 ring-brand/40 text-brand-light text-label font-bold flex items-center justify-center tabular-nums">
          {n}
        </span>
        <div>
          <h2 className="text-base font-semibold text-white">{titulo}</h2>
          <p className="text-caption text-gray-500 leading-snug">{bajada}</p>
        </div>
      </div>
      {children}
    </section>
  );
}

export default function CostosPage() {
  const c = useCostos();
  const facturado = c.series?.total_usd ?? 0;

  return (
    <div>
      <SectionHeader
        title="Costos"
        subtitle="Lo que pagamos, dividido por los videos que salieron, y repartido por cliente."
        right={
          <button
            onClick={c.colectar}
            disabled={c.colectando}
            className="text-caption px-3 py-1.5 rounded-lg glass hover:bg-white/[0.06] disabled:opacity-40 transition-colors"
          >
            {c.colectando ? "Recolectando…" : "Recolectar"}
          </button>
        }
      />

      {/* UN solo control de tiempo para los tres bloques. El mes es la
          unidad porque es la unidad en la que facturan los proveedores. */}
      <FilterBar>
        <FilterBar.Select
          label="Período" value={c.periodo} onChange={c.setPeriodo}
          options={[
            ...c.rangosMoviles,
            ...c.meses.map((m) => ({
              id: m.id,
              label: m.enCurso ? `${m.label} (en curso)` : m.label,
            })),
          ]}
        />
        <div className="flex items-center gap-2">
          <span className="text-section uppercase text-gray-500">Precio / video</span>
          <span className="text-caption text-gray-400">USD</span>
          <input
            type="number" step="0.5" min="0"
            value={c.precioPorVideo}
            onChange={(e) => c.setPrecioPorVideo(Math.max(0, Number(e.target.value) || 0))}
            className="w-20 bg-surface-3/40 ring-1 ring-white/[0.06] focus:ring-brand/40 focus:outline-none rounded-md px-2 py-1 text-caption text-white text-right"
          />
        </div>
        <span className="ml-auto text-label text-gray-500 tabular-nums">
          {c.vacio ? "sin días cerrados" : `${c.since} → ${c.until}`}
        </span>
      </FilterBar>

      {/* El mes en curso no está cerrado. Decirlo una vez arriba evita
          repetir el mismo asterisco en los tres bloques. */}
      {c.enCurso && (
        <div className="mt-4 rounded-card bg-surface-3/30 ring-1 ring-white/[0.06] p-3">
          <p className="text-caption text-gray-400">
            {c.esMes
              ? "Mes en curso: llega hasta ayer y va a seguir subiendo. Para comparar contra una factura, elegí un mes cerrado."
              : "Ventana móvil hasta ayer. Sirve para ver si algo movió el gasto; para comparar contra una factura, elegí un mes."}
          </p>
        </div>
      )}

      <div className="mt-6 space-y-10">
        <Bloque
          n={1}
          titulo="Lo que pagamos"
          bajada="Lo que cobran los proveedores. Es la factura, no una estimación."
        >
          <CostosInfraView
            embebido
            data={c.series} loading={c.loading}
            granularity={c.granularity} setGranularity={c.setGranularity}
            groupBy={c.groupBy} setGroupBy={c.setGroupBy}
            colectar={c.colectar} colectando={c.colectando}
            since={c.since} until={c.until}
          />
        </Bloque>

        <Bloque
          n={2}
          titulo="Costo por video"
          bajada={c.esMes
            ? `Ese total (${fmtMoney(facturado)}) dividido. La pregunta es por cuántos.`
            : "Se factura por mes calendario."}
        >
          <CostoPorVideoView data={c.unidad} loading={c.loading} esMes={c.esMes} />
        </Bloque>

        <Bloque
          n={3}
          titulo="A quién se le fue"
          bajada="Reparto por cliente y por modelo. Estimado: la factura no dice de quién fue el gasto."
        >
          <CostoAtribuidoView
            data={c.atribucion} loading={c.loading} facturado={facturado}
          />
          {/* La otra superficie de costo que queda, y por qué no está acá. */}
          <p className="text-label text-gray-500 leading-relaxed">
            El revenue y el margen por cuenta viven en <b>Insights → Margen</b>
            {" "}(super-admin). No están en esta tabla porque usan otro
            denominador —videos <i>aprobados</i>, por <code className="font-mono">approved_at</code>— y
            el precio fijo del plan de cada tenant, no el precio editable de
            arriba. En la misma fila invitarían a restar peras de manzanas.
          </p>
        </Bloque>
      </div>
    </div>
  );
}
