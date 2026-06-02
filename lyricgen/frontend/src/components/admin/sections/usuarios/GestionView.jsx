// Vista "Gestión de usuarios": tabla de usuarios con búsqueda, cambio de plan
// inline y acciones (workspace / AI / overage / activar / eliminar). Datos y
// mutaciones vienen de useGestion.
import { useState } from "react";

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

  const columns = [
    {
      key: "user",
      header: "Usuario",
      render: (u) => (
        <div className="flex items-start gap-2.5">
          <div className="w-7 h-7 shrink-0 rounded-lg bg-brand/20 flex items-center justify-center">
            <span className="text-label font-bold text-brand-light uppercase">{u.username?.[0]}</span>
          </div>
          <div className="min-w-0">
            <div className="flex items-center gap-1.5 flex-wrap">
              <span className={`font-medium ${u.is_active ? "text-white" : "text-gray-500 line-through"}`}>
                {u.username}
              </span>
              {u.role === "admin" && badge("Admin", "bg-brand/20 text-brand-light")}
              {u.ai_authorized && badge("AI", "bg-accent/15 text-accent")}
              {u.allow_overage && badge("Overage", "bg-amber-500/15 text-amber-300")}
            </div>
            <span className="block text-label text-gray-500 truncate">{u.email || "—"}</span>
          </div>
        </div>
      ),
    },
    {
      key: "workspace",
      header: "Workspace",
      render: (u) => (
        <div className={u.is_active ? "" : "opacity-40"}>
          <span className="block font-mono text-gray-300">{u.tenant_id || "—"}</span>
          {u.billing_group && (
            <span className="block text-label text-gray-500">
              factura con: <span className="font-mono">{u.billing_group}</span>
            </span>
          )}
        </div>
      ),
    },
    {
      key: "plan",
      header: "Plan",
      render: (u) => (
        <select
          value={u.plan}
          onClick={(e) => e.stopPropagation()}
          onChange={(e) => changePlan(u.id, e.target.value)}
          className="bg-surface-3/40 ring-1 ring-white/[0.06] focus:ring-brand/40 focus:outline-none rounded-md px-2 py-1 text-label text-white"
        >
          {PLANS.map((p) => (
            <option key={p} value={p}>{p}</option>
          ))}
        </select>
      ),
    },
    {
      key: "jobs",
      header: "Videos",
      align: "right",
      render: (u) => (
        <span className={`tabular-nums text-gray-300 ${u.is_active ? "" : "opacity-40"}`}>{u.job_count || 0}</span>
      ),
    },
    {
      key: "created",
      header: "Creado",
      render: (u) => (
        <span className={`text-gray-500 ${u.is_active ? "" : "opacity-40"}`}>{fmtDate(u.created_at)}</span>
      ),
    },
    {
      key: "actions",
      header: "Acciones",
      render: (u) => (
        <div className="flex gap-1 flex-wrap">
          <button
            onClick={() => setWorkspaceUser(u)}
            className="text-label px-2 py-1 rounded-md font-medium text-gray-300 hover:bg-white/[0.06] transition-colors duration-brand"
          >
            Workspace
          </button>
          <button
            onClick={() => toggleAI(u.id, u.ai_authorized)}
            className={`text-label px-2 py-1 rounded-md font-medium transition-colors duration-brand ${
              u.ai_authorized ? "text-amber-400 hover:bg-amber-500/10" : "text-accent hover:bg-accent/10"
            }`}
          >
            {u.ai_authorized ? "Revocar IA" : "Autorizar IA"}
          </button>
          <button
            onClick={() => toggleOverage(u.id, u.allow_overage)}
            title="Permitir pasar el cap mensual con cargo por video extra"
            className={`text-label px-2 py-1 rounded-md font-medium transition-colors duration-brand ${
              u.allow_overage ? "text-amber-400 hover:bg-amber-500/10" : "text-gray-400 hover:bg-white/[0.04]"
            }`}
          >
            {u.allow_overage ? "Frenar overage" : "Permitir overage"}
          </button>
          <button
            onClick={() => toggleActive(u.id, u.is_active)}
            className={`text-label px-2 py-1 rounded-md font-medium transition-colors duration-brand ${
              u.is_active ? "text-amber-400 hover:bg-amber-500/10" : "text-accent hover:bg-accent/10"
            }`}
          >
            {u.is_active ? "Desactivar" : "Activar"}
          </button>
          {/* Eliminar: solo para no-admins (el backend también lo bloquea) */}
          {u.role !== "admin" && (
            <button
              onClick={() => setDeleteTarget(u)}
              className="text-label px-2 py-1 rounded-md font-medium text-red-400 hover:bg-red-500/10 transition-colors duration-brand"
            >
              Eliminar
            </button>
          )}
        </div>
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
              className="px-4 py-2 rounded-button bg-brand text-white text-ui font-medium hover:bg-brand-dark transition-colors duration-brand"
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
