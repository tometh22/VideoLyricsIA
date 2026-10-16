// Liquidación por cuota, como dashboard: cuánto entró, cuánto costó, cuánto
// ganamos y cuánto le toca al socio — y a dónde fue cada dólar. El detalle
// por proveedor queda abajo, desplegable, para quien quiera auditarlo.
//
// Colores (dataviz): el reparto usa los 3 primeros slots categóricos
// validados (azul, naranja, aqua; CVD ΔE 9,2). Aqua queda bajo 3:1 sobre
// blanco, así que cada segmento lleva su etiqueta visible (alivio requerido).
// Los costos por categoría son magnitud → un solo tono (azul).
import { useMemo, useState } from "react";

import { addMonths, currentPeriod, liquidacion, liquidacionSnapshot, liquidacionTexto, monthLabel, monthsBetween, parseNumberAr } from "./finanzasModel";

// Fecha local (no UTC): un pago a las 22 h del último día no puede caer en el mes siguiente.
const today = () => {
  const d = new Date();
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}`;
};
import { BasisBadge } from "./ResultadoView";

const SERIES = { costos: "#2a78d6", socio: "#eb6834", genly: "#1baf7a" };
const BAR = "#2a78d6";

const usd = (v, d = 2) => (Number(v) || 0).toLocaleString("es-AR", { minimumFractionDigits: d, maximumFractionDigits: d });
const ars = (v) => `ARS ${(Number(v) || 0).toLocaleString("es-AR", { maximumFractionDigits: 0 })}`;
const pct = (a, b) => (b ? `${((a / b) * 100).toLocaleString("es-AR", { maximumFractionDigits: 1 })}%` : "—");

const MONTH_LONG = ["enero", "febrero", "marzo", "abril", "mayo", "junio", "julio", "agosto", "septiembre", "octubre", "noviembre", "diciembre"];
const monthLong = (p) => {
  const [y, m] = p.split("-").map(Number);
  return `${MONTH_LONG[m - 1]} ${y}`;
};

function Status({ tone, children }) {
  const tones = {
    ok: "bg-emerald-50 text-emerald-700 ring-emerald-200",
    warn: "bg-amber-50 text-amber-800 ring-amber-200",
    muted: "bg-slate-100 text-slate-600 ring-slate-200",
  };
  const icon = tone === "ok" ? "✓" : tone === "warn" ? "●" : "○";
  return (
    <span className={`inline-flex items-center gap-1 px-2 py-0.5 rounded-full text-[12px] font-semibold ring-1 ${tones[tone]}`}>
      <span aria-hidden="true">{icon}</span>{children}
    </span>
  );
}

function Tile({ label, value, sub, status, accent }) {
  return (
    <div className="fin-tile">
      <div className="flex items-start justify-between gap-2">
        <p className="fin-tile__label">{label}</p>
        {status}
      </div>
      <p className="fin-tile__value" style={accent ? { color: accent } : undefined}>{value}</p>
      {sub && <p className="fin-tile__sub">{sub}</p>}
    </div>
  );
}

function SplitBar({ parts, total }) {
  return (
    <div>
      <div className="fin-split" role="img" aria-label={parts.map((p) => `${p.label} ${pct(p.value, total)}`).join(", ")}>
        {parts.filter((p) => p.value > 0).map((p) => (
          <div
            key={p.key}
            className="fin-split__seg"
            style={{ width: `${(p.value / total) * 100}%`, background: p.color }}
            title={`${p.label}: US$ ${usd(p.value)} (${pct(p.value, total)})`}
          />
        ))}
      </div>
      <div className="mt-4 grid gap-3 sm:grid-cols-3">
        {parts.map((p) => (
          <div key={p.key} className="flex items-start gap-2.5">
            <span className="mt-1.5 h-3 w-3 rounded-sm shrink-0" style={{ background: p.color }} aria-hidden="true" />
            <div>
              <p className="text-[13px] text-slate-500">{p.label}</p>
              <p className="text-[18px] font-semibold text-slate-900 tabular-nums">US$ {usd(p.value)}</p>
              <p className="text-[12.5px] text-slate-500 tabular-nums">{pct(p.value, total)} de lo cobrado{p.note ? ` · ${p.note}` : ""}</p>
            </div>
          </div>
        ))}
      </div>
    </div>
  );
}

function CostBars({ lines, total, cobrado }) {
  const [open, setOpen] = useState(null);
  const sorted = [...lines].filter((l) => l.usd > 0 || l.rows.length).sort((a, b) => b.usd - a.usd);
  const max = Math.max(...sorted.map((l) => l.usd), 1);
  return (
    <div className="space-y-1">
      {sorted.map((l) => (
        <div key={l.key}>
          <button
            type="button"
            onClick={() => setOpen(open === l.key ? null : l.key)}
            className="fin-costrow"
            aria-expanded={open === l.key}
            title={`${l.label}: US$ ${usd(l.usd)} · ${pct(l.usd, total)} de los costos`}
          >
            <span className="fin-costrow__label">
              <span className="text-slate-400 mr-1.5">{open === l.key ? "▾" : "▸"}</span>
              <span>
                <span className="block font-semibold">{l.short || l.label}</span>
                {l.sub && <span className="block text-[12px] text-slate-500 font-normal">{l.sub}</span>}
              </span>
            </span>
            <span className="fin-costrow__track">
              <span className="fin-costrow__bar" style={{ width: `${Math.max(1.5, (l.usd / max) * 100)}%`, background: BAR }} />
            </span>
            <span className="fin-costrow__value tabular-nums">US$ {usd(l.usd)}</span>
            <span className="fin-costrow__pct tabular-nums">{pct(l.usd, cobrado)}</span>
          </button>
          {open === l.key && (
            <div className="fin-detail">
              {l.rows.map((e, i) => (
                <div key={`${e.id ?? "s"}-${i}`} className="fin-detail__row">
                  <span className="w-14 shrink-0 text-slate-400">{monthLabel(e.period)}</span>
                  <span className="flex-1 min-w-0 truncate text-slate-700">{[e.provider, e.concept].filter(Boolean).join(" · ")}</span>
                  <BasisBadge basis={e.basis} />
                  <span className="w-24 text-right tabular-nums text-slate-900">{usd(e.amount_usd)}</span>
                </div>
              ))}
            </div>
          )}
        </div>
      ))}
      <div className="flex justify-end gap-6 pt-2 pr-1 text-[12.5px] text-slate-500">
        <span>Total costos <b className="text-slate-900 tabular-nums">US$ {usd(total)}</b></span>
        <span>% = sobre lo cobrado</span>
      </div>
    </div>
  );
}

function RegistrarCobro({ l, contract, onCreate }) {
  const [open, setOpen] = useState(false);
  const [arsTxt, setArsTxt] = useState("");
  const [tcTxt, setTcTxt] = useState("");
  const [fecha, setFecha] = useState(today());
  const [saving, setSaving] = useState(false);
  const pendiente = Math.max(0, l.fee - (l.cobrado || 0));
  const arsValue = parseNumberAr(arsTxt) || 0;
  // El tipo de cambio es el de la factura (BNA del día). Si no se escribe, se
  // sugiere el que cierra contra lo pendiente; para un cobro parcial hay que escribirlo.
  const tc = parseNumberAr(tcTxt) || (arsValue > 0 && pendiente ? Math.round((arsValue / pendiente) * 100) / 100 : null);
  const usdValue = tc ? Math.round((arsValue / tc) * 100) / 100 : 0;
  const label = l.cobrada ? "Registrar el resto del cobro" : "Registrar cobro";
  if (!open) return <button type="button" className="fin-btn" onClick={() => setOpen(true)}>{label}</button>;
  const save = async () => {
    if (!tc || !usdValue) return;
    setSaving(true);
    try {
      await onCreate({
        period: fecha.slice(0, 7), section: "cobro", category: "Cobros", provider: contract.cliente || "Cliente",
        concept: `Cuota ${monthLabel(l.cuota)} (cobrada ${fecha})`, amount_usd: usdValue, basis: "captura",
        contract: contract.nombre, evidence: `Transferencia ${fecha}: ARS ${arsValue.toLocaleString("es-AR")} a ${tc}`,
        payload: { cuota: l.cuota, ars: arsValue, tipo_cambio: tc, fecha },
      });
      setOpen(false);
    } finally {
      setSaving(false);
    }
  };
  const input = "rounded-md border border-slate-300 px-2 py-1 text-[14px]";
  return (
    <div className="flex flex-wrap items-center gap-2 rounded-xl border border-violet-200 bg-violet-50 px-3 py-2">
      <span className="text-[13px] text-slate-700">ARS cobrados</span>
      <input className={`${input} w-32 text-right`} value={arsTxt} onChange={(e) => setArsTxt(e.target.value)} placeholder="9.517.000" aria-label="ARS cobrados" />
      <span className="text-[13px] text-slate-700">Tipo de cambio</span>
      <input className={`${input} w-24 text-right`} value={tcTxt} onChange={(e) => setTcTxt(e.target.value)} placeholder={tc ? String(tc) : "1.535"} aria-label="Tipo de cambio" />
      <input type="date" className={input} value={fecha} onChange={(e) => setFecha(e.target.value)} aria-label="Fecha de cobro" />
      <span className="text-[13px] text-slate-500">{usdValue ? `= US$ ${usd(usdValue)}${usdValue < pendiente - 0.01 ? " (parcial)" : ""}` : ""}</span>
      <button type="button" className="fin-btn" disabled={!usdValue || saving} onClick={save}>{saving ? "Guardando…" : "Guardar"}</button>
      <button type="button" className="text-[13px] text-slate-500" onClick={() => setOpen(false)}>Cancelar</button>
    </div>
  );
}

function RegistrarPago({ l, contract, onCreate }) {
  const [saving, setSaving] = useState(false);
  const save = async () => {
    if (!window.confirm(`¿Registrar el pago al socio de US$ ${usd(l.saldo)}? La liquidación de ${monthLabel(l.cuota)} queda congelada con estos números.`)) return;
    setSaving(true);
    try {
      const fecha = today();
      await onCreate({
        period: fecha.slice(0, 7), section: "pago_socio", category: "Socio", provider: "Socio",
        concept: `Pago cuota ${monthLabel(l.cuota)} (${fecha})`, amount_usd: l.saldo, basis: "captura",
        contract: contract.nombre,
        evidence: l.socioArs ? `ARS ${Math.round(l.saldo * l.tc).toLocaleString("es-AR")} al tipo de cambio de la cuota (${l.tc})` : "",
        payload: { cuota: l.cuota, fecha, ars: l.tc ? Math.round(l.saldo * l.tc) : null, snapshot: liquidacionSnapshot(l) },
      });
    } finally {
      setSaving(false);
    }
  };
  return (
    <button type="button" className="fin-btn" disabled={saving} onClick={save}>
      {saving ? "Guardando…" : `Registrar pago al socio (US$ ${usd(l.saldo)})`}
    </button>
  );
}

function cuotaStatus(l, cuota) {
  if (cuota > currentPeriod()) return { tone: "muted", label: "Futura" };
  if (!l.cobrada) return { tone: "warn", label: "Sin cobrar" };
  if (l.socio <= 0.01 && l.pagado <= 0.01) return { tone: "muted", label: "Sin ganancia para repartir" };
  if (l.saldo <= 0.01) return { tone: "ok", label: "Pagada al socio" };
  return { tone: "warn", label: "Cobrada · falta pagar" };
}

export default function LiquidacionView({ f, effective, months }) {
  const { contracts, params } = f;
  const [contractName, setContractName] = useState(null);
  const contract = contracts.find((c) => c.nombre === contractName) || contracts[contracts.length - 1];
  const cuotas = useMemo(() => {
    if (!contract) return [];
    return monthsBetween(contract.inicio, addMonths(contract.inicio, (Number(contract.meses) || 1) - 1));
  }, [contract]);
  const liqs = useMemo(
    () => Object.fromEntries(cuotas.map((m) => [m, liquidacion({
      entries: effective, contracts, params, cuota: m, deliveries: f.ops.deliveries || [], months, contract,
    })])),
    [cuotas, effective, contracts, params, f.ops, months, contract],
  );
  const defaultCuota = [...cuotas].reverse().find((m) => liqs[m]?.cobrada) || cuotas[0];
  const [selected, setSelected] = useState(null);
  const [copied, setCopied] = useState(false);
  const [showAcuerdo, setShowAcuerdo] = useState(false);

  if (!contract) {
    return <p className="text-caption text-gray-500">Cargá el contrato en "Contrato y reparto" para ver las liquidaciones.</p>;
  }
  const cuota = selected || defaultCuota;
  const l = liqs[cuota];
  const index = cuotas.indexOf(cuota) + 1;
  const st = cuotaStatus(l, cuota);
  const genly = Math.max(0, l.ganancia - l.socio);

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
    <div className="fin-dash space-y-5">
      {/* Encabezado de la cuota */}
      <div className="flex flex-wrap items-end justify-between gap-4">
        <div>
          <p className="text-[13px] font-semibold uppercase tracking-[0.12em] text-violet-600">Cuota {index} de {cuotas.length} · {contract.nombre}</p>
          <h3 className="mt-1 text-[28px] font-bold text-slate-900 capitalize">{monthLong(cuota)}</h3>
        </div>
        <div className="flex flex-wrap items-center gap-2">
          <Status tone={st.tone}>{st.label}</Status>
          {(!l.cobrada || l.cobroParcial) && cuota <= currentPeriod() && !l.frozen && <RegistrarCobro l={l} contract={contract} onCreate={f.createEntry} />}
          {l.cobrada && l.saldo > 0.01 && !l.frozen && <RegistrarPago l={l} contract={contract} onCreate={f.createEntry} />}
          {l.cobrada && <button type="button" onClick={copy} className="fin-btn">{copied ? "Copiada ✓" : "Copiar para mandar"}</button>}
        </div>
      </div>

      {contracts.length > 1 && (
        <div className="flex flex-wrap gap-2">
          {contracts.map((c) => (
            <button key={c.nombre} type="button" onClick={() => { setContractName(c.nombre); setSelected(null); }}
              className={`fin-cuota !flex-row !items-center gap-2 !py-2 ${c.nombre === contract.nombre ? "is-active" : ""}`}>
              <span className="text-[14px] font-semibold text-slate-900">{c.nombre}</span>
            </button>
          ))}
        </div>
      )}
      {l.cobroParcial && (
        <div className="rounded-2xl border border-amber-200 bg-amber-50 px-5 py-3 text-[14px] text-amber-900">
          <b>Cobro parcial.</b> Entraron US$ {usd(l.cobrado)} de los US$ {usd(l.fee)} de la cuota: la liquidación usa lo que entró.
        </div>
      )}
      {l.frozen && (
        <div className="rounded-2xl border border-emerald-200 bg-emerald-50 px-5 py-3 text-[14px] text-emerald-900">
          <b>Liquidación cerrada.</b> Se muestra tal como se pagó. Los costos que lleguen después se liquidan en la próxima cuota abierta.
          {Math.abs(l.drift || 0) > 0.01 && <> Ojo: renglones que estaban en este pago cambiaron después en US$ {usd(l.drift)}; cargá el ajuste en la cuota abierta.</>}
        </div>
      )}
      {l.cobrada && l.saldo < -0.01 && (
        <div className="rounded-2xl border border-sky-200 bg-sky-50 px-5 py-3 text-[14px] text-sky-900">
          <b>Pagado de más:</b> los pagos registrados superan en US$ {usd(-l.saldo)} lo que le corresponde. Revisá los pagos de esta cuota.
        </div>
      )}
      {!l.cobrada && (
        <div className="rounded-2xl border border-amber-200 bg-amber-50 px-5 py-4 text-[14px] leading-relaxed text-amber-900">
          <b>Cuota sin cobrar.</b> Todavía no hay liquidación: lo que ves son solo los gastos del mes cargados hasta hoy
          (servidores y herramientas). La IA y los operadores de este período se imputaron al cupo de las piezas que
          produjeron{cuota > contract.inicio ? " de cuotas anteriores, cuando el material llegó tarde" : ""}. La ganancia y la parte
          del socio se calculan cuando entra la cuota y se produce su cupo.
        </div>
      )}

      {/* Números principales */}
      <div className="grid gap-4 md:grid-cols-2 xl:grid-cols-4">
        <Tile
          label="Cobrado"
          value={`US$ ${usd(l.ingreso)}`}
          sub={l.cobrada ? `${ars(l.cobrado * l.tc)} · BNA ${l.tc}` : "Cuota del contrato · todavía no entró"}
          status={l.cobrada ? <Status tone="ok">Cobrada</Status> : <Status tone="muted">Proyección</Status>}
        />
        <Tile label={l.cobrada ? "Costos" : "Costos cargados hasta hoy"} value={`US$ ${usd(l.costos)}`} sub={l.cobrada ? `${pct(l.costos, l.ingreso)} de lo cobrado` : "Faltan los de producción de este cupo"} />
        <Tile
          label="Ganancia"
          value={l.cobrada ? `US$ ${usd(l.ganancia)}` : "—"}
          sub={l.cobrada ? `Margen ${pct(l.ganancia, l.ingreso)}` : "Se calcula al cobrar la cuota"}
          accent="#0f766e"
        />
        <Tile
          label={`Socio (${Math.round(l.pct * 100)}% de la ganancia)`}
          value={l.cobrada ? `US$ ${usd(l.socio)}` : "—"}
          sub={l.cobrada ? (l.socioArs ? ars(l.socioArs) : "—") : "Se calcula al cobrar la cuota"}
          accent="#c2410c"
          status={l.cobrada ? (l.saldo <= 0.01 ? <Status tone="ok">Pagado</Status> : <Status tone="warn">A pagar</Status>) : null}
        />
      </div>

      <div className="grid gap-4 xl:grid-cols-[1.1fr_1fr]">
        {/* A dónde va cada dólar */}
        {l.cobrada ? (
        <section className="fin-card">
          <h4 className="fin-card__title">A dónde va cada dólar de la cuota</h4>
          <p className="fin-card__sub">US$ {usd(l.ingreso)} cobrados</p>
          <div className="mt-5">
            <SplitBar
              total={l.ingreso || 1}
              parts={[
                { key: "costos", label: "Costos", value: l.costos, color: SERIES.costos },
                { key: "socio", label: "Socio", value: l.socio, color: SERIES.socio, note: l.socioArs ? ars(l.socioArs) : "" },
                { key: "genly", label: "Genly", value: genly, color: SERIES.genly },
              ]}
            />
          </div>
        </section>
        ) : (
          <section className="fin-card flex items-center justify-center text-center">
            <p className="max-w-sm text-[14px] text-slate-500">El reparto de cada dólar aparece cuando la cuota está cobrada y su cupo producido.</p>
          </section>
        )}

        {/* Costos por categoría */}
        <section className="fin-card">
          <h4 className="fin-card__title">Costos por categoría</h4>
          <p className="fin-card__sub">Tocá una fila para ver el detalle por proveedor y su comprobante</p>
          <div className="mt-4">
            <CostBars lines={l.lines} total={l.costos} cobrado={l.ingreso} />
          </div>
        </section>
      </div>

      {/* Las cuotas del contrato */}
      <section className="fin-card">
        <h4 className="fin-card__title">Cuotas del contrato</h4>
        <p className="fin-card__sub">Elegí una cuota para ver su liquidación</p>
        <div className="mt-4 grid gap-3 sm:grid-cols-3 xl:grid-cols-6">
          {cuotas.map((m, i) => {
            const q = liqs[m];
            const s = cuotaStatus(q, m);
            return (
              <button
                key={m}
                type="button"
                onClick={() => setSelected(m)}
                className={`fin-cuota ${m === cuota ? "is-active" : ""}`}
              >
                <span className="text-[12px] text-slate-500">Cuota {i + 1}</span>
                <span className="text-[16px] font-semibold text-slate-900 capitalize">{monthLabel(m)}</span>
                <span className="mt-1"><Status tone={s.tone}>{s.label}</Status></span>
                <span className="mt-2 text-[12.5px] text-slate-500">Socio</span>
                <span className="text-[15px] font-semibold tabular-nums text-slate-900">
                  {q.cobrada ? `US$ ${usd(q.socio, 0)}` : "—"}
                </span>
              </button>
            );
          })}
        </div>
      </section>

      {/* Acuerdo */}
      {contract.acuerdo_socio && (
        <section className="fin-card">
          <button type="button" onClick={() => setShowAcuerdo(!showAcuerdo)} className="flex w-full items-center justify-between text-left">
            <span className="fin-card__title">Acuerdo con el socio</span>
            <span className="text-[13px] text-violet-600">{showAcuerdo ? "Ocultar" : "Ver"}</span>
          </button>
          {showAcuerdo && <p className="mt-3 whitespace-pre-line text-[14px] leading-relaxed text-slate-700">{contract.acuerdo_socio}</p>}
        </section>
      )}
    </div>
  );
}
