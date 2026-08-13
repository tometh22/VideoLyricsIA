// Admin · Créditos de regalo (promos de lanzamiento).
//
// Emite un pool de créditos por CUENTA (billing_group o tenant) que se consume
// ANTES del cupo del plan, con vencimiento. Persistido en `credit_grants`
// (backend). Esta vista permite: previsualizar (dry-run), emitir a todas las
// cuentas activas, y ver el historial de lo emitido. El usuario recibe el aviso
// in-app automáticamente (banner de regalo) la próxima vez que entra.
//
// Panel interno (2 operadores) → texto en español, sin i18n (igual que el resto
// del admin).
import { useEffect, useState } from "react";
import { API, fetchJson, fmtDate } from "../../adminApi";

export default function CreditsView() {
  const [data, setData] = useState(null); // { items, summary }
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState(null);
  const [preview, setPreview] = useState(null);
  const [result, setResult] = useState(null);
  const [busy, setBusy] = useState(false);

  const [paid, setPaid] = useState(30);
  const [free, setFree] = useState(6);
  const [ttl, setTtl] = useState(30);
  const [reason, setReason] = useState("escenas_launch");
  // Regalo dirigido a UNA cuenta madre (billing_group) o tenant.
  const [targetKey, setTargetKey] = useState(""); // "billing_group:universal" | "tenant:foo"
  const [targetAmount, setTargetAmount] = useState(30);

  const load = async () => {
    setLoading(true);
    setError(null);
    try {
      setData(await fetchJson(`${API}/admin/credit-grants`));
    } catch (e) {
      setError(e.message);
    } finally {
      setLoading(false);
    }
  };
  useEffect(() => {
    load();
  }, []);

  const payload = (dry) => ({
    scope: "all",
    paid_amount: Number(paid),
    free_amount: Number(free),
    ttl_days: Number(ttl),
    reason: reason.trim() || "escenas_launch",
    dry_run: dry,
  });

  const post = (dry) =>
    fetchJson(`${API}/admin/credit-grants`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload(dry)),
    });

  const doPreview = async () => {
    setBusy(true);
    setError(null);
    setResult(null);
    try {
      setPreview(await post(true));
    } catch (e) {
      setError(e.message);
    } finally {
      setBusy(false);
    }
  };

  const doEmit = async () => {
    if (
      !window.confirm(
        `Vas a emitir créditos de regalo a TODAS las cuentas activas.\n\n` +
          `Pagos: ${paid} créditos · Free: ${free} · Vencen en ${ttl} días.\n` +
          `Idempotente: las cuentas que ya tienen este regalo se saltean.\n\n¿Confirmás?`,
      )
    )
      return;
    setBusy(true);
    setError(null);
    try {
      const res = await post(false);
      setResult(res);
      setPreview(null);
      await load();
    } catch (e) {
      setError(e.message);
    } finally {
      setBusy(false);
    }
  };

  const accounts = data?.accounts || [];

  const doEmitTarget = async () => {
    if (!targetKey) {
      setError("Elegí una cuenta.");
      return;
    }
    const sep = targetKey.indexOf(":");
    const type = targetKey.slice(0, sep);
    const id = targetKey.slice(sep + 1);
    const amount = Number(targetAmount);
    if (!amount || amount <= 0) {
      setError("El monto debe ser mayor a 0.");
      return;
    }
    const label = type === "billing_group" ? `la cuenta «${id}»` : `el tenant «${id}»`;
    if (
      !window.confirm(
        `Vas a emitir ${amount} créditos COMPARTIDOS a ${label}.\n\n` +
          `Es UN solo pool para toda la cuenta (lo comparten todos sus usuarios), ` +
          `NO ${amount} por usuario. Vencen en ${ttl} días.\n\n¿Confirmás?`,
      )
    )
      return;
    setBusy(true);
    setError(null);
    try {
      const reqBody = {
        scope: type,
        amount,
        ttl_days: Number(ttl),
        reason: reason.trim() || "escenas_launch",
      };
      if (type === "billing_group") reqBody.billing_group = id;
      else reqBody.tenant_id = id;
      const res = await fetchJson(`${API}/admin/credit-grants`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(reqBody),
      });
      setResult(res);
      setPreview(null);
      await load();
    } catch (e) {
      setError(e.message);
    } finally {
      setBusy(false);
    }
  };

  const inputCls =
    "w-24 bg-surface-2/40 ring-1 ring-white/10 rounded-lg px-2 py-1.5 text-sm text-white focus:ring-brand outline-none";

  return (
    <div className="space-y-6 animate-fade-in">
      <div>
        <h2 className="text-lg font-semibold">Créditos de regalo</h2>
        <p className="text-ui text-gray-500 mt-1">
          Un pool por cuenta que se consume antes del cupo del plan, con vencimiento. El
          usuario recibe el aviso in-app automáticamente.
        </p>
      </div>

      {error && (
        <div className="rounded-card bg-red-500/[0.08] ring-1 ring-red-500/30 px-4 py-3 text-ui text-red-200">
          {error}
        </div>
      )}

      {/* Resumen de lo activo */}
      <div className="grid grid-cols-2 gap-3 max-w-md">
        <div className="rounded-card glass px-4 py-3">
          <div className="text-2xl font-bold tabular-nums">
            {loading ? "…" : data?.summary?.active_accounts ?? 0}
          </div>
          <div className="text-caption text-gray-500">cuentas con regalo activo</div>
        </div>
        <div className="rounded-card glass px-4 py-3">
          <div className="text-2xl font-bold tabular-nums">
            {loading ? "…" : data?.summary?.active_credits ?? 0}
          </div>
          <div className="text-caption text-gray-500">créditos vigentes (suma)</div>
        </div>
      </div>

      {/* Emitir a todas */}
      <div className="rounded-card glass p-5 space-y-4 max-w-2xl">
        <h3 className="font-medium">Emitir a todas las cuentas activas</h3>
        <p className="text-caption text-gray-500 -mt-2">
          Un regalo por <strong>cuenta</strong> (cuenta de facturación si existe, si no por
          tenant) — un pool compartido por cuenta, <strong>no por usuario</strong>. Para dirigirlo
          a una cuenta puntual, usá el bloque de abajo.
        </p>
        <div className="flex flex-wrap items-end gap-4">
          <label className="text-ui text-gray-400">
            Pagos
            <input type="number" min="0" value={paid} onChange={(e) => setPaid(e.target.value)} className={`block mt-1 ${inputCls}`} />
          </label>
          <label className="text-ui text-gray-400">
            Free
            <input type="number" min="0" value={free} onChange={(e) => setFree(e.target.value)} className={`block mt-1 ${inputCls}`} />
          </label>
          <label className="text-ui text-gray-400">
            Vto (días)
            <input type="number" min="0" value={ttl} onChange={(e) => setTtl(e.target.value)} className={`block mt-1 ${inputCls}`} />
          </label>
          <label className="text-ui text-gray-400 flex-1 min-w-[160px]">
            Motivo (reason)
            <input type="text" value={reason} onChange={(e) => setReason(e.target.value)} className="block mt-1 w-full bg-surface-2/40 ring-1 ring-white/10 rounded-lg px-2 py-1.5 text-sm text-white focus:ring-brand outline-none" />
          </label>
        </div>
        <p className="text-caption text-gray-500">
          Pagos {paid} créditos ≈ {Math.floor(Number(paid) / 3)} videos con Escenas · Free {free} ≈{" "}
          {Math.floor(Number(free) / 3)}. (Escenas = 3 créditos; un video normal, 1.)
        </p>
        <div className="flex items-center gap-3">
          <button type="button" onClick={doPreview} disabled={busy} className="px-4 py-2 rounded-xl glass text-ui hover:text-white disabled:opacity-50">
            {busy ? "…" : "Previsualizar"}
          </button>
          <button type="button" onClick={doEmit} disabled={busy} className="px-4 py-2 rounded-xl bg-brand text-white text-ui font-medium hover:bg-brand/90 disabled:opacity-50">
            Emitir regalo
          </button>
        </div>

        {preview && (
          <div className="rounded-lg bg-surface-2/40 ring-1 ring-white/10 px-4 py-3 text-ui">
            <span className="text-brand-light font-medium">Preview:</span> impactaría a{" "}
            <strong>{preview.created}</strong> cuentas ({preview.skipped} ya tienen este regalo).
            Pagos {preview.amount_paid} · free {preview.amount_free} · vence{" "}
            {preview.expires_at ? fmtDate(preview.expires_at) : "nunca"}.
          </div>
        )}
        {result && (
          <div className="rounded-lg bg-accent/[0.08] ring-1 ring-accent/30 px-4 py-3 text-ui text-accent">
            ✓ Emitido a <strong>{result.created}</strong> cuentas ({result.skipped} ya tenían).
          </div>
        )}
      </div>

      {/* Emitir a una cuenta específica (cuenta madre) */}
      <div className="rounded-card glass p-5 space-y-4 max-w-2xl">
        <h3 className="font-medium">Emitir a una cuenta específica</h3>
        <p className="text-caption text-gray-500 -mt-2">
          Un solo pool <strong>compartido</strong> para toda la cuenta (ej. Universal y sus
          tenants AR/CL), no por usuario.
        </p>
        <div className="flex flex-wrap items-end gap-4">
          <label className="text-ui text-gray-400 flex-1 min-w-[220px]">
            Cuenta
            <select
              value={targetKey}
              onChange={(e) => setTargetKey(e.target.value)}
              className="block mt-1 w-full bg-surface-2/40 ring-1 ring-white/10 rounded-lg px-2 py-1.5 text-sm text-white focus:ring-brand outline-none"
            >
              <option value="">— Elegí una cuenta —</option>
              {accounts.map((a) => (
                <option key={`${a.type}:${a.id}`} value={`${a.type}:${a.id}`}>
                  {a.id} {a.type === "billing_group" ? "(facturación)" : "(tenant)"} · {a.users} usuario{a.users === 1 ? "" : "s"}
                </option>
              ))}
            </select>
          </label>
          <label className="text-ui text-gray-400">
            Créditos
            <input type="number" min="1" value={targetAmount} onChange={(e) => setTargetAmount(e.target.value)} className={`block mt-1 ${inputCls}`} />
          </label>
        </div>
        <p className="text-caption text-gray-500">
          {targetAmount} créditos ≈ {Math.floor(Number(targetAmount) / 3)} videos con Escenas para
          toda la cuenta. Vence en {ttl} días (configurable arriba).
        </p>
        <button type="button" onClick={doEmitTarget} disabled={busy} className="px-4 py-2 rounded-xl bg-brand text-white text-ui font-medium hover:bg-brand/90 disabled:opacity-50">
          Emitir a la cuenta
        </button>
      </div>

      {/* Historial */}
      <div className="rounded-card glass p-5 max-w-3xl">
        <h3 className="font-medium mb-3">Historial</h3>
        {loading ? (
          <p className="text-ui text-gray-500">Cargando…</p>
        ) : !data?.items?.length ? (
          <p className="text-ui text-gray-500">Todavía no se emitió ningún regalo.</p>
        ) : (
          <table className="w-full text-ui">
            <thead className="text-caption text-gray-500 text-left">
              <tr>
                <th className="pb-2">Cuenta</th>
                <th className="pb-2">Créditos</th>
                <th className="pb-2">Motivo</th>
                <th className="pb-2">Emitido</th>
                <th className="pb-2">Vence</th>
                <th className="pb-2">Estado</th>
              </tr>
            </thead>
            <tbody>
              {data.items.slice(0, 50).map((g) => (
                <tr key={g.id} className="border-t border-white/[0.06]">
                  <td className="py-2">{g.billing_group || g.tenant_id || "—"}</td>
                  <td className="py-2 tabular-nums">{g.amount}</td>
                  <td className="py-2 text-gray-400">{g.reason}</td>
                  <td className="py-2 text-gray-400">{fmtDate(g.granted_at)}</td>
                  <td className="py-2 text-gray-400">{g.expires_at ? fmtDate(g.expires_at) : "nunca"}</td>
                  <td className="py-2">
                    {g.revoked ? (
                      <span className="text-gray-500">revocado</span>
                    ) : g.active ? (
                      <span className="text-accent">activo</span>
                    ) : (
                      <span className="text-gray-500">vencido</span>
                    )}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </div>
    </div>
  );
}
