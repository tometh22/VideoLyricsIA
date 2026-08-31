// Hook del panel de costos de infraestructura (diario).
//
// Distinto de `useNegocio`, que alimenta "Costos y márgenes" con el costo
// MODELADO (`/admin/margin`: filas de ai_provenance × tabla de tarifas).
// Este consume `/admin/costs/series`, que es el costo que los proveedores
// realmente cobran, con grano diario.
//
// Los dos conviven a propósito: el modelado es el único que sabe atribuir
// gasto por job y por tenant (una factura no puede decir a qué cliente
// pertenece un dólar); el facturado es la verdad pero no se desagrega.
//
// COBERTURA
// `series.coverage.complete === false` significa que faltan celdas
// (día, fuente) y el total es un PISO, no el total. La vista tiene que
// decirlo de forma que no se pueda pasar por alto: un día que el proveedor
// no contestó se dibuja idéntico a un día barato, y es la falla que este
// panel existe para no tener.
import { useCallback, useEffect, useMemo, useState } from "react";

import { API, fetchJson } from "../../adminApi";
import { useAdmin } from "../../AdminContext";

// Rangos preconfigurados. El "mes pasado" existe porque es el único que
// puede estar completo: el mes en curso siempre tiene días sin cerrar.
export const RANGOS = [
  { id: "7d", label: "7 días", dias: 7 },
  { id: "30d", label: "30 días", dias: 30 },
  { id: "90d", label: "90 días", dias: 90 },
  { id: "mes_actual", label: "Mes actual" },
  { id: "mes_pasado", label: "Mes pasado" },
];

function iso(d) {
  return d.toISOString().slice(0, 10);
}

export function rangoAFechas(rangoId) {
  const hoy = new Date();
  // El día de hoy nunca entra: todavía está acumulando y siempre se vería
  // como una caída al final del gráfico.
  const ayer = new Date(Date.UTC(hoy.getUTCFullYear(), hoy.getUTCMonth(),
                                 hoy.getUTCDate() - 1));
  const cfg = RANGOS.find((r) => r.id === rangoId) || RANGOS[1];

  if (cfg.dias) {
    const desde = new Date(ayer);
    desde.setUTCDate(desde.getUTCDate() - (cfg.dias - 1));
    return { since: iso(desde), until: iso(ayer) };
  }
  if (rangoId === "mes_actual") {
    const primero = new Date(Date.UTC(ayer.getUTCFullYear(), ayer.getUTCMonth(), 1));
    return { since: iso(primero), until: iso(ayer) };
  }
  // mes pasado, completo
  const finMesPasado = new Date(Date.UTC(ayer.getUTCFullYear(), ayer.getUTCMonth(), 0));
  const iniMesPasado = new Date(Date.UTC(finMesPasado.getUTCFullYear(),
                                         finMesPasado.getUTCMonth(), 1));
  return { since: iso(iniMesPasado), until: iso(finMesPasado) };
}

export default function useCostosInfra() {
  const { flashError } = useAdmin();

  const [rango, setRango] = useState("30d");
  const [granularity, setGranularity] = useState("day");
  const [groupBy, setGroupBy] = useState("source");
  const [data, setData] = useState(null);
  const [loading, setLoading] = useState(true);
  const [colectando, setColectando] = useState(false);

  const { since, until } = useMemo(() => rangoAFechas(rango), [rango]);

  const cargar = useCallback(async () => {
    setLoading(true);
    try {
      const url = `${API}/admin/costs/series?since=${since}&until=${until}` +
        `&granularity=${granularity}&group_by=${groupBy}`;
      setData(await fetchJson(url));
    } catch (err) {
      flashError(`No pude cargar los costos: ${err.message || err}`);
      setData(null);
    } finally {
      setLoading(false);
    }
  }, [since, until, granularity, groupBy, flashError]);

  useEffect(() => { cargar(); }, [cargar]);

  // Disparador manual del colector. El camino normal es el cron diario;
  // esto existe para reparar a mano después de un outage sin esperar a la
  // próxima corrida.
  const colectar = useCallback(async () => {
    setColectando(true);
    try {
      const out = await fetchJson(`${API}/admin/costs/collect?days=35`,
                                  { method: "POST" });
      await cargar();
      return out;
    } catch (err) {
      flashError(`No pude recolectar: ${err.message || err}`);
      return null;
    } finally {
      setColectando(false);
    }
  }, [cargar, flashError]);

  return {
    rango, setRango, granularity, setGranularity, groupBy, setGroupBy,
    since, until, data, loading, colectando, colectar, recargar: cargar,
  };
}
