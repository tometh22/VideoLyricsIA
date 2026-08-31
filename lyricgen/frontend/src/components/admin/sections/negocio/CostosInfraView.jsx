// Costo de infraestructura, diario y filtrable.
//
// Muestra lo que los proveedores COBRAN (billing_sources → cost_daily), no
// el costo modelado que muestra "Costos y márgenes" (ai_provenance × tabla
// de tarifas). Los dos conviven: el modelado atribuye por job y por tenant,
// que ninguna factura puede hacer; éste es la verdad pero no se desagrega.
//
// LA REGLA VISUAL DE ESTA PANTALLA
// Un día que el proveedor no contestó se dibuja idéntico a un día barato.
// Por eso la cobertura no es un detalle al pie: cuando faltan celdas, el
// total se rotula como PISO y el banner dice exactamente qué falta. Si esa
// distinción no se ve, el panel es peor que no tener panel — que es
// literalmente el requisito con el que se pidió.
import { useMemo, useState } from "react";

import {
  Area, AreaChart, Bar, BarChart, CartesianGrid, Cell, Legend,
  ResponsiveContainer, Tooltip, XAxis, YAxis,
} from "recharts";

import { fmtMoney } from "../../adminApi";
import DataTable from "../../primitives/DataTable";
import EmptyState from "../../primitives/EmptyState";
import FilterBar from "../../primitives/FilterBar";
import KpiCard from "../../primitives/KpiCard";
import SectionHeader from "../../layout/SectionHeader";
import { RANGOS } from "./useCostosInfra";

// Paleta de series. Violeta de marca + teal en su paso oscuro (accent.dark,
// NO accent.DEFAULT: #14C8A8 cae fuera de la banda de luminosidad para
// fondo oscuro y se lava). El par pasa los chequeos de separación CVD
// (ΔE 25,5 deuteranopía) contra la superficie #12121A.
const COLORES = ["#6D4AFF", "#0FA88B", "#FBBF24", "#F87171", "#60A5FA", "#C084FC"];

const ETIQUETA_FUENTE = {
  gcp: "Google Vertex", railway: "Railway", r2: "Cloudflare R2",
  openai: "OpenAI", replicate: "Replicate", fixed: "Suscripciones",
};
const ETIQUETA_COMPORTAMIENTO = {
  fijo: "Fijo (capacidad)", variable: "Variable (por video)",
  stock: "Stock (acumulado)", sin_clasificar: "Sin clasificar",
};

function nombre(grupo, groupBy) {
  if (groupBy === "source") return ETIQUETA_FUENTE[grupo] || grupo;
  if (groupBy === "behavior") return ETIQUETA_COMPORTAMIENTO[grupo] || grupo;
  return grupo;
}

function TooltipCosto({ active, payload, label, groupBy }) {
  if (!active || !payload?.length) return null;
  const total = payload.reduce((a, p) => a + (Number(p.value) || 0), 0);
  return (
    <div className="rounded-xl bg-[#15151c]/95 ring-1 ring-white/10 px-3 py-2 shadow-xl backdrop-blur">
      <p className="text-label text-gray-400 mb-1">{label}</p>
      {payload.filter((p) => Number(p.value) > 0).map((p) => (
        <p key={p.dataKey} className="text-caption tabular-nums flex items-center gap-1.5">
          <span className="w-2 h-2 rounded-full inline-block" style={{ background: p.color }} />
          <span className="text-gray-300">{nombre(p.dataKey, groupBy)}:</span>
          <span className="text-white font-semibold">{fmtMoney(p.value)}</span>
        </p>
      ))}
      <p className="text-caption tabular-nums text-white font-semibold mt-1 pt-1 border-t border-white/10">
        Total {fmtMoney(total)}
      </p>
    </div>
  );
}

/** Banner de cobertura. Deliberadamente imposible de confundir con un aviso
 *  menor: mientras falten celdas, el número grande es un piso. */
function BannerCobertura({ cobertura, onColectar, colectando }) {
  if (!cobertura || cobertura.complete) return null;
  const faltan = cobertura.missing_total ?? cobertura.missing?.length ?? 0;
  const porFuente = (cobertura.missing || []).reduce((acc, m) => {
    acc[m.source] = (acc[m.source] || 0) + 1;
    return acc;
  }, {});
  return (
    <div className="rounded-card bg-[#F5A524]/[0.07] ring-1 ring-[#F5A524]/25 px-5 py-4 mb-5">
      <div className="flex items-start justify-between gap-4 flex-wrap">
        <div>
          <p className="text-caption font-semibold text-[#F5A524]">
            El total es un piso, no el total
          </p>
          <p className="text-caption text-gray-400 mt-1">
            Faltan <span className="text-gray-200 tabular-nums">{faltan}</span> de{" "}
            <span className="tabular-nums">{cobertura.expected_cells}</span> celdas
            (día × fuente). Un día que el proveedor no contestó no se puede
            distinguir de un día barato, así que no se cuenta como cero.
          </p>
          <p className="text-caption text-gray-500 mt-1.5">
            {Object.entries(porFuente)
              .sort((a, b) => b[1] - a[1])
              .map(([f, n]) => `${ETIQUETA_FUENTE[f] || f}: ${n} día${n > 1 ? "s" : ""}`)
              .join(" · ")}
          </p>
        </div>
        <button
          onClick={onColectar}
          disabled={colectando}
          className="shrink-0 text-caption px-3 py-1.5 rounded-lg bg-brand hover:bg-brand-dark disabled:opacity-40 transition-colors"
        >
          {colectando ? "Recolectando…" : "Recolectar ahora"}
        </button>
      </div>
    </div>
  );
}

export default function CostosInfraView({
  data, loading, rango, setRango, granularity, setGranularity,
  groupBy, setGroupBy, colectar, colectando, since, until,
}) {
  const [verTabla, setVerTabla] = useState(false);

  const grupos = useMemo(
    () => Object.keys(data?.by_group || {}), [data]);

  // Recharts quiere un objeto plano por bucket con una clave por serie.
  const datosGrafico = useMemo(() => (data?.series || []).map((b) => ({
    bucket: b.bucket,
    ...Object.fromEntries(grupos.map((g) => [g, b.by[g] ?? 0])),
  })), [data, grupos]);

  const incompleto = data && !data.coverage?.complete;
  const diasConDato = data?.series?.length || 0;
  const promedioDia = diasConDato ? (data.total_usd / diasConDato) : null;

  if (!loading && !data) {
    return <EmptyState title="Sin datos de costo"
                       hint="Todavía no se recolectó nada para este rango." />;
  }

  return (
    <div>
      <SectionHeader
        title="Costo de infraestructura"
        subtitle="Lo que cobran los proveedores, por día. Distinto del costo modelado de “Costos y márgenes”."
        right={
          <button
            onClick={colectar}
            disabled={colectando}
            className="text-caption px-3 py-1.5 rounded-lg glass hover:bg-white/[0.06] disabled:opacity-40 transition-colors"
          >
            {colectando ? "Recolectando…" : "Recolectar"}
          </button>
        }
      />

      <FilterBar>
        <FilterBar.Chips
          label="Rango" value={rango} onChange={setRango}
          options={RANGOS.map((r) => ({ id: r.id, label: r.label }))}
        />
        <FilterBar.Select
          label="Grano" value={granularity} onChange={setGranularity}
          options={[
            { id: "day", label: "Día" },
            { id: "week", label: "Semana" },
            { id: "month", label: "Mes" },
          ]}
        />
        <FilterBar.Select
          label="Abrir por" value={groupBy} onChange={setGroupBy}
          options={[
            { id: "source", label: "Proveedor" },
            { id: "behavior", label: "Fijo / variable" },
            { id: "sku", label: "SKU" },
          ]}
        />
      </FilterBar>

      <div className="mt-5">
        <BannerCobertura cobertura={data?.coverage} onColectar={colectar}
                         colectando={colectando} />

        <div className="grid grid-cols-2 lg:grid-cols-4 gap-4 mb-6">
          <KpiCard
            loading={loading}
            label={incompleto ? "Total (piso)" : "Total del rango"}
            value={data ? fmtMoney(data.total_usd) : "—"}
            hint={`${since} → ${until}`}
            tone={incompleto ? "warn" : "brand"}
          />
          <KpiCard
            loading={loading}
            label="Promedio por día"
            value={promedioDia != null ? fmtMoney(promedioDia) : "—"}
            hint={`${diasConDato} bucket${diasConDato === 1 ? "" : "s"} con dato`}
          />
          <KpiCard
            loading={loading}
            label="Sale de una factura"
            value={data ? fmtMoney(data.invoiced_usd) : "—"}
            hint="importe que cobró el proveedor"
          />
          <KpiCard
            loading={loading}
            label="Modelo propio"
            value={data ? fmtMoney(data.estimated_usd) : "—"}
            hint={
              data?.estimated_share != null
                ? `${Math.round(data.estimated_share * 100)}% del total · métrica valorizada por nosotros`
                : "Railway, R2 y Replicate no exponen importe por API"
            }
            tone={data?.estimated_share > 0.5 ? "warn" : "default"}
          />
        </div>

        <div className="glass rounded-card p-5 mb-6">
          <div className="flex items-baseline justify-between mb-4">
            <h3 className="text-sm font-semibold text-white">Evolución</h3>
            <button onClick={() => setVerTabla((v) => !v)}
                    className="text-label text-gray-500 hover:text-white underline">
              {verTabla ? "Ver gráfico" : "Ver como tabla"}
            </button>
          </div>

          {verTabla ? (
            <DataTable
              dense
              rowKey={(r) => r.bucket}
              rows={datosGrafico}
              columns={[
                { key: "bucket", header: granularity === "month" ? "Mes" : "Desde" },
                ...grupos.map((g) => ({
                  key: g, header: nombre(g, groupBy), align: "right",
                  render: (r) => <span className="tabular-nums">{fmtMoney(r[g])}</span>,
                })),
                {
                  key: "_t", header: "Total", align: "right",
                  render: (r) => (
                    <span className="tabular-nums font-semibold">
                      {fmtMoney(grupos.reduce((a, g) => a + (r[g] || 0), 0))}
                    </span>
                  ),
                },
              ]}
            />
          ) : datosGrafico.length < 2 ? (
            <div className="h-[220px] grid place-items-center text-label text-gray-600">
              hacen falta al menos 2 buckets para dibujar una evolución
            </div>
          ) : (
            <div style={{ height: 260 }} className="w-full">
              <ResponsiveContainer width="100%" height="100%">
                {/* Barras apiladas y no área: el costo por día es una
                    magnitud discreta, no una señal continua, y apilarlas
                    deja leer el total y la composición en el mismo gesto. */}
                <BarChart data={datosGrafico} margin={{ top: 8, right: 8, bottom: 0, left: -18 }}>
                  <CartesianGrid stroke="rgba(255,255,255,0.05)" vertical={false} />
                  <XAxis dataKey="bucket" tick={{ fill: "#6B7280", fontSize: 10 }}
                         axisLine={false} tickLine={false} minTickGap={24} />
                  <YAxis tick={{ fill: "#6B7280", fontSize: 10 }} axisLine={false}
                         tickLine={false} width={52}
                         tickFormatter={(v) => `$${v >= 100 ? Math.round(v) : v.toFixed(1)}`} />
                  <Tooltip cursor={{ fill: "rgba(255,255,255,0.04)" }}
                           content={<TooltipCosto groupBy={groupBy} />} />
                  <Legend
                    wrapperStyle={{ fontSize: 11, paddingTop: 8 }}
                    formatter={(v) => (
                      <span style={{ color: "#9CA3AF" }}>{nombre(v, groupBy)}</span>
                    )}
                  />
                  {grupos.map((g, i) => (
                    <Bar key={g} dataKey={g} stackId="c"
                         fill={COLORES[i % COLORES.length]}
                         radius={i === grupos.length - 1 ? [4, 4, 0, 0] : 0} />
                  ))}
                </BarChart>
              </ResponsiveContainer>
            </div>
          )}
        </div>

        <div className="glass rounded-card p-5">
          <h3 className="text-sm font-semibold text-white mb-1">
            {groupBy === "source" ? "Por proveedor"
              : groupBy === "behavior" ? "Por comportamiento de costo" : "Por SKU"}
          </h3>
          {groupBy === "behavior" && (
            <p className="text-label text-gray-500 mb-4">
              El <span className="text-gray-300">fijo</span> es capacidad que se paga
              haya o no videos; el <span className="text-gray-300">variable</span> es lo
              único que crece con el volumen; el <span className="text-gray-300">stock</span> es
              storage acumulado que no baja solo. Sin separarlos, el “costo por video”
              baja al subir el volumen y hace parecer una mejora lo que recorta la
              ganancia absoluta.
            </p>
          )}
          <DataTable
            loading={loading}
            rowKey={(r) => r.grupo}
            rows={grupos.map((g) => ({
              grupo: g, monto: data.by_group[g],
              share: data.total_usd ? data.by_group[g] / data.total_usd : 0,
            }))}
            empty={<EmptyState title="Sin costo en el rango" />}
            columns={[
              {
                key: "grupo", header: "Concepto",
                render: (r) => (
                  <span className="flex items-center gap-2">
                    <span className="w-2.5 h-2.5 rounded-sm shrink-0"
                          style={{ background: COLORES[grupos.indexOf(r.grupo) % COLORES.length] }} />
                    {nombre(r.grupo, groupBy)}
                  </span>
                ),
              },
              {
                key: "share", header: "", align: "left",
                render: (r) => (
                  <div className="h-1.5 rounded-full bg-white/[0.06] overflow-hidden w-28">
                    <div className="h-full rounded-full"
                         style={{ width: `${Math.max(2, r.share * 100)}%`,
                                  background: COLORES[grupos.indexOf(r.grupo) % COLORES.length] }} />
                  </div>
                ),
              },
              {
                key: "pct", header: "%", align: "right",
                render: (r) => (
                  <span className="tabular-nums text-gray-400">
                    {(r.share * 100).toFixed(1)}%
                  </span>
                ),
              },
              {
                key: "monto", header: "Costo", align: "right",
                render: (r) => (
                  <span className="tabular-nums font-semibold">{fmtMoney(r.monto)}</span>
                ),
              },
            ]}
          />
        </div>

        {data?.openai_line_item_filter?.length > 0 && (
          <p className="text-label text-gray-600 mt-4">
            OpenAI filtrado a <code className="text-gray-500">
              {data.openai_line_item_filter.join(", ")}
            </code> — la organización está compartida con otros proyectos.
            Se cambia con <code className="text-gray-500">OPENAI_COST_LINE_ITEMS</code> y
            reinterpreta la historia ya guardada, sin re-colectar.
          </p>
        )}
      </div>
    </div>
  );
}
