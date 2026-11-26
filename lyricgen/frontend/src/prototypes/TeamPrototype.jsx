import { useRef, useState } from "react";
import GenlyLogo from "../components/GenlyLogo";
import "../components/TeamCoordination.css";

const PEOPLE = ["Lucía", "Mateo", "Sofía"];
const CAMPAIGNS = ["Lanzamientos · Noviembre", "Catálogo · Pop argentino", "Sesiones · Acústicos"];
const STATES = { pending: "Pendiente", working: "En curso", blocked: "Bloqueado", review: "Revisión final", done: "Aprobado" };
const SONGS = ["Volver a empezar", "Cerca del sol", "Todo cambia", "Un lugar", "Sin mirar atrás", "La distancia", "En el aire", "Otra vez", "Lo que queda", "Días de verano", "Tu voz", "Hasta mañana", "El camino", "A tiempo", "Un instante", "Horizonte", "Entre nosotros", "La señal"];
const seed = () => SONGS.map((title, i) => ({
  id: i + 1, title, artist: ["Banda del Sur", "Clara Lagos", "Los Días"][i % 3],
  campaign: CAMPAIGNS[Math.floor(i / 6)], owner: i % 5 === 0 ? "" : PEOPLE[i % 3],
  status: ["pending", "working", "review", "done", "blocked", "pending"][i % 6],
  priority: i % 4 === 0 ? "Alta" : "Normal", due: "2026-11-20",
  note: i % 6 === 4 ? "Falta confirmar la letra del segundo estribillo." : "",
}));
const count = (tasks, state) => tasks.filter(t => t.status === state).length;
const pct = tasks => tasks.length ? Math.round(count(tasks, "done") / tasks.length * 100) : 0;
function Badge({ status }) { return <span className={`tp-badge ${status}`}><span />{STATES[status]}</span>; }
function Avatar({ name }) { return <span className={`tp-avatar person-${PEOPLE.indexOf(name)}`}>{name ? name.slice(0, 1) : "—"}</span>; }
function Progress({ tasks }) { return <div className="tp-progress" role="progressbar" aria-label="Videos aprobados" aria-valuenow={pct(tasks)} aria-valuemin={0} aria-valuemax={100}><span style={{ width: `${pct(tasks)}%` }} /></div>; }

export default function TeamPrototype() {
  const [tasks, setTasks] = useState(seed);
  const [view, setView] = useState("team");
  const [person, setPerson] = useState(PEOPLE[0]);
  const [campaign, setCampaign] = useState("");
  const [owner, setOwner] = useState("");
  const [status, setStatus] = useState("");
  const [query, setQuery] = useState("");
  const [selected, setSelected] = useState([]);
  const [assignment, setAssignment] = useState(null);
  const [target, setTarget] = useState(PEOPLE[0]);
  const [priority, setPriority] = useState("Normal");
  const [due, setDue] = useState("2026-11-20");
  const [detail, setDetail] = useState(null);
  const [note, setNote] = useState("");
  const [message, setMessage] = useState("");
  const [activity, setActivity] = useState(["Sofía envió «Todo cambia» a revisión final.", "Mateo informó un bloqueo en «Sin mirar atrás»."]);
  const assignDialog = useRef(null);
  const detailDialog = useRef(null);
  const mine = view === "mine";
  const scope = tasks.filter(t => !mine || t.owner === person);
  const rows = scope.filter(t => (!campaign || t.campaign === campaign) && (!owner || (owner === "unassigned" ? !t.owner : t.owner === owner)) && (!status || t.status === status) && `${t.title} ${t.artist}`.toLocaleLowerCase().includes(query.toLocaleLowerCase()));
  const activeDetail = tasks.find(t => t.id === detail);
  const record = text => { setMessage(text); setActivity(a => [text, ...a].slice(0, 5)); };
  const changeView = next => { setView(next); setOwner(""); setStatus(""); setCampaign(""); setQuery(""); setSelected([]); };
  const openAssignment = (ids, label) => {
    setAssignment({ ids, label }); setTarget(PEOPLE[0]); setPriority("Normal"); setDue("2026-11-20"); assignDialog.current.showModal();
  };
  const assign = e => {
    e.preventDefault();
    setTasks(ts => ts.map(t => assignment.ids.includes(t.id) ? { ...t, owner: target, priority, due } : t));
    record(`${assignment.ids.length} videos asignados a ${target}.`);
    setSelected([]); assignDialog.current.close();
  };
  const openDetail = t => { setDetail(t.id); setNote(t.note); detailDialog.current.showModal(); };
  const updateStatus = next => {
    setTasks(ts => ts.map(t => t.id === detail ? { ...t, status: next, note: note.trim() } : t));
    record(`${activeDetail.title}: ${STATES[next].toLowerCase()}.`); detailDialog.current.close();
  };
  const reset = () => { setTasks(seed()); setSelected([]); setActivity([]); setMessage("Demo restablecida."); };

  return <div className="tp-shell">
    <aside className="tp-sidebar">
      <GenlyLogo size="sm" priority />
      <div className="tp-space"><span className="tp-space-icon">G</span><div>Equipo de producción<small>Espacio de Agus</small></div></div>
      <p className="tp-eyebrow">COORDINACIÓN</p>
      <nav aria-label="Vistas del prototipo">
        <button className={!mine ? "active" : ""} onClick={() => changeView("team")}><span>▦</span> Mi equipo <span className="tp-nav-count">3</span></button>
        <button className={mine ? "active" : ""} onClick={() => changeView("mine")}><span>☑</span> Mis tareas</button>
      </nav>
      <div className="tp-sidebar-note"><span className="tp-live-dot" /> Todo en un mismo lugar<p>Asigná, acompañá y revisá el avance de cada entrega.</p></div>
      <div className="tp-profile"><Avatar name={mine ? person : "Agus"} /><div>{mine ? person : "Agus"}<small>{mine ? "Operador · vista de ejemplo" : "Coordinadora · vista de ejemplo"}</small></div></div>
    </aside>
    <div className="tp-body">
      <div className="tp-demo"><span><b>PROTOTIPO</b> Datos ficticios · Los cambios se reinician al recargar</span><button onClick={reset}>Restablecer demo ↺</button></div>
      <header className="tp-topbar"><span>Producción <span>/</span> {mine ? "Mis tareas" : "Mi equipo"}</span><span>Noviembre 2026 · Demo</span></header>
      <main className="tp-main">
        <div className="tp-heading"><div><p className="tp-eyebrow">{mine ? "TU ESPACIO DE TRABAJO" : "CENTRO DE COORDINACIÓN"}</p><h1>{mine ? `Tus próximas entregas, ${person}` : "Cada entrega, en buenas manos."}</h1><p>{mine ? "Tus videos asignados y el próximo paso para avanzar." : "Repartí el trabajo y acompañá el progreso de tu equipo."}</p></div>{mine ? <label className="tp-field">Simular operador<select value={person} onChange={e => { setPerson(e.target.value); setSelected([]); }}>{PEOPLE.map(p => <option key={p}>{p}</option>)}</select></label> : <button className="tp-primary" onClick={() => { setOwner("unassigned"); setStatus(""); setCampaign(""); setQuery(""); setSelected([]); document.getElementById("tp-work").scrollIntoView({ behavior: "smooth" }); }}>Repartir sin asignar <span>↗</span></button>}</div>
        <div className="tp-stats">
          {[["Videos en total", scope.length, "En las 3 campañas de ejemplo", ""], ["En curso", count(scope, "working"), "Trabajo iniciado por el equipo", "purple"], ["Esperando revisión", count(scope, "review"), "Trabajo listo para revisión final", "cyan"], ["Aprobados", count(scope, "done"), `${pct(scope)}% del total listo para entregar`, "green"]].map(([label, value, hint, tone]) => <section className={`tp-stat ${tone}`} key={label}><p>{label}</p><strong>{value}<span>{label === "Aprobados" ? ` / ${scope.length}` : ""}</span></strong><small>{hint}</small></section>)}
        </div>
        {!mine && <>
          <div className="tp-section-heading"><h2>Así viene el equipo <span>3 operadores</span></h2><span>Carga actual · todas las campañas</span></div>
          <div className="tp-people">{PEOPLE.map(p => { const list = tasks.filter(t => t.owner === p); return <button key={p} className={`tp-person ${owner === p ? "chosen" : ""}`} onClick={() => { setOwner(owner === p ? "" : p); setSelected([]); }}><div className="tp-person-top"><Avatar name={p} /><div><h3>{p}</h3><small>{list.filter(t => t.status !== "done").length} videos por completar</small></div><span>↗</span></div><div className="tp-person-numbers"><span><b>{count(list, "working")}</b> en curso</span><span><b>{count(list, "review")}</b> en revisión</span><span className={count(list, "blocked") ? "tp-warning" : ""}><b>{count(list, "blocked")}</b> bloqueados</span></div><Progress tasks={list} /><div className="tp-progress-caption"><span>{count(list, "done")} de {list.length} aprobados</span><b>{pct(list)}%</b></div></button>; })}</div>
          <div className="tp-section-heading"><h2>Campañas en marcha</h2><span>Avance = videos aprobados / total</span></div>
          <div className="tp-campaigns">{CAMPAIGNS.map((c, i) => { const list = tasks.filter(t => t.campaign === c); const remaining = list.filter(t => t.status !== "done"); return <section key={c} className="tp-campaign"><div className={`tp-cover cover-${i}`}>♫</div><div className="tp-campaign-content"><button className="tp-text-button" onClick={() => { setCampaign(campaign === c ? "" : c); setSelected([]); }}><h3>{c}</h3></button><small>{list.length} videos · {list.filter(t => !t.owner).length} sin asignar</small><Progress tasks={list} /><div className="tp-progress-caption"><span>{count(list, "done")} aprobados</span><span>{pct(list)}%</span></div></div><button className="tp-small-button" disabled={!remaining.length} onClick={() => openAssignment(remaining.map(t => t.id), c)}>Asignar campaña</button></section>; })}</div>
        </>}
        <section id="tp-work" className="tp-work">
          <div className="tp-section-heading"><h2>{mine ? "Mi bandeja de trabajo" : "Videos y responsables"} <span>{rows.length} videos</span></h2>{!mine && <button className="tp-text-button tp-warning" onClick={() => { setStatus(status === "blocked" ? "" : "blocked"); setSelected([]); }}>{count(tasks, "blocked")} bloqueados →</button>}</div>
          <div className="tp-filters"><label className="tp-search">Buscar video<input placeholder="Buscar canción o artista…" value={query} onChange={e => { setQuery(e.target.value); setSelected([]); }} /></label><label>Campaña<select value={campaign} onChange={e => { setCampaign(e.target.value); setSelected([]); }}><option value="">Todas las campañas</option>{CAMPAIGNS.map(c => <option key={c}>{c}</option>)}</select></label>{!mine && <label>Responsable<select value={owner} onChange={e => { setOwner(e.target.value); setSelected([]); }}><option value="">Todo el equipo</option><option value="unassigned">Sin asignar</option>{PEOPLE.map(p => <option key={p}>{p}</option>)}</select></label>}<label>Estado<select value={status} onChange={e => { setStatus(e.target.value); setSelected([]); }}><option value="">Todos los estados</option>{Object.entries(STATES).map(([s, label]) => <option key={s} value={s}>{label}</option>)}</select></label><button className="tp-text-button" onClick={() => { setQuery(""); setOwner(""); setStatus(""); setCampaign(""); setSelected([]); }}>Limpiar</button></div>
          {selected.length > 0 && !mine && <div className="tp-selection"><b>{selected.length} videos seleccionados</b><span>Asigná o cambiá su responsable en un paso.</span><button className="tp-primary" onClick={() => openAssignment(selected, "Selección de videos")}>Asignar selección</button><button aria-label="Quitar selección" onClick={() => setSelected([])}>✕</button></div>}
          <div className="tp-table-wrap"><table><thead><tr>{!mine && <th><input type="checkbox" aria-label="Seleccionar todos los videos visibles pendientes de aprobación" checked={rows.some(t => t.status !== "done") && rows.filter(t => t.status !== "done").every(t => selected.includes(t.id))} onChange={e => setSelected(e.target.checked ? rows.filter(t => t.status !== "done").map(t => t.id) : [])} /></th>}<th>VIDEO / CAMPAÑA</th><th>RESPONSABLE</th><th>ESTADO</th><th>PRIORIDAD</th><th>ENTREGA</th><th><span className="sr-only">Acción</span></th></tr></thead><tbody>{rows.map(t => <tr key={t.id} className={selected.includes(t.id) ? "selected" : ""}>{!mine && <td><input type="checkbox" disabled={t.status === "done"} aria-label={`Seleccionar ${t.title}`} checked={selected.includes(t.id)} onChange={e => setSelected(s => e.target.checked ? [...s, t.id] : s.filter(id => id !== t.id))} /></td>}<td><button className="tp-song" onClick={() => openDetail(t)}>{t.title}</button><small>{t.artist} · {t.campaign.split(" · ")[0]}</small></td><td><div className="tp-owner"><Avatar name={t.owner} /><span className={!t.owner ? "tp-warning" : ""}>{t.owner || "Sin asignar"}</span></div></td><td><Badge status={t.status} /></td><td><span className={t.priority === "Alta" ? "tp-warning" : "tp-muted"}>{t.priority === "Alta" ? "↑ " : ""}{t.priority}</span></td><td>{new Intl.DateTimeFormat("es-AR", { day: "numeric", month: "short", timeZone: "UTC" }).format(new Date(`${t.due}T12:00:00Z`))}</td><td><button className="tp-small-button" onClick={() => openDetail(t)}>{mine && t.status === "pending" ? "Empezar →" : "Ver tarea →"}</button></td></tr>)}</tbody></table>{!rows.length && <div className="tp-empty"><h3>No hay videos en esta vista</h3><p>Probá con otros filtros o asigná tareas a este operador.</p></div>}</div>
        </section>
        <section className="tp-activity"><h2>Últimos movimientos <span>Ejemplo de actividad</span></h2>{activity.map((a, i) => <p key={`${i}-${a}`}><span className="tp-live-dot" />{a}<small>{i === 0 ? "Más reciente" : "Anterior"}</small></p>)}</section>
        <p className="tp-footnote">Demo de coordinación · Las acciones de trabajo simulan el flujo existente de revisión de letra/timing y aprobación final.</p>
      </main>
    </div>
    <div className="tp-toast" role="status" key={message}>{message}</div>
    <dialog ref={assignDialog} className="tp-dialog" aria-label="Asignar videos"><form onSubmit={assign}><div className="tp-dialog-heading"><p className="tp-eyebrow">REPARTIR TRABAJO</p><button type="button" aria-label="Cerrar asignación" onClick={() => assignDialog.current.close()}>✕</button></div><h2>Asignar {assignment?.ids.length} videos</h2><p>{assignment?.label}</p><div className="tp-info">Los videos aprobados quedan fuera. Esta acción reemplaza el responsable, la prioridad y la fecha de los videos seleccionados.</div><label>Responsable<select value={target} onChange={e => setTarget(e.target.value)}>{PEOPLE.map(p => <option key={p}>{p}</option>)}</select></label><p className="tp-muted">{target} tiene {tasks.filter(t => t.owner === target && t.status !== "done").length} videos por completar.</p><div className="tp-form-row"><label>Prioridad<select value={priority} onChange={e => setPriority(e.target.value)}><option>Normal</option><option>Alta</option></select></label><label>Fecha de entrega<input type="date" required value={due} onChange={e => setDue(e.target.value)} /></label></div><footer><button type="button" className="tp-small-button" onClick={() => assignDialog.current.close()}>Cancelar</button><button className="tp-primary" type="submit">Confirmar asignación</button></footer></form></dialog>
    <dialog ref={detailDialog} className="tp-dialog" aria-label="Detalle de tarea"><div className="tp-dialog-heading"><p className="tp-eyebrow">DETALLE DE TAREA · DEMO</p><button aria-label="Cerrar tarea" onClick={() => detailDialog.current.close()}>✕</button></div>{activeDetail && <><h2>{activeDetail.title}</h2><p>{activeDetail.artist} · {activeDetail.campaign}</p><Badge status={activeDetail.status} /><div className="tp-detail-meta"><Avatar name={activeDetail.owner} /><span>{activeDetail.owner || "Sin asignar"} · Prioridad {activeDetail.priority.toLowerCase()}</span></div><label>Nota o motivo del bloqueo<textarea value={note} onChange={e => setNote(e.target.value)} placeholder="Contale al equipo qué falta para avanzar…" rows={3} /></label><p className="tp-muted">Simulación: el editor y el render reales no se abren desde esta demo.</p><footer className="tp-detail-actions"><button className="tp-small-button" onClick={() => { setTasks(ts => ts.map(t => t.id === detail ? { ...t, note: note.trim() } : t)); record(`Nota guardada en «${activeDetail.title}».`); detailDialog.current.close(); }}>Guardar nota</button>{activeDetail.owner && activeDetail.status === "pending" && <button className="tp-primary" onClick={() => updateStatus("working")}>Iniciar tarea</button>}{activeDetail.status === "working" && <><button className="tp-small-button" disabled={!note.trim()} title="Escribí el motivo para informar un bloqueo" onClick={() => updateStatus("blocked")}>Informar bloqueo</button><button className="tp-primary" onClick={() => updateStatus("review")}>Enviar a revisión final</button></>}{activeDetail.status === "blocked" && <button className="tp-primary" onClick={() => updateStatus("working")}>Resolver y retomar</button>}{!mine && activeDetail.status === "review" && <><button className="tp-small-button" onClick={() => updateStatus("working")}>Pedir correcciones</button><button className="tp-primary" onClick={() => updateStatus("done")}>Aprobar entrega</button></>}{!mine && activeDetail.status !== "done" && <button className="tp-small-button" onClick={() => { detailDialog.current.close(); openAssignment([activeDetail.id], activeDetail.title); }}>Cambiar responsable</button>}</footer></>}</dialog>
  </div>;
}
