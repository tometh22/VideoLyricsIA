// Hook único de la página de Costos.
//
// Por qué uno solo
// ----------------
// Antes había tres superficies de costo, cada una con su propio control de
// tiempo: `/admin/margin` contaba `since_days` hacia atrás desde HOY,
// `/admin/costs/series` iba por rango de fechas y `/admin/cost/unit-economics`
// por mes de facturación. Abiertas de a una parecían coherentes; puestas una
// al lado de la otra mostraban tres períodos distintos sin decirlo.
//
// Acá el período se elige UNA vez y las tres consultas se derivan de él. Si
// dicen números distintos —y van a decirlos— es porque miden cosas
// distintas, no porque miren meses distintos.
//
// El mes es la unidad, no el día: los proveedores facturan por mes
// calendario. El día existe como granularidad DENTRO del mes, para ver la
// forma del gasto, no para compararlo contra una factura.
import { useCallback, useEffect, useMemo, useState } from "react";

import { API, fetchJson } from "../../adminApi";
import { useAdmin } from "../../AdminContext";

/** Los últimos N meses, del más reciente al más viejo, como `YYYY-MM`. */
export function mesesDisponibles(n = 12, hoy = new Date()) {
  const out = [];
  for (let i = 0; i < n; i += 1) {
    const d = new Date(Date.UTC(hoy.getUTCFullYear(), hoy.getUTCMonth() - i, 1));
    out.push({
      id: `${d.getUTCFullYear()}-${String(d.getUTCMonth() + 1).padStart(2, "0")}`,
      label: d.toLocaleDateString("es", { month: "short", year: "numeric",
                                          timeZone: "UTC" }),
      enCurso: i === 0,
    });
  }
  return out;
}

/** Rango de días de un mes, recortado a AYER si el mes está en curso.
 *
 * El día de hoy todavía está acumulando: incluirlo dibuja siempre una caída
 * al final del gráfico que se lee como una mejora.
 */
export function diasDelMes(periodo, hoy = new Date()) {
  const [y, m] = periodo.split("-").map(Number);
  const primero = new Date(Date.UTC(y, m - 1, 1));
  const ultimo = new Date(Date.UTC(y, m, 0));
  const ayer = new Date(Date.UTC(hoy.getUTCFullYear(), hoy.getUTCMonth(),
                                 hoy.getUTCDate() - 1));
  const fin = ultimo > ayer ? ayer : ultimo;
  const iso = (d) => d.toISOString().slice(0, 10);
  // El día 1 del mes en curso no tiene ningún día cerrado todavía.
  if (fin < primero) {
    return { since: iso(primero), until: iso(primero), vacio: true,
             enCurso: true };
  }
  return { since: iso(primero), until: iso(fin), vacio: false,
           enCurso: ultimo > ayer };
}

// Rangos MÓVILES, que un selector por mes no puede expresar. Existen para
// una pregunta distinta: "¿lo que cambié ayer movió el gasto?". Cruzan el
// borde de mes a propósito — el 3 de septiembre "últimos 7 días" tiene que
// llegar hasta el 28 de agosto.
export const RANGOS_MOVILES = [
  { id: "7d", label: "Últimos 7 días", dias: 7 },
  { id: "30d", label: "Últimos 30 días", dias: 30 },
];

/** Días de un período, sea un mes `YYYY-MM` o un rango móvil `7d`/`30d`. */
export function rangoDelPeriodo(periodo, hoy = new Date()) {
  const movil = RANGOS_MOVILES.find((r) => r.id === periodo);
  if (!movil) return { ...diasDelMes(periodo, hoy), esMes: true };

  const iso = (d) => d.toISOString().slice(0, 10);
  const ayer = new Date(Date.UTC(hoy.getUTCFullYear(), hoy.getUTCMonth(),
                                 hoy.getUTCDate() - 1));
  const desde = new Date(ayer);
  desde.setUTCDate(desde.getUTCDate() - (movil.dias - 1));
  // `esMes: false` es lo que hace que el bloque 2 se calle en vez de
  // mentir: el costo por video se factura por mes calendario, así que
  // sobre una ventana móvil no tiene respuesta.
  return { since: iso(desde), until: iso(ayer), vacio: false,
           enCurso: true, esMes: false };
}

export default function useCostos() {
  const { flashError } = useAdmin();

  const meses = useMemo(() => mesesDisponibles(12), []);
  // Arranca en el mes PASADO, no en el actual: es el único que puede estar
  // completo, y el panel existe para cerrar el mes.
  const [periodo, setPeriodo] = useState(() => meses[1]?.id || meses[0].id);
  const [granularity, setGranularity] = useState("day");
  const [groupBy, setGroupBy] = useState("source");
  // Precio de venta por video. Default $13,50 = tarifa Universal vigente;
  // editable para modelar otros deals sin tocar código.
  const [precioPorVideo, setPrecioPorVideo] = useState(13.5);
  // El precio se APLICA con retraso. Sin esto, escribir "13.5" son cuatro
  // eventos de cambio = hasta doce requests, incluida la más lenta de la
  // página. La serie diaria ni siquiera usa el precio.
  const [precioAplicado, setPrecioAplicado] = useState(13.5);
  useEffect(() => {
    const t = setTimeout(() => setPrecioAplicado(precioPorVideo), 400);
    return () => clearTimeout(t);
  }, [precioPorVideo]);

  const [series, setSeries] = useState(null);
  const [unidad, setUnidad] = useState(null);
  const [atribucion, setAtribucion] = useState(null);
  const [loading, setLoading] = useState(true);
  const [colectando, setColectando] = useState(false);

  const { since, until, vacio, enCurso, esMes } = useMemo(
    () => rangoDelPeriodo(periodo), [periodo]);

  const cargar = useCallback(async () => {
    setLoading(true);
    try {
      // Las tres en paralelo y cada una con su propio catch: que GCP no
      // haya respondido no puede dejar la página entera en blanco.
      const [s, u, a] = await Promise.all([
        vacio
          ? Promise.resolve(null)
          : fetchJson(`${API}/admin/costs/series?since=${since}&until=${until}`
                      + `&granularity=${granularity}&group_by=${groupBy}`)
              .catch((err) => { flashError(`Costos facturados: ${err.message || err}`);
                                return null; }),
        // Sólo tiene sentido sobre un mes de facturación. Sobre una
        // ventana móvil no se pide: devolver el mes que contiene `until`
        // sería mostrar julio entero rotulado "últimos 7 días".
        esMes
          ? fetchJson(`${API}/admin/cost/unit-economics?period=${periodo}`
                      + `&price_per_video_usd=${precioAplicado}`)
              .catch((err) => { flashError(`Costo por video: ${err.message || err}`);
                                return null; })
          : Promise.resolve(null),
        fetchJson(`${API}/admin/margin?since=${since}&until=${until}`
                  + `&revenue_per_video_usd=${precioAplicado}`)
          .catch((err) => { flashError(`Atribución: ${err.message || err}`);
                            return null; }),
      ]);
      setSeries(s);
      setUnidad(u);
      setAtribucion(a);
    } finally {
      // Los tres catches de arriba cubren el caso de hoy, pero una cuarta
      // llamada sin `.catch` dejaría la página en spinner para siempre.
      setLoading(false);
    }
  }, [since, until, vacio, esMes, periodo, granularity, groupBy,
      precioAplicado, flashError]);

  useEffect(() => { cargar(); }, [cargar]);

  // Disparador manual. El camino normal es el cron diario; esto existe para
  // reparar a mano después de un outage sin esperar a la corrida de mañana.
  //
  // Dispara los DOS colectores, que llenan tablas distintas:
  //   /admin/costs/collect  → `cost_daily`     → bloque 1 (serie diaria)
  //   /admin/cost/refresh   → `cost_snapshots` → bloque 2 (costo por video)
  //
  // Con uno solo, apretar "Recolectar" llenaba la mitad de la página y
  // dejaba el costo por canción en "—" sin ningún control que lo arreglara.
  // Cada uno con su catch: `refresh` puede tardar minutos (Replicate
  // pagina) y que falle no debe tirar abajo lo que sí se recolectó.
  const colectar = useCallback(async () => {
    setColectando(true);
    try {
      const [diario, mensual] = await Promise.all([
        fetchJson(`${API}/admin/costs/collect?days=35`, { method: "POST" })
          .catch((err) => { flashError(`Serie diaria: ${err.message || err}`);
                            return null; }),
        // `refresh` es por mes; sobre una ventana móvil no aplica.
        esMes
          ? fetchJson(`${API}/admin/cost/refresh?period=${periodo}`,
                      { method: "POST" })
              .catch((err) => { flashError(`Costo del mes: ${err.message || err}`);
                                return null; })
          : Promise.resolve(null),
      ]);
      await cargar();
      return { diario, mensual };
    } finally {
      setColectando(false);
    }
  }, [cargar, periodo, esMes, flashError]);

  return {
    meses, rangosMoviles: RANGOS_MOVILES,
    periodo, setPeriodo, since, until, vacio, enCurso, esMes,
    granularity, setGranularity, groupBy, setGroupBy,
    precioPorVideo, setPrecioPorVideo,
    series, unidad, atribucion, loading,
    colectar, colectando, recargar: cargar,
  };
}
