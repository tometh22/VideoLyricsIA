// Cómo leer las finanzas: qué mira cada rol y qué vista responde su pregunta.
const LENTES = [
  {
    rol: "CFO",
    pregunta: "¿Los números son ciertos y comparables mes a mes?",
    mira: "Devengado por mes: ingreso = lo entregado × precio; costo = lo consumido. Cada número con su base (factura, API, estimado). Caja aparte: lo cobrado contra lo devengado, para saber si hay anticipo o saldo a cobrar.",
    vista: "Resultado + Movimientos",
  },
  {
    rol: "CEO",
    pregunta: "¿Cada contrato deja plata y cuánto cuesta producir una pieza?",
    mira: "El contrato completo (el de agosto se produjo de agosto a octubre), margen bruto, costo por pieza, horas del equipo y cuánto falta que mande el cliente.",
    vista: "Contrato y reparto + Métricas",
  },
  {
    rol: "Inversor",
    pregunta: "¿Escala? ¿De qué depende?",
    mira: "Ingreso recurrente contratado (fee mensual), margen bruto, concentración de clientes (hoy 100% Universal), resultado por hora del equipo y cumplimiento del cupo. Le importa la tendencia, no un mes aislado.",
    vista: "Métricas",
  },
];

export default function CriterioView() {
  return (
    <div className="space-y-5">
      <section className="glass rounded-card p-5 space-y-2 text-caption text-gray-300 leading-relaxed">
        <h3 className="text-base font-semibold text-white">El criterio de esta planilla</h3>
        <p><span className="text-white font-semibold">La base es el mes, por devengado.</span> El ingreso de un mes es lo que se entregó ese mes por el precio del contrato, aunque se haya cobrado antes o después. El costo de un mes es lo que se consumió ese mes, aunque la factura llegue después o abarque dos meses.</p>
        <p><span className="text-white font-semibold">El contrato agrupa los meses que lo produjeron.</span> Si el material llega tarde, el contrato de agosto se produce en septiembre y octubre: sus costos son los de esos meses.</p>
        <p><span className="text-white font-semibold">El reparto con el socio se calcula sobre el acumulado</span> del período elegido, no mes a mes: un mes con mucho costo y poca entrega compensa al siguiente.</p>
        <p><span className="text-white font-semibold">Escalones:</span> ingreso → <em>margen bruto</em> (después de infraestructura, IA y operadores) → <em>resultado operativo</em> (después de la estructura) → <em>base del reparto</em> (después del trabajo del equipo que se decida restar) → socio y Genly.</p>
        <p><span className="text-white font-semibold">Costos directos vs. estructura:</span> directo es lo que crece con el volumen (Railway, Google Cloud, R2, OpenAI, Replicate, Upwork). Estructura es lo que se paga aunque no haya clientes (Workspace, GitHub, Vercel, dominio). Con un solo cliente, todo termina restándose igual; la separación sirve para saber cuánto deja el contrato por sí solo.</p>
        <p><span className="text-white font-semibold">El desarrollo de producto no se le cobra al reparto:</span> construye un activo de Genly que sirve para otros clientes.</p>
      </section>
      <div className="grid gap-4 md:grid-cols-3">
        {LENTES.map((l) => (
          <section key={l.rol} className="glass rounded-card p-4 space-y-2">
            <p className="text-section uppercase tracking-[0.08em] text-brand-light font-semibold">{l.rol}</p>
            <p className="text-caption text-white">{l.pregunta}</p>
            <p className="text-caption text-gray-400 leading-relaxed">{l.mira}</p>
            <p className="text-label text-gray-500">Vista: {l.vista}</p>
          </section>
        ))}
      </div>
    </div>
  );
}
