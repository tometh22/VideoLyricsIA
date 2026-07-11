import { useEffect, useRef, useState } from "react";
import { useI18n } from "../i18n";
import WhatsNewBell from "./WhatsNew/WhatsNewBell";
import HelpButton from "./HelpCenter/HelpButton";

export default function GlobalTopbar({ user, activeRenders = 0, onSearch, onCreate, onNavigate, onLogout, onToggleNavigation, navigationOpen }) {
  const { t, lang, setLang } = useI18n();
  const [menuOpen, setMenuOpen] = useState(false);
  const menuRef = useRef(null);
  const menuPanelRef = useRef(null);
  const menuTriggerRef = useRef(null);

  useEffect(() => {
    if (!menuOpen) return undefined;
    const close = (event) => { if (!menuRef.current?.contains(event.target)) setMenuOpen(false); };
    const escape = (event) => {
      if (event.key === "Escape") {
        setMenuOpen(false);
        menuTriggerRef.current?.focus();
      }
    };
    document.addEventListener("mousedown", close);
    document.addEventListener("keydown", escape);
    return () => { document.removeEventListener("mousedown", close); document.removeEventListener("keydown", escape); };
  }, [menuOpen]);

  useEffect(() => {
    if (menuOpen) requestAnimationFrame(() => menuPanelRef.current?.querySelector('[role="menuitem"]')?.focus());
  }, [menuOpen]);

  const handleMenuKeyDown = (event) => {
    const items = [...(menuPanelRef.current?.querySelectorAll('[role^="menuitem"]') || [])];
    if (!items.length) return;
    const current = items.indexOf(document.activeElement);
    if (["ArrowDown", "ArrowUp", "Home", "End"].includes(event.key)) event.preventDefault();
    if (event.key === "ArrowDown") items[(current + 1 + items.length) % items.length]?.focus();
    if (event.key === "ArrowUp") items[(current - 1 + items.length) % items.length]?.focus();
    if (event.key === "Home") items[0]?.focus();
    if (event.key === "End") items[items.length - 1]?.focus();
  };

  const go = (destination) => { setMenuOpen(false); onNavigate?.(destination); };
  const renderStatusLabel = activeRenders === 0
    ? t("topbar.no_renders")
    : activeRenders === 1
      ? t("topbar.render_one")
      : (t("topbar.renders_many") || "{count} renders activos").replace("{count}", activeRenders);

  return (
    <header className="global-topbar">
      <button type="button" onClick={onToggleNavigation} className={`global-topbar__icon ${navigationOpen ? "md:hidden" : ""}`} aria-label={navigationOpen ? t("topbar.close_navigation") : t("topbar.open_navigation")}>
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2"><path d="M4 6h16M4 12h16M4 18h16"/></svg>
      </button>

      <button type="button" onClick={onSearch} className="global-search-trigger" aria-label={t("topbar.search")}>
        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8"><circle cx="11" cy="11" r="7"/><path d="M20 20l-4-4"/></svg>
        <span>{t("topbar.search")}</span>
        <kbd>⌘ K</kbd>
      </button>

      <div className="global-topbar__actions">
        <div className={`render-status ${activeRenders > 0 ? "is-active" : ""}`} role="status" aria-live="polite" aria-label={renderStatusLabel} title={renderStatusLabel}>
          <span aria-hidden="true" />
          <strong>{activeRenders > 0 ? activeRenders : 0}</strong>
          <em className="hidden xl:inline">{activeRenders === 1 ? t("topbar.render_one").replace("1 ", "") : t("topbar.renders_many").replace("{count} ", "")}</em>
        </div>
        <div className="global-topbar__secondary is-news"><WhatsNewBell /></div>
        <div className="global-topbar__secondary is-help"><HelpButton className="!h-10 !w-10 !rounded-xl" /></div>
        <button type="button" onClick={onCreate} className="global-create-button" aria-label={t("nav.new_batch") || "Crear video"}>
          <svg aria-hidden="true" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.2"><path d="M12 5v14M5 12h14"/></svg>
          <span>{t("nav.new_batch") || "Crear video"}</span>
        </button>

        <div className="relative" ref={menuRef}>
          <button ref={menuTriggerRef} type="button" onClick={() => setMenuOpen((value) => !value)} className="user-menu-trigger" aria-haspopup="menu" aria-expanded={menuOpen} aria-label={menuOpen ? t("topbar.close_user_menu") : t("topbar.open_user_menu")}>
            <span>{user?.full_name?.charAt(0) || user?.username?.charAt(0) || "G"}</span>
            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2"><path d="M6 9l6 6 6-6"/></svg>
          </button>
          {menuOpen && (
            <div ref={menuPanelRef} className="user-menu" role="menu" onKeyDown={handleMenuKeyDown}>
              <div className="user-menu__identity"><strong>{user?.full_name || user?.username}</strong><small>{user?.email || `Plan ${user?.plan || "free"}`}</small></div>
              <button role="menuitem" onClick={() => go("settings")}>{t("nav.settings") || "Configuración"}</button>
              {user?.role === "admin" && <button role="menuitem" onClick={() => go("admin")}>Admin</button>}
              <div className="user-menu__languages" aria-label={t("topbar.language")}>{["es", "en", "pt"].map((code) => <button role="menuitemradio" key={code} onClick={() => setLang(code)} aria-checked={lang === code} className={lang === code ? "is-active" : ""}>{code.toUpperCase()}</button>)}</div>
              <button role="menuitem" onClick={onLogout} className="is-danger">{t("nav.logout") || "Cerrar sesión"}</button>
            </div>
          )}
        </div>
      </div>
    </header>
  );
}
