import { useI18n } from "../../i18n";

// Novedades / "latest news" page — blog-card format (Musixmatch-style).
// Hand-curated, real shipped capabilities only. `img` is optional; falls back
// to a neon gradient header when absent.
export default function News() {
  const { t } = useI18n();
  const items = [
    { t: t("news.1_t"), d: t("news.1_d"), date: "May 2026", img: "/samples/looks/look-studio.png" },
    { t: t("news.2_t"), d: t("news.2_d"), date: "May 2026", img: "/samples/looks/look-retro.png" },
    { t: t("news.3_t"), d: t("news.3_d"), date: "Abr 2026", img: "/samples/looks/look-aurora.png" },
  ];
  const featured = items[0];
  const rest = items.slice(1);

  return (
    <div className="px-6 pt-24 pb-28 max-w-6xl mx-auto">
      <div className="max-w-2xl mb-14">
        <h1 className="text-4xl sm:text-5xl font-extrabold tracking-tight mb-4">{t("news.title")}</h1>
        <p className="text-gray-400 text-lg leading-relaxed">{t("news.sub")}</p>
      </div>

      <div className="grid lg:grid-cols-2 gap-6">
        {/* Featured */}
        <article className="glass rounded-3xl overflow-hidden glass-hover lg:row-span-2 flex flex-col">
          <div className="aspect-video bg-gradient-to-br from-brand/30 to-accent/15 overflow-hidden">
            {featured.img && <img src={featured.img} alt="" className="w-full h-full object-cover" />}
          </div>
          <div className="p-8 flex-1 flex flex-col">
            <div className="flex items-center gap-2 mb-3">
              <span className="px-2 py-0.5 rounded-full bg-accent/15 text-accent text-[10px] font-bold uppercase tracking-wider">Nuevo</span>
              <span className="text-xs text-gray-500">{featured.date}</span>
            </div>
            <h2 className="text-2xl font-bold mb-2">{featured.t}</h2>
            <p className="text-gray-400 leading-relaxed flex-1">{featured.d}</p>
          </div>
        </article>

        {/* Rest */}
        {rest.map((n) => (
          <article key={n.t} className="glass rounded-3xl overflow-hidden glass-hover flex gap-5 p-5">
            <div className="w-32 shrink-0 rounded-2xl overflow-hidden bg-gradient-to-br from-brand/25 to-accent/15 self-stretch flex items-center justify-center">
              {n.img ? <img src={n.img} alt="" className="w-full h-full object-cover" /> : <span className="px-2 py-0.5 rounded-full bg-white/15 text-white text-[10px] font-bold uppercase">Nuevo</span>}
            </div>
            <div className="py-1">
              <span className="text-xs text-gray-500">{n.date}</span>
              <h3 className="font-bold mt-1 mb-1.5">{n.t}</h3>
              <p className="text-sm text-gray-400 leading-relaxed">{n.d}</p>
            </div>
          </article>
        ))}
      </div>
    </div>
  );
}
