import { useI18n } from "../../i18n";

// Novedades / "latest news" page. Stage 1: real shipped capabilities as cards
// (Musixmatch-style). Hand-curated — only features actually shipped.
export default function News() {
  const { t } = useI18n();
  const items = [
    { t: t("news.1_t"), d: t("news.1_d") },
    { t: t("news.2_t"), d: t("news.2_d") },
    { t: t("news.3_t"), d: t("news.3_d") },
  ];
  return (
    <div className="px-6 pt-24 pb-28 max-w-5xl mx-auto">
      <div className="text-center max-w-2xl mx-auto mb-14">
        <h1 className="text-4xl sm:text-5xl font-extrabold tracking-tight mb-4">{t("news.title")}</h1>
        <p className="text-gray-400 text-lg leading-relaxed">{t("news.sub")}</p>
      </div>
      <div className="grid grid-cols-1 sm:grid-cols-3 gap-5">
        {items.map((n) => (
          <article key={n.t} className="glass rounded-3xl overflow-hidden glass-hover">
            <div className="aspect-video bg-gradient-to-br from-brand/25 to-accent/15 flex items-center justify-center">
              <span className="px-2 py-0.5 rounded-full bg-white/15 text-white text-[10px] font-bold uppercase tracking-wider">Nuevo</span>
            </div>
            <div className="p-6">
              <h3 className="font-semibold mb-1.5">{n.t}</h3>
              <p className="text-sm text-gray-400 leading-relaxed">{n.d}</p>
            </div>
          </article>
        ))}
      </div>
    </div>
  );
}
