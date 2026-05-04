import { useI18n } from "../i18n";

export default function Sidebar({ onNav, activeView, open, onToggle, user, onLogout }) {
  const { t } = useI18n();

  if (!open) return null;

  const items = [
    {
      id: "dashboard", label: t("nav.dashboard"),
      icon: <svg className="w-5 h-5" fill="none" stroke="currentColor" strokeWidth="1.5" viewBox="0 0 24 24"><rect x="3" y="3" width="7" height="7" rx="1"/><rect x="14" y="3" width="7" height="7" rx="1"/><rect x="3" y="14" width="7" height="7" rx="1"/><rect x="14" y="14" width="7" height="7" rx="1"/></svg>,
    },
    {
      id: "new", label: t("nav.new_batch"),
      icon: <svg className="w-5 h-5" fill="none" stroke="currentColor" strokeWidth="1.5" viewBox="0 0 24 24"><path d="M12 5v14M5 12h14" strokeLinecap="round"/></svg>,
    },
    {
      id: "history", label: t("nav.history"),
      icon: <svg className="w-5 h-5" fill="none" stroke="currentColor" strokeWidth="1.5" viewBox="0 0 24 24"><circle cx="12" cy="12" r="10"/><polyline points="12 6 12 12 16 14"/></svg>,
    },
    {
      id: "settings", label: t("nav.settings"),
      icon: <svg className="w-5 h-5" fill="none" stroke="currentColor" strokeWidth="1.5" viewBox="0 0 24 24"><circle cx="12" cy="12" r="3"/><path d="M19.4 15a1.65 1.65 0 00.33 1.82l.06.06a2 2 0 010 2.83 2 2 0 01-2.83 0l-.06-.06a1.65 1.65 0 00-1.82-.33 1.65 1.65 0 00-1 1.51V21a2 2 0 01-2 2 2 2 0 01-2-2v-.09A1.65 1.65 0 009 19.4a1.65 1.65 0 00-1.82.33l-.06.06a2 2 0 01-2.83 0 2 2 0 010-2.83l.06-.06A1.65 1.65 0 004.68 15a1.65 1.65 0 00-1.51-1H3a2 2 0 01-2-2 2 2 0 012-2h.09A1.65 1.65 0 004.6 9a1.65 1.65 0 00-.33-1.82l-.06-.06a2 2 0 010-2.83 2 2 0 012.83 0l.06.06A1.65 1.65 0 009 4.68a1.65 1.65 0 001-1.51V3a2 2 0 012-2 2 2 0 012 2v.09a1.65 1.65 0 001 1.51 1.65 1.65 0 001.82-.33l.06-.06a2 2 0 012.83 0 2 2 0 010 2.83l-.06.06A1.65 1.65 0 0019.4 9a1.65 1.65 0 001.51 1H21a2 2 0 012 2 2 2 0 01-2 2h-.09a1.65 1.65 0 00-1.51 1z"/></svg>,
    },
  ];

  // Admin link for admin users
  if (user?.role === "admin") {
    items.push({
      id: "admin", label: "Admin",
      icon: <svg className="w-5 h-5" fill="none" stroke="currentColor" strokeWidth="1.5" viewBox="0 0 24 24"><path d="M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10z" strokeLinecap="round" strokeLinejoin="round"/></svg>,
    });
  }

  return (
    <aside data-tour="sidebar" className="fixed left-0 top-0 bottom-0 w-64 bg-surface-1/95 backdrop-blur-xl border-r border-white/[0.04] z-20 flex flex-col" style={{boxShadow: '4px 0 24px rgba(0,0,0,0.3)'}}>

      {/* Logo */}
      <div className="flex items-center justify-between px-5 py-5 border-b border-white/[0.04]">
        <div className="flex items-center gap-2.5">
          <div className="w-8 h-8 rounded-lg bg-gradient-to-br from-brand to-brand-light flex items-center justify-center">
            <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="white" strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round">
              <path d="M9 18V5l12-2v13" /><circle cx="6" cy="18" r="3" /><circle cx="18" cy="16" r="3" />
            </svg>
          </div>
          <div>
            <span className="text-sm font-bold tracking-tight">GenLy AI</span>
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
            data-tour={`nav-${item.id}`}
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

      {/* Plan badge */}
      {user && (
        <div className="px-5 py-3 border-t border-white/[0.04]">
          <button onClick={() => onNav("settings")}
            className="w-full flex items-center gap-2 px-3 py-2 rounded-xl bg-brand/5 hover:bg-brand/10 transition-all">
            <span className="text-[10px] font-bold text-brand uppercase tracking-wider">
              Plan {user.plan || "free"}
            </span>
            <svg className="w-3 h-3 text-gray-500 ml-auto" fill="none" stroke="currentColor" strokeWidth="2" viewBox="0 0 24 24">
              <path d="M9 5l7 7-7 7" strokeLinecap="round" strokeLinejoin="round"/>
            </svg>
          </button>
        </div>
      )}

      {/* User & logout */}
      <div className="px-5 py-4 border-t border-white/[0.04] space-y-3">
        <div className="flex items-center gap-2">
          <div className="w-2 h-2 rounded-full bg-accent animate-pulse" />
          <span className="text-[11px] text-gray-500">{t("nav.system_ok")}</span>
        </div>
        {user && (
          <div className="flex items-center justify-between">
            <div className="flex items-center gap-2 min-w-0">
              <div className="w-6 h-6 rounded-lg bg-brand/20 flex items-center justify-center shrink-0">
                <span className="text-[10px] font-bold text-brand uppercase">{user.username?.charAt(0)}</span>
              </div>
              <span className="text-xs text-gray-400 truncate">{user.username}</span>
            </div>
            {onLogout && (
              <button
                onClick={onLogout}
                title={t("nav.logout")}
                className="text-gray-500 hover:text-red-400 transition-colors p-1"
              >
                <svg className="w-4 h-4" fill="none" stroke="currentColor" strokeWidth="1.5" viewBox="0 0 24 24">
                  <path d="M17 16l4-4m0 0l-4-4m4 4H7m6 4v1a3 3 0 01-3 3H6a3 3 0 01-3-3V7a3 3 0 013-3h4a3 3 0 013 3v1" strokeLinecap="round" strokeLinejoin="round"/>
                </svg>
              </button>
            )}
          </div>
        )}
      </div>
    </aside>
  );
}
