// Estado vacío diseñado — reemplaza los "No invoices yet" sueltos en gris.
export default function EmptyState({ title, message, action }) {
  return (
    <div className="flex flex-col items-center justify-center py-12 px-6 text-center">
      <div className="w-10 h-10 rounded-xl bg-surface-3/40 ring-1 ring-white/[0.06] flex items-center justify-center mb-3">
        <svg className="w-5 h-5 text-gray-500" fill="none" stroke="currentColor" strokeWidth="1.5" viewBox="0 0 24 24">
          <circle cx="11" cy="11" r="7" />
          <path d="M21 21l-4.35-4.35" />
        </svg>
      </div>
      <p className="text-ui text-gray-300 font-medium">{title}</p>
      {message && <p className="text-caption text-gray-500 mt-1 max-w-sm">{message}</p>}
      {action && <div className="mt-4">{action}</div>}
    </div>
  );
}
