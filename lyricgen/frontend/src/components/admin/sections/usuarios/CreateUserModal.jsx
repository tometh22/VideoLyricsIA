// Modal de creación de usuario. Estado local del form acá; al enviar llama a
// onCreate(payload) (provisto por useGestion) que devuelve un string de error
// o null. El error se muestra inline; en éxito el padre cierra el modal.
import { useState } from "react";

const PLANS = ["free", "100", "250", "500", "1000", "unlimited"];

const EMPTY = {
  username: "",
  password: "",
  email: "",
  tenant_id: "",
  plan_id: "100",
  role: "user",
  allow_overage: false,
  ai_authorized: false,
};

const inputClass =
  "w-full bg-surface-3/40 ring-1 ring-white/[0.06] focus:ring-brand/40 focus:outline-none rounded-button px-3 py-2.5 text-ui text-white placeholder:text-gray-600";

export default function CreateUserModal({ onCreate, onClose }) {
  const [form, setForm] = useState(EMPTY);
  const [createError, setCreateError] = useState("");
  const [submitting, setSubmitting] = useState(false);

  const set = (patch) => setForm((f) => ({ ...f, ...patch }));

  const handleSubmit = async (e) => {
    e.preventDefault();
    setCreateError("");
    setSubmitting(true);
    const err = await onCreate(form);
    setSubmitting(false);
    if (err) {
      setCreateError(err);
      return;
    }
    onClose();
  };

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/60 backdrop-blur-sm p-4">
      <div className="glass-elevated rounded-card p-8 w-full max-w-md">
        <h3 className="text-section text-white font-semibold mb-6">Nuevo usuario</h3>
        <form onSubmit={handleSubmit} className="space-y-4">
          <input
            type="text"
            placeholder="Usuario"
            value={form.username}
            onChange={(e) => set({ username: e.target.value })}
            className={inputClass}
            required
          />
          <input
            type="email"
            placeholder="Email"
            value={form.email}
            onChange={(e) => set({ email: e.target.value })}
            className={inputClass}
          />
          <input
            type="password"
            placeholder="Contraseña"
            value={form.password}
            onChange={(e) => set({ password: e.target.value })}
            className={inputClass}
            required
          />
          <div>
            <input
              type="text"
              placeholder="Tenant ID (vacío = aislado por usuario)"
              value={form.tenant_id}
              onChange={(e) => set({ tenant_id: e.target.value })}
              className={inputClass}
            />
            <p className="text-label text-gray-500 mt-1.5">
              Mismo tenant ID = el equipo comparte historial y videos (ej. varios
              operadores de UMG en el mismo workspace). Vacío = aislado.
            </p>
          </div>
          <select
            value={form.plan_id}
            onChange={(e) => set({ plan_id: e.target.value })}
            className={inputClass}
          >
            {PLANS.map((p) => (
              <option key={p} value={p}>Plan {p}</option>
            ))}
          </select>
          <select
            value={form.role}
            onChange={(e) => set({ role: e.target.value })}
            className={inputClass}
          >
            <option value="user">Usuario</option>
            <option value="admin">Admin</option>
          </select>
          <label className="flex items-start gap-2 text-ui text-gray-300 cursor-pointer select-none">
            <input
              type="checkbox"
              checked={form.allow_overage}
              onChange={(e) => set({ allow_overage: e.target.checked })}
              className="accent-amber-500 mt-0.5"
            />
            <span>
              Permitir overage
              <span className="block text-label text-gray-500 mt-0.5">
                El usuario puede pasar el cap mensual; los videos extra se facturan al cierre.
              </span>
            </span>
          </label>
          <label className="flex items-start gap-2 text-ui text-gray-300 cursor-pointer select-none">
            <input
              type="checkbox"
              checked={form.ai_authorized}
              onChange={(e) => set({ ai_authorized: e.target.checked })}
              className="accent-brand mt-0.5"
            />
            <span>
              Autorizar uso de IA
              <span className="block text-label text-gray-500 mt-0.5">
                Permite generar variaciones y fondos con IA (UMG Guideline 5). Si lo
                dejás sin tildar podés autorizarlo después desde la lista.
              </span>
            </span>
          </label>
          {createError && <p className="text-ui text-red-400">{createError}</p>}
          <div className="flex gap-3 pt-2">
            <button
              type="submit"
              disabled={submitting}
              className="flex-1 px-4 py-2.5 rounded-button bg-brand text-white text-ui font-medium hover:bg-brand-dark transition-colors duration-brand disabled:opacity-50"
            >
              {submitting ? "Creando…" : "Crear"}
            </button>
            <button
              type="button"
              onClick={onClose}
              className="flex-1 px-4 py-2.5 rounded-button ring-1 ring-white/[0.06] text-gray-300 text-ui hover:text-white hover:bg-white/[0.04] transition-colors duration-brand"
            >
              Cancelar
            </button>
          </div>
        </form>
      </div>
    </div>
  );
}
