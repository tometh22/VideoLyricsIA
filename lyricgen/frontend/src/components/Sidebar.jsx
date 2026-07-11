import { Link } from "react-router-dom";
import { useI18n } from "../i18n";
import BrandLockup from "./BrandLockup";
import GenlyLogo from "./GenlyLogo";
import { IS_PRODUCTION, APP_ENV } from "../env";
import UsageBadge from "./UsageBadge";

const API = import.meta.env.VITE_API_URL || "";

// Each nav item carries its router path. The plain-left-click handler
// in the <Link> below calls onNav(id) (which fires the wizard-leaving
// confirm in App.handleNav) and preventDefault; modifier-click and
// right-click fall through so the browser handles them natively →
// "Open in new tab" / Cmd+Click work as users expect.
const ITEMS_BASE = (t) => [
  {
    id: "dashboard", label: t("nav.dashboard"), path: "/dashboard",
    icon: <svg className="w-4 h-4" fill="none" stroke="currentColor" strokeWidth="1.5" viewBox="0 0 24 24"><rect x="3" y="3" width="7" height="7" rx="1"/><rect x="14" y="3" width="7" height="7" rx="1"/><rect x="3" y="14" width="7" height="7" rx="1"/><rect x="14" y="14" width="7" height="7" rx="1"/></svg>,
  },
  {
    id: "new", label: t("nav.new_batch"), path: "/new",
    icon: <svg className="w-4 h-4" fill="none" stroke="currentColor" strokeWidth="1.5" viewBox="0 0 24 24"><path d="M12 5v14M5 12h14" strokeLinecap="round"/></svg>,
  },
  {
    id: "history", label: t("nav.history"), path: "/videos",
    icon: <svg className="w-4 h-4" fill="none" stroke="currentColor" strokeWidth="1.5" viewBox="0 0 24 24"><circle cx="12" cy="12" r="10"/><polyline points="12 6 12 12 16 14"/></svg>,
  },
  {
    id: "settings", label: t("nav.settings"), path: "/account",
    icon: <svg className="w-4 h-4" fill="none" stroke="currentColor" strokeWidth="1.5" viewBox="0 0 24 24"><circle cx="12" cy="12" r="3"/><path d="M19.4 15a1.65 1.65 0 00.33 1.82l.06.06a2 2 0 010 2.83 2 2 0 01-2.83 0l-.06-.06a1.65 1.65 0 00-1.82-.33 1.65 1.65 0 00-1 1.51V21a2 2 0 01-2 2 2 2 0 01-2-2v-.09A1.65 1.65 0 009 19.4a1.65 1.65 0 00-1.82.33l-.06.06a2 2 0 01-2.83 0 2 2 0 010-2.83l.06-.06A1.65 1.65 0 004.68 15a1.65 1.65 0 00-1.51-1H3a2 2 0 01-2-2 2 2 0 012-2h.09A1.65 1.65 0 004.6 9a1.65 1.65 0 00-.33-1.82l-.06-.06a2 2 0 010-2.83 2 2 0 012.83 0l.06.06A1.65 1.65 0 009 4.68a1.65 1.65 0 001-1.51V3a2 2 0 012-2 2 2 0 012 2v.09a1.65 1.65 0 001 1.51 1.65 1.65 0 001.82-.33l.06-.06a2 2 0 012.83 0 2 2 0 010 2.83l-.06.06A1.65 1.65 0 0019.4 9a1.65 1.65 0 001.51 1H21a2 2 0 012 2 2 2 0 01-2 2h-.09a1.65 1.65 0 00-1.51 1z"/></svg>,
  },
];

// True for plain left-click — the only case where we suppress the
// browser's default link behaviour and route through onNav.
function _isPlainLeftClick(e) {
  return e.button === 0 && !e.metaKey && !e.ctrlKey && !e.shiftKey && !e.altKey;
}

export default function Sidebar({ onNav, activeView, open, onToggle, user, onLogout }) {
  const { t } = useI18n();

  const items = ITEMS_BASE(t);
  if (user?.role === "admin") {
    items.push({
      id: "admin", label: "Admin", path: "/admin",
      icon: <svg className="w-4 h-4" fill="none" stroke="currentColor" strokeWidth="1.5" viewBox="0 0 24 24"><path d="M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10z" strokeLinecap="round" strokeLinejoin="round"/></svg>,
    });
  }
  const groups = [
    { label: "Producción", items: items.filter((item) => ["dashboard", "new", "history"].includes(item.id)) },
    { label: "Workspace", items: items.filter((item) => ["settings", "admin"].includes(item.id)) },
  ];

  return (
    <aside className={`app-sidebar ${open ? "is-open" : "is-collapsed"}`}>

      {/* Logo — full lockup per brand kit §10 (navbar uses full lockup,
          favicon uses mark-only). The "Pro" pill lives outside the
          locked-up artwork so the brand SVG geometry is preserved.
          Wrapped in <Link> so right-click → "open in new tab" works
          and middle-click opens a new tab natively. Plain left-click
          is routed through onNav("dashboard") to preserve the
          wizard-leaving confirm logic in App.handleNav. */}
      <div className="app-sidebar__brand">
        <div className="flex min-w-0 items-center gap-2.5">
          <Link
            to="/dashboard"
            onClick={(e) => {
              if (!_isPlainLeftClick(e)) return;
              e.preventDefault();
              onNav("dashboard");
            }}
            aria-label="Ir al dashboard"
            className="flex items-center"
          >
            {open ? <BrandLockup size="sm" /> : <GenlyLogo variant="icon" />}
          </Link>
          {open && (IS_PRODUCTION ? (
            <span className="text-[8px] font-medium text-brand bg-brand/10 px-1.5 py-0.5 rounded-full uppercase tracking-widest">Pro</span>
          ) : (
            <span
              className="text-[8px] font-bold text-amber-300 bg-amber-500/15 ring-1 ring-amber-500/40 px-1.5 py-0.5 rounded-full uppercase tracking-widest"
              title={`Environment: ${APP_ENV}`}
            >
              {APP_ENV === "development" ? "Dev" : "Staging"}
            </span>
          ))}
        </div>
        <button onClick={onToggle} className="app-sidebar__toggle" aria-label={open ? "Contraer navegación" : "Expandir navegación"}>
          <svg className={`w-4 h-4 transition-transform ${open ? "" : "rotate-180"}`} fill="none" stroke="currentColor" strokeWidth="2" viewBox="0 0 24 24">
            <path d="M15 18l-6-6 6-6" strokeLinecap="round" strokeLinejoin="round" />
          </svg>
        </button>
      </div>

      {/* Nav items — rendered as <Link> (= <a href>) so right-click /
          Cmd+Click / middle-click open a new tab natively. Plain
          left-click prevents the default navigation and calls onNav
          so the App's wizard-leaving confirm fires. */}
      <nav className="app-sidebar__nav" data-tour="sidebar-nav">
        {groups.map((group) => (
          <section key={group.label} className="app-sidebar__group">
            {open && <p>{group.label}</p>}
            {group.items.map((item) => (
              <Link
                key={item.id}
                to={item.path}
                title={!open ? item.label : undefined}
                aria-current={activeView === item.id ? "page" : undefined}
                onClick={(e) => {
                  if (!_isPlainLeftClick(e)) return;
                  e.preventDefault();
                  onNav(item.id);
                }}
                className={`app-sidebar__link ${activeView === item.id ? "is-active" : ""}`}
              >
                <span className="app-sidebar__link-icon">{item.icon}</span>
                {open && <span className="truncate">{item.label}</span>}
              </Link>
            ))}
          </section>
        ))}
      </nav>

      {/* Plan badge */}
      {user && open && (
        <div className="px-3 py-3 border-t border-white/[0.045]">
          <Link
            to="/account"
            onClick={(e) => {
              if (!_isPlainLeftClick(e)) return;
              e.preventDefault();
              onNav("settings");
            }}
            className="w-full flex items-center gap-2 px-3 py-2 rounded-lg bg-brand/5 hover:bg-brand/10 transition-all"
          >
            <span className="text-[10px] font-bold text-brand uppercase tracking-wider">
              Plan {user.plan || "free"}
            </span>
            <svg className="w-3 h-3 text-gray-500 ml-auto" fill="none" stroke="currentColor" strokeWidth="2" viewBox="0 0 24 24">
              <path d="M9 5l7 7-7 7" strokeLinecap="round" strokeLinejoin="round"/>
            </svg>
          </Link>
        </div>
      )}

      {/* Usage badge — shows monthly usage against plan limit. Hidden for
          unlimited plans + when /usage hasn't loaded yet. See
          UsageBadge for color thresholds + admin-only overage display. */}
      {open && <UsageBadge user={user} />}

      {/* User & logout */}
      <div className={`app-sidebar__footer ${open ? "" : "is-compact"}`}>
        <div className="app-sidebar__health" title={t("nav.system_ok")}>
          <div className="w-2 h-2 rounded-full bg-accent" />
          {open && <span>{t("nav.system_ok")}</span>}
        </div>
        {user && (
          <div className="flex items-center justify-between">
            <div className="flex items-center gap-2 min-w-0">
              {user.avatar_url ? (
                <img
                  src={`${API}/auth/avatar/${user.id}`}
                  alt=""
                  className="w-6 h-6 rounded-full object-cover shrink-0 ring-1 ring-white/[0.08]"
                  onError={(e) => {
                    // Si la imagen no carga (404 / token expirado) caemos al
                    // círculo con la inicial sin romper el layout.
                    e.currentTarget.style.display = "none";
                    const fb = e.currentTarget.nextElementSibling;
                    if (fb) fb.style.display = "flex";
                  }}
                />
              ) : null}
              <div
                className="w-6 h-6 rounded-lg bg-brand/20 items-center justify-center shrink-0"
                style={{ display: user.avatar_url ? "none" : "flex" }}
              >
                <span className="text-[10px] font-bold text-brand uppercase">{user.username?.charAt(0)}</span>
              </div>
              {open && <span className="text-xs text-gray-400 truncate">{user.username}</span>}
            </div>
            {onLogout && open && (
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
