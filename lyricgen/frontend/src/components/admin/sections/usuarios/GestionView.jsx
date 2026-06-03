// Vista "Gestión de usuarios": tabla de usuarios con búsqueda, cambio de plan
// inline y acciones. Datos y mutaciones vienen de useGestion.
//
// Layout (iteración 2026-06-02 por feedback de Tomás): 4 columnas que entran
// sin scroll horizontal. Las acciones viven en un menú "⋯" por fila — los 5
// botones inline anteriores desbordaban la tabla y al envolverse hacían las
// filas gigantes.
import { useEffect, useRef, useState } from "react";

import { fmtDate } from "../../adminApi";
import DataTable from "../../primitives/DataTable";
import EmptyState from "../../primitives/EmptyState";
import FilterBar from "../../primitives/FilterBar";
import SectionHeader from "../../layout/SectionHeader";
import CreateUserModal from "./CreateUserModal";
import useGestion from "./useGestion";

const PLANS = ["free", "100", "250", "500", "1000", "unlimited"];

const badge = (text, classes) => (
  <span className={`text-section px-1.5 py-0.5 rounded-full font-bold uppercase ${classes}`}>{text}</span>
);

// Menú de acciones por fila (kebab "⋯"). Cierra al clickear afuera o al
// ejecutar una acción.
function RowActions({ user, onWorkspace, onToggleAI, onToggleOverage, onToggleActive, onDelete }) {
  const [open, setOpen] = useState(false);
  // Posición fija calculada al abrir: el menú escapa del overflow-x-auto de
  // la tabla (un absolute adentro del contenedor con overflow se recorta).
  const [pos, setPos] = useState({ top: 0, left: 0 });
  const ref = useRef(null);

  useEffect(() => {
    if (!open) return;
    const close = (e) => {
      if (ref.current && !ref.current.contains(e.target)) setOpen(false);
    };
    document.addEventListener("mousedown", close);
    return () => document.removeEventListener("mousedown", close);
  }, [open]);

  const toggle = (e) => {
    e.stopPropagation();
    const rect = e.currentTarget.getBoundingClientRect();
    // 224px = w-56. Alineado al borde derecho del botón.
    setPos({ top: rect.bottom + 4, left: Math.max(8, rect.right - 224) });
    setOpen((v) => !v);
  };

  const item = (label, onClick, danger = false) => (
    <button
      type="button"
      onClick={(e) => { e.stopPropagation(); setOpen(false); onClick(); }}
      className={`block w-full text-left px-3 py-2 text-caption transition-colors duration-brand ${
        danger ? "text-red-400 hover:bg-red-500/10" : "text-gray-300 hover:bg-white/[0.06]"
      }`}
    >
      {label}
    </button>
  );

  return (
    <div ref={ref} className="relative inline-block">
      <button
        type="button"
        onClick={toggle}
        className="w-8 h-8 rounded-lg flex items-center justify-center text-gray-400 hover:text-white hover:bg-white/[0.06] transition-colors duration-brand"
        title="Acciones"
      >
        <svg className="w-4 h-4" fill="currentColor" viewBox="0 0 24 24">
          <circle cx="5" cy="12" r="1.8" /><circle cx="12" cy="12" r="1.8" /><circle cx="19" cy="12" r="1.8" />
        </svg>
      </button>
      {open && (
        <div
          className="fixed z-50 w-56 py-1 rounded-button bg-surface-2 ring-1 ring-white/[0.08] shadow-depth-lg"
          style={{ top: pos.top, left: pos.left }}
        >
          {item("Cambiar workspace…", onWorkspace)}
          {item(user.ai_authorized ? "Revocar autorización de IA" : "Autorizar IA", onToggleAI)}
          {item(user.allow_overage ? "Frenar overage" : "Permitir overage", onToggleOverage)}
          {item(user.is_active ? "Desactivar cuenta" : "Reactivar cuenta", onToggleActive)}
          {user.role !== "admin" && (
            <>
              <div className="my-1 border-t border-white/[0.06]" />
              {item("Eliminar usuario…", onDelete, true)}
            </>
          )}
        </div>
      )}
    </div>
  );
}

// Modal para mover a un usuario de workspace (tenant) y/o cuenta de
// facturación. El checkbox de "mover videos" está prendido por default —
// sin él, el usuario pierde la visibilidad de su historial.
function WorkspaceModal({ user, onSave, onClose }) {
  const [tenantId, setTenantId] = useState(user.tenant_id || "");
  const [billingGroup, setBillingGroup] = useState(user.billing_group || "");
  const [moveJobs, setMoveJobs] = useState(true);
  const [saving, setSaving] = useState(false);

  const save = async () => {
    setSaving(true);
    await onSave(user.id, {
      tenantId: tenantId.trim(),
      billingGroup: billingGroup.trim(),
      moveJobs,
    });
    setSaving(false);
    onClose();
  };

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/60 backdrop-blur-sm p-4">
      <div className="glass-elevated rounded-card p-6 w-full max-w-md space-y-4">
        <div>
          <h3 className="text-lg font-semibold text-white">Workspace de {user.username}</h3>
          <p className="text-caption text-gray-500 mt-0.5">
            Tenant = con quién comparte videos · Grupo de facturación = con quién comparte el plan mensual
          </p>
        </div>

        <div className="space-y-3">
          <label className="block">
            <span className="text-section uppercase text-gray-500">Tenant (workspace)</span>
            <input
              type="text"
              value={tenantId}
              onChange={(e) => setTenantId(e.target.value)}
              placeholder="ej: universal_argentina"
              className="mt-1 w-full bg-surface-3/40 ring-1 ring-white/[0.06] focus:ring-brand/40 focus:outline-none rounded-md px-3 py-2 text-ui text-white font-mono"
            />
          </label>
          <label className="block">
            <span className="text-section uppercase text-gray-500">Grupo de facturación (opcional)</span>
            <input
              type="text"
              value={billingGroup}
              onChange={(e) => setBillingGroup(e.target.value)}
              placeholder="ej: universal_music (vacío = cuota por tenant)"
              className="mt-1 w-full bg-surface-3/40 ring-1 ring-white/[0.06] focus:ring-brand/40 focus:outline-none rounded-md px-3 py-2 text-ui text-white font-mono"
            />
          </label>
          <label className="flex items-center gap-2 text-caption text-gray-400 cursor-pointer select-none">
            <input
              type="checkbox"
              checked={moveJobs}
              onChange={(e) => setMoveJobs(e.target.checked)}
              className="accent-brand"
            />
            Mover sus videos al tenant nuevo (recomendado — conserva su historial)
          </label>
        </div>

        <div className="flex justify-end gap-2 pt-2">
          <button
            onClick={onClose}
            className="px-4 py-2 rounded-button text-ui text-gray-400 hover:text-white transition-colors duration-brand"
          >
            Cancelar
          </button>
          <button
            onClick={save}
            disabled={saving || !tenantId.trim()}
            className="px-4 py-2 rounded-button bg-brand text-white text-ui font-medium hover:bg-brand-dark transition-colors duration-brand disabled:opacity-40"
          >
            {saving ? "Guardando…" : "Guardar"}
          </button>
        </div>
      </div>
    </div>
  );
}

// Confirmación de eliminación: hay que escribir el username para habilitar
// el botón — borrar un usuario no puede ser un misclick.
function DeleteModal({ user, onDelete, onClose }) {
  const [confirmation, setConfirmation] = useState("");
  const [deleting, setDeleting] = useState(false);
  const matches = confirmation === user.username;

  const doDelete = async () => {
    setDeleting(true);
    await onDelete(user.id);
    setDeleting(false);
    onClose();
  };

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/60 backdrop-blur-sm p-4">
      <div className="glass-elevated rounded-card p-6 w-full max-w-md space-y-4 ring-1 ring-red-500/30">
        <div>
          <h3 className="text-lg font-semibold text-red-300">Eliminar a {user.username}</h3>
          <p className="text-caption text-gray-400 mt-2 leading-relaxed">
            La cuenta se desactiva y anonimiza (no puede volver a loguearse).
            Sus videos quedan en la base para auditoría pero dejan de ser visibles para el usuario.
            Esta acción no se puede deshacer desde la UI.
          </p>
        </div>

        <label className="block">
          <span className="text-section uppercase text-gray-500">
            Escribí <span className="font-mono text-gray-300 normal-case">{user.username}</span> para confirmar
          </span>
          <input
            type="text"
            value={confirmation}
            onChange={(e) => setConfirmation(e.target.value)}
            className="mt-1 w-full bg-surface-3/40 ring-1 ring-red-500/20 focus:ring-red-500/40 focus:outline-none rounded-md px-3 py-2 text-ui text-white font-mono"
            autoFocus
          />
        </label>

        <div className="flex justify-end gap-2 pt-2">
          <button
            onClick={onClose}
            className="px-4 py-2 rounded-button text-ui text-gray-400 hover:text-white transition-colors duration-brand"
          >
            Cancelar
          </button>
          <button
            onClick={doDelete}
            disabled={!matches || deleting}
            className="px-4 py-2 rounded-button bg-red-500/80 text-white text-ui font-medium hover:bg-red-500 transition-colors duration-brand disabled:opacity-30"
          >
            {deleting ? "Eliminando…" : "Eliminar usuario"}
          </button>
        </div>
      </div>
    </div>
  );
}

export default function GestionView() {
  const {
    users,
    usersTotal,
    search,
    setSearch,
    loadUsers,
    createUser,
    changePlan,
    toggleAI,
    toggleOverage,
    toggleActive,
    updateWorkspace,
    deleteUser,
  } = useGestion();

  const [showCreate, setShowCreate] = useState(false);
  const [workspaceUser, setWorkspaceUser] = useState(null);
  const [deleteTarget, setDeleteTarget] = useState(null);

  // 4 columnas — entran sin scroll horizontal en el ancho del admin.
  const columns = [
    {
      key: "user",
      header: "Usuario",
      render: (u) => (
        <div className="flex items-center gap-2.5 min-w-0">
          <div className="w-7 h-7 shrink-0 rounded-lg bg-brand/20 flex items-center justify-center">
            <span className="text-label font-bold text-brand-light uppercase">{u.username?.[0]}</span>
          </div>
          <div className="min-w-0">
            <div className="flex items-center gap-1.5 flex-wrap">
              <span className={`font-medium truncate ${u.is_active ? "text-white" : "text-gray-500 line-through"}`}>
                {u.username}
              </span>
              {u.role === "admin" && badge("Admin", "bg-brand/20 text-brand-light")}
              {u.ai_authorized && badge("AI", "bg-accent/15 text-accent")}
              {u.allow_overage && badge("Ovg", "bg-amber-500/15 text-amber-300")}
            </div>
            <span className="block text-label text-gray-500 truncate">
              {u.email && u.email !== u.username ? u.email : `creado ${fmtDate(u.created_at)}`}
            </span>
          </div>
        </div>
      ),
    },
    {
      key: "workspace",
      header: "Workspace",
      render: (u) => (
        <div className={`min-w-0 ${u.is_active ? "" : "opacity-40"}`}>
          <span className="block font-mono text-gray-300 truncate">{u.tenant_id || "—"}</span>
          <span className="block text-label text-gray-500 truncate">
            {u.billing_group
              ? <>factura: <span className="font-mono">{u.billing_group}</span></>
              : `${u.job_count || 0} videos`}
          </span>
        </div>
      ),
    },
    {
      key: "plan",
      header: "Plan",
      width: "110px",
      render: (u) => (
        <select
          value={u.plan}
          onClick={(e) => e.stopPropagation()}
          onChange={(e) => changePlan(u.id, e.target.value)}
          className="bg-surface-3/40 ring-1 ring-white/[0.06] focus:ring-brand/40 focus:outline-none rounded-md px-2 py-1 text-label text-white w-full"
        >
          {PLANS.map((p) => (
            <option key={p} value={p}>{p}</option>
          ))}
        </select>
      ),
    },
    {
      key: "actions",
      header: "",
      width: "48px",
      align: "right",
      render: (u) => (
        <RowActions
          user={u}
          onWorkspace={() => setWorkspaceUser(u)}
          onToggleAI={() => toggleAI(u.id, u.ai_authorized)}
          onToggleOverage={() => toggleOverage(u.id, u.allow_overage)}
          onToggleActive={() => toggleActive(u.id, u.is_active)}
          onDelete={() => setDeleteTarget(u)}
        />
      ),
    },
  ];

  return (
    <div className="space-y-4">
      <SectionHeader
        title="Gestión de usuarios"
        subtitle={`${usersTotal} usuarios`}
        right={
          <>
            <FilterBar.Search
              value={search}
              onChange={setSearch}
              onSubmit={loadUsers}
              placeholder="Buscar usuarios…"
            />
            <button
              onClick={() => setShowCreate(true)}
              className="px-4 py-2 rounded-button bg-brand text-white text-ui font-medium hover:bg-brand-dark transition-colors duration-brand whitespace-nowrap"
            >
              + Nuevo usuario
            </button>
          </>
        }
      />

      <div className="glass rounded-card p-5">
        <DataTable
          columns={columns}
          rows={users}
          rowKey={(u) => u.id}
          empty={<EmptyState title="Sin usuarios" message="No hay usuarios que coincidan con la búsqueda." />}
        />
      </div>

      {showCreate && (
        <CreateUserModal onCreate={createUser} onClose={() => setShowCreate(false)} />
      )}
      {workspaceUser && (
        <WorkspaceModal
          user={workspaceUser}
          onSave={updateWorkspace}
          onClose={() => setWorkspaceUser(null)}
        />
      )}
      {deleteTarget && (
        <DeleteModal
          user={deleteTarget}
          onDelete={deleteUser}
          onClose={() => setDeleteTarget(null)}
        />
      )}
    </div>
  );
}
