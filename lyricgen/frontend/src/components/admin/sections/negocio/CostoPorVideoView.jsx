// Bloque "Costo por video" — `/admin/cost/unit-economics`.
//
// Por qué esta vista existe separada del gasto total
// --------------------------------------------------
// El total facturado es un hecho; el costo POR VIDEO es una división, y
// toda la dificultad está en el denominador. Medido en ago-2026 sobre las
// bases de producción y staging:
//
//   jobs creados          ~470   ← el más halagador, y el que citaba el doc viejo
//   jobs entregados        163   ← incluye barridos de CI (preflight_*, mx_*, …)
//   jobs de cliente         77   ← 47% de los anteriores
//   canciones de cliente   ~27   ← la unidad que se factura
//
// Entre el primero y el último hay ~17x. Elegir mal no da un número
// "aproximado": da uno que parece rentable cuando no lo es. Por eso los
// cuatro se muestran juntos, con el honesto arriba y los otros al lado —
// esconder los malos haría que alguien los recalcule por su cuenta.
//
// Una canción arrastra ~2,87 jobs (variantes, re-renders, ediciones). La
// factura del proveedor no distingue quién la causó, así que el gasto de CI
// y de I+D se reparte entre los videos vendidos: es lo que efectivamente
// pasa en el estado de resultados de fin de mes.
import { fmtMoneyOrDash as dinero } from "../../adminApi";
import EmptyState from "../../primitives/EmptyState";
import KpiCard from "../../primitives/KpiCard";


// Los cuatro denominadores, del más halagador al más honesto. El orden es
// el argumento: se lee de arriba a abajo y el número baja.
const DENOMINADORES = [
  {
    campo: "videos_created",
    label: "Jobs creados",
    ayuda: "Todo lo que arrancó, haya salido o no. No es un denominador: "
      + "un preview descartado no es un video entregado.",
  },
  {
    campo: "videos_delivered",
    label: "Jobs entregados",
    ayuda: "Los que llegaron a done o pending_review — incluidos los "
      + "barridos automáticos de CI, que no le facturan a nadie.",
  },
  {
    campo: "delivered_client_jobs",
    label: "Jobs de cliente",
    ayuda: "Entregados, sin CI ni cuentas del equipo. Sigue contando "
      + "dos veces la misma canción si tuvo variante o re-render.",
  },
  {
    campo: "delivered_client_songs",
    label: "Canciones de cliente",
    ayuda: "La unidad que se factura. Es el denominador correcto.",
    honesto: true,
  },
];

function Denominador({ item, valor, referencia }) {
  // Cuánto infla cada denominador respecto del honesto. Es el dato que
  // convierte la tabla en un argumento en vez de una lista.
  const infla = referencia > 0 && valor > 0 ? valor / referencia : null;
  return (
    <div
      className={`rounded-card p-3 ring-1 ${
        item.honesto
          ? "bg-brand/[0.07] ring-brand/30"
          : "bg-surface-3/30 ring-white/[0.04]"
      }`}
    >
      <div className="flex items-baseline justify-between gap-2">
        <p className="text-section uppercase tracking-wider text-gray-500">
          {item.label}
        </p>
        {item.honesto ? (
          <span className="text-label text-brand-light font-semibold uppercase">
            el correcto
          </span>
        ) : infla && infla > 1.05 ? (
          <span className="text-label text-amber-400 tabular-nums">
            ×{infla.toFixed(1)}
          </span>
        ) : null}
      </div>
      <p className="text-base font-bold tabular-nums text-white mt-0.5">
        {valor ?? "—"}
      </p>
      <p className="text-label text-gray-500 leading-snug mt-1">{item.ayuda}</p>
    </div>
  );
}

export default function CostoPorVideoView({ data, loading, esMes = true }) {
  // Sobre una ventana móvil no hay respuesta, y decir "—" haría pensar que
  // falta un dato. Los proveedores facturan por mes calendario: el costo
  // por video de "los últimos 7 días" no existe, no está faltando.
  if (!esMes) {
    return (
      <EmptyState
        title="El costo por video se calcula por mes"
        message="Los proveedores facturan por mes calendario. Elegí un mes arriba para ver el costo por canción entregada."
      />
    );
  }

  if (loading) {
    return (
      <div className="flex items-center justify-center py-12">
        <div className="w-6 h-6 border-2 border-brand border-t-transparent rounded-full animate-spin" />
      </div>
    );
  }
  if (!data) {
    return (
      <EmptyState
        title="Sin datos de costo por video"
        message="No hay ningún snapshot de costo para este período."
      />
    );
  }

  const canciones = data.delivered_client_songs || 0;
  const porCancion = data.cost_per_client_song;
  const esPiso = data.cost_per_delivered_is_floor;
  // Un entorno contado contra una factura que cubre los dos entornos deja
  // el numerador entero sobre la mitad de los videos: sobre-estima.
  const unSoloEntorno = data.counted_environments === 1;

  const margen = porCancion !== null && porCancion !== undefined
    ? data.price_per_video_usd - porCancion
    : null;

  return (
    <div className="space-y-4">
      {/* El número, y de inmediato con cuánta confianza */}
      <div className="grid grid-cols-2 lg:grid-cols-4 gap-4">
        <KpiCard
          value={dinero(porCancion)}
          label={esPiso ? "Costo / canción (piso)" : "Costo / canción entregada"}
          tone="brand"
          hint={`${dinero(data.real_cost_usd)} ÷ ${canciones} canciones`}
        />
        <KpiCard
          value={dinero(data.real_cost_usd)}
          label="Facturado en el período"
          hint={data.cost_complete
            ? "todas las fuentes respondieron"
            : `faltan: ${(data.missing_sources || []).join(", ") || "—"}`}
          tone={data.cost_complete ? "default" : "warn"}
        />
        <KpiCard
          value={dinero(data.price_per_video_usd)}
          label="Precio / video"
          hint="editable arriba · se usa para el margen"
        />
        <KpiCard
          value={dinero(margen)}
          label="Margen / canción"
          tone={margen !== null && margen < 0 ? "danger" : "accent"}
          hint={esPiso ? "sobre un costo PISO: es optimista" : "precio − costo"}
        />
      </div>

      {/* Avisos que cambian cómo se lee el número de arriba. Van antes del
          detalle a propósito: un número incompleto no se puede mirar sin
          saber que lo está. */}
      {esPiso && (
        <div className="rounded-card bg-amber-400/[0.08] ring-1 ring-amber-400/25 p-3">
          <p className="text-caption text-amber-200">
            El costo del período está <b>incompleto</b>: lo que ves es un{" "}
            <b>piso</b>, no el costo. El margen sale por lo tanto optimista.
          </p>
        </div>
      )}
      {unSoloEntorno && (
        <div className="rounded-card bg-amber-400/[0.08] ring-1 ring-amber-400/25 p-3">
          <p className="text-caption text-amber-200">
            Se contó <b>un solo entorno</b> y la factura cubre los dos. El
            costo por canción queda <b>sobre-estimado</b> — falta
            configurar <code className="font-mono">PEER_DATABASE_URL</code>.
          </p>
        </div>
      )}

      {/* Los cuatro denominadores */}
      <div className="glass-elevated rounded-card p-5">
        <div className="flex items-center justify-between mb-1">
          <h3 className="text-sm font-semibold">Por cuántos se divide</h3>
          <span className="text-label text-gray-500">4 denominadores</span>
        </div>
        <p className="text-label text-gray-500 mb-4 leading-relaxed">
          El mismo período dividido de cuatro formas. La diferencia entre el
          primero y el último no es ruido: es el margen.
        </p>
        <div className="grid grid-cols-1 sm:grid-cols-2 lg:grid-cols-4 gap-3">
          {DENOMINADORES.map((item) => (
            <Denominador
              key={item.campo}
              item={item}
              valor={data[item.campo]}
              referencia={canciones}
            />
          ))}
        </div>
      </div>
    </div>
  );
}
