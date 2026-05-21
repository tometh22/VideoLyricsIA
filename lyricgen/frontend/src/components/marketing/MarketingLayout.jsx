import { Link, NavLink, Outlet, useNavigate } from "react-router-dom";
import { useI18n } from "../../i18n";
import BrandLockup from "../BrandLockup";

// Shared chrome for all public marketing pages: announcement bar, top nav with
// dropdowns, and the multi-column footer. Page content renders through <Outlet/>.
// This is what turns the marketing site from a lonely one-pager into a real
// multi-page site (Home / Artistas / Sellos / Precios / Novedades).
export default function MarketingLayout() {
  const { t, lang, setLang } = useI18n();
  const navigate = useNavigate();
  const goLogin = () => navigate("/login");

  const linkCls = ({ isActive }) =>
    `text-sm transition-colors ${isActive ? "text-white" : "text-gray-300 hover:text-white"}`;

  return (
    <div className="min-h-screen bg-surface relative overflow-x-hidden">
      {/* Ambient neon background, fixed behind everything */}
      <div className="fixed inset-0 pointer-events-none">
        <div className="absolute top-[-20%] left-[10%] w-[700px] h-[700px] bg-brand/[0.05] rounded-full blur-[150px]" />
        <div className="absolute bottom-[-10%] right-[5%] w-[500px] h-[500px] bg-brand-light/[0.04] rounded-full blur-[120px]" />
      </div>

      {/* Announcement bar */}
      <Link
        to="/novedades"
        className="relative z-40 block bg-gradient-to-r from-brand/30 via-brand/20 to-accent/20 border-b border-white/[0.06] text-center py-2 px-4 text-xs text-white/90 hover:text-white transition-colors"
      >
        <span className="inline-flex items-center gap-2">
          <span className="px-1.5 py-0.5 rounded bg-white/15 text-[10px] font-bold uppercase tracking-wider">Nuevo</span>
          {t("ann.bar") || "Editor visual de timing + masters ProRes ya disponibles"}
          <span className="font-semibold">{t("ann.cta") || "Ver novedades"} →</span>
        </span>
      </Link>

      {/* Nav */}
      <nav className="sticky top-0 z-40 bg-surface/85 backdrop-blur-xl border-b border-white/[0.06]">
        <div className="flex items-center justify-between px-6 sm:px-8 py-4 max-w-6xl mx-auto">
          <Link to="/" aria-label="GenLy — inicio"><BrandLockup size="md" /></Link>

          <div className="hidden md:flex items-center gap-7">
            {/* Producto dropdown */}
            <div className="relative group">
              <button className="text-sm text-gray-300 hover:text-white transition-colors inline-flex items-center gap-1">
                {t("nav.product") || "Producto"}
                <svg className="w-3 h-3" fill="none" stroke="currentColor" strokeWidth="2" viewBox="0 0 24 24"><path d="M6 9l6 6 6-6"/></svg>
              </button>
              <div className="absolute left-0 top-full pt-3 hidden group-hover:block">
                <div className="w-56 glass rounded-2xl p-2 shadow-depth-lg">
                  <Link to="/#features" className="block px-3 py-2 rounded-lg text-sm text-gray-300 hover:text-white hover:bg-white/5">{t("landing.features")}</Link>
                  <Link to="/#how" className="block px-3 py-2 rounded-lg text-sm text-gray-300 hover:text-white hover:bg-white/5">{t("landing.how")}</Link>
                  <Link to="/novedades" className="block px-3 py-2 rounded-lg text-sm text-gray-300 hover:text-white hover:bg-white/5">{t("news.title")}</Link>
                </div>
              </div>
            </div>

            {/* Soluciones dropdown */}
            <div className="relative group">
              <button className="text-sm text-gray-300 hover:text-white transition-colors inline-flex items-center gap-1">
                {t("nav.solutions") || "Soluciones"}
                <svg className="w-3 h-3" fill="none" stroke="currentColor" strokeWidth="2" viewBox="0 0 24 24"><path d="M6 9l6 6 6-6"/></svg>
              </button>
              <div className="absolute left-0 top-full pt-3 hidden group-hover:block">
                <div className="w-56 glass rounded-2xl p-2 shadow-depth-lg">
                  <Link to="/artistas" className="block px-3 py-2 rounded-lg text-sm text-gray-300 hover:text-white hover:bg-white/5">{t("nav.for_artists") || "Para artistas"}</Link>
                  <Link to="/sellos" className="block px-3 py-2 rounded-lg text-sm text-gray-300 hover:text-white hover:bg-white/5">{t("nav.for_labels") || "Para sellos"}</Link>
                </div>
              </div>
            </div>

            <NavLink to="/precios" className={linkCls}>{t("landing.pricing")}</NavLink>
            <NavLink to="/novedades" className={linkCls}>{t("news.title")}</NavLink>

            <div className="flex items-center gap-1 ml-1">
              {["es", "en", "pt"].map((code) => (
                <button key={code} onClick={() => setLang(code)}
                  className={`text-[10px] font-bold px-2 py-1 rounded-md transition-all uppercase ${lang === code ? "text-white bg-white/10" : "text-gray-600 hover:text-gray-400"}`}>
                  {code}
                </button>
              ))}
            </div>
            <button onClick={goLogin} className="text-sm text-gray-300 hover:text-white transition-colors">{t("login.title")}</button>
            <button onClick={goLogin} className="btn-primary text-xs py-2 px-5">{t("nav.start")}</button>
          </div>

          <button onClick={goLogin} className="md:hidden btn-primary text-xs py-2 px-5">{t("nav.start")}</button>
        </div>
      </nav>

      {/* Page content */}
      <main className="relative z-10">
        <Outlet />
      </main>

      {/* Footer */}
      <footer className="relative z-10 border-t border-white/[0.06] pt-14 pb-8 px-8">
        <div className="max-w-6xl mx-auto grid grid-cols-2 sm:grid-cols-4 gap-8">
          <div className="col-span-2 sm:col-span-1">
            <BrandLockup size="md" />
            <p className="text-xs text-gray-600 mt-3 leading-relaxed max-w-[14rem]">{t("footer.tagline")}</p>
          </div>
          <div>
            <p className="text-xs font-semibold text-gray-300 uppercase tracking-wider mb-3">{t("footer.col_product")}</p>
            <ul className="space-y-2 text-sm text-gray-500">
              <li><Link to="/artistas" className="hover:text-white transition-colors">{t("nav.for_artists") || "Para artistas"}</Link></li>
              <li><Link to="/sellos" className="hover:text-white transition-colors">{t("nav.for_labels") || "Para sellos"}</Link></li>
              <li><Link to="/precios" className="hover:text-white transition-colors">{t("landing.pricing")}</Link></li>
              <li><Link to="/novedades" className="hover:text-white transition-colors">{t("news.title")}</Link></li>
            </ul>
          </div>
          <div>
            <p className="text-xs font-semibold text-gray-300 uppercase tracking-wider mb-3">{t("footer.col_company")}</p>
            <ul className="space-y-2 text-sm text-gray-500">
              <li><Link to="/sellos#contact" className="hover:text-white transition-colors">{t("footer.contact")}</Link></li>
              <li><a href="mailto:tomas@epical.digital" className="hover:text-white transition-colors">tomas@epical.digital</a></li>
            </ul>
          </div>
          <div>
            <p className="text-xs font-semibold text-gray-300 uppercase tracking-wider mb-3">{t("footer.col_legal")}</p>
            <ul className="space-y-2 text-sm text-gray-500">
              <li><Link to="/precios#faq" className="hover:text-white transition-colors">{t("footer.rights")}</Link></li>
            </ul>
          </div>
        </div>
        <div className="max-w-6xl mx-auto mt-12 pt-6 border-t border-white/[0.04] flex flex-col sm:flex-row items-center justify-between gap-3">
          <p className="text-xs text-gray-600">© {new Date().getFullYear()} {t("landing.footer")}</p>
          <div className="flex items-center gap-1">
            {["es", "en", "pt"].map((code) => (
              <button key={code} onClick={() => setLang(code)}
                className={`text-[10px] font-bold px-2 py-1 rounded-md transition-all uppercase ${lang === code ? "text-white bg-white/10" : "text-gray-600 hover:text-gray-400"}`}>
                {code}
              </button>
            ))}
          </div>
        </div>
      </footer>
    </div>
  );
}
