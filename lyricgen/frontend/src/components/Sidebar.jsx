export default function Sidebar({ onNav, activeView, open, onToggle }) {
  if (!open) return null;

  const items = [
    {
      id: "dashboard", label: "Dashboard",
      icon: <svg className="w-5 h-5" fill="none" stroke="currentColor" strokeWidth="1.5" viewBox="0 0 24 24"><rect x="3" y="3" width="7" height="7" rx="1"/><rect x="14" y="3" width="7" height="7" rx="1"/><rect x="3" y="14" width="7" height="7" rx="1"/><rect x="14" y="14" width="7" height="7" rx="1"/></svg>,
    },
    {
      id: "new", label: "Nuevo batch",
      icon: <svg className="w-5 h-5" fill="none" stroke="currentColor" strokeWidth="1.5" viewBox="0 0 24 24"><path d="M12 5v14M5 12h14" strokeLinecap="round"/></svg>,
    },
    {
      id: "history", label: "Historial",
      icon: <svg className="w-5 h-5" fill="none" stroke="currentColor" strokeWidth="1.5" viewBox="0 0 24 24"><circle cx="12" cy="12" r="10"/><polyline points="12 6 12 12 16 14"/></svg>,
    },
  ];

  return (
    <aside className="fixed left-0 top-0 bottom-0 w-64 bg-surface-1 border-r border-white/[0.04] z-20 flex flex-col">
      {/* Logo */}
      <div className="flex items-center justify-between px-5 py-5 border-b border-white/[0.04]">
        <div className="flex items-center gap-2.5">
          <div className="w-8 h-8 rounded-lg bg-gradient-to-br from-brand to-brand-light flex items-center justify-center">
            <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="white" strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round">
              <path d="M9 18V5l12-2v13" /><circle cx="6" cy="18" r="3" /><circle cx="18" cy="16" r="3" />
            </svg>
          </div>
          <div>
            <span className="text-sm font-bold tracking-tight">LyricGen</span>
            <span className="text-[8px] font-medium text-brand bg-brand/10 px-1.5 py-0.5 rounded-full ml-1.5 uppercase tracking-widest">Pro</span>
          </div>
        </div>
        <button onClick={onToggle} className="text-gray-500 hover:text-white transition-colors">
          <svg className="w-4 h-4" fill="none" stroke="currentColor" strokeWidth="2" viewBox="0 0 24 24">
            <path d="M11 19l-7-7 7-7M18 19l-7-7 7-7" />
          </svg>
        </button>
      </div>

      {/* Nav items */}
      <nav className="flex-1 py-4 px-3">
        {items.map((item) => (
          <button
            key={item.id}
            onClick={() => onNav(item.id)}
            className={`w-full flex items-center gap-3 px-4 py-3 rounded-xl mb-1 text-sm font-medium transition-all duration-200
              ${activeView === item.id
                ? "bg-brand/10 text-white"
                : "text-gray-400 hover:text-white hover:bg-surface-2/60"
              }`}
          >
            <span className={activeView === item.id ? "text-brand" : "text-gray-500"}>{item.icon}</span>
            {item.label}
          </button>
        ))}
      </nav>

      {/* System status */}
      <div className="px-5 py-4 border-t border-white/[0.04]">
        <div className="flex items-center gap-2">
          <div className="w-2 h-2 rounded-full bg-accent animate-pulse" />
          <span className="text-[11px] text-gray-500">Sistema operativo</span>
        </div>
      </div>
    </aside>
  );
}
