// Datos de la solapa Finanzas: renglones editables + métricas de operación.
import { useCallback, useEffect, useMemo, useState } from "react";

import { API, fetchJson } from "../../adminApi";
import { addMonths, contractsFrom, currentPeriod, monthsBetween, paramsFrom } from "./finanzasModel";

const JSON_HEADERS = { "Content-Type": "application/json" };

export default function useFinanzas() {
  const [entries, setEntries] = useState([]);
  const [ops, setOps] = useState({ deliveries: [], received: [], reviewers: [], change_requests: [], lead_time: [] });
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);
  const [since, setSince] = useState(null);
  const [until, setUntil] = useState(currentPeriod());

  const contracts = useMemo(() => contractsFrom(entries), [entries]);
  const params = useMemo(() => paramsFrom(entries), [entries]);
  // Sin rango elegido: desde el primer contrato (o tres meses atrás).
  const effectiveSince = since || contracts[0]?.inicio || addMonths(currentPeriod(), -2);
  const months = useMemo(() => monthsBetween(effectiveSince, until), [effectiveSince, until]);

  const loadEntries = useCallback(async () => {
    const data = await fetchJson(`${API}/admin/finance/entries`);
    setEntries(data.entries || []);
    return data.entries || [];
  }, []);

  useEffect(() => {
    let active = true;
    setLoading(true);
    loadEntries()
      .catch((err) => active && setError(err.message))
      .finally(() => active && setLoading(false));
    return () => { active = false; };
  }, [loadEntries]);

  useEffect(() => {
    let active = true;
    fetchJson(`${API}/admin/finance/operations?since=${effectiveSince}&until=${until}`)
      .then((data) => active && setOps(data))
      .catch((err) => active && setError(err.message));
    return () => { active = false; };
  }, [effectiveSince, until]);

  const mutate = useCallback(async (fn) => {
    setError(null);
    try {
      const result = await fn();
      await loadEntries();
      return result;
    } catch (err) {
      setError(err.message);
      throw err;
    }
  }, [loadEntries]);

  const createEntry = (body) => mutate(() => fetchJson(`${API}/admin/finance/entries`, {
    method: "POST", headers: JSON_HEADERS, body: JSON.stringify(body),
  }));
  const updateEntry = (id, body) => mutate(() => fetchJson(`${API}/admin/finance/entries/${id}`, {
    method: "PATCH", headers: JSON_HEADERS, body: JSON.stringify(body),
  }));
  const deleteEntry = (id) => mutate(() => fetchJson(`${API}/admin/finance/entries/${id}`, { method: "DELETE" }));
  const importEntries = (rows, replace) => mutate(() => fetchJson(`${API}/admin/finance/entries/import`, {
    method: "POST", headers: JSON_HEADERS, body: JSON.stringify({ entries: rows, replace }),
  }));
  const saveParams = (payload) => (params.entryId
    ? updateEntry(params.entryId, { payload })
    : createEntry({ period: months[0], section: "parametros", concept: "Parámetros del reparto", payload }));

  return {
    entries, ops, loading, error, setError,
    contracts, params, months,
    since: effectiveSince, setSince, until, setUntil,
    createEntry, updateEntry, deleteEntry, importEntries, saveParams,
  };
}
