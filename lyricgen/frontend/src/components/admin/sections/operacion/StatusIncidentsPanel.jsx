// Redacción de incidentes de la página pública `/status` + la barra de la
// home. Es la única pantalla del admin cuyo output lo ve el CLIENTE, así
// que está construida alrededor de dos riesgos concretos:
//
//   1. Publicar de más. Una barra roja en la home de UMG por un falso
//      positivo cuesta más credibilidad que el incidente real que venía
//      después. Por eso hay preview del texto exacto antes de publicar y
//      el banner se puede apagar sin resolver el incidente.
//   2. Publicar y abandonar. Un incidente que queda "Investigando" tres
//      días es peor que no haber avisado. Los abiertos se listan arriba
//      con las horas que llevan sin novedades.
//
// El timeline es append-only por diseño del backend: no hay UI para editar
// una entrada publicada porque no existe el endpoint. Corregir = publicar
// otra entrada.
import { useCallback, useEffect, useMemo, useState } from "react";

import { useAdmin } from "../../AdminContext";
import { API, fetchJson, fmtAgo } from "../../adminApi";
import SectionHeader from "../../layout/SectionHeader";
import {
  INCIDENT_IMPACTS,
  INCIDENT_STATUSES,
  statusStyle,
} from "../../../../lib/serviceStatus";

const IMPACT_LABEL = {
  none: "Aviso (no baja el uptime)",
  minor: "Menor — demoras",
  major: "Alto — servicios afectados",
  critical: "Crítico — caída",
};

const INCIDENT_STATUS_LABEL = {
  investigating: "Investigando",
  identified: "Causa identificada",
  monitoring: "En observación",
  resolved: "Resuelto",
};

const COMPONENT_STATE_LABEL = {
  operational: "Operativo",
  maintenance: "Mantenimiento",
  degraded: "Con demoras",
  partial_outage: "Parcialmente caído",
  major_outage: "Caído",
  unknown: "Sin datos",
};

function StateDot({ status }) {
  return <span className={`inline-block w-2 h-2 rounded-full shrink-0 ${statusStyle(status).dot}`} />;
}

/** Lo que la sonda ve ahora mismo, con la jerga interna incluida.
 *
 * Separado del bloque de incidentes a propósito: esto es diagnóstico y no
 * se publica. Un `no_consumer` o un `backlog_240` le dice al operador qué
 * mirar; al cliente no le dice nada. */
function ProbeStrip({ probe, onRefresh }) {
  if (!probe) {
    return <div className="glass rounded-card px-5 py-4 text-caption text-gray-500">Cargando la sonda…</div>;
  }
  return (
    <div className="glass rounded-card px-5 py-4">
      <div className="flex items-center justify-between gap-3 mb-3 flex-wrap">
        <span className="text-section uppercase font-bold tracking-wider text-gray-400">
          Lo que ve la sonda ahora
        </span>
        <div className="flex items-center gap-3">
          <span className="text-section text-gray-600">
            umbral de banner automático: {probe.auto_banner_min}
          </span>
          <button type="button" onClick={onRefresh}
                  className="text-caption text-brand-light hover:text-white">
            Recargar
          </button>
        </div>
      </div>
      <div className="grid grid-cols-1 md:grid-cols-2 gap-x-6 gap-y-2">
        {(probe.components || []).map((c) => (
          <div key={c.id} className="flex items-center gap-2 text-caption min-w-0">
            <StateDot status={c.status} />
            <span className="text-gray-300 truncate">{c.label}</span>
            <span className={`ml-auto shrink-0 ${statusStyle(c.status).text}`}>
              {COMPONENT_STATE_LABEL[c.status] || c.status}
            </span>
            {c.reason && (
              <span className="shrink-0 font-mono text-gray-600">· {c.reason}</span>
            )}
          </div>
        ))}
      </div>
      <p className="text-section text-gray-600 mt-3 normal-case tracking-normal">
        La sonda prende la barra sola desde “{probe.auto_banner_min}”. Un incidente
        redactado siempre le gana: el cliente lee tu texto, no el genérico.
      </p>
    </div>
  );
}

/** Preview del texto exacto que va a ver el cliente en la barra. */
function BannerPreview({ title, impact, components, componentLabels }) {
  const impactStatus = {
    none: "maintenance", minor: "degraded",
    major: "partial_outage", critical: "major_outage",
  }[impact] || "degraded";
  const critical = impactStatus === "major_outage" || impactStatus === "partial_outage";
  const names = (components || []).map((c) => componentLabels[c] || c).join(", ");
  return (
    <div className={`rounded-card ring-1 px-4 py-3 text-ui ${
      critical
        ? "bg-red-500/[0.12] ring-red-500/30 text-red-100"
        : "bg-amber-500/10 ring-amber-500/30 text-amber-100"
    }`}>
      <span className="font-semibold">{title || "(sin título todavía)"}</span>
      <span className="opacity-80">
        {" · "}{INCIDENT_STATUS_LABEL.investigating}
        {names ? ` · ${names}` : ""}
      </span>
    </div>
  );
}

function NewIncidentForm({ components, onCreated, flashError }) {
  const [title, setTitle] = useState("");
  const [body, setBody] = useState("");
  const [impact, setImpact] = useState("major");
  const [picked, setPicked] = useState([]);
  const [banner, setBanner] = useState(true);
  const [isPublic, setIsPublic] = useState(true);
  const [busy, setBusy] = useState(false);

  const labels = useMemo(
    () => Object.fromEntries((components || []).map((c) => [c.id, c.label])),
    [components],
  );

  const toggle = (id) => setPicked(
    (cur) => (cur.includes(id) ? cur.filter((x) => x !== id) : [...cur, id]),
  );

  const submit = async () => {
    setBusy(true);
    try {
      await fetchJson(`${API}/admin/status/incidents`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          title: title.trim(),
          body: body.trim(),
          impact,
          components: picked,
          banner,
          public: isPublic,
        }),
      });
      setTitle(""); setBody(""); setPicked([]); setImpact("major");
      setBanner(true); setIsPublic(true);
      onCreated();
    } catch (err) {
      flashError(err.message);
    } finally {
      setBusy(false);
    }
  };

  const ready = title.trim().length >= 3 && body.trim().length > 0;

  return (
    <div className="glass rounded-card px-5 py-5 space-y-4">
      <span className="block text-section uppercase font-bold tracking-wider text-gray-400">
        Publicar un incidente
      </span>

      <label className="block">
        <span className="block text-caption text-gray-400 mb-1">
          Título — lo que el cliente lee primero
        </span>
        <input
          value={title}
          onChange={(e) => setTitle(e.target.value)}
          maxLength={200}
          placeholder="Demoras en la transcripción de audios nuevos"
          className="w-full rounded-button bg-surface-2 ring-1 ring-white/10 px-3 py-2 text-ui text-white placeholder:text-gray-600 focus:ring-brand focus:outline-none"
        />
      </label>

      <label className="block">
        <span className="block text-caption text-gray-400 mb-1">
          Primera actualización — qué pasa y qué estamos haciendo
        </span>
        <textarea
          value={body}
          onChange={(e) => setBody(e.target.value)}
          rows={3}
          maxLength={5000}
          placeholder="Detectamos demoras en las transcripciones cargadas en la última hora. Estamos investigando."
          className="w-full rounded-button bg-surface-2 ring-1 ring-white/10 px-3 py-2 text-ui text-white placeholder:text-gray-600 focus:ring-brand focus:outline-none"
        />
      </label>

      <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
        <label className="block">
          <span className="block text-caption text-gray-400 mb-1">Impacto</span>
          <select
            value={impact}
            onChange={(e) => setImpact(e.target.value)}
            className="w-full rounded-button bg-surface-2 ring-1 ring-white/10 px-3 py-2 text-ui text-white focus:ring-brand focus:outline-none"
          >
            {INCIDENT_IMPACTS.map((i) => (
              <option key={i} value={i}>{IMPACT_LABEL[i] || i}</option>
            ))}
          </select>
        </label>

        <div>
          <span className="block text-caption text-gray-400 mb-1">
            Servicios afectados
          </span>
          <div className="flex flex-wrap gap-1.5">
            {(components || []).map((c) => (
              <button
                key={c.id}
                type="button"
                onClick={() => toggle(c.id)}
                aria-pressed={picked.includes(c.id)}
                className={`px-2.5 py-1 rounded-full text-caption ring-1 transition-colors duration-brand ${
                  picked.includes(c.id)
                    ? "bg-brand/20 ring-brand/40 text-white"
                    : "bg-white/[0.03] ring-white/10 text-gray-400 hover:text-white"
                }`}
              >
                {c.label}
              </button>
            ))}
          </div>
          {/* El backend lee "sin componentes" como "toda la plataforma", que
              es lo que el cliente entiende cuando el aviso no aclara. */}
          {picked.length === 0 && (
            <p className="text-section text-amber-400/80 mt-1.5 normal-case tracking-normal">
              Sin servicios marcados el incidente cuenta como toda la plataforma.
            </p>
          )}
        </div>
      </div>

      <div className="flex flex-wrap items-center gap-5">
        <label className="flex items-center gap-2 text-caption text-gray-300">
          <input type="checkbox" checked={banner}
                 onChange={(e) => setBanner(e.target.checked)} />
          Mostrar la barra en la home
        </label>
        <label className="flex items-center gap-2 text-caption text-gray-300">
          <input type="checkbox" checked={isPublic}
                 onChange={(e) => setIsPublic(e.target.checked)} />
          Visible en /status
        </label>
      </div>

      {banner && isPublic && (
        <div>
          <span className="block text-caption text-gray-400 mb-1.5">
            Así lo va a ver el cliente
          </span>
          <BannerPreview title={title} impact={impact} components={picked}
                         componentLabels={labels} />
        </div>
      )}

      <button
        type="button"
        onClick={submit}
        disabled={!ready || busy}
        className="px-4 py-2 rounded-button text-ui font-semibold bg-brand text-white hover:bg-brand-light disabled:opacity-50 transition-colors duration-brand"
      >
        {busy ? "Publicando…" : "Publicar"}
      </button>
    </div>
  );
}

function IncidentRow({ incident, componentLabels, onChanged, flashError }) {
  const [body, setBody] = useState("");
  const [nextStatus, setNextStatus] = useState(incident.status);
  const [busy, setBusy] = useState(false);
  const [confirmDelete, setConfirmDelete] = useState(false);

  const impactStatus = {
    none: "maintenance", minor: "degraded",
    major: "partial_outage", critical: "major_outage",
  }[incident.impact] || "degraded";

  const call = async (fn) => {
    setBusy(true);
    try {
      await fn();
      onChanged();
    } catch (err) {
      flashError(err.message);
    } finally {
      setBusy(false);
    }
  };

  const postUpdate = () => call(async () => {
    await fetchJson(`${API}/admin/status/incidents/${incident.id}/updates`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ body: body.trim(), status: nextStatus }),
    });
    setBody("");
  });

  const patch = (payload) => call(() => fetchJson(
    `${API}/admin/status/incidents/${incident.id}`,
    {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    },
  ));

  const remove = () => call(() => fetchJson(
    `${API}/admin/status/incidents/${incident.id}`, { method: "DELETE" },
  ));

  const lastUpdate = (incident.updates || [])[0];
  // Horas desde la última novedad publicada. Un incidente abierto sin
  // novedades hace medio día es una promesa incumplida, no un aviso.
  const staleHours = !incident.resolved && lastUpdate
    ? Math.floor((Date.now() - new Date(lastUpdate.created_at).getTime()) / 3_600_000)
    : null;

  return (
    <div className="glass rounded-card px-5 py-4">
      <div className="flex items-start justify-between gap-3 flex-wrap mb-2">
        <div className="min-w-0">
          <div className="flex items-center gap-2 flex-wrap">
            <StateDot status={incident.resolved ? "operational" : impactStatus} />
            <span className="font-semibold text-white">{incident.title}</span>
            {!incident.public && (
              <span className="text-section px-1.5 py-0.5 rounded bg-white/[0.06] text-gray-400">
                interno
              </span>
            )}
            {incident.banner && (
              <span className="text-section px-1.5 py-0.5 rounded bg-red-500/15 text-red-300">
                barra activa
              </span>
            )}
          </div>
          <p className="text-caption text-gray-500 mt-1">
            {INCIDENT_STATUS_LABEL[incident.status] || incident.status}
            {" · "}{IMPACT_LABEL[incident.impact] || incident.impact}
            {(incident.components || []).length > 0 && (
              <> · {incident.components.map((c) => componentLabels[c] || c).join(", ")}</>
            )}
            {" · empezó "}{fmtAgo(incident.started_at)}
            {incident.resolved && <> · resuelto {fmtAgo(incident.resolved_at)}</>}
          </p>
          {staleHours !== null && staleHours >= 6 && (
            <p className="text-caption text-amber-400 mt-1">
              Sin novedades desde hace {staleHours} h — publicá una actualización
              o resolvelo.
            </p>
          )}
        </div>
        <div className="flex items-center gap-2 shrink-0">
          {!incident.resolved && (
            <button
              type="button"
              disabled={busy}
              onClick={() => patch({ banner: !incident.banner })}
              className="text-caption px-2.5 py-1 rounded-lg ring-1 ring-white/10 text-gray-300 hover:text-white disabled:opacity-50"
            >
              {incident.banner ? "Apagar barra" : "Prender barra"}
            </button>
          )}
          {confirmDelete ? (
            <>
              <button type="button" disabled={busy} onClick={remove}
                      className="text-caption px-2.5 py-1 rounded-lg bg-red-500/20 text-red-300 hover:bg-red-500/30 disabled:opacity-50">
                Confirmar borrado
              </button>
              <button type="button" onClick={() => setConfirmDelete(false)}
                      className="text-caption text-gray-500 hover:text-white px-1">
                No
              </button>
            </>
          ) : (
            <button type="button" onClick={() => setConfirmDelete(true)}
                    className="text-caption px-2.5 py-1 rounded-lg ring-1 ring-white/10 text-gray-500 hover:text-red-300">
              Borrar
            </button>
          )}
        </div>
      </div>

      {/* Timeline publicado. Sin botón de editar: el backend no lo expone. */}
      <ol className="space-y-1.5 mb-3">
        {(incident.updates || []).map((u) => (
          <li key={u.id} className="text-caption text-gray-400">
            <span className="text-gray-300 font-medium">
              {INCIDENT_STATUS_LABEL[u.status] || u.status}
            </span>
            {" — "}{u.body}
            <span className="text-gray-600"> · {fmtAgo(u.created_at)}</span>
          </li>
        ))}
      </ol>

      <div className="flex flex-col sm:flex-row gap-2">
        <input
          value={body}
          onChange={(e) => setBody(e.target.value)}
          maxLength={5000}
          placeholder="Nueva actualización para el cliente…"
          className="flex-1 min-w-0 rounded-button bg-surface-2 ring-1 ring-white/10 px-3 py-2 text-caption text-white placeholder:text-gray-600 focus:ring-brand focus:outline-none"
        />
        <select
          value={nextStatus}
          onChange={(e) => setNextStatus(e.target.value)}
          className="rounded-button bg-surface-2 ring-1 ring-white/10 px-2 py-2 text-caption text-white focus:ring-brand focus:outline-none"
        >
          {INCIDENT_STATUSES.map((st) => (
            <option key={st} value={st}>{INCIDENT_STATUS_LABEL[st] || st}</option>
          ))}
        </select>
        <button
          type="button"
          onClick={postUpdate}
          disabled={busy || !body.trim()}
          className="px-3 py-2 rounded-button text-caption font-semibold bg-brand text-white hover:bg-brand-light disabled:opacity-50 transition-colors duration-brand"
        >
          Publicar
        </button>
      </div>
    </div>
  );
}

export default function StatusIncidentsPanel() {
  const { flashError } = useAdmin();
  const [probe, setProbe] = useState(null);
  const [incidents, setIncidents] = useState(null);
  const [components, setComponents] = useState([]);

  const loadProbe = useCallback(async () => {
    try {
      setProbe(await fetchJson(`${API}/admin/status/components`));
    } catch (err) {
      flashError(err.message);
    }
  }, [flashError]);

  const loadIncidents = useCallback(async () => {
    try {
      const data = await fetchJson(`${API}/admin/status/incidents`);
      setIncidents(data.incidents || []);
      setComponents(data.components || []);
    } catch (err) {
      flashError(err.message);
      setIncidents([]);
    }
  }, [flashError]);

  useEffect(() => { loadProbe(); loadIncidents(); }, [loadProbe, loadIncidents]);

  const componentLabels = useMemo(
    () => Object.fromEntries(components.map((c) => [c.id, c.label])),
    [components],
  );

  const open = (incidents || []).filter((i) => !i.resolved);
  const closed = (incidents || []).filter((i) => i.resolved);

  return (
    <div className="space-y-5">
      <SectionHeader
        title="Estado público del servicio"
        subtitle="Lo que se publica en /status y en la barra de la home. Lo ve el cliente."
      />

      <ProbeStrip probe={probe} onRefresh={loadProbe} />

      <NewIncidentForm
        components={components}
        onCreated={loadIncidents}
        flashError={flashError}
      />

      {incidents === null ? (
        <div className="glass rounded-card px-5 py-4 text-caption text-gray-500">
          Cargando incidentes…
        </div>
      ) : (
        <>
          <div className="space-y-3">
            <span className="block text-section uppercase font-bold tracking-wider text-gray-400">
              Abiertos ({open.length})
            </span>
            {open.length === 0 ? (
              <div className="glass rounded-card px-5 py-4 text-caption text-gray-500">
                Ningún incidente abierto.
              </div>
            ) : open.map((inc) => (
              <IncidentRow key={inc.id} incident={inc}
                           componentLabels={componentLabels}
                           onChanged={loadIncidents} flashError={flashError} />
            ))}
          </div>

          {closed.length > 0 && (
            <div className="space-y-3">
              <span className="block text-section uppercase font-bold tracking-wider text-gray-400">
                Resueltos ({closed.length})
              </span>
              {closed.map((inc) => (
                <IncidentRow key={inc.id} incident={inc}
                             componentLabels={componentLabels}
                             onChanged={loadIncidents} flashError={flashError} />
              ))}
            </div>
          )}
        </>
      )}
    </div>
  );
}
