// Vista "Gestión de usuarios": tabla de usuarios con búsqueda, cambio de plan
// inline y acciones (AI / overage / activar). Datos y mutaciones vienen de
// useGestion.
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
  } = useGestion();

  const [showCreate, setShowCreate] = useState(false);

  const columns = [
    {
      key: "user",
      header: "Usuario",
      render: (u) => (
        <div className="flex items-start gap-2.5">
          <div className="w-7 h-7 shrink-0 rounded-lg bg-brand/20 flex items-center justify-center">
            <span className="text-label font-bold text-brand-light uppercase">{u.username?.[0]}</span>
          </div>
          <div>
            <div className="flex items-center gap-1.5 flex-wrap">
              <span className={`font-medium ${u.is_active ? "text-white" : "text-gray-500 line-through"}`}>
                {u.username}
              </span>
              {u.role === "admin" && badge("Admin", "bg-brand/20 text-brand-light")}
              {u.ai_authorized && badge("AI", "bg-accent/15 text-accent")}
              {u.allow_overage && badge("Overage", "bg-amber-500/15 text-amber-300")}
            </div>
            <span className="block text-label text-gray-500">{u.email || "—"}</span>
          </div>
        </div>
      ),
    },
    {
      key: "tenant",
      header: "Tenant",
      render: (u) => (
        <span className={`font-mono text-gray-400 ${u.is_active ? "" : "opacity-40"}`}>{u.tenant_id || "—"}</span>
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
              u.is_active ? "text-red-400 hover:bg-red-500/10" : "text-accent hover:bg-accent/10"
            }`}
          >
            {u.is_active ? "Desactivar" : "Activar"}
          </button>
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
    </div>
  );
}
