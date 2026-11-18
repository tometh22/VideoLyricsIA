import { useCallback, useEffect, useRef, useState } from "react";
import { Link, useLocation } from "react-router-dom";
import "./TeamCoordination.css";
import { fetchWithTimeout } from "../fetchWithTimeout";

const API = import.meta.env.VITE_API_URL || "";
const STATES = {
  pending: "Pendiente",
  working: "En curso",
  processing: "Procesando",
  blocked: "Bloqueado",
  failed: "Fallido",
  review: "Revisión final",
  done: "Aprobado",
  discarded: "Descartado",
};
const PHASES = {
  waiting_upload: "Esperando carga",
  uploading: "Subiendo audio",
  waiting_processing: "En espera",
  transcribing: "Transcribiendo",
  separating: "Separando audio",
  separation_ready: "Audio listo",
  lyrics_ready: "Letra y timing por revisar",
  lyrics_approved: "Letra aprobada",
  rendering: "Renderizando",
  final_review: "Revisión final",
  done: "Aprobado",
  failed: "Requiere atención",
  discarded: "Descartado",
};
const total = (counts) =>
  Object.entries(counts || {}).reduce(
    (n, [s, v]) => n + (s === "discarded" ? 0 : v),
    0,
  );
const percentage = (counts) =>
  total(counts) ? Math.round(((counts.done || 0) / total(counts)) * 100) : 0;
const eligible = (t) => !["done", "discarded"].includes(t.status);
function Avatar({ name = "" }) {
  return <span className="tp-avatar person-0">{name.slice(0, 1) || "—"}</span>;
}
function Progress({ counts }) {
  return (
    <>
      <div
        className="tp-progress"
        role="progressbar"
        aria-label="Videos aprobados"
        aria-valuenow={percentage(counts)}
        aria-valuemin={0}
        aria-valuemax={100}
      >
        <span style={{ width: `${percentage(counts)}%` }} />
      </div>
      <div className="tp-progress-caption">
        <span>
          {counts.done || 0} de {total(counts)} aprobados
        </span>
        <b>{percentage(counts)}%</b>
      </div>
    </>
  );
}
async function api(path, { signal, ...options } = {}) {
  const response = await fetchWithTimeout(`${API}/team${path}`, {
    ...options,
    signal,
    cache: "no-store",
    headers: {
      Authorization: `Bearer ${localStorage.getItem("genly_token") || ""}`,
      ...(options.body ? { "Content-Type": "application/json" } : {}),
    },
  });
  const data = await response.json().catch(() => ({}));
  if (!response.ok)
    throw new Error(
      typeof data.detail === "string"
        ? data.detail
        : `No se pudo completar la operación (${response.status})`,
    );
  return data;
}

export default function TeamPage({ user }) {
  const { pathname } = useLocation();
  const [access, setAccess] = useState(null);
  const [tenant, setTenant] = useState("");
  const [summary, setSummary] = useState(null);
  const [page, setPage] = useState({ items: [], total: 0 });
  const [campaign, setCampaign] = useState("");
  const [owner, setOwner] = useState("");
  const [status, setStatus] = useState("");
  const [search, setSearch] = useState("");
  const [offset, setOffset] = useState(0);
  const [selected, setSelected] = useState([]);
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [dialogError, setDialogError] = useState("");
  const [message, setMessage] = useState("");
  const [assignment, setAssignment] = useState(null);
  const [target, setTarget] = useState("");
  const [priority, setPriority] = useState("normal");
  const [due, setDue] = useState("");
  const [detail, setDetail] = useState(null);
  const [note, setNote] = useState("");
  const [blocked, setBlocked] = useState("");
  const [refresh, setRefresh] = useState(0);
  const assignmentDialog = useRef(null);
  const taskDialog = useRef(null);
  const mine = pathname === "/my-tasks" || !access?.can_manage;
  const manages = !!summary?.can_manage && !mine;
  const tenantQuery = new URLSearchParams({ tenant_id: tenant }).toString();
  const memberName = (id) =>
    summary?.members.find((p) => p.id === id)?.name ||
    (id ? "Usuario no disponible" : "Sin asignar");
  const reload = useCallback(() => setRefresh((r) => r + 1), []);

  useEffect(() => {
    const controller = new AbortController();
    api("/access", { signal: controller.signal })
      .then((data) => {
        setAccess(data);
        setTenant(data.tenant_id);
      })
      .catch((e) => {
        if (e.name !== "AbortError") {
          setError(e.message);
          setLoading(false);
        }
      });
    return () => controller.abort();
  }, []);
  useEffect(() => {
    setSelected([]);
    setOffset(0);
  }, [tenant, campaign, owner, status, search, mine]);
  useEffect(() => {
    if (!tenant) return undefined;
    const controller = new AbortController();
    setLoading(true);
    setError("");
    const params = new URLSearchParams({
      tenant_id: tenant,
      mine: String(mine),
      offset: String(offset),
      limit: "50",
    });
    if (campaign) params.set("campaign_id", campaign);
    if (owner === "unassigned") params.set("unassigned", "true");
    else if (owner) params.set("assignee_id", owner);
    if (status) params.set("state", status);
    if (search.trim()) params.set("search", search.trim());
    const timer = setTimeout(() => {
      Promise.all([
        api(`/dashboard?${tenantQuery}&mine=${mine}`, {
          signal: controller.signal,
        }),
        api(`/tasks?${params}`, { signal: controller.signal }),
      ])
        .then(([data, rows]) => {
          setSummary(data);
          setAccess((previous) => ({
            ...previous,
            can_manage: data.can_manage,
          }));
          setPage(rows);
        })
        .catch((e) => {
          if (e.name !== "AbortError") setError(e.message);
        })
        .finally(() => {
          if (!controller.signal.aborted) setLoading(false);
        });
    }, 180);
    return () => {
      clearTimeout(timer);
      controller.abort();
    };
  }, [
    tenant,
    tenantQuery,
    mine,
    campaign,
    owner,
    status,
    search,
    offset,
    refresh,
  ]);
  useEffect(() => {
    const id = setInterval(() => {
      if (
        !document.hidden &&
        !busy &&
        !assignmentDialog.current?.open &&
        !taskDialog.current?.open
      )
        reload();
    }, 30000);
    return () => clearInterval(id);
  }, [busy, reload]);
  const clearFilters = () => {
    setCampaign("");
    setOwner("");
    setStatus("");
    setSearch("");
    setOffset(0);
    setSelected([]);
  };
  const openAssignment = (items, label) => {
    setAssignment({
      items: items
        .filter(eligible)
        .map((t) => ({ id: t.id, version: t.version })),
      label,
    });
    setTarget("");
    setPriority("normal");
    setDue("");
    setDialogError("");
    assignmentDialog.current.showModal();
  };
  const assignCampaign = async (c) => {
    setBusy(true);
    setError("");
    try {
      const rows = await api(
        `/tasks?${tenantQuery}&campaign_id=${encodeURIComponent(c.id)}&limit=1000`,
      );
      if (rows.total > rows.items.length)
        throw new Error(
          "Esta campaña supera 1000 videos. Asigná por selección de videos.",
        );
      if (!rows.items.some(eligible))
        throw new Error("La campaña no tiene videos pendientes de aprobación.");
      openAssignment(rows.items, c.name);
    } catch (e) {
      setError(e.message);
    } finally {
      setBusy(false);
    }
  };
  const saveAssignment = async (event) => {
    event.preventDefault();
    setBusy(true);
    setDialogError("");
    try {
      const result = await api(`/assignments?${tenantQuery}`, {
        method: "POST",
        body: JSON.stringify({
          items: assignment.items,
          assignee_id: target ? Number(target) : null,
          priority,
          due_date: due || null,
        }),
      });
      setMessage(
        `${result.assigned.length} videos actualizados.${result.skipped.length ? ` ${result.skipped.length} omitidos por estar aprobados, descartados o en campañas cerradas.` : ""}`,
      );
      assignmentDialog.current.close();
      setSelected([]);
      reload();
    } catch (e) {
      setDialogError(e.message);
      reload();
    } finally {
      setBusy(false);
    }
  };
  const openTask = (t) => {
    setDetail(t);
    setNote(t.note);
    setBlocked(t.blocked_reason);
    setDialogError("");
    taskDialog.current.showModal();
  };
  const saveWork = async (action) => {
    setBusy(true);
    setDialogError("");
    try {
      await api(`/tasks/${encodeURIComponent(detail.id)}?${tenantQuery}`, {
        method: "PATCH",
        body: JSON.stringify({
          version: detail.version,
          action,
          note,
          blocked_reason: blocked,
        }),
      });
      setMessage("Tarea actualizada.");
      taskDialog.current.close();
      reload();
    } catch (e) {
      setDialogError(e.message);
      reload();
    } finally {
      setBusy(false);
    }
  };
  const toggleCoordinator = async (member) => {
    setBusy(true);
    setError("");
    try {
      await api(`/coordinators/${member.id}?${tenantQuery}`, {
        method: "PUT",
        body: JSON.stringify({ enabled: !member.coordinator }),
      });
      setMessage(
        `Permiso de coordinación ${member.coordinator ? "retirado" : "habilitado"} para ${member.name}.`,
      );
      reload();
    } catch (e) {
      setError(e.message);
    } finally {
      setBusy(false);
    }
  };
  const counts = summary?.counts || {};
  const detailCanEdit =
    detail &&
    eligible(detail) &&
    detail.version > 0 &&
    (summary?.can_manage || detail.assignee_id === user?.id);
  // Keep the established campaign/editor routing, lock acquisition and approval gates.
  const workPath = (t) =>
    t.phase === "lyrics_ready" && t.job_id
      ? `/review/${encodeURIComponent(t.job_id)}?return_to=${encodeURIComponent(`/campaigns/${t.campaign_id}`)}`
      : ["final_review", "done"].includes(t.phase) && t.job_id
        ? `/videos/${encodeURIComponent(t.job_id)}`
        : `/campaigns/${encodeURIComponent(t.campaign_id)}`;

  return (
    <div className="tp-shell tp-live">
      <main className="tp-main">
        <div className="tp-heading">
          <div>
            <p className="tp-eyebrow">COORDINACIÓN DE PRODUCCIÓN</p>
            <h1>{mine ? "Mis tareas" : "Cada entrega, en buenas manos."}</h1>
            <p>
              {mine
                ? "Tus videos asignados y el próximo paso para avanzar."
                : "Repartí el trabajo y acompañá el progreso de tu equipo."}
            </p>
          </div>
          <div className="tp-live-actions">
            {access?.can_grant && (
              <label>
                Equipo
                <select
                  disabled={busy}
                  value={tenant}
                  onChange={(e) => {
                    setTenant(e.target.value);
                    setSummary(null);
                    setPage({ items: [], total: 0 });
                    clearFilters();
                  }}
                >
                  {access.tenants.map((t) => (
                    <option key={t}>{t}</option>
                  ))}
                </select>
              </label>
            )}
            <button
              className="tp-small-button"
              disabled={loading || busy}
              onClick={() => (access ? reload() : window.location.reload())}
            >
              Actualizar
            </button>
          </div>
        </div>
        <nav className="tp-view-tabs" aria-label="Vistas del equipo">
          {access?.can_manage && (
            <Link to="/team" aria-current={!mine ? "page" : undefined}>
              Mi equipo
            </Link>
          )}
          <Link to="/my-tasks" aria-current={mine ? "page" : undefined}>
            Mis tareas
          </Link>
          <span>Actualización automática cada 30 s</span>
        </nav>
        {error && (
          <div className="tp-error" role="alert">
            {error}{" "}
            <button
              onClick={() => (access ? reload() : window.location.reload())}
            >
              Reintentar
            </button>
          </div>
        )}
        {message && (
          <div className="tp-info" role="status">
            {message}
            <button aria-label="Cerrar aviso" onClick={() => setMessage("")}>
              ✕
            </button>
          </div>
        )}
        {loading && (
          <p role="status" className="tp-muted">
            Actualizando el equipo…
          </p>
        )}
        {summary && (
          <>
            <div className="tp-stats">
              {[
                ["Videos en total", total(counts), "Sin incluir descartados"],
                [
                  "En curso",
                  counts.working || 0,
                  `${counts.processing || 0} procesándose automáticamente`,
                ],
                [
                  "Esperando revisión",
                  counts.review || 0,
                  "Pendientes de revisión final",
                ],
                [
                  "Aprobados",
                  counts.done || 0,
                  `${percentage(counts)}% listo para entregar`,
                ],
              ].map(([label, value, hint], i) => (
                <section
                  className={`tp-stat ${["", "purple", "cyan", "green"][i]}`}
                  key={label}
                >
                  <p>{label}</p>
                  <strong>{value}</strong>
                  <small>{hint}</small>
                </section>
              ))}
            </div>
            {manages && (
              <>
                <div className="tp-section-heading">
                  <h2>
                    Así viene el equipo{" "}
                    <span>{summary.members.length} personas</span>
                  </h2>
                  <button
                    className="tp-text-button tp-warning"
                    onClick={() => {
                      clearFilters();
                      setOwner("unassigned");
                    }}
                  >
                    {summary.unassigned} sin asignar →
                  </button>
                </div>
                <div className="tp-people">
                  {summary.members.map((p) => (
                    <section className="tp-person" key={p.id}>
                      <button
                        className="tp-person-top"
                        onClick={() =>
                          setOwner(owner === String(p.id) ? "" : String(p.id))
                        }
                      >
                        <Avatar name={p.name} />
                        <div>
                          <h3>{p.name}</h3>
                          <small>
                            {p.active
                              ? `${Math.max(0, total(p.counts) - (p.counts.done || 0))} videos por completar`
                              : "Usuario inactivo"}
                          </small>
                        </div>
                      </button>
                      <div className="tp-person-numbers">
                        <span>
                          <b>{p.counts.working || 0}</b> en curso
                        </span>
                        <span>
                          <b>{p.counts.review || 0}</b> en revisión
                        </span>
                        <span className="tp-warning">
                          <b>
                            {(p.counts.blocked || 0) + (p.counts.failed || 0)}
                          </b>{" "}
                          con problemas
                        </span>
                      </div>
                      <Progress counts={p.counts} />
                      {access.can_grant && (
                        <label className="tp-grant">
                          <input
                            type="checkbox"
                            checked={p.coordinator}
                            disabled={busy || (!p.active && !p.coordinator)}
                            onChange={() => toggleCoordinator(p)}
                          />{" "}
                          Puede coordinar este equipo
                        </label>
                      )}
                    </section>
                  ))}
                </div>
                <div className="tp-section-heading">
                  <h2>Campañas del equipo</h2>
                  <span>Avance = aprobados / videos no descartados</span>
                </div>
                <div className="tp-campaigns">
                  {summary.campaigns.map((c) => (
                    <section className="tp-campaign" key={c.id}>
                      <div className="tp-campaign-content">
                        <button
                          className="tp-text-button"
                          onClick={() => setCampaign(c.id)}
                        >
                          <h3>{c.name}</h3>
                        </button>
                        <Progress counts={c.counts} />
                      </div>
                      <button
                        className="tp-small-button"
                        disabled={
                          busy ||
                          loading ||
                          total(c.counts) === (c.counts.done || 0)
                        }
                        onClick={() => assignCampaign(c)}
                      >
                        Asignar campaña
                      </button>
                    </section>
                  ))}
                </div>
              </>
            )}
            <section className="tp-work">
              <div className="tp-section-heading">
                <h2>
                  {mine ? "Mi bandeja de trabajo" : "Videos y responsables"}{" "}
                  <span>{page.total} videos</span>
                </h2>
              </div>
              <div className="tp-filters">
                <label className="tp-search">
                  Buscar video
                  <input
                    value={search}
                    onChange={(e) => setSearch(e.target.value)}
                    placeholder="Canción, archivo o artista…"
                  />
                </label>
                <label>
                  Campaña
                  <select
                    value={campaign}
                    onChange={(e) => setCampaign(e.target.value)}
                  >
                    <option value="">Todas las campañas</option>
                    {summary.campaigns.map((c) => (
                      <option key={c.id} value={c.id}>
                        {c.name}
                      </option>
                    ))}
                  </select>
                </label>
                {manages && (
                  <label>
                    Responsable
                    <select
                      value={owner}
                      onChange={(e) => setOwner(e.target.value)}
                    >
                      <option value="">Todo el equipo</option>
                      <option value="unassigned">Sin asignar</option>
                      {summary.members.map((p) => (
                        <option key={p.id} value={p.id}>
                          {p.name}
                        </option>
                      ))}
                    </select>
                  </label>
                )}
                <label>
                  Estado
                  <select
                    value={status}
                    onChange={(e) => setStatus(e.target.value)}
                  >
                    <option value="">Todos los estados</option>
                    {Object.entries(STATES).map(([s, label]) => (
                      <option key={s} value={s}>
                        {label}
                      </option>
                    ))}
                  </select>
                </label>
                <button className="tp-text-button" onClick={clearFilters}>
                  Limpiar
                </button>
              </div>
              {manages && selected.length > 0 && (
                <div className="tp-selection">
                  <b>{selected.length} seleccionados</b>
                  <button
                    className="tp-primary"
                    disabled={loading || busy || !!error}
                    onClick={() =>
                      openAssignment(
                        page.items.filter((t) => selected.includes(t.id)),
                        "Selección de videos",
                      )
                    }
                  >
                    Asignar selección
                  </button>
                  <button onClick={() => setSelected([])}>Cancelar</button>
                </div>
              )}
              <div className="tp-table-wrap">
                <table>
                  <thead>
                    <tr>
                      {manages && (
                        <th>
                          <input
                            type="checkbox"
                            aria-label="Seleccionar pendientes de esta página"
                            disabled={loading || busy}
                            checked={
                              page.items.some(eligible) &&
                              page.items
                                .filter(eligible)
                                .every((t) => selected.includes(t.id))
                            }
                            onChange={(e) =>
                              setSelected(
                                e.target.checked
                                  ? page.items.filter(eligible).map((t) => t.id)
                                  : [],
                              )
                            }
                          />
                        </th>
                      )}
                      <th>VIDEO / CAMPAÑA</th>
                      <th>RESPONSABLE</th>
                      <th>ESTADO</th>
                      <th>PRIORIDAD</th>
                      <th>ENTREGA</th>
                      <th>
                        <span className="sr-only">Acción</span>
                      </th>
                    </tr>
                  </thead>
                  <tbody>
                    {page.items.map((t) => (
                      <tr
                        key={t.id}
                        className={selected.includes(t.id) ? "selected" : ""}
                      >
                        {manages && (
                          <td>
                            <input
                              type="checkbox"
                              aria-label={`Seleccionar ${t.title}`}
                              disabled={!eligible(t) || loading || busy}
                              checked={selected.includes(t.id)}
                              onChange={(e) =>
                                setSelected((ids) =>
                                  e.target.checked
                                    ? [...ids, t.id]
                                    : ids.filter((id) => id !== t.id),
                                )
                              }
                            />
                          </td>
                        )}
                        <td>
                          <button
                            className="tp-song"
                            onClick={() => openTask(t)}
                          >
                            {t.title}
                          </button>
                          <small>
                            {t.artist || "Artista no informado"} · {t.campaign}
                          </small>
                        </td>
                        <td>
                          <div className="tp-owner">
                            <Avatar name={memberName(t.assignee_id)} />
                            <span>{memberName(t.assignee_id)}</span>
                          </div>
                        </td>
                        <td>
                          <span className={`tp-badge ${t.status}`}>
                            <span />
                            {STATES[t.status]}
                          </span>
                          <small>{PHASES[t.phase] || t.phase}</small>
                        </td>
                        <td
                          className={t.priority === "high" ? "tp-warning" : ""}
                        >
                          {t.priority === "high" ? "↑ Alta" : "Normal"}
                        </td>
                        <td>{t.due_date || "Sin fecha"}</td>
                        <td>
                          <button
                            className="tp-small-button"
                            onClick={() => openTask(t)}
                          >
                            Ver tarea →
                          </button>
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
                {!page.items.length && !loading && (
                  <div className="tp-empty">
                    <h3>No hay videos en esta vista</h3>
                    <p>
                      {mine
                        ? "Tus tareas aparecerán cuando te asignen videos."
                        : "Probá con otros filtros o agregá videos a una campaña."}
                    </p>
                  </div>
                )}
              </div>
              <div className="tp-pagination">
                <span>
                  {page.total
                    ? `${offset + 1}–${Math.min(offset + 50, page.total)} de ${page.total}`
                    : "0 videos"}
                </span>
                <button
                  className="tp-small-button"
                  disabled={!offset || loading}
                  onClick={() => {
                    setOffset(Math.max(0, offset - 50));
                    setSelected([]);
                  }}
                >
                  Anterior
                </button>
                <button
                  className="tp-small-button"
                  disabled={offset + 50 >= page.total || loading}
                  onClick={() => {
                    setOffset(offset + 50);
                    setSelected([]);
                  }}
                >
                  Siguiente
                </button>
              </div>
            </section>
            <section className="tp-activity">
              <h2>Últimos movimientos</h2>
              {summary.activity.length ? (
                summary.activity.map((e) => (
                  <p key={e.id}>
                    <span className="tp-live-dot" />
                    <span>
                      {e.actor} ·{" "}
                      {{
                        assigned: "Actualizó la asignación",
                        start: "Inició una tarea",
                        note: "Guardó una nota",
                        block: "Informó un bloqueo",
                        unblock: "Resolvió un bloqueo",
                        coordinator: "Actualizó un permiso de coordinación",
                      }[e.action] || e.action}
                      {e.detail.title ? ` · ${e.detail.title}` : ""}
                    </span>
                    <small>{new Date(e.created_at).toLocaleString()}</small>
                  </p>
                ))
              ) : (
                <p>Todavía no hay movimientos de coordinación.</p>
              )}
            </section>
          </>
        )}
        <dialog
          className="tp-dialog"
          ref={assignmentDialog}
          aria-label="Asignar videos"
          onCancel={(e) => {
            if (busy) e.preventDefault();
          }}
        >
          <form onSubmit={saveAssignment}>
            <div className="tp-dialog-heading">
              <p className="tp-eyebrow">REPARTIR TRABAJO</p>
              <button
                type="button"
                disabled={busy}
                aria-label="Cerrar asignación"
                onClick={() => assignmentDialog.current.close()}
              >
                ✕
              </button>
            </div>
            <h2>Asignar {assignment?.items.length} videos</h2>
            <p>{assignment?.label}</p>
            <div className="tp-info">
              Se reemplazan el responsable, la prioridad y la fecha. Los
              aprobados y descartados quedan fuera. Solo se incluyen los videos
              actuales de la campaña.
            </div>
            {dialogError && (
              <p className="tp-error" role="alert">
                {dialogError}
              </p>
            )}
            <label>
              Responsable
              <select
                aria-label="Responsable"
                value={target}
                onChange={(e) => setTarget(e.target.value)}
              >
                <option value="">Sin asignar</option>
                {summary?.members
                  .filter((p) => p.active)
                  .map((p) => (
                    <option key={p.id} value={p.id}>
                      {p.name}
                    </option>
                  ))}
              </select>
            </label>
            <div className="tp-form-row">
              <label>
                Prioridad
                <select
                  value={priority}
                  onChange={(e) => setPriority(e.target.value)}
                >
                  <option value="normal">Normal</option>
                  <option value="high">Alta</option>
                </select>
              </label>
              <label>
                Fecha de entrega
                <input
                  type="date"
                  value={due}
                  onChange={(e) => setDue(e.target.value)}
                />
              </label>
            </div>
            <footer>
              <button
                type="button"
                className="tp-small-button"
                disabled={busy}
                onClick={() => assignmentDialog.current.close()}
              >
                Cancelar
              </button>
              <button
                type="submit"
                className="tp-primary"
                disabled={busy || !assignment?.items.length}
              >
                {busy ? "Guardando…" : "Confirmar asignación"}
              </button>
            </footer>
          </form>
        </dialog>
        <dialog
          className="tp-dialog"
          ref={taskDialog}
          aria-label="Detalle de tarea"
          onCancel={(e) => {
            if (busy) e.preventDefault();
          }}
        >
          <div className="tp-dialog-heading">
            <p className="tp-eyebrow">DETALLE DE TAREA</p>
            <button
              aria-label="Cerrar tarea"
              disabled={busy}
              onClick={() => taskDialog.current.close()}
            >
              ✕
            </button>
          </div>
          {detail && (
            <>
              <h2>{detail.title}</h2>
              <p>
                {detail.campaign} · {memberName(detail.assignee_id)}
              </p>
              <span className={`tp-badge ${detail.status}`}>
                {STATES[detail.status]}
              </span>
              <p>{PHASES[detail.phase]}</p>
              {dialogError && (
                <p role="alert" className="tp-error">
                  {dialogError}
                </p>
              )}
              <label>
                Nota
                <textarea
                  value={note}
                  disabled={!detailCanEdit || busy}
                  onChange={(e) => setNote(e.target.value)}
                  maxLength={2000}
                  rows={3}
                />
              </label>
              <label>
                Motivo del bloqueo
                <textarea
                  value={blocked}
                  disabled={!detailCanEdit || busy}
                  onChange={(e) => setBlocked(e.target.value)}
                  maxLength={1000}
                  rows={2}
                />
              </label>
              <p>
                El avance y la aprobación se actualizan desde el editor y la
                revisión del video.
              </p>
              <footer className="tp-detail-actions">
                {detailCanEdit && (
                  <>
                    <button
                      className="tp-small-button"
                      disabled={busy}
                      onClick={() => saveWork("note")}
                    >
                      Guardar nota
                    </button>
                    {detail.status === "pending" && (
                      <button
                        className="tp-small-button"
                        disabled={busy}
                        onClick={() => saveWork("start")}
                      >
                        Iniciar tarea
                      </button>
                    )}
                    <button
                      className="tp-small-button"
                      disabled={
                        busy || (!detail.blocked_reason && !blocked.trim())
                      }
                      onClick={() =>
                        saveWork(detail.blocked_reason ? "unblock" : "block")
                      }
                    >
                      {detail.blocked_reason
                        ? "Resolver bloqueo"
                        : "Informar bloqueo"}
                    </button>
                  </>
                )}
                <Link className="tp-primary" to={workPath(detail)}>
                  {detail.phase === "lyrics_ready"
                    ? "Abrir revisión de letra"
                    : ["final_review", "done"].includes(detail.phase)
                      ? "Abrir video"
                      : "Abrir campaña"}{" "}
                  →
                </Link>
              </footer>
            </>
          )}
        </dialog>
      </main>
    </div>
  );
}
